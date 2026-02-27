from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import services.reviews_service as reviews_service


def _freeze_now(monkeypatch, *, ts: int = 1_700_000_000) -> None:
    fixed = datetime.fromtimestamp(ts, tz=timezone.utc)
    monkeypatch.setattr(reviews_service, "_now", lambda: fixed)


def test_build_signed_review_media_url_returns_relative_by_default(monkeypatch) -> None:
    monkeypatch.delenv("REVIEW_MEDIA_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("REVIEWS_MEDIA_SIGNING_SECRET", "unit-test-secret")
    _freeze_now(monkeypatch)

    url = reviews_service.build_signed_review_media_url(public_id="pub_123", ttl_seconds=300)
    assert url.startswith("/agent/shop/v1/review-media/pub_123?")

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    exp = int(qs["exp"][0])
    sig = qs["sig"][0]
    assert reviews_service.verify_review_media_signature(public_id="pub_123", exp=exp, sig=sig) is True


def test_build_signed_review_media_url_returns_absolute_when_public_base_configured(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_MEDIA_PUBLIC_BASE_URL", "https://web-production-fedb.up.railway.app")
    monkeypatch.setenv("REVIEWS_MEDIA_SIGNING_SECRET", "unit-test-secret")
    _freeze_now(monkeypatch)

    url = reviews_service.build_signed_review_media_url(public_id="pub_456", ttl_seconds=300)
    assert url.startswith("https://web-production-fedb.up.railway.app/agent/shop/v1/review-media/pub_456?")

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    exp = int(qs["exp"][0])
    sig = qs["sig"][0]
    assert reviews_service.verify_review_media_signature(public_id="pub_456", exp=exp, sig=sig) is True


def test_build_signed_review_media_url_sanitizes_public_base_value(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_MEDIA_PUBLIC_BASE_URL", " \"https://web-production-fedb.up.railway.app/\\n\" ")
    monkeypatch.setenv("REVIEWS_MEDIA_SIGNING_SECRET", "unit-test-secret")
    _freeze_now(monkeypatch)

    url = reviews_service.build_signed_review_media_url(public_id="pub_789", ttl_seconds=300)
    assert url.startswith("https://web-production-fedb.up.railway.app/agent/shop/v1/review-media/pub_789?")
