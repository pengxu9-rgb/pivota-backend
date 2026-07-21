from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import HTTPException


import services.buyer_reviews_service as buyer_reviews_service


def _enable_upload_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEWS_BUYER_SUBMIT_ENABLED", "true")
    monkeypatch.setenv("PDP_UGC_UPLOAD_ENABLED", "true")
    monkeypatch.delenv("REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST", raising=False)


@pytest.mark.asyncio
async def test_attach_media_from_user_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_upload_flags(monkeypatch)
    executed: list[Any] = []
    s3_calls = []

    async def fake_fetch_one(query: Any) -> Dict[str, Any] | None:
        q = str(query)
        if "buyer_review_user_subject" in q:
            return {"user_id": "u_1", "review_id": 88}
        if "product_reviews" in q:
            return {"id": 88, "merchant_id": "m_1", "status": "under_review"}
        return None

    async def fake_execute(query: Any) -> int | None:
        executed.append(query)
        if len(executed) == 1:
            return 501
        return None

    def fake_s3_put(public_id: str, *, filename: str, blob: bytes, content_type: str) -> str:
        s3_calls.append(
            {
                "public_id": public_id,
                "filename": filename,
                "blob": blob,
                "content_type": content_type,
            }
        )
        return f"s3://bucket/{public_id}"

    monkeypatch.setattr(buyer_reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_reviews_service.database, "execute", fake_execute)
    monkeypatch.setattr(buyer_reviews_service, "_reviews_media_s3_put", fake_s3_put)
    monkeypatch.setattr(buyer_reviews_service, "_new_media_public_id", lambda: "media_public_1")

    result = await buyer_reviews_service.attach_buyer_review_media_from_user(
        request=object(),  # type: ignore[arg-type]
        user_id="u_1",
        review_id=88,
        filename="proof.png",
        content_type="image/png",
        blob=b"abc",
    )

    assert result == {
        "status": "success",
        "review_id": 88,
        "media": {"id": 501, "public_id": "media_public_1", "type": "image"},
        "media_moderation_state": "under_review",
    }
    assert len(executed) == 2
    assert len(s3_calls) == 1
    assert s3_calls[0]["content_type"] == "image/png"
    insert_params = executed[0].compile().params
    update_params = executed[1].compile().params
    assert insert_params.get("status") == "under_review"
    assert "media_count" not in update_params


@pytest.mark.asyncio
async def test_attach_media_from_user_rejects_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_upload_flags(monkeypatch)
    calls = {"fetch_one": 0}

    async def fake_fetch_one(query: Any) -> None:
        calls["fetch_one"] += 1
        return None

    async def fake_execute(query: Any) -> int:
        raise AssertionError("execute should not be called for non-owner")

    monkeypatch.setattr(buyer_reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_reviews_service.database, "execute", fake_execute)

    with pytest.raises(HTTPException) as exc:
        await buyer_reviews_service.attach_buyer_review_media_from_user(
            request=object(),  # type: ignore[arg-type]
            user_id="u_2",
            review_id=99,
            filename="proof.png",
            content_type="image/png",
            blob=b"abc",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "NOT_FOUND"
    assert calls["fetch_one"] == 1


@pytest.mark.asyncio
async def test_attach_media_from_user_rejects_unsupported_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_upload_flags(monkeypatch)

    async def fake_fetch_one(query: Any) -> Dict[str, Any] | None:
        q = str(query)
        if "buyer_review_user_subject" in q:
            return {"user_id": "u_1", "review_id": 88}
        if "product_reviews" in q:
            return {"id": 88, "merchant_id": "m_1", "status": "under_review"}
        return None

    async def fake_execute(query: Any) -> int:
        raise AssertionError("execute should not be called when MIME is invalid")

    def fail_s3(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("s3 upload should not be called when MIME is invalid")

    monkeypatch.setattr(buyer_reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_reviews_service.database, "execute", fake_execute)
    monkeypatch.setattr(buyer_reviews_service, "_reviews_media_s3_put", fail_s3)

    with pytest.raises(HTTPException) as exc:
        await buyer_reviews_service.attach_buyer_review_media_from_user(
            request=object(),  # type: ignore[arg-type]
            user_id="u_1",
            review_id=88,
            filename="proof.pdf",
            content_type="application/pdf",
            blob=b"abc",
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "UNSUPPORTED_MEDIA_TYPE"


@pytest.mark.asyncio
async def test_attach_media_from_user_rejects_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_upload_flags(monkeypatch)
    monkeypatch.setenv("REVIEWS_BUYER_MEDIA_MAX_BYTES", "3")

    async def fake_fetch_one(query: Any) -> Dict[str, Any] | None:
        q = str(query)
        if "buyer_review_user_subject" in q:
            return {"user_id": "u_1", "review_id": 88}
        if "product_reviews" in q:
            return {"id": 88, "merchant_id": "m_1", "status": "under_review"}
        return None

    async def fake_execute(query: Any) -> int:
        raise AssertionError("execute should not be called when file is too large")

    monkeypatch.setattr(buyer_reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_reviews_service.database, "execute", fake_execute)

    with pytest.raises(HTTPException) as exc:
        await buyer_reviews_service.attach_buyer_review_media_from_user(
            request=object(),  # type: ignore[arg-type]
            user_id="u_1",
            review_id=88,
            filename="proof.png",
            content_type="image/png",
            blob=b"1234",
        )

    assert exc.value.status_code == 413
    assert exc.value.detail == "MEDIA_TOO_LARGE"


@pytest.mark.asyncio
async def test_attach_media_from_user_rejects_invalid_review_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_upload_flags(monkeypatch)

    async def fake_fetch_one(query: Any) -> Dict[str, Any] | None:
        q = str(query)
        if "buyer_review_user_subject" in q:
            return {"user_id": "u_1", "review_id": 88}
        if "product_reviews" in q:
            return {"id": 88, "merchant_id": "m_1", "status": "removed"}
        return None

    async def fake_execute(query: Any) -> int:
        raise AssertionError("execute should not be called when review status is invalid")

    monkeypatch.setattr(buyer_reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_reviews_service.database, "execute", fake_execute)

    with pytest.raises(HTTPException) as exc:
        await buyer_reviews_service.attach_buyer_review_media_from_user(
            request=object(),  # type: ignore[arg-type]
            user_id="u_1",
            review_id=88,
            filename="proof.png",
            content_type="image/png",
            blob=b"abc",
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "REVIEW_STATUS_INVALID"


@pytest.mark.asyncio
async def test_attach_media_from_user_returns_503_when_storage_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_upload_flags(monkeypatch)

    async def fake_fetch_one(query: Any) -> Dict[str, Any] | None:
        q = str(query)
        if "buyer_review_user_subject" in q:
            return {"user_id": "u_1", "review_id": 88}
        if "product_reviews" in q:
            return {"id": 88, "merchant_id": "m_1", "status": "under_review"}
        return None

    async def fake_execute(query: Any) -> int:
        raise AssertionError("execute should not be called when storage upload is unavailable")

    monkeypatch.setattr(buyer_reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_reviews_service.database, "execute", fake_execute)
    monkeypatch.setattr(buyer_reviews_service, "_reviews_media_s3_put", lambda *args, **kwargs: None)

    with pytest.raises(HTTPException) as exc:
        await buyer_reviews_service.attach_buyer_review_media_from_user(
            request=object(),  # type: ignore[arg-type]
            user_id="u_1",
            review_id=88,
            filename="proof.png",
            content_type="image/png",
            blob=b"abc",
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "MEDIA_STORAGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_attach_media_from_user_rejects_when_upload_rollout_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEWS_BUYER_SUBMIT_ENABLED", "true")
    monkeypatch.setenv("PDP_UGC_UPLOAD_ENABLED", "false")
    calls = {"fetch_one": 0}

    async def fake_fetch_one(query: Any) -> None:
        calls["fetch_one"] += 1
        return None

    monkeypatch.setattr(buyer_reviews_service.database, "fetch_one", fake_fetch_one)

    with pytest.raises(HTTPException) as exc:
        await buyer_reviews_service.attach_buyer_review_media_from_user(
            request=object(),  # type: ignore[arg-type]
            user_id="u_1",
            review_id=88,
            filename="proof.png",
            content_type="image/png",
            blob=b"abc",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "PDP_UGC_UPLOAD_DISABLED"
    assert calls["fetch_one"] == 0


@pytest.mark.asyncio
async def test_attach_media_from_user_rejects_not_allowed_merchant(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_upload_flags(monkeypatch)
    monkeypatch.setenv("REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST", "allowed_mid")

    async def fake_fetch_one(query: Any) -> Dict[str, Any] | None:
        q = str(query)
        if "buyer_review_user_subject" in q:
            return {"user_id": "u_1", "review_id": 88}
        if "product_reviews" in q:
            return {"id": 88, "merchant_id": "blocked_mid", "status": "under_review"}
        return None

    async def fake_execute(query: Any) -> int:
        raise AssertionError("execute should not be called when merchant is not allowed")

    monkeypatch.setattr(buyer_reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_reviews_service.database, "execute", fake_execute)

    with pytest.raises(HTTPException) as exc:
        await buyer_reviews_service.attach_buyer_review_media_from_user(
            request=object(),  # type: ignore[arg-type]
            user_id="u_1",
            review_id=88,
            filename="proof.png",
            content_type="image/png",
            blob=b"abc",
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "NOT_ALLOWED"
