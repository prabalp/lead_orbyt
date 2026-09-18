import httpx

from leadorbyt.email import (
    ConsoleEmailSender,
    ResendEmailSender,
    ResendSendError,
    TransactionalEmail,
    get_email_sender,
    verification_email,
)


def test_verification_email_contains_link():
    message = verification_email(
        to="ada@example.com", app_url="https://leads.example.com", raw_token="abc_def"
    )
    assert message.to == "ada@example.com"
    assert "Verify your Lead Orbyt" in message.subject
    assert "https://leads.example.com/verify-email?token=abc_def" in message.text
    assert "24 hours" in message.text


def test_get_email_sender_falls_back_to_console(monkeypatch):
    monkeypatch.setattr("leadorbyt.config.RESEND_API_KEY", "")
    assert isinstance(get_email_sender(), ConsoleEmailSender)


def test_get_email_sender_uses_resend_when_keyed(monkeypatch):
    monkeypatch.setattr("leadorbyt.config.RESEND_API_KEY", "re_test")
    monkeypatch.setattr("leadorbyt.config.EMAIL_FROM_ADDRESS", "Lead Orbyt <hi@example.com>")
    sender = get_email_sender()
    assert isinstance(sender, ResendEmailSender)
    assert sender.from_address == "Lead Orbyt <hi@example.com>"


def test_resend_sender_posts_payload(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return httpx.Response(200, json={"id": "msg_1"})

    monkeypatch.setattr("leadorbyt.email.httpx.post", fake_post)
    sender = ResendEmailSender("re_test", "Lead Orbyt <onboarding@resend.dev>")
    sender.send(TransactionalEmail(to="ada@example.com", subject="Hello", text="body"))
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer re_test"
    assert captured["kwargs"]["json"]["to"] == ["ada@example.com"]
    assert captured["kwargs"]["json"]["from"] == "Lead Orbyt <onboarding@resend.dev>"


def test_resend_sender_raises_on_error(monkeypatch):
    def fake_post(url, **kwargs):
        return httpx.Response(401, json={"message": "Invalid API key"})

    monkeypatch.setattr("leadorbyt.email.httpx.post", fake_post)
    sender = ResendEmailSender("re_bad", "onboarding@resend.dev")
    try:
        sender.send(TransactionalEmail(to="ada@example.com", subject="Hello", text="body"))
    except ResendSendError as exc:
        assert "Invalid API key" in str(exc)
    else:
        raise AssertionError("expected ResendSendError")
