"""The measurement lane. Decision logic only — the SQL is exercised by the Postgres gate file.

The failure this file is built against is not a crash. It is a sweep that runs to completion,
reports a clean 100% refusal rate, and is read as a fact about merchants when it is a fact about
our own fence being shut. So the guards get as much attention as the arithmetic.
"""

from __future__ import annotations

import pytest

from services import checkout_preflight as cp
from scripts import measure_checkout_preflight as sweep

VID = "41234567890123"
OTHER_VID = "42999888777666"
PK = "prod::m_brand::shopify::serum"


def _row(**kw):
    r = {
        "id": "eps_1",
        "attached_product_key": PK,
        "external_product_id": "brand-serum",
        "url": "https://brand.example/products/serum",
        "seed_data": {"snapshot": {}},
    }
    r.update(kw)
    return r


def _shopify_seed(variants):
    return {"snapshot": {"storefront_platform": "shopify", "variants": variants}}


# --- which hand-overs the sweep is allowed to ask about -----------------------------------------

def test_the_catalog_lane_wins_over_the_seed_stamp():
    """The route prefers the catalog id, so the sweep must measure the id a buyer would be handed.

    Measuring the other one would report a refusal rate for a cart nobody is ever given.
    """
    seed = _shopify_seed([{"shopify_variant_id": OTHER_VID}])
    got, lane = sweep.gated_variant_id(_row(), seed, {PK: VID})
    assert (got, lane) == (VID, "catalog")


def test_the_seed_stamp_is_used_when_catalog_is_silent():
    seed = _shopify_seed([{"shopify_variant_id": VID}])
    assert sweep.gated_variant_id(_row(), seed, {}) == (VID, "seed_stamp")


def test_a_seed_with_no_identity_is_not_asked_about():
    """MUTANT: sweep every seed rather than the gated population.

    These are the hand-overs the gate is blind to BY DESIGN. Folding them into the denominator
    reports our own coverage as the merchants' verdict — the denominator mistake this subsystem
    has now shipped three times.
    """
    got, lane = sweep.gated_variant_id(_row(), {"snapshot": {}}, {})
    assert got is None and lane == "no_identity"


def test_a_multi_variant_product_is_not_asked_about():
    """MUTANT: pick the first stamped id on a multi-variant product.

    The hand-over is at PRODUCT grain — the buyer has not chosen — so a product with two variants
    has no single cart to build and the resolver declines. Guessing here would measure a gate that
    does not exist, and the guess is the wrong-size hazard the whole variant programme is about.
    """
    seed = _shopify_seed([{"shopify_variant_id": VID}, {"shopify_variant_id": OTHER_VID}])
    got, lane = sweep.gated_variant_id(_row(), seed, {})
    assert got is None and lane == "shopify_but_no_sole_stamp"


def test_a_storefront_we_cannot_prove_is_shopify_is_not_asked_about():
    seed = {"snapshot": {"variants": [{"shopify_variant_id": VID}]}}
    seed["snapshot"].pop("storefront_platform", None)
    got, lane = sweep.gated_variant_id(_row(), seed, {})
    assert got == VID or lane in ("no_identity", "seed_stamp")


# --- the offer handed to the gate ---------------------------------------------------------------

def test_the_offer_carries_the_seed_so_gone_can_be_concluded():
    """MUTANT: drop `source.seed_data` from the offer.

    `_check_one` refuses to conclude `gone` without storefront evidence, because a 404 from a
    storefront we cannot prove is Shopify means "we could not ask". Without it every dead handle
    answers `not_a_known_shopify_storefront` and the sweep UNDERSTATES the refusal rate — while
    still producing a full, plausible report.
    """
    seed = _shopify_seed([{"shopify_variant_id": VID}])
    offer = sweep._offer_for(_row(), VID, seed)
    assert offer["source"]["seed_data"] is seed
    assert offer["execution_spec"] == {
        "pdp_url": "https://brand.example/products/serum", "variant_id": VID}
    assert offer["offer_id"].startswith("sweep:")


# --- the guards ---------------------------------------------------------------------------------

def test_it_refuses_to_run_with_the_fence_shut(monkeypatch, capsys):
    """MUTANT: drop the fence check in main().

    THE POINT OF THIS FILE. With the fence shut every ask answers `not_yet_checked`, so the sweep
    completes, reports a 100% refusal rate, and is wrong in the one direction nobody double-checks
    — it looks like the merchants refusing everything. Exit before the first row.
    """
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    import sys as _sys
    monkeypatch.setattr(_sys, "argv", ["measure_checkout_preflight.py"])
    assert sweep.main() == 2
    assert "egress_fence_closed" in capsys.readouterr().out


def test_it_refuses_to_run_with_the_gate_off(monkeypatch, capsys):
    """A sweep in `off` mode computes verdicts and `record` drops every row — a full report and an
    empty table."""
    import sys as _sys
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", "true")
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_MODE", raising=False)
    monkeypatch.setattr(_sys, "argv", ["measure_checkout_preflight.py"])
    assert sweep.main() == 2
    assert "preflight_mode_off" in capsys.readouterr().out


# --- the arithmetic -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_rate_is_over_what_was_asked_not_over_what_was_seen(monkeypatch):
    """MUTANT: divide by `seeds_seen`.

    Half of these seeds have no identity, so the gate never applies to them. Dividing by the seeds
    walked would report 0.25 where the merchants refused 0.5 of what they were actually asked.
    """
    rows = [
        _row(id="eps_1", seed_data=_shopify_seed([{"shopify_variant_id": VID}])),
        _row(id="eps_2", seed_data=_shopify_seed([{"shopify_variant_id": OTHER_VID}])),
        _row(id="eps_3", seed_data={"snapshot": {}}),
        _row(id="eps_4", seed_data={"snapshot": {}}),
    ]
    monkeypatch.setattr(sweep, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(sweep.database, "fetch_all", _fake_fetch(rows))

    seen = []

    async def fake_preflight(offer):
        seen.append(offer["execution_spec"]["variant_id"])
        blocked = offer["execution_spec"]["variant_id"] == OTHER_VID
        return cp.PreflightVerdict(
            outcome=(cp.BLOCK if blocked else cp.OK),
            reason=(cp.R_GONE if blocked else cp.R_OK),
            would_block=blocked, mode="shadow")

    monkeypatch.setattr(cp, "preflight", fake_preflight)
    out = await sweep.run(limit=10, after=None, apply=False)
    assert out["seeds_seen"] == 4
    assert out["asked"] == 2, "only the two with identity are gated"
    assert out["would_block"] == 1
    assert out["would_block_rate"] == 0.5
    assert out["not_gated"] == {"no_identity": 2}


@pytest.mark.asyncio
async def test_a_run_of_unanswerable_asks_aborts_the_sweep(monkeypatch):
    """MUTANT: never abort.

    A live IP block presents as a run of `could_not_ask_merchant`, and continuing through it both
    wastes the window and fills the table with refusals that are about our address, not the
    merchants'.
    """
    rows = [_row(id=f"eps_{i}", seed_data=_shopify_seed([{"shopify_variant_id": VID}]))
            for i in range(30)]
    monkeypatch.setattr(sweep, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(sweep, "CONSECUTIVE_BLOCK_ABORT", 5)
    monkeypatch.setattr(sweep.database, "fetch_all", _fake_fetch(rows))

    async def always_blocked(offer):
        return cp.PreflightVerdict(
            outcome=cp.UNVERIFIABLE, reason=cp.R_UNVERIFIABLE, would_block=True, mode="shadow")

    monkeypatch.setattr(cp, "preflight", always_blocked)
    out = await sweep.run(limit=100, after=None, apply=False)
    assert out["aborted_on_block"] is True
    assert out["asked"] == 5
    assert out["next_cursor"] is None, "an aborted run has no trustworthy resume point"


@pytest.mark.asyncio
async def test_one_good_answer_clears_the_block_streak(monkeypatch):
    """A dead handle between two live ones is not a block. Counting it as one would abort a healthy
    sweep on ordinary catalog rot, which is 6.7% of a live sample."""
    rows = [_row(id=f"eps_{i}", seed_data=_shopify_seed([{"shopify_variant_id": VID}]))
            for i in range(9)]
    monkeypatch.setattr(sweep, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(sweep, "CONSECUTIVE_BLOCK_ABORT", 3)
    monkeypatch.setattr(sweep.database, "fetch_all", _fake_fetch(rows))

    calls = {"n": 0}

    async def alternating(offer):
        calls["n"] += 1
        bad = calls["n"] % 2 == 1
        return cp.PreflightVerdict(
            outcome=(cp.UNVERIFIABLE if bad else cp.OK),
            reason=(cp.R_UNVERIFIABLE if bad else cp.R_OK),
            would_block=bad, mode="shadow")

    monkeypatch.setattr(cp, "preflight", alternating)
    out = await sweep.run(limit=100, after=None, apply=False)
    assert out["aborted_on_block"] is False
    assert out["asked"] == 9


@pytest.mark.asyncio
async def test_apply_records_under_the_sweep_source_not_live(monkeypatch):
    """MUTANT: record with the default source.

    Live rows describe demand, sweep rows describe the catalog. Written as `live`, one sweep would
    swamp the number that actually justifies arming a gate on the checkout path.
    """
    rows = [_row(seed_data=_shopify_seed([{"shopify_variant_id": VID}]))]
    monkeypatch.setattr(sweep, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(sweep.database, "fetch_all", _fake_fetch(rows))

    got = {}

    async def fake_record(offer, *, source=cp.SOURCE_LIVE):
        got["source"] = source
        return cp.PreflightVerdict(outcome=cp.OK, reason=cp.R_OK, would_block=False, mode="shadow")

    monkeypatch.setattr(cp, "preflight_and_record", fake_record)
    await sweep.run(limit=10, after=None, apply=True)
    assert got["source"] == cp.SOURCE_SWEEP


@pytest.mark.asyncio
async def test_a_dry_run_records_nothing(monkeypatch):
    rows = [_row(seed_data=_shopify_seed([{"shopify_variant_id": VID}]))]
    monkeypatch.setattr(sweep, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(sweep.database, "fetch_all", _fake_fetch(rows))

    async def boom(*a, **k):
        raise AssertionError("a dry run must not write an observation")

    monkeypatch.setattr(cp, "preflight_and_record", boom)

    async def ok(offer):
        return cp.PreflightVerdict(outcome=cp.OK, reason=cp.R_OK, would_block=False, mode="shadow")

    monkeypatch.setattr(cp, "preflight", ok)
    out = await sweep.run(limit=10, after=None, apply=False)
    assert out["mode"] == "dry_run" and out["asked"] == 1


def _fake_fetch(rows):
    async def fetch_all(sql, values=None):
        if "catalog_skus" in str(sql):
            return []
        return rows
    return fetch_all
