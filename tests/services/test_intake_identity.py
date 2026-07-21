"""ADR-011 — the resolve-or-attach primitive's golden ATTACH/MINT/FLAG/SKIP matrix.

SPU identity model (founder direction 2026-07-09): content_key is ALWAYS the
GTIN-less brand+title FAMILY key; GTIN is a match-ATTRIBUTE, never key-material.
Covers:
  - MINT keys purely on brand+title; the canonicalized GTIN is returned for the
    door to persist as the attribute (never folded into content_key);
  - Tier-0 GTIN attribute match is authoritative and cross-merchant → ATTACH;
  - same GTIN under a different brand+title family → ATTACH-to-GTIN + FLAG drift;
  - same brand+title, different GTIN → FLAG collision (two products on the
    deliberately non-unique family key; told apart by the gtin attribute
    downstream);
  - a product seen with-then-without a barcode converges on ONE identity;
  - canonical_url / source_product_id exact matchers, the composed ADR-008 brand
    guard (per-door FLAG-vs-SKIP), per-door flags default OFF, fail-open.

DB lookups are monkeypatched at the module-helper seam so the matrix is pure
unit-golden — no infrastructure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import services.intake_identity as ii  # noqa: E402
from services.catalog_identity import make_content_key  # noqa: E402
from services.product_group_autogrouper import (  # noqa: E402
    make_singleton_product_group_id,
)

BRAND = "Anua"
TITLE = "Heartleaf 77% Soothing Toner"
CK = make_content_key(BRAND, TITLE)          # the single canonical family key
GTIN_RAW = "8809640733458"                   # 13-digit EAN
GTIN14 = "08809640733458"                    # GS1-canonical 14-digit
GTIN14_OTHER = "00000000000017"
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
        "content_key": CK,
        "gtin": None,
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

    monkeypatch.setattr(ii, "_rows_by_gtin", none_rows)
    monkeypatch.setattr(ii, "_rows_by_content_key", none_rows)
    monkeypatch.setattr(ii, "_candidates_by_canonical_url", none_rows)
    monkeypatch.setattr(ii, "_candidates_by_source_id", none_rows)
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


# --- canonical_gtin + flags ---------------------------------------------------------


def test_canonical_gtin_only_keeps_clean_gtin14():
    assert ii.canonical_gtin("8809640733458") == GTIN14      # 13 → padded 14
    assert ii.canonical_gtin("012345678905") == "00012345678905"  # 12 → 14
    assert ii.canonical_gtin(" 08809640733458 ") == GTIN14   # trims + already 14
    assert ii.canonical_gtin("1234567890123456") is None     # 16 malformed → dropped
    assert ii.canonical_gtin("") is None
    assert ii.canonical_gtin(None) is None


def test_all_door_flags_default_off():
    for door in ii._DOOR_FLAG_ENV:
        assert ii.intake_identity_enabled(door) is False


def test_flag_enables_one_door(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_MIRROR", "1")
    assert ii.intake_identity_enabled(ii.DOOR_EXTERNAL_SEED_MIRROR) is True
    assert ii.intake_identity_enabled(ii.DOOR_CATALOG_SYNC) is False


# --- MINT (GTIN-less canonical key) -------------------------------------------------


@pytest.mark.asyncio
async def test_mint_when_nothing_matches(quiet):
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_MINT
    assert out["content_key"] == CK
    assert out["product_group_id"] == make_singleton_product_group_id(CK)
    assert out["gtin"] is None
    assert out["attach"] is None
    prov = quiet["provenance"][0]
    assert prov["door"] == ii.DOOR_URL_AUDIT
    assert prov["action"] == ii.ACTION_MINT
    assert prov["matcher"] is None


@pytest.mark.asyncio
async def test_mint_with_gtin_keeps_gtinless_key_returns_attribute(quiet):
    """The GTIN never enters content_key — it comes back as the attribute for
    the door to persist. This is the crux of the SPU model."""
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN_RAW, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_MINT
    assert out["content_key"] == CK          # GTIN-less family key, unchanged
    assert out["gtin"] == GTIN14             # canonicalized attribute
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


# --- Tier-0a: GTIN attribute (authoritative, cross-merchant) ------------------------


@pytest.mark.asyncio
async def test_attach_on_gtin_attribute_clean(quiet, monkeypatch):
    async def by_gtin(g: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(gtin=GTIN14, content_key=CK)] if g == GTIN14 else []

    monkeypatch.setattr(ii, "_rows_by_gtin", by_gtin)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN_RAW, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert out["content_key"] == CK
    assert quiet["provenance"][0]["matcher"] == "gtin_match"
    assert out["attach"]["same_merchant"] is False


@pytest.mark.asyncio
async def test_gtin_beats_brand_title_and_converges_across_title_drift(quiet, monkeypatch):
    """Same GTIN under a DIFFERENT brand+title family → GTIN authoritative:
    ATTACH to it, FLAG the drift, never fork a second identity."""
    other_ck = make_content_key("Anua", "Heartleaf Toner 250 ml")  # drifted title

    async def by_gtin(g: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(gtin=GTIN14, content_key=other_ck, product_key="pk_other")]

    monkeypatch.setattr(ii, "_rows_by_gtin", by_gtin)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN_RAW, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_FLAG
    assert out["content_key"] == other_ck          # converged onto the GTIN's identity
    assert out["attach"]["product_key"] == "pk_other"
    assert quiet["provenance"][0]["matcher"] == "gtin_match_brand_title_drift"
    assert len(quiet["reviews"]) == 1


@pytest.mark.asyncio
async def test_gtinless_observation_converges_onto_family(quiet, monkeypatch):
    """The convergence the SPU model wins that GTIN-in-key lost: a later
    barcode-less observation of the same product ATTACHes on brand+title."""
    async def by_ck(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(gtin=GTIN14, content_key=CK)] if ck == CK else []

    monkeypatch.setattr(ii, "_rows_by_content_key", by_ck)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=None, door=ii.DOOR_EXTERNAL_SEED_MIRROR, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert out["content_key"] == CK
    assert quiet["provenance"][0]["matcher"] == "content_key"


@pytest.mark.asyncio
async def test_gtin_only_attach_when_brand_title_absent(quiet, monkeypatch):
    """No brand+title, but the GTIN matches an existing identity → ATTACH
    (we never MINT an identity from a GTIN alone, but we can attach to one)."""
    async def by_gtin(g: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(gtin=GTIN14, content_key=CK)]

    monkeypatch.setattr(ii, "_rows_by_gtin", by_gtin)
    out = await ii.resolve_or_attach_content_identity(
        None, "", gtin=GTIN_RAW, door=ii.DOOR_CATALOG_ENRICHMENT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert out["content_key"] == CK


# --- Tier-0b: brand+title family, and the collision FLAG ----------------------------


@pytest.mark.asyncio
async def test_attach_on_content_key_no_gtin(quiet, monkeypatch):
    async def by_ck(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row()] if ck == CK else []

    monkeypatch.setattr(ii, "_rows_by_content_key", by_ck)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, door=ii.DOOR_EXTERNAL_SEED_MIRROR, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert quiet["provenance"][0]["matcher"] == "content_key"


@pytest.mark.asyncio
async def test_flag_brand_title_collision_distinct_gtin(quiet, monkeypatch):
    """Same brand+title, but a DIFFERENT GTIN already lives on the family key →
    two distinct products colliding on the non-unique key → FLAG. The row still
    lands under the shared family key; the gtin attribute discriminates them."""
    async def by_gtin(g: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return []  # incoming GTIN is not itself known yet

    async def by_ck(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(gtin=GTIN14_OTHER, product_key="pk_existing")] if ck == CK else []

    monkeypatch.setattr(ii, "_rows_by_gtin", by_gtin)
    monkeypatch.setattr(ii, "_rows_by_content_key", by_ck)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN_RAW, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_FLAG
    assert out["content_key"] == CK          # shared family key (hash can't fork)
    assert out["gtin"] == GTIN14             # its own attribute discriminates it
    assert quiet["provenance"][0]["matcher"] == "brand_title_collision"
    assert quiet["reviews"][0]["detail"]["existing_gtins"] == [GTIN14_OTHER]


@pytest.mark.asyncio
async def test_attach_when_family_gtin_agrees(quiet, monkeypatch):
    """Incoming GTIN equals an existing family member's GTIN → same product →
    clean ATTACH (no collision flag)."""
    async def by_gtin(g: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return []

    async def by_ck(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row(gtin=GTIN14)] if ck == CK else []

    monkeypatch.setattr(ii, "_rows_by_gtin", by_gtin)
    monkeypatch.setattr(ii, "_rows_by_content_key", by_ck)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN_RAW, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_ATTACH
    assert quiet["provenance"][0]["matcher"] == "content_key"


@pytest.mark.asyncio
async def test_attach_reuses_existing_curated_pg(quiet, monkeypatch):
    async def by_ck(ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        return [_row()] if ck == CK else []

    async def existing_pg(row: Dict[str, Any]) -> Optional[str]:
        return "pg_curated123"

    monkeypatch.setattr(ii, "_rows_by_content_key", by_ck)
    monkeypatch.setattr(ii, "_existing_pg_for_listing", existing_pg)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["product_group_id"] == "pg_curated123"  # curated pg wins over singleton


# --- Tier-0c/0d: URL / source-id exact matchers -------------------------------------


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
        assert out["content_key"] == CK  # proceeds with the family identity


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

    monkeypatch.setattr(ii, "_rows_by_gtin", boom)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, TITLE, gtin=GTIN_RAW, door=ii.DOOR_URL_AUDIT, merchant_ctx=_ctx()
    )
    assert out["action"] == ii.ACTION_MINT  # never blocks intake
    assert out["content_key"] == CK
    assert out["gtin"] == GTIN14
    assert out["evidence"]["evidence"]["reason"] == "error"
