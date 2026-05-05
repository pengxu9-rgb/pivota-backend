import requests

from utils import email_sender


def test_email_provider_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "sg_test")
    monkeypatch.setenv("EMAIL_PROVIDER", "ses")

    assert email_sender._email_provider() == "ses"


def test_email_provider_uses_sendgrid_when_key_present(monkeypatch):
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("SMTP2GO_API_KEY", raising=False)
    monkeypatch.delenv("SMTP2GO_EMAIL_API_KEY", raising=False)
    monkeypatch.setenv("SENDGRID_API_KEY", "sg_test")

    assert email_sender._email_provider() == "sendgrid"


def test_email_provider_uses_smtp2go_when_key_present(monkeypatch):
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.setenv("SENDGRID_API_KEY", "sg_test")
    monkeypatch.setenv("SMTP2GO_EMAIL_API_KEY", "smtp2go_test")

    assert email_sender._email_provider() == "smtp2go"


def test_email_provider_defaults_to_ses_without_override_or_sendgrid(monkeypatch):
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SMTP2GO_API_KEY", raising=False)
    monkeypatch.delenv("SMTP2GO_EMAIL_API_KEY", raising=False)

    assert email_sender._email_provider() == "ses"


def test_send_email_posts_smtp2go_payload(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"data": {"succeeded": 1, "failed": 0, "email_id": "email_123"}}

    def fake_post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setenv("EMAIL_PROVIDER", "smtp2go")
    monkeypatch.setenv("SMTP2GO_EMAIL_API_KEY", "smtp2go_test")
    monkeypatch.delenv("SMTP2GO_EMAIL_ENDPOINT", raising=False)
    monkeypatch.setattr(requests, "post", fake_post)

    result = email_sender.send_email(
        to_email="buyer@example.com",
        subject="Your code",
        text_body="Your code is 123456",
        html_body="<p>Your code is <strong>123456</strong></p>",
        from_email="noreply@pivota.ai",
        from_name="Pivota",
        reply_to="support@pivota.ai",
        tags={"type": "accounts_login_otp"},
    )

    assert result.ok is True
    assert result.provider == "smtp2go"
    assert result.message_id == "email_123"
    assert calls == [
        {
            "url": "https://api.smtp2go.com/v3/email/send",
            "headers": {
                "Content-Type": "application/json",
                "X-Smtp2go-Api-Key": "smtp2go_test",
                "accept": "application/json",
            },
            "json": {
                "sender": "noreply@pivota.ai",
                "to": ["buyer@example.com"],
                "subject": "Your code",
                "text_body": "Your code is 123456",
                "html_body": "<p>Your code is <strong>123456</strong></p>",
                "custom_headers": [
                    {"header": "Reply-To", "value": "support@pivota.ai"},
                    {"header": "X-Pivota-type", "value": "accounts_login_otp"},
                ],
            },
            "timeout": 10,
        }
    ]
