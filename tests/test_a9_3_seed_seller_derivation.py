"""A9-3 (ADR-009 D3) — write-time seller-of-record derivation for external seeds.

Proves `services.seller_identity.derive_seed_seller` (and the small per-writer
wrappers that feed it) resolve:
  - SELF  : the destination belongs to the anchor merchant → (anchor, 'self');
  - CROSS : otherwise → (observed_seller, 'cross');
  - UNRESOLVABLE : no destination, OR a cross seed with empty brand / non-
            registrable domain → (None, None) with a LOUD log — never guessed,
            never assumed 'self' (founder no-fallback directive).
Also proves the anchor extraction from a `prod::` storage key (IDENTITY_REFERENCE
§2 / Trap T1) and the `_anchor_owns_domain` lookups.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import seller_identity as si  # noqa: E402


# --- anchor extraction (pure) --------------------------------------------------


def test_anchor_merchant_from_prod_storage_key():
    # prod::{merchant}::{platform}::{pid} → merchant (IDENTITY_REFERENCE §2)
    assert (
        si.anchor_merchant_from_product_key("prod::merch_abc::shopify::123")
        == "merch_abc"
    )


def test_anchor_merchant_from_pipe_transport_key_defensive():
    # Trap T1 transport form should never be persisted, but be defensive.
    assert si.anchor_merchant_from_product_key("merch_x|shopify|9") == "merch_x"


def test_anchor_merchant_from_synthetic_or_empty_is_none():
    # The enrichment agent's synthetic pk_<hash> has no tenant anchor → None → CROSS.
    assert si.anchor_merchant_from_product_key("pk_deadbeef") is None
    assert si.anchor_merchant_from_product_key("") is None
    assert si.anchor_merchant_from_product_key(None) is None


# --- _anchor_owns_domain -------------------------------------------------------


class _OwnDB:
    """Answers the three _anchor_owns_domain lookups from canned tables."""

    def __init__(
        self,
        *,
        catalog_source_ref: Optional[str] = None,
        mcp_shop_domain: Optional[str] = None,
        store_domains: Optional[List[str]] = None,
    ) -> None:
        self.catalog_source_ref = catalog_source_ref
        self.mcp_shop_domain = mcp_shop_domain
        self.store_domains = store_domains or []

    async def fetch_one(self, query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "catalog_merchants" in query:
            return {"source_ref": self.catalog_source_ref} if self.catalog_source_ref else None
        if "merchant_onboarding" in query:
            return {"mcp_shop_domain": self.mcp_shop_domain} if self.mcp_shop_domain else None
        return None

    async def fetch_all(self, query: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"domain": d} for d in self.store_domains]


@pytest.mark.asyncio
async def test_anchor_owns_domain_via_catalog_source_ref(monkeypatch):
    monkeypatch.setattr(si, "database", _OwnDB(catalog_source_ref="anuko.myshopify.com"))
    assert await si._anchor_owns_domain("merch_1", "anuko.myshopify.com") is True
    # a different registrable domain does not match
    assert await si._anchor_owns_domain("merch_1", "someoneelse.com") is False


@pytest.mark.asyncio
async def test_anchor_owns_domain_via_mcp_and_store(monkeypatch):
    monkeypatch.setattr(si, "database", _OwnDB(mcp_shop_domain="https://brand.com/shop"))
    assert await si._anchor_owns_domain("merch_2", "brand.com") is True
    monkeypatch.setattr(si, "database", _OwnDB(store_domains=["www.brand.com", "x.io"]))
    assert await si._anchor_owns_domain("merch_2", "brand.com") is True


@pytest.mark.asyncio
async def test_anchor_owns_domain_empty_inputs_false(monkeypatch):
    monkeypatch.setattr(si, "database", _OwnDB())
    assert await si._anchor_owns_domain("", "brand.com") is False
    assert await si._anchor_owns_domain("merch_2", "") is False


class _DoorDB(_OwnDB):
    """_OwnDB where any of the three ownership lookups can be independently
    broken (raises like a dead/wedged connection)."""

    def __init__(self, *, break_catalog=False, break_onboarding=False,
                 break_stores=False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.break_catalog = break_catalog
        self.break_onboarding = break_onboarding
        self.break_stores = break_stores

    async def fetch_one(self, query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "catalog_merchants" in query and self.break_catalog:
            raise RuntimeError("connection was closed in the middle of operation")
        if "merchant_onboarding" in query and self.break_onboarding:
            raise AssertionError("Connection is already acquired")
        return await super().fetch_one(query, values)

    async def fetch_all(self, query: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self.break_stores:
            raise RuntimeError("connection was closed in the middle of operation")
        return await super().fetch_all(query, values)


class TestAnchorOwnsDomainFailFastDoors:
    """2026-08-09, run 31345305445: on a wedged connection the per-lookup
    swallows made _anchor_owns_domain answer CROSS for every caller, silently
    fragmenting one merchant into observed duplicates. Every door must answer
    BOTH ways: a working lookup answers the question, a broken lookup RAISES —
    never defaults to the permissive answer."""

    @pytest.mark.asyncio
    async def test_broken_catalog_merchants_lookup_propagates(self, monkeypatch):
        monkeypatch.setattr(si, "database", _DoorDB(break_catalog=True))
        with pytest.raises(RuntimeError, match="closed in the middle"):
            await si._anchor_owns_domain("merch_1", "brand.com")

    @pytest.mark.asyncio
    async def test_broken_merchant_onboarding_lookup_propagates(self, monkeypatch):
        # The first lookup is healthy (and answers "no match") — the failure in
        # the SECOND lookup must still propagate, not fall through to stores.
        monkeypatch.setattr(si, "database", _DoorDB(
            catalog_source_ref="other.com", break_onboarding=True))
        with pytest.raises(AssertionError, match="already acquired"):
            await si._anchor_owns_domain("merch_1", "brand.com")

    @pytest.mark.asyncio
    async def test_broken_merchant_stores_lookup_propagates(self, monkeypatch):
        # First two lookups healthy, no match; the LAST door must also raise
        # rather than settle for the accumulated False.
        monkeypatch.setattr(si, "database", _DoorDB(
            catalog_source_ref="other.com", mcp_shop_domain="other.com",
            break_stores=True))
        with pytest.raises(RuntimeError, match="closed in the middle"):
            await si._anchor_owns_domain("merch_1", "brand.com")

    @pytest.mark.asyncio
    async def test_healthy_lookups_still_answer_both_ways(self, monkeypatch):
        # Positive control both ways: owner -> True, non-owner -> False.
        monkeypatch.setattr(si, "database", _DoorDB(store_domains=["www.brand.com"]))
        assert await si._anchor_owns_domain("merch_1", "brand.com") is True
        monkeypatch.setattr(si, "database", _DoorDB(store_domains=["other.com"]))
        assert await si._anchor_owns_domain("merch_1", "brand.com") is False


# --- derive_seed_seller branching ---------------------------------------------


@pytest.mark.asyncio
async def test_derive_self_when_anchor_owns_destination(monkeypatch):
    async def _owns(anchor: str, registrable: str) -> bool:
        return True

    async def _mint(**_: Any) -> str:  # must NOT be called on the self path
        raise AssertionError("ensure_observed_seller must not run for a self seed")

    monkeypatch.setattr(si, "_anchor_owns_domain", _owns)
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    seller_ref, seed_kind = await si.derive_seed_seller(
        anchor_merchant_id="merch_anchor",
        brand="Anuko",
        destination_domain="https://anuko.com/p/1",
        source_system="test",
    )
    assert (seller_ref, seed_kind) == ("merch_anchor", "self")


@pytest.mark.asyncio
async def test_derive_retailer_destination_is_cross_not_self(monkeypatch):
    # A KNOWN-RETAILER destination is NEVER 'self', even when the anchor "owns" it
    # (an observed seller minted from the retailer host). It must be 'cross' so the
    # trust policy requires identity coverage (→ shadow) instead of serving the seed
    # brand-official/PUBLIC. Regression: VODANA no-D2C crawled from Amazon minted
    # self→public (2026-07-20).
    async def _owns(anchor: str, registrable: str) -> bool:
        return True  # pre-fix this alone forced 'self'

    async def _mint(**_: Any) -> str:
        return "merch_obs_retailerseller"

    monkeypatch.setattr(si, "_anchor_owns_domain", _owns)
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    for dest in (
        "https://www.amazon.com/dp/B08LV4KT49",
        "https://global.oliveyoung.com/product/detail?prdtNo=GA241026123",
        "https://www.stylekorean.com/brand/vodana",
    ):
        seller_ref, seed_kind = await si.derive_seed_seller(
            anchor_merchant_id="merch_anchor",
            brand="VODANA",
            destination_domain=dest,
            source_system="test",
        )
        assert seed_kind == "cross", f"{dest} must be cross, got {seed_kind}"
        assert seller_ref == "merch_obs_retailerseller"


@pytest.mark.asyncio
async def test_derive_self_still_holds_for_brand_own_domain(monkeypatch):
    # A brand's OWN (non-retailer) domain still resolves 'self' → public — unchanged
    # for D2C brands (e.g. the StyleKorean-pilot Shopify mints). The retailer guard
    # must not touch this path.
    async def _owns(anchor: str, registrable: str) -> bool:
        return True

    async def _mint(**_: Any) -> str:
        raise AssertionError("self path must not mint an observed seller")

    monkeypatch.setattr(si, "_anchor_owns_domain", _owns)
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    seller_ref, seed_kind = await si.derive_seed_seller(
        anchor_merchant_id="merch_anchor",
        brand="Anuko",
        destination_domain="https://anuko.com/p/1",
        source_system="test",
    )
    assert (seller_ref, seed_kind) == ("merch_anchor", "self")


def test_stylekorean_is_now_a_known_retailer():
    from services.offer_seller_identity import is_known_retailer
    assert is_known_retailer("stylekorean.com")
    assert is_known_retailer("www.stylekorean.com")
    assert is_known_retailer("global.oliveyoung.com")
    assert not is_known_retailer("anuko.com")  # a brand's own domain is not a retailer


@pytest.mark.asyncio
async def test_derive_cross_mints_observed_seller(monkeypatch):
    async def _owns(anchor: str, registrable: str) -> bool:
        return False

    async def _mint(**kwargs: Any) -> str:
        assert kwargs["brand"] == "OtherBrand"
        return "merch_obs_cafecafecafecafe"

    monkeypatch.setattr(si, "_anchor_owns_domain", _owns)
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    seller_ref, seed_kind = await si.derive_seed_seller(
        anchor_merchant_id="merch_anchor",
        brand="OtherBrand",
        destination_domain="otherbrand.com",
        source_system="test",
    )
    assert (seller_ref, seed_kind) == ("merch_obs_cafecafecafecafe", "cross")


@pytest.mark.asyncio
async def test_derive_cross_with_no_anchor(monkeypatch):
    async def _mint(**_: Any) -> str:
        return "merch_obs_1234123412341234"

    # anchor None → skip ownership check entirely → CROSS
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    seller_ref, seed_kind = await si.derive_seed_seller(
        anchor_merchant_id=None,
        brand="Brand",
        destination_domain="brand.com",
        source_system="test",
    )
    assert seed_kind == "cross"
    assert seller_ref == "merch_obs_1234123412341234"


@pytest.mark.asyncio
async def test_derive_no_destination_is_quiet_null(monkeypatch, caplog):
    # No destination to key a seller on → (None, None), NO loud warning (content-
    # only writer; pre-A9-4 legacy state). Never assumed 'self'.
    with caplog.at_level(logging.WARNING, logger="services.seller_identity"):
        seller_ref, seed_kind = await si.derive_seed_seller(
            anchor_merchant_id="merch_anchor",
            brand="Brand",
            destination_domain=None,
            source_system="test",
        )
    assert (seller_ref, seed_kind) == (None, None)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.asyncio
async def test_derive_unresolvable_cross_is_loud_null(monkeypatch, caplog):
    # Present destination but ensure_observed_seller raises (empty brand) → NULL +
    # LOUD warning. Never guessed a seller, never assumed 'self'.
    async def _owns(anchor: str, registrable: str) -> bool:
        return False

    async def _mint(**_: Any) -> str:
        raise ValueError("cannot mint an observed seller identity from empty inputs")

    monkeypatch.setattr(si, "_anchor_owns_domain", _owns)
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    with caplog.at_level(logging.WARNING, logger="services.seller_identity"):
        seller_ref, seed_kind = await si.derive_seed_seller(
            anchor_merchant_id="merch_anchor",
            brand="",
            destination_domain="brand.com",
            source_system="test",
        )
    assert (seller_ref, seed_kind) == (None, None)
    assert any(
        "UNRESOLVABLE" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )


# --- per-writer wrappers ---------------------------------------------------------


@pytest.mark.asyncio
async def test_enrichment_apply_wrapper_derives_from_plan_row(monkeypatch):
    # catalog_enrichment_agent/apply: brand rides in the seed_data JSON string;
    # attached_product_key is a synthetic pk_<hash> → no anchor → CROSS.
    from services.catalog_enrichment_agent import apply as enr

    captured: Dict[str, Any] = {}

    async def _derive(**kwargs: Any):
        captured.update(kwargs)
        return ("merch_obs_feedfeedfeedfeed", "cross")

    monkeypatch.setattr(si, "derive_seed_seller", _derive)
    seller_ref, seed_kind = await enr._derive_seed_seller_for_plan_row({
        "seed_data": '{"brand": "GlowLab"}',
        "attached_product_key": "pk_deadbeef",
        "domain": "retailer.com",
        "destination_url": "https://retailer.com/p/1",
        "tool": "enrichment_v3",
    })
    assert (seller_ref, seed_kind) == ("merch_obs_feedfeedfeedfeed", "cross")
    assert captured["anchor_merchant_id"] is None       # synthetic key → no anchor
    assert captured["brand"] == "GlowLab"
    assert captured["destination_domain"] == "retailer.com"


@pytest.mark.asyncio
async def test_seed_data_writer_wrapper_content_only_proposal_is_null(monkeypatch):
    # seed_data_writer: a content-only proposal (no destination) → (None, None),
    # the honest pre-A9-4 state — never assumed 'self'.
    import services.seed_data_writer as sdw

    called = {"n": 0}

    async def _mint(**_: Any) -> str:
        called["n"] += 1
        return "merch_obs_x"

    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    seller_ref, seed_kind = await sdw._derive_seed_seller_from_proposal(
        {"title": "New title", "description": "..."}
    )
    assert (seller_ref, seed_kind) == (None, None)
    assert called["n"] == 0                              # nothing minted from nothing


@pytest.mark.asyncio
async def test_seed_data_writer_wrapper_with_destination_derives(monkeypatch):
    import services.seed_data_writer as sdw

    async def _owns(anchor: str, registrable: str) -> bool:
        return True

    monkeypatch.setattr(si, "_anchor_owns_domain", _owns)
    seller_ref, seed_kind = await sdw._derive_seed_seller_from_proposal({
        "attached_product_key": "prod::merch_anchor::shopify::42",
        "brand": "Brand",
        "destination_url": "https://brand.com/p",
    })
    assert (seller_ref, seed_kind) == ("merch_anchor", "self")


@pytest.mark.asyncio
async def test_employee_products_wrapper_threads_through(monkeypatch):
    # routes/employee_products._derive_seed_seller_columns is a thin adapter over
    # derive_seed_seller — prove the anchor parse + argument threading.
    import routes.employee_products as ep

    captured: Dict[str, Any] = {}

    async def _derive(**kwargs: Any):
        captured.update(kwargs)
        return ("merch_anchor", "self")

    monkeypatch.setattr(si, "derive_seed_seller", _derive)
    seller_ref, seed_kind = await ep._derive_seed_seller_columns(
        attached_product_key="prod::merch_anchor::shopify::7",
        brand="Brand",
        destination="brand.com",
        source_system="employee_seed",
    )
    assert (seller_ref, seed_kind) == ("merch_anchor", "self")
    assert captured["anchor_merchant_id"] == "merch_anchor"
    assert captured["destination_domain"] == "brand.com"
    assert captured["source_system"] == "employee_seed"


# --- repo tripwire: every seed INSERT writer carries seller_ref -------------------


def test_every_seed_insert_carries_seller_ref_columns():
    """ADR-009 D3 tripwire (mirrors the A9-2 banned-bucket repo tripwire): every
    `INSERT INTO external_product_seeds` in scripts/routes/services must bind the
    seller_ref + seed_kind columns, so no writer can silently regress to
    NULL-by-omission for NEW seeds."""
    root = Path(si.__file__).resolve().parents[1]
    missing: List[str] = []
    for sub in ("scripts", "routes", "services"):
        for path in (root / sub).rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "INSERT INTO external_product_seeds" not in text:
                continue
            for i, chunk in enumerate(text.split("INSERT INTO external_product_seeds")[1:], 1):
                head = chunk[:1400]  # the column list + VALUES clause
                if "seller_ref" not in head or "seed_kind" not in head:
                    missing.append(f"{path.relative_to(root)} (insert #{i})")
    assert not missing, (
        "external_product_seeds INSERT(s) without seller_ref/seed_kind — every "
        "NEW seed must derive its seller-of-record at write time (ADR-009 D3, "
        "services/seller_identity.derive_seed_seller):\n  " + "\n  ".join(missing)
    )
