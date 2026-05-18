import pytest

from services.wix_connection import (
    WIX_PRODUCTS_QUERY_URL,
    WixConnectionValidationError,
    extract_wix_site_id,
    normalize_wix_api_key,
    normalize_wix_site_id,
    validate_wix_catalog_access,
)


def test_normalize_wix_site_id_rejects_public_store_url() -> None:
    with pytest.raises(WixConnectionValidationError) as exc_info:
        normalize_wix_site_id("https://peng652.wixsite.com/aydan-1")

    assert exc_info.value.code == "WIX_SITE_ID_EXPECTED"


def test_extract_wix_site_id_prefers_credential_blob() -> None:
    site_id = extract_wix_site_id(
        "https://example.wixsite.com/store",
        '{"site_id":"site_123","api_key":"token_123"}',
    )

    assert site_id == "site_123"


def test_normalize_wix_api_key_reads_json_blob() -> None:
    assert normalize_wix_api_key('{"site_id":"site_123","api_key":"token_123"}') == "token_123"


@pytest.mark.asyncio
async def test_validate_wix_catalog_access_requires_real_200(monkeypatch) -> None:
    import httpx

    captured = {}

    class DummyResponse:
        status_code = 403
        text = "forbidden"

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return DummyResponse()

    monkeypatch.setattr(httpx, "AsyncClient", DummyAsyncClient)

    with pytest.raises(WixConnectionValidationError) as exc_info:
        await validate_wix_catalog_access("site_123", "token_123")

    assert captured["url"] == WIX_PRODUCTS_QUERY_URL
    assert captured["json"] == {"query": {"paging": {"limit": 1}}}
    assert captured["headers"]["wix-site-id"] == "site_123"
    assert exc_info.value.code == "WIX_PERMISSION_DENIED"
