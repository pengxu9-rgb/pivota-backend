"""ADR-011 — the resolve-or-attach primitive's golden ATTACH/MINT/FLAG/SKIP matrix.

Covers the review-driven R3 semantics (GTIN'd content_key form first, GTIN-less
fallback second, disagreement → FLAG never a silent second identity), the
Tier-0 exact matchers (content_key / canonical_url / source_product_id), the
composed ADR-008 brand guard with per-door FLAG-vs-SKIP semantics, per-door
flags defaulting OFF, and fail-open on any internal error.

DB lookups are monkeypatched at the module-helper seam (each helper is one
small exact query) so the matrix is pure unit-golden — no infrastructure.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

import services.intake_identity as ii  # noqa: E402
from services.catalog_identity import make_content_key, normalize_gtin  # noqa: E402
from services.product_group_autogrouper import (  # noqa: E402
    make_singleton_product_group_id,
)

BRAND = "Anua"
TITLE = "Heartleaf 77% Soothing Toner"
GTIN = "8809640733458"
CK_GTIN = make_content_key(BRAND, TITLE, GTIN)
CK_PLAIN = make_content_key(BRAND, TITLE)
MERCHANT = "m_anua"


def _row(**over: Any) -> Dict[str, Any]:
    row = {
        "product_key": "prod::m_other::external_seed::x1",
        "merchant_id": "m_other",
        "platform": "external_seed",
        "source_product_id": "x1",
        "canonical_url": "https://anua.com/products/heartleaf-toner",
        "title": TITLE,
        "brand": BRAND,
        "content_key": CK_PLAIN,
        "pivota_signature_id": "sig_" + "a" * 32,
        "pivota_canonical_url": "https://agent.pivota.cc/products/sig_" + "a" * 32,
    }
    row.update(over)
    return row


@pytest.fixture()
def quiet(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    """Baseline: every lookup empty, provenance + review captured, no DB."""
    calls: Dict[str, List[Any]] = {"provenance": [], "reviews": [], "guard": []}

    async def none_rows(*a: Any, **k: Any) -> List[Dict[str, Any]]:
        return []

    async def none_one(*a: Any, **k: Any) -> Optional[Any]:
        return None

    async def capture_provenance(p: Dict[str, Any]) -> None:
        calls["provenance"].append(p)

    async def capture_review(door: str, ctx: Dict, ck: Optional[str], matcher: str, detail: Dict) -> None:
        calls["reviews"].append({"door": door, "matcher": matcher, "detail": detail})

    monkeypatch.setattr(ii, "_rows_by_content_key", none_rows)
    monkeypatch.setattr(ii, "_candidates_by_canonical_url", none_rows)
    monkeypatch.setattr(ii, "_candidates_by_source_id", none_rows)
    monkeypatch.setattr(ii, "_known_gtin13_for_content_key", none_one)
    monkeypatch.setattr(ii, "_content_key_for_gtin13", none_one)
    monkeypatch.setattr(ii, "_existing_pg_for_listing", none_one)
    monkeypatch.setattr(ii, "_write_provenance", capture_provenance)
    monkeypatch.setattr(ii, "_flag_review", capture_review)

    import services.audit_index_intake as intake

    async def guard_proceed(merchant_id: str, fields: Dict, **kw: Any) -> Dict[str, Any]:
        calls["guard"].append({"merchant_id": merchant_id, **kw})
        return {"action": "proceed", "reason": "no_conflict"}

    monkeypatch.setattr(
        intake, "apply_intake_brand_fragmentation_guard", guard_proceed
    )
    return calls


def _ctx(**over: Any) -> Dict[str, Any]:
    ctx = {"merchant_id": MERCHANT, "platform": "url_audit",
           "source_domain": "anua.com", "product_key": "prod::m_anua::url_audit::y"}
    ctx.update(over)
    return ctx


# --- Flags ------------------------------------------------------------------------


def test_all_door_flags_default_off():
    for door in ii._DOOR_FLAG_ENV:
        assert ii.intake_identity_enabled(door) is False


def test_flag_enables_one_door(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_MIRROR", "1")
    assert ii.intake_identity_enabled(ii.DOOR_EXTERNAL_SEED_MIRROR) is True
    assert ii.intake_identity_enabled(ii.DOOR_CATALOG_SYNC) is False


# --- MINT -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_when_nothing_matches(quiet):
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_MINT
    assert out["content_key"] == CK_PLAIN
    assert out["product_group_id"] == make_singleton_product_group_id(CK_PLAIN)
    assert out["attach"] is None
    # provenance {door, action, matcher, evidence} written for EVERY outcome
    assert len(quiet["provenance"]) == 1
    prov = quiet["provenance"][0]
    assert prov["door"] == ii.DOOR_URL_AUDIT
    assert prov["action"] == ii.ACTION_MINT
    assert prov["matcher"] is None
    assert prov["evidence"]["deposit_basis"] == "unresolved"


@pytest.mark.asyncio
async def test_mint_with_gtin_uses_gtin_form(quiet):
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_MINT
    assert out["content_key"] == CK_GTIN  # R3: the GTIN-aware form
    assert quiet["provenance"][0]["evidence"]["deposit_basis"] == "gtin"


@pytest.mark.asyncio
async def test_mint_null_identity_inputs(quiet):
    out = await ii.resolve_or_attach_content_identity(
        None, "", door=ii.DOOR_BRAND_AUTHORED, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_MINT
    assert out["content_key"] is None
    assert out["product_group_id"] is None  # honest absence — never invent a pg
    assert quiet["provenance"][0]["evidence"]["reason"] == "no_identity_inputs"


# --- ATTACH: content_key forms (Tier-0a, R3) ---------------------------------------


@pytest.mark.asyncio
async def test_attach_on_gtin_content_key_form(quiet, monkeypatch):
    async def rows(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(content_key=CK_GTIN)] if ck == CK_GTIN else []

    monkeypatch.setattr(ii, "_rows_by_content_key", rows)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert out["content_key"] == CK_GTIN
    assert out["product_group_id"] == make_singleton_product_group_id(CK_GTIN)
    assert quiet["provenance"][0]["matcher"] == "content_key_gtin"
    assert out["attach"]["same_merchant"] is False


@pytest.mark.asyncio
async def test_attach_gtin_less_fallback_when_gtin_form_misses(quiet, monkeypatch):
    """R3: the legacy catalog is GTIN-less — a GTIN'd source must find its
    GTIN-less twin instead of minting a parallel identity."""
    async def rows(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(content_key=CK_PLAIN)] if ck == CK_PLAIN else []

    monkeypatch.setattr(ii, "_rows_by_content_key", rows)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert out["content_key"] == CK_PLAIN  # reuses the twin, no second identity
    assert quiet["provenance"][0]["matcher"] == "content_key_brand_title_gtin_fallback"


@pytest.mark.asyncio
async def test_attach_plain_no_gtin(quiet, monkeypatch):
    async def rows(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row()] if ck == CK_PLAIN else []

    monkeypatch.setattr(ii, "_rows_by_content_key", rows)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, door=ii.DOOR_EXTERNAL_SEED_MIRROR, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert quiet["provenance"][0]["matcher"] == "content_key_brand_title"


@pytest.mark.asyncio
async def test_attach_reuses_existing_curated_pg(quiet, monkeypatch):
    async def rows(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row()] if ck == CK_PLAIN else []

    async def existing_pg(row: Dict[str, Any]) -> Optional[str]:
        return "pg_curated123"

    monkeypatch.setattr(ii, "_rows_by_content_key", rows)
    monkeypatch.setattr(ii, "_existing_pg_for_listing", existing_pg)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["product_group_id"] == "pg_curated123"  # curated pg wins over singleton


# --- FLAG: GTIN disagreements (R3) --------------------------------------------------


@pytest.mark.asyncio
async def test_flag_gtin_disagreement_same_brand_title(quiet, monkeypatch):
    """Twin exists on brand+title but is known under a DIFFERENT GTIN → FLAG,
    proceed with the fresh GTIN'd identity — flagged, never silent."""
    async def rows(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(content_key=CK_PLAIN)] if ck == CK_PLAIN else []

    async def known(ck: str) -> Optional[str]:
        return normalize_gtin("0000000000001")  # a different product's GTIN

    monkeypatch.setattr(ii, "_rows_by_content_key", rows)
    monkeypatch.setattr(ii, "_known_gtin13_for_content_key", known)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_FLAG
    assert out["content_key"] == CK_GTIN  # fresh GTIN'd identity, not the twin's
    assert quiet["provenance"][0]["matcher"] == "gtin_disagreement"
    assert len(quiet["reviews"]) == 1
    assert quiet["reviews"][0]["matcher"] == "gtin_disagreement"


@pytest.mark.asyncio
async def test_attach_when_twin_gtin_agrees(quiet, monkeypatch):
    async def rows(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(content_key=CK_PLAIN)] if ck == CK_PLAIN else []

    async def known(ck: str) -> Optional[str]:
        return normalize_gtin(GTIN)  # same GTIN — agreement, not conflict

    monkeypatch.setattr(ii, "_rows_by_content_key", rows)
    monkeypatch.setattr(ii, "_known_gtin13_for_content_key", known)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert out["content_key"] == CK_PLAIN


@pytest.mark.asyncio
async def test_flag_gtin_known_under_other_brand_title(quiet, monkeypatch):
    """Reverse disagreement: no content_key hit, but the GTIN already lives
    under a different brand+title identity → FLAG."""
    async def other(gtin13: str) -> Optional[Dict[str, Any]]:
        return {"content_key": "ck_" + "f" * 32, "brand": "Other", "title": "Thing"}

    monkeypatch.setattr(ii, "_content_key_for_gtin13", other)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_FLAG
    assert out["content_key"] == CK_GTIN
    assert quiet["provenance"][0]["matcher"] == "gtin_conflict"
    assert len(quiet["reviews"]) == 1


# --- ATTACH: URL / source-id exact matchers (Tier-0b/0c) ----------------------------


@pytest.mark.asyncio
async def test_attach_on_canonical_url_exact(quiet, monkeypatch):
    candidate = _row(content_key="ck_" + "b" * 32)

    async def cands(path: str) -> List[Dict[str, Any]]:
        return [candidate]

    monkeypatch.setattr(ii, "_candidates_by_canonical_url", cands)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE,
        canonical_url="https://www.anua.com/products/heartleaf-toner/",  # www+slash drift
        door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx(),
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert out["content_key"] == candidate["content_key"]
    assert quiet["provenance"][0]["matcher"] == "canonical_url_match"


@pytest.mark.asyncio
async def test_url_ambiguity_defers_to_mint(quiet, monkeypatch):
    async def cands(path: str) -> List[Dict[str, Any]]:
        return [_row(product_key="pk1"), _row(product_key="pk2")]  # two exact hits

    monkeypatch.setattr(ii, "_candidates_by_canonical_url", cands)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, canonical_url="https://anua.com/products/heartleaf-toner",
        door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx(),
    )
    assert out["action"] == ii.ACTION_MINT  # conservative: ambiguous ≠ attach


@pytest.mark.asyncio
async def test_attach_on_same_merchant_source_id(quiet, monkeypatch):
    listing = _row(
        product_key=f"prod::{MERCHANT}::url_audit::anua.com~abc123",
        merchant_id=MERCHANT, platform="url_audit",
        source_product_id="anua.com~abc123", content_key="ck_" + "c" * 32,
    )

    async def cands(spid: str, merchant_id: str) -> List[Dict[str, Any]]:
        assert merchant_id == MERCHANT  # same-merchant scope only
        return [listing]

    monkeypatch.setattr(ii, "_candidates_by_source_id", cands)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, source_product_id="anua.com~abc123",
        door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx(),
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert quiet["provenance"][0]["matcher"] == "source_product_id_match"
    # R4 signal: same-merchant attach carries the listing's identity + sig
    assert out["attach"]["same_merchant"] is True
    assert out["attach"]["source_product_id"] == "anua.com~abc123"
    assert out["attach"]["pivota_signature_id"] == listing["pivota_signature_id"]


# --- Brand-fragmentation guard (composed P1.4, per-door semantics) ------------------


@pytest.mark.asyncio
async def test_skip_on_brand_conflict_observed_door(quiet, monkeypatch):
    import services.audit_index_intake as intake

    async def guard(merchant_id: str, fields: Dict, **kw: Any) -> Dict[str, Any]:
        assert kw["block_on_conflict"] is True  # observed-data door blocks
        return {"action": "skip", "conflict_merchant_id": "m_other",
                "conflict_product_key": "pk_other"}

    monkeypatch.setattr(intake, "apply_intake_brand_fragmentation_guard", guard)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, door=ii.DOOR_EXTERNAL_SEED_MIRROR, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_SKIP
    assert quiet["provenance"][0]["matcher"] == "brand_host_fragmentation"


@pytest.mark.asyncio
async def test_flag_on_brand_conflict_first_party_door(quiet, monkeypatch):
    import services.audit_index_intake as intake

    async def guard(merchant_id: str, fields: Dict, **kw: Any) -> Dict[str, Any]:
        assert kw["block_on_conflict"] is False  # first-party: never blocked
        return {"action": "flag", "conflict_merchant_id": "m_other",
                "conflict_product_key": "pk_other"}

    monkeypatch.setattr(intake, "apply_intake_brand_fragmentation_guard", guard)
    for door in (ii.DOOR_CATALOG_SYNC, ii.DOOR_BRAND_AUTHORED):
        out = await ii.resolve_or_attach_content_identity(
            BRAND, TITLE, door=door, merchant_ctx=_ctx()
        )
        assert out["action"] == ii.ACTION_FLAG
        assert out["content_key"] == CK_PLAIN  # proceeds with the fresh identity


@pytest.mark.asyncio
async def test_brand_guard_memo_dedupes_per_run(quiet, monkeypatch):
    memo: set = set()
    out1 = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, door=ii.DOOR_CATALOG_SYNC,
        merchant_ctx=_ctx(brand_guard_memo=memo),
    )
    out2 = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE + " 2", door=ii.DOOR_CATALOG_SYNC,
        merchant_ctx=_ctx(brand_guard_memo=memo),
    )
    assert out1["action"] == out2["action"] == ii.ACTION_MINT
    assert len(quiet["guard"]) == 1  # guarded once per distinct brand per run


# --- Fail-open ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_open_mints_on_internal_error(quiet, monkeypatch):
    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(ii, "_rows_by_content_key", boom)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_MINT  # never blocks intake
    assert out["content_key"] == CK_GTIN
    assert out["evidence"]["evidence"]["reason"] == "error"
