from utils import email_sender


def test_email_provider_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "sg_test")
    monkeypatch.setenv("EMAIL_PROVIDER", "ses")

    assert email_sender._email_provider() == "ses"


def test_email_provider_uses_sendgrid_when_key_present(monkeypatch):
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.setenv("SENDGRID_API_KEY", "sg_test")

    assert email_sender._email_provider() == "sendgrid"


def test_email_provider_defaults_to_ses_without_override_or_sendgrid(monkeypatch):
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)

    assert email_sender._email_provider() == "ses"
