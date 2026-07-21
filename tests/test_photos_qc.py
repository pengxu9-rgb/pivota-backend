import os
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


from main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class FakeS3:
    def __init__(self):
        self.objects = {}

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        # Minimal fake URL; tests don't execute the actual PUT, they only validate
        # that presign returns a URL and then simulate the object existing via FakeS3.objects.
        bucket = Params.get("Bucket")
        key = Params.get("Key")
        method = "download" if ClientMethod == "get_object" else "upload"
        return f"https://storage.example/{bucket}/{key}?sig=fake&method={method}"

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise Exception("NotFound")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise Exception("NotFound")

        class Body:
            def __init__(self, b):
                self._b = b

            def read(self):
                return self._b

        return {"Body": Body(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_photos_presign_confirm_qc_delete(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.photos as photos

    # Configure module globals
    photos.PHOTO_UPLOAD_BUCKET = "bucket-test"
    os.environ["ADMIN_API_KEY"] = "test-admin-key"
    monkeypatch.setenv("PHOTO_UPLOAD_ACCESS_KEY_ID", "test-photo-ak")
    monkeypatch.setenv("PHOTO_UPLOAD_SECRET_ACCESS_KEY", "test-photo-sk")

    fake_s3 = FakeS3()
    monkeypatch.setattr(photos, "_s3_client", lambda: fake_s3)

    # In-memory DB
    store = {}

    async def fake_execute(query: str, values=None):
        q = str(query)
        v = values or {}
        if "INSERT INTO photo_uploads" in q:
            store[v["upload_id"]] = {
                "upload_id": v["upload_id"],
                "agent_id": v.get("agent_id"),
                "user_id": v.get("user_id"),
                "consented": True,
                "status": "created",
                "bucket": v.get("bucket"),
                "object_key": v.get("object_key"),
                "content_type": v.get("content_type"),
                "byte_size": v.get("byte_size"),
                "expires_at": v.get("expires_at"),
                "deleted_at": None,
                "qc_status": None,
                "qc_advice": None,
                "qc_details": None,
            }
            return 1
        if "UPDATE photo_uploads SET" in q:
            upload_id = v.get("upload_id")
            if upload_id in store:
                for key, val in v.items():
                    if key == "upload_id":
                        continue
                    if key in store[upload_id]:
                        store[upload_id][key] = val
                # crude status update detection
                if "status" in v:
                    store[upload_id]["status"] = v["status"]
            return 1
        return 1

    async def fake_fetch_one(query: str, values=None):
        v = values or {}
        upload_id = v.get("id") or v.get("upload_id")
        if upload_id and upload_id in store:
            return store[upload_id]
        return None

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        v = values or {}
        if "FROM photo_uploads" in q and "expires_at <=" in q:
            now = v.get("now")
            out = []
            for row in store.values():
                if row.get("deleted_at") is not None:
                    continue
                expires_at = row.get("expires_at")
                if not expires_at or not now:
                    continue
                if isinstance(expires_at, str):
                    try:
                        expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    except Exception:
                        continue
                if expires_at <= now:
                    out.append(row)
            return out
        return []

    monkeypatch.setattr(photos.database, "execute", fake_execute)
    monkeypatch.setattr(photos.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(photos.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(photos, "_ensure_photo_uploads_table", AsyncMock(return_value=None))

    # QC: force deterministic status
    monkeypatch.setattr(photos, "_qc_classify_image", lambda _b: ("too_dark", {"avg_luma": 10}))

    # 1) presign
    res = client.post(
        "/photos/presign",
        headers={"X-API-Key": "test-api-key"},
        json={"content_type": "image/jpeg", "consent": True, "byte_size": 1234, "user_id": "u_1"},
    )
    assert res.status_code == 200
    body = res.json()
    upload_id = body["upload_id"]
    assert body["upload"]["url"].startswith("https://")
    assert body["upload"]["method"] == "PUT"
    assert body["upload"]["headers"]["Content-Type"] == "image/jpeg"
    assert body["tips"]["daylight"]

    # simulate upload by placing object in fake storage
    key = store[upload_id]["object_key"]
    fake_s3.objects[(photos.PHOTO_UPLOAD_BUCKET, key)] = b"fake-image-bytes"

    # 2) confirm triggers qc
    res2 = client.post(
        "/photos/confirm",
        headers={"X-API-Key": "test-api-key"},
        json={"upload_id": upload_id, "byte_size": 1234},
    )
    assert res2.status_code == 200

    # 3) qc result
    res3 = client.get(
        "/photos/qc",
        headers={"X-API-Key": "test-api-key"},
        params={"upload_id": upload_id},
    )
    assert res3.status_code == 200
    qc = res3.json()["qc"]
    assert qc["qc_status"] in {"passed", "too_dark", "has_filter", "blurry"}
    assert "tips" in qc["advice"]
    assert qc["advice"]["retryable"] is True

    # 4) download URL supports both GET and POST contracts
    res_dl = client.get(
        "/photos/download-url",
        headers={"X-API-Key": "test-api-key"},
        params={"upload_id": upload_id},
    )
    assert res_dl.status_code == 200
    dl_body = res_dl.json()
    assert dl_body["download"]["method"] == "GET"
    assert dl_body["download"]["url"].startswith("https://storage.example/")
    assert "method=download" in dl_body["download"]["url"]
    assert dl_body["content_type"] == "image/jpeg"

    res_dl_post = client.post(
        "/photos/download-url",
        headers={"X-API-Key": "test-api-key"},
        json={"upload_id": upload_id},
    )
    assert res_dl_post.status_code == 200
    assert res_dl_post.json()["download"]["url"].startswith("https://storage.example/")

    # 5) delete
    res4 = client.delete(
        "/photos",
        headers={"X-API-Key": "test-api-key"},
        params={"upload_id": upload_id},
    )
    assert res4.status_code == 200
    assert res4.json()["deleted"] is True

    # 6) cleanup (expired rows)
    res5 = client.post(
        "/photos/presign",
        headers={"X-API-Key": "test-api-key"},
        json={"content_type": "image/jpeg", "consent": True, "byte_size": 10, "user_id": "u_2"},
    )
    assert res5.status_code == 200
    upload_id_2 = res5.json()["upload_id"]

    # force expiry
    store[upload_id_2]["expires_at"] = (datetime.utcnow() - timedelta(hours=1)).replace(tzinfo=None)

    # simulate upload present
    key2 = store[upload_id_2]["object_key"]
    fake_s3.objects[(photos.PHOTO_UPLOAD_BUCKET, key2)] = b"fake-image-bytes"

    res6 = client.post(
        "/photos/cleanup",
        headers={"X-ADMIN-KEY": "test-admin-key"},
        params={"limit": 10},
    )
    assert res6.status_code == 200
    body6 = res6.json()
    assert body6["status"] == "success"
    assert body6["deleted"] >= 1
    assert store[upload_id_2]["deleted_at"] is not None
    assert store[upload_id_2]["status"] == "deleted"
    assert (photos.PHOTO_UPLOAD_BUCKET, key2) not in fake_s3.objects


def test_photos_presign_fails_fast_when_storage_credentials_missing(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.photos as photos

    photos.PHOTO_UPLOAD_BUCKET = "bucket-test"
    monkeypatch.delenv("PHOTO_UPLOAD_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("PHOTO_UPLOAD_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SECRET_KEY", raising=False)
    monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", raising=False)

    def _unexpected_s3_client():
        raise AssertionError("_s3_client should not be called when credentials are missing")

    monkeypatch.setattr(photos, "_s3_client", _unexpected_s3_client)

    res = client.post(
        "/photos/presign",
        headers={"X-API-Key": "test-api-key"},
        json={"content_type": "image/jpeg", "consent": True, "byte_size": 1234, "user_id": "u_1"},
    )

    assert res.status_code == 500
    assert res.json()["detail"] == "STORAGE_CREDENTIALS_NOT_CONFIGURED"


def test_photo_schema_auto_ensure_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.photos as photos

    async def fail_execute(*_args, **_kwargs):
        raise AssertionError("schema DDL should not run when request auto-ensure is disabled")

    previous_ready = photos._photo_schema_ready
    previous_enabled = photos.PHOTO_SCHEMA_ENSURE_ON_REQUEST
    monkeypatch.setattr(photos.database, "execute", fail_execute)
    photos._photo_schema_ready = False
    photos.PHOTO_SCHEMA_ENSURE_ON_REQUEST = False
    try:
        asyncio.run(photos._ensure_photo_uploads_table())
    finally:
        photos._photo_schema_ready = previous_ready
        photos.PHOTO_SCHEMA_ENSURE_ON_REQUEST = previous_enabled
