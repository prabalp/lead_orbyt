from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from leadorbyt import auth, social_connect, store
from leadorbyt.errors import ErrorType, LeadOrbytError


def _key(monkeypatch):
    secret = Fernet.generate_key().decode()
    monkeypatch.setattr("leadorbyt.config.TOKEN_ENCRYPTION_KEY", secret)
    monkeypatch.setattr("leadorbyt.config.REDDIT_CLIENT_ID", "cid")
    monkeypatch.setattr("leadorbyt.config.REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setattr("leadorbyt.config.REDDIT_USER_AGENT", "leadorbyt-test")
    monkeypatch.setattr("leadorbyt.config.PUBLIC_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("leadorbyt.config.X_OAUTH_CLIENT_ID", "x-client")


def test_linkedin_and_facebook_are_unsupported(isolated_db, monkeypatch):
    _key(monkeypatch)
    user_id = store.create_user("ada@example.com")
    linkedin = social_connect.start_connect(user_id, "linkedin")
    assert linkedin["status"] == "unsupported"
    assert "Terms of Service" in linkedin["reason"]
    facebook = social_connect.start_connect(user_id, "facebook")
    assert facebook["status"] == "unsupported"


def test_start_connect_reddit_returns_browser_url(isolated_db, monkeypatch):
    _key(monkeypatch)
    user_id = store.create_user("ada@example.com")
    result = social_connect.start_connect(user_id, "reddit")
    assert result["status"] == "login_required"
    assert result["login_url"].startswith("http://127.0.0.1:8000/connect/reddit?ticket=")
    assert "password" in result["next_action"].lower()


def test_connect_without_encryption_key_fails(isolated_db, monkeypatch):
    monkeypatch.setattr("leadorbyt.config.TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr("leadorbyt.config.REDDIT_CLIENT_ID", "cid")
    monkeypatch.setattr("leadorbyt.config.REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setattr("leadorbyt.config.REDDIT_USER_AGENT", "ua")
    user_id = store.create_user("ada@example.com")
    try:
        social_connect.start_connect(user_id, "reddit")
    except LeadOrbytError as exc:
        assert exc.type == ErrorType.INVALID_INPUT
    else:
        raise AssertionError("expected encryption key to be required")


def test_roundtrip_token_storage(isolated_db, monkeypatch):
    _key(monkeypatch)
    user_id = store.create_user("ada@example.com")
    social_connect._store_tokens(
        user_id, "reddit", {"access_token": "raw-access", "refresh_token": "raw-refresh", "expires_in": 3600}, "ada"
    )
    assert social_connect.valid_access_token(user_id, "reddit") == "raw-access"
    row = store.get_connected_account(user_id, "reddit")
    assert row["account_label"] == "ada"
    assert "raw-access" not in row["access_token_enc"]


def test_connection_summary_lists_unsupported(isolated_db, monkeypatch):
    _key(monkeypatch)
    user_id = store.create_user("ada@example.com")
    summary = social_connect.connection_summary(user_id)
    names = {p["provider"]: p for p in summary["providers"]}
    assert names["reddit"]["connectable"] is True
    assert names["linkedin"]["connectable"] is False
    assert names["facebook"]["connectable"] is False


def test_connect_ticket_route_is_public(isolated_db, monkeypatch):
    _key(monkeypatch)
    user_id = store.create_user("ada@example.com")
    started = social_connect.start_connect(user_id, "reddit")
    ticket = started["login_url"].rsplit("ticket=", 1)[-1]

    app = Starlette(
        routes=[
            Route("/connect/{provider}", social_connect.connect_start),
            Route("/mcp", lambda r: None),
        ]
    )
    app.add_middleware(auth.ApiKeyAuthMiddleware)
    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/connect/reddit?ticket={ticket}")
    assert response.status_code == 302
    assert "reddit.com/api/v1/authorize" in response.headers["location"]
