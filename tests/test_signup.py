import json
import re
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from leadorbyt import auth, email as transactional_email, signup, store
from leadorbyt.errors import ErrorType, LeadOrbytError

TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-]+)")


class CaptureSender:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def _token(sender: CaptureSender) -> str:
    assert sender.sent
    match = TOKEN_RE.search(sender.sent[-1].text)
    assert match, sender.sent[-1].text
    return match.group(1)


def _use_sender(monkeypatch) -> CaptureSender:
    sender = CaptureSender()
    monkeypatch.setattr(transactional_email, "get_email_sender", lambda: sender)
    return sender


def test_issue_access_creates_tenant_and_key(isolated_db):
    result = signup.issue_access("Ada@Example.com")
    assert result["email"] == "ada@example.com"
    assert result["created"] is True
    assert result["status"] == "verified"
    assert store.get_user_for_key(auth.hash_key(result["api_key"])) == result["user_id"]
    assert result["mcp_url"].endswith("/mcp")
    assert result["api_key"] in result["claude_desktop_config"]
    user = store.get_user_by_email("ada@example.com")
    assert user["email_verified_at"]


def test_same_email_reuses_tenant_and_mints_new_key(isolated_db):
    first = signup.issue_access("user@example.com")
    second = signup.issue_access("USER@example.com")
    assert first["user_id"] == second["user_id"]
    assert first["api_key"] != second["api_key"]
    assert second["created"] is False
    assert store.get_user_for_key(auth.hash_key(first["api_key"])) == first["user_id"]
    assert store.get_user_for_key(auth.hash_key(second["api_key"])) == first["user_id"]


def test_invalid_email_rejected():
    try:
        signup.request_access("not-an-email")
    except LeadOrbytError as exc:
        assert exc.type == ErrorType.INVALID_INPUT
    else:
        raise AssertionError("expected invalid email to fail")


def test_disabled_email_cannot_mint(isolated_db, monkeypatch):
    user_id, _ = store.get_or_create_user_by_email("blocked@example.com")
    with store._cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = 1 WHERE id = ?", (user_id,))
    _use_sender(monkeypatch)
    try:
        signup.request_access("blocked@example.com")
    except LeadOrbytError:
        pass
    else:
        raise AssertionError("expected disabled account to fail")


def test_request_access_sends_mail_without_minting_key(isolated_db, monkeypatch):
    sender = _use_sender(monkeypatch)
    pending = signup.request_access("Ada@Example.com")
    assert pending == {"email": "ada@example.com", "status": "pending"}
    assert store.get_user_by_email("ada@example.com") is None
    token = _token(sender)
    hashed = auth.hash_key(token)
    row = store.get_email_verification(hashed)
    assert row["email"] == "ada@example.com"
    assert row["consumed_at"] is None
    assert store.list_users() == []


def test_get_verify_does_not_consume_token(isolated_db, monkeypatch):
    sender = _use_sender(monkeypatch)
    signup.request_access("host@example.com")
    token = _token(sender)
    email = signup.preview_verification(token)
    assert email == "host@example.com"
    row = store.get_email_verification(auth.hash_key(token))
    assert row["consumed_at"] is None


def test_confirm_verification_mints_key(isolated_db, monkeypatch):
    sender = _use_sender(monkeypatch)
    signup.request_access("host@example.com")
    token = _token(sender)
    result = signup.confirm_verification(token)
    assert result["status"] == "verified"
    assert store.get_user_for_key(auth.hash_key(result["api_key"])) == result["user_id"]
    try:
        signup.confirm_verification(token)
    except LeadOrbytError as exc:
        assert "already been used" in exc.message
    else:
        raise AssertionError("expected consumed token to fail")


def test_new_request_supersedes_prior_token(isolated_db, monkeypatch):
    sender = _use_sender(monkeypatch)
    signup.request_access("host@example.com")
    first = _token(sender)
    signup.request_access("host@example.com")
    second = _token(sender)
    assert first != second
    try:
        signup.confirm_verification(first)
    except LeadOrbytError:
        pass
    else:
        raise AssertionError("expected superseded token to fail")
    result = signup.confirm_verification(second)
    assert result["email"] == "host@example.com"


def test_expired_token_rejected(isolated_db, monkeypatch):
    sender = _use_sender(monkeypatch)
    signup.request_access("host@example.com")
    token = _token(sender)
    with store._cursor() as cur:
        cur.execute(
            "UPDATE email_verification_tokens SET expires_at = ? WHERE token_hash = ?",
            (time.time() - 1, auth.hash_key(token)),
        )
    try:
        signup.confirm_verification(token)
    except LeadOrbytError as exc:
        assert "expired" in exc.message
    else:
        raise AssertionError("expected expired token to fail")


async def _ok(_request: Request):
    return PlainTextResponse("secret")


def _client():
    app = Starlette(
        routes=[
            Route("/", signup.home),
            Route("/signup", signup.signup, methods=["GET", "POST"]),
            Route("/verify-email", signup.verify_email, methods=["GET", "POST"]),
            Route("/mcp", _ok),
        ]
    )
    app.add_middleware(auth.ApiKeyAuthMiddleware)
    return TestClient(app)


def test_home_is_public():
    response = _client().get("/")
    assert response.status_code == 200
    assert "Turn a conversation into a" in response.text
    assert "Get MCP setup" in response.text
    assert "Get MCP access" in response.text
    assert 'id="setup"' in response.text
    assert "find_leads_maps" in response.text
    assert "find_web_signals" in response.text
    assert "find_people_leads" in response.text
    assert "does not send outreach" in response.text.lower()


def test_mcp_still_requires_key():
    response = _client().get("/mcp")
    assert response.status_code == 401


def test_signup_form_sends_verification(isolated_db, monkeypatch):
    signup._ip_hits.clear()
    signup._email_hits.clear()
    sender = _use_sender(monkeypatch)
    response = _client().post("/signup", data={"email": "host@example.com"})
    assert response.status_code == 200
    assert "Check your inbox" in response.text
    assert "Your Claude connection" not in response.text
    assert sender.sent and sender.sent[0].to == "host@example.com"


def test_verify_get_then_post_shows_key(isolated_db, monkeypatch):
    signup._ip_hits.clear()
    signup._email_hits.clear()
    sender = _use_sender(monkeypatch)
    client = _client()
    client.post("/signup", data={"email": "host@example.com"})
    token = _token(sender)
    preview = client.get(f"/verify-email?token={token}")
    assert preview.status_code == 200
    assert "Confirm my email" in preview.text
    assert "Your Claude connection" not in preview.text
    confirmed = client.post("/verify-email", data={"token": token})
    assert confirmed.status_code == 200
    assert "Your Claude connection" in confirmed.text
    assert "mcp-remote" in confirmed.text
    assert "Authorization: Bearer" in confirmed.text


def test_signup_json_then_verify_json(isolated_db, monkeypatch):
    signup._ip_hits.clear()
    signup._email_hits.clear()
    sender = _use_sender(monkeypatch)
    client = _client()
    response = client.post(
        "/signup",
        content=json.dumps({"email": "api@example.com"}),
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"email": "api@example.com", "status": "pending"}
    token = _token(sender)
    verified = client.post(
        "/verify-email",
        content=json.dumps({"token": token}),
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    assert verified.status_code == 200
    payload = verified.json()
    assert payload["email"] == "api@example.com"
    assert payload["api_key"]
    assert payload["mcp_url"].endswith("/mcp")
