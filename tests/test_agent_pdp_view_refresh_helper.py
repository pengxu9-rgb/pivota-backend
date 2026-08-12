"""Tests for refresh_agent_pdp_view_for_content_key — the canonical
fetch->assemble->upsert orchestration used by the catalog_sync auto-servable
hook (so a fresh internal-merchant SKU gets an APV row before recompute).

🚨 WHAT THE _FakeDB CASES BELOW CANNOT PROVE. `_FakeDB.fetch_one` returns a
canned row whatever SQL it is handed, so the enrichment-write cases assert only
the CONTROL FLOW around the bridge — the flag gate, the identity guard, the
best-effort swallow. They cannot see whether the statement is valid, and they
did not: all four passed for weeks while the bridge queried
`catalog_products.platform_product_id`, a column that does not exist, so every
real call raised UndefinedColumn into the swallow and the bridge was dead.

The statement itself is constrained in two other places, and both are load
bearing: tests/test_enrichment_bridge_and_cohort_postgres.py EXECUTES it against
a real catalog_products row (reverting the column turns it red), and the
repo-wide prepare gate plans it. Do not add a case here that claims the bridge
"works" — this file cannot support that claim."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import agent_pdp_view_assembler as apv  # noqa: E402

_PRODUCT_FOR_TRISTATE = {
    "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
    "source_product_id": "sp-1", "title": "Glow Serum",
    "description": "A long enough description for the agent PDP view row.",
    "brand": "AuraGlow",
}


class _FakeDB:
    def __init__(self, fetch_one_result: Optional[Dict[str, Any]] = None) -> None:
        self.executes: List[Dict[str, Any]] = []
        self.fetch_ones: List[Dict[str, Any]] = []
        self._fetch_one_result = fetch_one_result

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        self.executes.append({"sql": sql, "params": params})

    async def fetch_one(self, sql: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.fetch_ones.append({"sql": sql, "params": params})
        return self._fetch_one_result


def _patch_fetches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    products: List[Dict[str, Any]],
) -> None:
    async def fake_products(content_key: str, *, db: Any = None):
        return products

    async def fake_skus(product_keys, *, db: Any = None):
        return []

    async def fake_offers(product_keys, *, db: Any = None):
        return []

    async def fake_seed(product_keys, *, db: Any = None):
        return None

    async def fake_evidence(product_keys, *, db: Any = None):
        return {}

    monkeypatch.setattr(apv, "fetch_products_for_key", fake_products)
    monkeypatch.setattr(apv, "fetch_skus_for_keys", fake_skus)
    monkeypatch.setattr(apv, "fetch_offers_for_keys", fake_offers)
    monkeypatch.setattr(apv, "fetch_external_seed_for_keys", fake_seed)
    monkeypatch.setattr(apv, "fetch_evidence_for_keys", fake_evidence)


@pytest.mark.asyncio
async def test_refresh_builds_and_upserts_when_title_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1",
            "merchant_id": "m1",
            "platform": "shopify",
            "source_product_id": "sp-1",
            "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
            "image_url": "https://img.example/serum.jpg",
        }],
    )
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-1", refresh_source="catalog_sync", db=db
    )
    assert built is True
    assert len(db.executes) == 1
    assert db.executes[0]["sql"] is apv.UPSERT_SQL
    assert db.executes[0]["params"]["content_key"] == "ck-1"
    assert db.executes[0]["params"]["refresh_source"] == "catalog_sync"


@pytest.mark.asyncio
async def test_refresh_projects_evidence_into_agent_pdp_view(monkeypatch: pytest.MonkeyPatch) -> None:
    # Graded claims authored on the canonical record must reach the agent view.
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
            "source_product_id": "sp-1", "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
        }],
    )
    profile = {"claims": [{"claim_text": "Helps brighten", "source_type": "ingredient_mechanism",
                           "substantiation_status": "substantiated"}], "review_state": "observed"}
    disclaimers = [{"text": "FDA disclaimer"}]

    async def fake_evidence(product_keys, *, db: Any = None):
        return {"evidence_profile": profile, "required_disclaimers": disclaimers}

    monkeypatch.setattr(apv, "fetch_evidence_for_keys", fake_evidence)
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key("ck-1", refresh_source="catalog_sync", db=db)
    assert built is True
    params = db.executes[0]["params"]
    # JSONB columns are serialized to a JSON string by row_to_upsert_params.
    assert params["evidence_profile"] == apv.to_jsonb(profile)
    assert params["required_disclaimers"] == apv.to_jsonb(disclaimers)
    assert "Helps brighten" in params["evidence_profile"]


@pytest.mark.asyncio
async def test_refresh_overlays_enrichment_description_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2 publish bridge: refresh fetches the product_enrichment overlay and the
    generated description_markdown reaches the upserted agent_pdp_view.description
    — the wire that lets enriched copy reach the served PDP AND the
    serving-eligibility gate (which reads that stored description)."""
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
            "source_product_id": "sp-1", "title": "Good Night Collagen",
            "description": "thin raw storefront description",
            "brand": "BB Lab",
        }],
    )

    async def fake_enrichment(products: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return {"description_markdown": "Low-molecular collagen, 30 sticks. Halal-certified."}

    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", fake_enrichment)
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-1", refresh_source="canonical_pdp_enrichment", db=db
    )
    assert built is True
    assert (
        db.executes[0]["params"]["description"]
        == "Low-molecular collagen, 30 sticks. Halal-certified."
    )


@pytest.mark.asyncio
async def test_refresh_returns_false_when_no_products(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetches(monkeypatch, products=[])
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-missing", refresh_source="catalog_sync", db=db
    )
    assert built is False
    assert db.executes == []


@pytest.mark.asyncio
async def test_refresh_returns_false_when_row_too_thin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Products exist but no title → assemble_row returns None → no upsert.
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-2",
            "merchant_id": "m1",
            "platform": "shopify",
            "source_product_id": "sp-2",
            "title": None,
        }],
    )
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-2", refresh_source="catalog_sync", db=db
    )
    assert built is False
    assert db.executes == []


# ── B① write-triggered enrichment propagation ──────────────────────────────


@pytest.mark.asyncio
async def test_enrichment_write_refresh_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default OFF: no content_key resolution, no view rebuild — current behavior.
    monkeypatch.delenv("SERVE_PDP_ENRICHMENT_ON_WRITE", raising=False)
    db = _FakeDB(fetch_one_result={"content_key": "ck-1"})
    out = await apv.refresh_agent_pdp_view_for_enrichment_write(
        "m1", "shopify", "sp-1", db=db
    )
    assert out is False
    assert db.fetch_ones == []  # didn't even resolve the content_key
    assert db.executes == []


@pytest.mark.asyncio
async def test_enrichment_write_refresh_false_on_missing_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    db = _FakeDB(fetch_one_result={"content_key": "ck-1"})
    out = await apv.refresh_agent_pdp_view_for_enrichment_write("m1", None, "sp-1", db=db)
    assert out is False
    assert db.fetch_ones == []


@pytest.mark.asyncio
async def test_enrichment_write_refresh_false_when_no_catalog_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    db = _FakeDB(fetch_one_result=None)  # no catalog_products row maps to a content_key
    out = await apv.refresh_agent_pdp_view_for_enrichment_write(
        "m1", "shopify", "sp-1", db=db
    )
    assert out is False
    assert len(db.fetch_ones) == 1  # tried to resolve
    assert db.executes == []


@pytest.mark.asyncio
async def test_enrichment_write_refresh_rebuilds_view_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag on + content_key resolves → the helper rebuilds the served view via
    # refresh_agent_pdp_view_for_content_key (one upsert with the resolved key).
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
            "source_product_id": "sp-1", "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
        }],
    )
    db = _FakeDB(fetch_one_result={"content_key": "ck-resolved"})
    out = await apv.refresh_agent_pdp_view_for_enrichment_write(
        "m1", "shopify", "sp-1", db=db
    )
    assert out is True
    assert len(db.fetch_ones) == 1
    assert len(db.executes) == 1
    assert db.executes[0]["params"]["content_key"] == "ck-resolved"
    assert db.executes[0]["params"]["refresh_source"] == "enrichment_write"


@pytest.mark.asyncio
async def test_enrichment_write_refresh_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Best-effort: a DB error during resolution must never raise into the writer.
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")

    class _BoomDB:
        async def fetch_one(self, sql: str, params: Dict[str, Any]):
            raise RuntimeError("db down")

    out = await apv.refresh_agent_pdp_view_for_enrichment_write(
        "m1", "shopify", "sp-1", db=_BoomDB()
    )
    assert out is False


# ---------------------------------------------------------------------------
# tri-state overlay fetch
# ---------------------------------------------------------------------------
# A failed overlay READ and a successful read that finds NOTHING both used to
# arrive as None, so the write could not tell "preserve what is published" from
# "the operator deleted it". These pin the distinction at the point it is made;
# tests/test_agent_pdp_view_overlay_preservation_postgres.py proves the UPSERT
# then honours it against real Postgres.

@pytest.mark.asyncio
async def test_a_failed_enrichment_fetch_asks_the_write_to_preserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetches(monkeypatch, products=[dict(_PRODUCT_FOR_TRISTATE)])

    async def boom(products):
        return apv.FETCH_FAILED

    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", boom)
    row = await apv.build_agent_pdp_view_row("ck-1", refresh_source="t", db=_FakeDB())
    assert row is not None
    assert row["preserve_enrichment"] is True
    assert row["preserve_evidence"] is False


@pytest.mark.asyncio
async def test_a_successful_fetch_finding_nothing_does_not_preserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that keeps this a tri-state rather than a never-downgrade rule:
    a genuine removal must still reach the served row."""
    _patch_fetches(monkeypatch, products=[dict(_PRODUCT_FOR_TRISTATE)])

    async def absent(products):
        return None

    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", absent)
    row = await apv.build_agent_pdp_view_row("ck-1", refresh_source="t", db=_FakeDB())
    assert row is not None
    assert row["preserve_enrichment"] is False


@pytest.mark.asyncio
async def test_a_failed_evidence_fetch_preserves_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetches(monkeypatch, products=[dict(_PRODUCT_FOR_TRISTATE)])

    async def boom(product_keys, *, db=None):
        raise RuntimeError("evidence store down")

    async def absent(products):
        return None

    monkeypatch.setattr(apv, "fetch_evidence_for_keys", boom)
    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", absent)
    row = await apv.build_agent_pdp_view_row("ck-1", refresh_source="t", db=_FakeDB())
    assert row is not None
    assert row["preserve_evidence"] is True
    assert row["preserve_enrichment"] is False


@pytest.mark.asyncio
async def test_the_sentinel_is_reached_through_the_real_swallow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the REAL _fetch_enrichment_for_canonical, not a stub of it.

    The per-member `except ... continue` inside it is the actual source of the
    ambiguity; a test that replaces the whole function cannot show that a raising
    get_enrichment now yields FETCH_FAILED rather than None.
    """
    import db.product_enrichment as pe_module

    async def boom(*args, **kwargs):
        raise RuntimeError("connection reset by peer")

    # The bulk, merchant-scoped fetch — the assembler no longer calls
    # get_enrichment per cluster member.
    monkeypatch.setattr(pe_module, "get_enrichments_for_products", boom)
    result = await apv._fetch_enrichment_for_canonical([dict(_PRODUCT_FOR_TRISTATE)])
    assert result is apv.FETCH_FAILED

    async def nothing(*args, **kwargs):
        return {}

    monkeypatch.setattr(pe_module, "get_enrichments_for_products", nothing)
    assert await apv._fetch_enrichment_for_canonical([dict(_PRODUCT_FOR_TRISTATE)]) is None


# ---------------------------------------------------------------------------
# unresolvable-identity warnings
# ---------------------------------------------------------------------------
# 248 of 360 product_enrichment rows (69%, measured 2026-08-11) are unjoinable:
# catalog_products was re-keyed under them and nothing re-keys the overlay. The
# bridge saw every one of those writes and returned False in silence, so the
# drift was invisible until someone went looking. These pin the only signal that
# exists at write time.

class _IdentityDB:
    """Returns whatever the catalog lookup should yield; records the SQL so the
    widened predicate can be asserted on the statement actually sent."""

    def __init__(self, row):
        self._row = row
        self.sql_seen: List[str] = []

    async def fetch_one(self, sql: str, params: Dict[str, Any]):
        self.sql_seen.append(sql)
        return self._row

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        raise AssertionError("must not write when the identity is unresolvable")


@pytest.mark.asyncio
async def test_an_unresolvable_identity_is_logged_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    apv._UNRESOLVABLE_WARNED.clear()
    db = _IdentityDB(None)  # no catalog row under this triple

    with caplog.at_level(logging.WARNING, logger=apv.logger.name):
        out = await apv.refresh_agent_pdp_view_for_enrichment_write(
            "m-drifted", "shopify", "sp-old-slug", db=db
        )

    assert out is False
    hits = [
        json.loads(r.getMessage()) for r in caplog.records
        if isinstance(r.msg, str) and "enrichment_write_pdp_refresh_unresolvable" in r.msg
    ]
    assert hits, "an unresolvable enrichment write was swallowed silently"
    assert hits[0]["reason"] == "no_catalog_row_for_identity"
    # The identity must be IN the log — a warning that does not say which product
    # drifted cannot be acted on.
    assert hits[0]["merchant_id"] == "m-drifted"
    assert hits[0]["example_platform_product_id"] == "sp-old-slug"


@pytest.mark.asyncio
async def test_an_unkeyed_product_is_reported_as_its_own_reason(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A product that exists but is not content-keyed yet is a DIFFERENT problem
    from one that does not exist: it needs no intervention. Reporting both as
    "no catalog row" would send someone hunting for identity drift that is not
    there."""
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    apv._UNRESOLVABLE_WARNED.clear()
    db = _IdentityDB({"content_key": None})

    with caplog.at_level(logging.WARNING, logger=apv.logger.name):
        out = await apv.refresh_agent_pdp_view_for_enrichment_write(
            "m1", "shopify", "sp-1", db=db
        )

    assert out is False
    hits = [
        json.loads(r.getMessage()) for r in caplog.records
        if isinstance(r.msg, str) and "enrichment_write_pdp_refresh_unresolvable" in r.msg
    ]
    assert hits and hits[0]["reason"] == "catalog_row_has_no_content_key"


class _PredicateHonouringCatalog:
    """A fake catalog that EVALUATES the lookup's content_key predicate.

    The test this replaces asserted on SQL TEXT. That is the anti-pattern
    tests/test_enrichment_bridge_and_cohort_postgres.py exists to eliminate, and
    it failed the same way: `AND NOT (content_key IS NULL)` — semantically the
    exact filter the widening removed, spelled differently — passed the grep and
    every other test in the repo. This fake holds ONE row under the identity with
    content_key=None and applies whatever NULL-ness condition the statement
    carries, so any spelling that filters unkeyed rows makes the row vanish and
    the reason flips to no_catalog_row_for_identity.
    """

    ROW = {"merchant_id": "m1", "platform": "shopify",
           "source_product_id": "sp-1", "content_key": None}

    def __init__(self) -> None:
        self.params_seen: List[Dict[str, Any]] = []

    async def fetch_one(self, sql: str, params: Dict[str, Any]):
        self.params_seen.append(dict(params))
        normalized = " ".join(sql.split()).lower()
        if (self.ROW["merchant_id"] != params.get("merchant_id")
                or self.ROW["platform"] != params.get("platform")
                or self.ROW["source_product_id"] != params.get("platform_product_id")):
            return None
        # Any spelling that demands a non-NULL content_key excludes this row.
        excludes_unkeyed = (
            "content_key is not null" in normalized
            or "not (content_key is null)" in normalized
            or "btrim(content_key) <> ''" in normalized
            or "coalesce(content_key" in normalized
        )
        if excludes_unkeyed and self.ROW["content_key"] is None:
            return None
        return {"content_key": self.ROW["content_key"]}

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        raise AssertionError("must not write for an unkeyed product")


@pytest.mark.asyncio
async def test_an_unkeyed_row_reaches_the_bridge_and_is_named_correctly(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """One behavioural test covering what five separate gaps left unconstrained:
    the widened predicate, the reason code, the logged identity, the exact bound
    params, the logger the record lands on, and its level."""
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    apv._UNRESOLVABLE_WARNED.clear()
    db = _PredicateHonouringCatalog()

    with caplog.at_level(logging.DEBUG, logger=apv.logger.name):
        out = await apv.refresh_agent_pdp_view_for_enrichment_write(
            "m1", "shopify", "sp-1", db=db
        )

    assert out is False
    # The identity was bound to the RIGHT parameters — a fake that ignores params
    # cannot catch merchant_id and platform_product_id being swapped.
    assert db.params_seen == [
        {"merchant_id": "m1", "platform": "shopify", "platform_product_id": "sp-1"}
    ]
    records = [
        r for r in caplog.records
        if isinstance(r.msg, str) and "enrichment_write_pdp_refresh_unresolvable" in r.msg
    ]
    assert records, "an unkeyed product produced no warning"
    record = records[0]
    # WARNING exactly. ERROR still satisfies caplog's floor but would become a
    # billed Sentry event (main.py initialises the SDK at sample_rate=1.0, whose
    # LoggingIntegration promotes ERROR from breadcrumb to event).
    assert record.levelno == logging.WARNING, f"level is {record.levelname}"
    # On THIS module's logger — caplog's handler is on the root, so without this
    # the warnings could move to a logger prod filters out and stay green.
    assert record.name == apv.logger.name
    payload = json.loads(record.getMessage())  # must be parseable JSON, not a repr
    assert payload["reason"] == "catalog_row_has_no_content_key"
    assert payload["merchant_id"] == "m1"
    assert payload["example_platform_product_id"] == "sp-1"


@pytest.mark.asyncio
async def test_a_whitespace_content_key_never_reaches_the_refresh(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """'   ' is truthy. Without .strip() it skipped both branches and was passed
    to refresh_agent_pdp_view_for_content_key, which would assemble and UPSERT an
    agent_pdp_view row keyed on whitespace."""
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")

    for blank in ("", "   ", "\t"):
        apv._UNRESOLVABLE_WARNED.clear()
        caplog.clear()
        db = _IdentityDB({"content_key": blank})
        with caplog.at_level(logging.WARNING, logger=apv.logger.name):
            out = await apv.refresh_agent_pdp_view_for_enrichment_write(
                "m1", "shopify", "sp-1", db=db
            )
        assert out is False, f"{blank!r} was treated as a usable content_key"
        payloads = [
            json.loads(r.getMessage()) for r in caplog.records
            if isinstance(r.msg, str) and "unresolvable" in r.msg
        ]
        assert payloads and payloads[0]["reason"] == "catalog_row_has_no_content_key", (
            f"{blank!r} did not report as an unkeyed row"
        )


@pytest.mark.asyncio
async def test_a_batch_over_one_merchant_emits_one_line_not_hundreds(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The alert has to survive an ordinary worker pass.

    jobs/product_enrichment_worker.py walks every cached row for a merchant and
    the bulk endpoint allows 1000, against a corpus with 248 known-unresolvable
    rows. One line per write would saturate on day one and be muted, hiding the
    next drift exactly as the silence did.
    """
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    apv._UNRESOLVABLE_WARNED.clear()
    db = _IdentityDB(None)

    with caplog.at_level(logging.WARNING, logger=apv.logger.name):
        for i in range(200):
            await apv.refresh_agent_pdp_view_for_enrichment_write(
                "m-stale", "shopify", f"sp-{i}", db=db
            )
        # A DIFFERENT merchant is still worth its own line — that shape is what
        # separates an id-space migration from one merchant's stale cache.
        await apv.refresh_agent_pdp_view_for_enrichment_write(
            "m-other", "shopify", "sp-x", db=db
        )

    payloads = [
        json.loads(r.getMessage()) for r in caplog.records
        if isinstance(r.msg, str) and "unresolvable" in r.msg
    ]
    assert len(payloads) == 2, f"expected 2 lines (one per merchant), got {len(payloads)}"
    assert {p["merchant_id"] for p in payloads} == {"m-stale", "m-other"}


@pytest.mark.asyncio
async def test_a_resolvable_identity_logs_no_warning(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The happy path must stay quiet, or the signal is worthless."""
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
            "source_product_id": "sp-1", "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
        }],
    )
    db = _FakeDB(fetch_one_result={"content_key": "ck-resolved"})

    with caplog.at_level(logging.WARNING, logger=apv.logger.name):
        out = await apv.refresh_agent_pdp_view_for_enrichment_write(
            "m1", "shopify", "sp-1", db=db
        )

    assert out is True
    assert not [
        r for r in caplog.records
        if isinstance(r.msg, str) and "enrichment_write_pdp_refresh_unresolvable" in r.msg
    ], "the happy path emitted a drift warning"
