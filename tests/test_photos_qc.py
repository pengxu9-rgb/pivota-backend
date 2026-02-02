import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

from main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class FakeS3:
    def __init__(self):
        self.objects = {}

    def generate_presigned_post(self, Bucket, Key, Fields, Conditions, ExpiresIn):
        return {"url": "https://storage.example/upload", "fields": {"key": Key, **(Fields or {})}}

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

    monkeypatch.setattr(photos.database, "execute", fake_execute)
    monkeypatch.setattr(photos.database, "fetch_one", fake_fetch_one)
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
    assert "fields" in body["upload"]
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

    # 4) delete
    res4 = client.delete(
        "/photos",
        headers={"X-API-Key": "test-api-key"},
        params={"upload_id": upload_id},
    )
    assert res4.status_code == 200
    assert res4.json()["deleted"] is True

