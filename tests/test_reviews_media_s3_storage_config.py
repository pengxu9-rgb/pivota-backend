from __future__ import annotations

import sys
import types

import services.reviews_service as reviews_service


def test_reviews_media_bucket_falls_back_to_photo_upload_bucket(monkeypatch):
    monkeypatch.delenv("REVIEWS_MEDIA_S3_BUCKET", raising=False)
    monkeypatch.setenv("PHOTO_UPLOAD_BUCKET", "photo-bucket")

    assert reviews_service._reviews_media_s3_bucket() == "photo-bucket"


def test_reviews_media_endpoint_and_region_fallback(monkeypatch):
    monkeypatch.delenv("REVIEWS_MEDIA_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("REVIEWS_MEDIA_S3_REGION", raising=False)
    monkeypatch.setenv("PHOTO_UPLOAD_ENDPOINT_URL", "https://example-r2")
    monkeypatch.setenv("PHOTO_UPLOAD_REGION", "auto")

    assert reviews_service._reviews_media_s3_endpoint_url() == "https://example-r2"
    assert reviews_service._reviews_media_s3_region() == "auto"


def test_reviews_media_prefix_falls_back_to_photo_upload_prefix(monkeypatch):
    monkeypatch.delenv("REVIEWS_MEDIA_S3_PREFIX", raising=False)
    monkeypatch.setenv("PHOTO_UPLOAD_PREFIX", "selfies")

    assert reviews_service._reviews_media_s3_prefix() == "selfies"


def test_reviews_media_prefix_override_wins_over_photo_prefix(monkeypatch):
    monkeypatch.setenv("REVIEWS_MEDIA_S3_PREFIX", "reviews-dedicated")
    monkeypatch.setenv("PHOTO_UPLOAD_PREFIX", "selfies")

    assert reviews_service._reviews_media_s3_prefix() == "reviews-dedicated"


def test_reviews_media_prefix_defaults_when_no_env(monkeypatch):
    monkeypatch.delenv("REVIEWS_MEDIA_S3_PREFIX", raising=False)
    monkeypatch.delenv("PHOTO_UPLOAD_PREFIX", raising=False)

    assert reviews_service._reviews_media_s3_prefix() == "reviews-media"


def test_reviews_media_put_uses_s3_client_helper(monkeypatch):
    monkeypatch.setenv("REVIEWS_MEDIA_S3_BUCKET", "reviews-bucket")
    monkeypatch.setenv("REVIEWS_MEDIA_S3_PREFIX", "reviews-media")

    captured = {}

    class _FakeClient:
        def put_object(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(reviews_service, "_reviews_media_s3_client", lambda: _FakeClient())

    uri = reviews_service._reviews_media_s3_put(
        "public123",
        filename="proof.png",
        blob=b"abc",
        content_type="image/png",
    )

    assert uri == "s3://reviews-bucket/reviews-media/public123.png"
    assert captured["Bucket"] == "reviews-bucket"
    assert captured["Key"] == "reviews-media/public123.png"
    assert captured["Body"] == b"abc"
    assert captured["ContentType"] == "image/png"


def test_reviews_media_client_endpoint_fallback_does_not_use_aws_session_token(monkeypatch):
    monkeypatch.setenv("REVIEWS_MEDIA_S3_ENDPOINT_URL", "https://storage.example.com")
    monkeypatch.delenv("REVIEWS_MEDIA_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("REVIEWS_MEDIA_S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("REVIEWS_MEDIA_S3_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("PHOTO_UPLOAD_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("PHOTO_UPLOAD_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("PHOTO_UPLOAD_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-ak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-sk")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "aws-sts-token")

    captured = {}

    fake_boto3 = types.ModuleType("boto3")

    def _fake_client(service_name, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return object()

    fake_boto3.client = _fake_client

    fake_botocore_client = types.ModuleType("botocore.client")

    class _FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_botocore_client.Config = _FakeConfig

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore.client", fake_botocore_client)

    client = reviews_service._reviews_media_s3_client()

    assert client is not None
    assert captured["service_name"] == "s3"
    assert captured["aws_access_key_id"] == "aws-ak"
    assert captured["aws_secret_access_key"] == "aws-sk"
    assert "aws_session_token" not in captured
