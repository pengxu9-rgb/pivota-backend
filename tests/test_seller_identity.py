"""A9-2 — observed seller-of-record identity minting (ADR-009 D2).

Covers services/seller_identity.py (deterministic minting + resolve-or-mint +
claimed-brand attach) and the ADR-009 D2 tripwires that keep the banned
'external_seed' bucket out of new writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.mirror_external_seeds_to_catalog_products as mirror_module  # noqa: E402
import services.seller_identity as si  # noqa: E402


# ---------------------------------------------------------------------------
# Deterministic minting (make_observed_merchant_id / etld1)
# ---------------------------------------------------------------------------

def test_id_is_deterministic_same_brand_domain():
    a = si.make_observed_merchant_id("Anuko", "anuko.com")
    b = si.make_observed_merchant_id("Anuko", "anuko.com")
    assert a == b
    assert a.startswith(si.OBSERVED_ID_PREFIX)
    # 'merch_obs_' + 16 hex
    assert len(a) == len(si.OBSERVED_ID_PREFIX) + 16


def test_id_differs_on_brand():
    assert si.make_observed_merchant_id("Anuko", "anuko.com") != si.make_observed_merchant_id(
        "Other", "anuko.com"
    )


def test_id_differs_on_domain():
    assert si.make_observed_merchant_id("Anuko", "anuko.com") != si.make_observed_merchant_id(
        "Anuko", "anuko.co.kr"
    )


def test_brand_case_and_whitespace_collapse():
    # normalize_brand lowercases, trims, collapses internal whitespace + strips
    # suffix tokens — all these must resolve to ONE identity.
    canonical = si.make_observed_merchant_id("The Ordinary", "theordinary.com")
    assert canonical == si.make_observed_merchant_id("  the   ordinary  ", "theordinary.com")
    assert canonical == si.make_observed_merchant_id("The Ordinary Inc.", "theordinary.com")


def test_domain_normalized_scheme_www_path_collapse():
    canonical = si.make_observed_merchant_id("Anuko", "anuko.com")
    assert canonical == si.make_observed_merchant_id("Anuko", "https://www.anuko.com/products/x")


def test_empty_brand_raises():
    with pytest.raises(ValueError):
        si.make_observed_merchant_id("", "anuko.com")


def test_empty_domain_raises():
    with pytest.raises(ValueError):
        si.make_observed_merchant_id("Anuko", "")


def test_non_registrable_domain_raises():
    # a bare TLD is not a registrable domain — must never mint from it
    with pytest.raises(ValueError):
        si.make_observed_merchant_id("Anuko", "com")


def test_etld1_shared_platform_tenant_is_own_org():
    # a myshopify tenant is its own registrable org (3-label eTLD+1)
    assert si.etld1("anuko.myshopify.com") == "anuko.myshopify.com"
    # multi-label registry suffix
    assert si.etld1("shop.brand.co.uk") == "brand.co.uk"
    # plain subdomain collapses to the registrable base
    assert si.etld1("www.theordinary.com") == "theordinary.com"


def test_minted_id_never_equals_banned_bucket():
    # the prefix guarantees it, but pin the invariant explicitly
    ids = {
        si.make_observed_merchant_id(b, d)
        for b in ("Anuko", "The Ordinary", "external_seed")
        for d in ("anuko.com", "external-seed.com", "theordinary.com")
    }
    assert si.BANNED_BUCKET_MERCHANT_ID not in ids


# ---------------------------------------------------------------------------
# ensure_observed_seller — resolve-or-mint
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, claimed_rows=None):
        self.merchants: set = set()  # ids that "exist" in catalog_merchants
        self.claimed_rows = claimed_rows or []

    async def fetch_all(self, sql, params=None):
        # only the claimed-merchant lookup uses fetch_all
        return self.claimed_rows

    async def fetch_one(self, sql, params=None):
        mid = (params or {}).get("mid")
        return {"merchant_id": mid} if mid in self.merchants else None


@pytest.fixture
def wired(monkeypatch):
    db = _FakeDB()
    upserts = []

    async def fake_upsert(**kwargs):
        upserts.append(kwargs)
        db.merchants.add(kwargs["merchant_id"])  # now it "exists"

    monkeypatch.setattr(si, "database", db)
    monkeypatch.setattr(si, "upsert_catalog_merchant", fake_upsert)
    return db, upserts


async def test_ensure_mints_once_then_resolves(wired):
    db, upserts = wired
    id1 = await si.ensure_observed_seller(
        brand="Anuko", domain="anuko.com", source_system="external_brand_crawl"
    )
    assert id1.startswith(si.OBSERVED_ID_PREFIX)
    assert len(upserts) == 1
    # observed = identity-lifecycle state (unclaimed), NOT a serving switch —
    # see the ADR-009 amendment note in routes/pivota_canonical_routes.py
    assert upserts[0]["status"] == si.OBSERVED_STATUS
    assert upserts[0]["source_ref"] == "anuko.com"

    # second call for the SAME identity (different surface form) resolves, no dup
    id2 = await si.ensure_observed_seller(
        brand="anuko", domain="https://www.anuko.com/p", source_system="external_brand_crawl"
    )
    assert id2 == id1
    assert len(upserts) == 1  # no duplicate row


async def test_ensure_returns_claimed_tenant_without_minting(wired):
    db, upserts = wired
    db.claimed_rows = [{"merchant_id": "merch_tenant_123", "brand_domain": "www.anuko.com"}]
    resolved = await si.ensure_observed_seller(
        brand="Anuko", domain="anuko.com", source_system="external_brand_crawl"
    )
    # claiming attaches, never re-keys: resolve to the tenant, don't mint observed
    assert resolved == "merch_tenant_123"
    assert upserts == []


async def test_ensure_raises_on_empty_inputs(wired):
    with pytest.raises(ValueError):
        await si.ensure_observed_seller(brand="", domain="anuko.com", source_system="x")


# ---------------------------------------------------------------------------
# Tripwire 1 (runtime): the mirror write path refuses the banned bucket
# ---------------------------------------------------------------------------

class _OneRowDB:
    """Minimal fake: _apply's only pre-loop query is fetch_all(missing rows)."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch_all(self, sql, params=None):
        return self._rows

    async def execute(self, sql, params=None):  # pragma: no cover - must not reach
        raise AssertionError("write attempted despite banned seller")


async def test_mirror_apply_redlines_external_seed_write(monkeypatch):
    async def _returns_bucket(**kwargs):
        return si.BANNED_BUCKET_MERCHANT_ID  # simulate a resolver regression

    monkeypatch.setattr(mirror_module, "ensure_observed_seller", _returns_bucket)
    monkeypatch.setattr(
        mirror_module,
        "database",
        _OneRowDB(
            [
                {
                    "id": 1,
                    "external_product_id": "ext_abc",
                    "mirrored_brand": "Anuko",
                    "domain": "anuko.com",
                    "destination_url": "https://anuko.com/p",
                    "canonical_url": None,
                    "title": "Hair Oil",
                }
            ]
        ),
    )
    with pytest.raises(RuntimeError, match="ADR-009 D2"):
        await mirror_module._apply(limit=0)


# ---------------------------------------------------------------------------
# Tripwire 2 (repo): forward ingestion no longer CONSTRUCTS the singleton
# 'external_seed' identity for new writes. Historical read/backfill references
# (report metrics, legacy repair scripts) are allowed.
# ---------------------------------------------------------------------------

def test_onboard_does_not_construct_singleton_product_key():
    src = (ROOT / "scripts" / "onboard_external_brand_from_crawl.py").read_text()
    assert "prod::external_seed::external_seed::" not in src


def test_mirror_apply_does_not_select_singleton_product_key():
    src = (ROOT / "scripts" / "mirror_external_seeds_to_catalog_products.py").read_text()
    # the write-path SELECT used to build `'prod::…' || external_product_id AS
    # product_key`; that construction is gone (per-brand key built in Python).
    assert "external_product_id AS product_key" not in src


# ---------------------------------------------------------------------------
# Serving semantics (ADR-009 amendment, A9-2 review): merchant status is an
# IDENTITY-LIFECYCLE field, not a serving switch. Observed sellers' pages
# served under the shared bucket before A9-2 and MUST keep serving — otherwise
# per-brand identity would darken Path B's public citation output (a mainline
# regression). Product-level gates (serving_eligible / index_eligible) remain
# the sole serving control; the merchant gate means "not disabled".
# ---------------------------------------------------------------------------

def test_canonical_pdp_read_serves_observed_sellers():
    src = (ROOT / "routes" / "pivota_canonical_routes.py").read_text()
    # the by-signature PDP read gates on indexable + status in (active, observed)
    # — observed (unclaimed) sellers serve; a plain 'active'-only gate would be
    # the regression this test pins against.
    assert 'catalog_merchants.c.status.in_(["active", "observed"])' in src
    assert 'catalog_merchants.c.status == "active"' not in src
    assert "catalog_merchants.c.indexable.is_(True)" in src
