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


# --- which hand-overs the sweep is allowed to ask about ---------------------------------------
#
# These go through the REAL resolver on purpose. Review found the first cut restating its
# admission rules and getting five of them wrong, so a test that restated them too would have
# agreed with the bug.

def test_the_population_is_the_resolvers_and_not_a_restatement():
    """MUTANT: hand-roll the admission rules again.

    `HandoverVariantResolver` canonicalises gids, vetoes a stamp the classifier calls a forgery,
    refuses a truncated id, refuses more than one live candidate, and forbids falling back to the
    seed stamp after catalog CONSIDERED and REFUSED candidates. Every one of those was wrong in
    the restated version, and each wrong one measures a hand-over the gate would never make.
    """
    import inspect

    src = inspect.getsource(sweep)
    assert "HandoverVariantResolver" in src
    for restated in ("variant_id_provenance", "sole_stamped_variant_id", "storefront_is_shopify"):
        assert restated not in src, (
            f"{restated} is an admission rule; calling it here restates the resolver")


@pytest.mark.asyncio
async def test_a_seed_the_resolver_declines_is_never_asked_about(monkeypatch):
    """MUTANT: ask anyway when the resolver returns no id.

    These are the hand-overs the gate is blind to BY DESIGN. Folding them into the denominator
    reports our own coverage as the merchants' verdict.
    """
    rows = [_row(id="eps_1"), _row(id="eps_2")]
    monkeypatch.setattr(sweep, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(sweep.database, "fetch_all", _fake_fetch(rows))
    monkeypatch.setattr(sweep, "_handover_key", lambda r, sd: PK)

    class _Declines:
        async def prime(self, keys):
            return None

        def choose(self, **kw):
            from services.handover_variant_identity import HandoverVariant

            return HandoverVariant(variant_id=None, reason="no_merchant_issued_sku")

    monkeypatch.setattr(sweep, "HandoverVariantResolver", lambda: _Declines())

    async def boom(offer):
        raise AssertionError("a declined hand-over must never reach a merchant")

    monkeypatch.setattr(cp, "preflight", boom)
    out = await sweep.run(limit=10, after=None, apply=False, run_id="t")
    assert out["gated"] == 0
    assert out["not_gated"] == {"no_merchant_issued_sku": 2}


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

def _wire(monkeypatch, rows, gated, verdict_fn):
    monkeypatch.setattr(sweep, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(sweep.database, "fetch_all", _fake_fetch(rows))
    monkeypatch.setattr(sweep, "_handover_key", lambda r, sd: PK)
    monkeypatch.setattr(sweep, "HandoverVariantResolver", _resolver_yielding(gated))
    monkeypatch.setattr(cp, "preflight", verdict_fn)


def _v(reason, outcome=None):
    return cp.PreflightVerdict(
        outcome=(outcome or (cp.OK if reason == cp.R_OK else cp.BLOCK)),
        reason=reason, would_block=(reason != cp.R_OK), mode="shadow")


@pytest.mark.asyncio
async def test_the_rate_excludes_refusals_no_merchant_was_asked_about(monkeypatch):
    """MUTANT: count every reason != R_OK as blocked, over everything gated.

    THE DEFECT THIS SUBSYSTEM KEEPS REPEATING, now four times. `NO_CONTACT_REASONS` exists because
    a rate that counts refusals decided WITHOUT contacting a merchant reads 0.99 where the
    merchants refused 0.20. The first cut of this script fixed the seeds-walked denominator and
    reintroduced the same defect one level down, under a comment claiming it was right — and the
    JSON printed here is the only thing an operator reads from a one-off job.
    """
    rows = [_row(id=f"eps_{i}", external_product_id=f"p{i}") for i in range(10)]
    gated = {f"p{i}": VID for i in range(10)}
    # 6 refused before any merchant was contacted, 1 of the remaining 4 refused BY a merchant.
    order = ([cp.R_NO_MERCHANT_VARIANT] * 4 + [cp.R_NOT_YET_CHECKED] * 2
             + [cp.R_GONE] + [cp.R_OK] * 3)
    seq = iter(order)

    async def verdicts(offer):
        return _v(next(seq))

    _wire(monkeypatch, rows, gated, verdicts)
    out = await sweep.run(limit=20, after=None, apply=False, run_id="t")
    assert out["gated"] == 10
    assert out["no_contact"] == 6
    assert out["answered"] == 4, "only four asks reached a merchant"
    assert out["would_block"] == 1
    assert out["would_block_rate"] == 0.25, "not 0.7"


@pytest.mark.asyncio
async def test_the_rate_is_none_when_no_merchant_answered(monkeypatch):
    """0/0 must not read as "the merchants refused nothing" — the state on a fully cold run."""
    rows = [_row(id=f"eps_{i}", external_product_id=f"p{i}") for i in range(3)]
    _wire(monkeypatch, rows, {f"p{i}": VID for i in range(3)},
          lambda offer: _async(_v(cp.R_NOT_YET_CHECKED, cp.UNVERIFIABLE)))
    out = await sweep.run(limit=20, after=None, apply=False, run_id="t")
    assert out["answered"] == 0 and out["would_block_rate"] is None


@pytest.mark.asyncio
async def test_one_dead_storefront_does_not_abort_the_sweep(monkeypatch):
    """MUTANT: abort on a bare consecutive count.

    Seeds are id-ordered and ids embed the brand, so one dead storefront's rows are CONTIGUOUS.
    A bare count aborts on ordinary catalog rot and reports one dead merchant as a 100% refusal
    rate — the exact shape this script exists to prevent, and the first cut computed
    `per_host_blocks` and then never read it.
    """
    rows = [_row(id=f"eps_{i}", external_product_id=f"p{i}",
                 url="https://dead.example/products/x") for i in range(20)]
    _wire(monkeypatch, rows, {f"p{i}": VID for i in range(20)},
          lambda offer: _async(_v(cp.R_UNVERIFIABLE, cp.UNVERIFIABLE)))
    monkeypatch.setattr(sweep, "CONSECUTIVE_BLOCK_ABORT", 5)
    out = await sweep.run(limit=50, after=None, apply=False, run_id="t")
    assert out["aborted_on_block"] is False, "one host is a dead shop, not our IP being blocked"
    assert out["gated"] == 20


@pytest.mark.asyncio
async def test_a_spread_of_hosts_refusing_together_does_abort(monkeypatch):
    """...and the cross-domain shape, which IS our IP, still stops the sweep."""
    rows = [_row(id=f"eps_{i}", external_product_id=f"p{i}",
                 url=f"https://shop{i}.example/products/x") for i in range(20)]
    _wire(monkeypatch, rows, {f"p{i}": VID for i in range(20)},
          lambda offer: _async(_v(cp.R_UNVERIFIABLE, cp.UNVERIFIABLE)))
    monkeypatch.setattr(sweep, "CONSECUTIVE_BLOCK_ABORT", 5)
    monkeypatch.setattr(sweep, "_ABORT_DISTINCT_HOSTS", 4)
    out = await sweep.run(limit=50, after=None, apply=False, run_id="t")
    assert out["aborted_on_block"] is True
    assert len(out["aborted_on_hosts"]) >= 4
    assert out["next_cursor"] is None, "an aborted run has no trustworthy resume point"


@pytest.mark.asyncio
async def test_apply_records_under_the_sweep_source_and_a_run_id(monkeypatch):
    """MUTANT: record with the default source, or without a run id.

    Live rows describe demand and sweep rows describe the catalog; written as `live`, one sweep
    swamps the number that justifies arming a gate. And without a run id a sweep that aborted
    part-way is indistinguishable from a good one inside the window — the rows are already
    committed by then.
    """
    rows = [_row(external_product_id="p0")]
    got = {}

    async def fake_record(offer, *, source=cp.SOURCE_LIVE, run_id=None):
        got.update(source=source, run_id=run_id)
        return _v(cp.R_OK)

    _wire(monkeypatch, rows, {"p0": VID}, lambda offer: _async(_v(cp.R_OK)))
    monkeypatch.setattr(cp, "preflight_and_record", fake_record)
    await sweep.run(limit=10, after=None, apply=True, run_id="sweep-123")
    assert got["source"] == cp.SOURCE_SWEEP
    assert got["run_id"] == "sweep-123"


@pytest.mark.asyncio
async def test_a_dry_run_records_nothing(monkeypatch):
    rows = [_row(external_product_id="p0")]

    async def boom(*a, **k):
        raise AssertionError("a dry run must not write an observation")

    _wire(monkeypatch, rows, {"p0": VID}, lambda offer: _async(_v(cp.R_OK)))
    monkeypatch.setattr(cp, "preflight_and_record", boom)
    out = await sweep.run(limit=10, after=None, apply=False, run_id="t")
    assert out["mode"] == "dry_run" and out["gated"] == 1


def _async(value):
    async def _coro():
        return value
    return _coro()


def _fake_fetch(rows):
    async def fetch_all(sql, values=None):
        if "catalog_skus" in str(sql):
            return []
        return rows
    return fetch_all


def _resolver_yielding(by_seed):
    """A stub resolver that answers per seed id, so a test can control the gated population
    without restating the admission rules it is not testing."""
    from services.handover_variant_identity import HandoverVariant

    class _Stub:
        def __init__(self):
            self.seen = []

        async def prime(self, keys):
            self.seen = list(keys)

        def choose(self, *, product_key=None, product_id=None, seed_data=None, **kw):
            vid = by_seed.get(str(product_id))
            return HandoverVariant(
                variant_id=vid, reason=("catalog" if vid else "no_merchant_issued_sku"))

    return _Stub


def test_the_observation_columns_live_in_the_model_not_only_the_migration():
    """MUTANT: declare `source`/`run_id` in db/migrations/220 alone.

    THE P0 THIS PR SHIPPED FIRST. `web` deploys with SKIP_HEAVY_STARTUP_INIT, so db/migrations/
    never runs there — `db/catalog.py`'s model plus `metadata.create_all` is what actually builds
    this table on production, which the model's own `created_at` comment already explains. A
    column added to the migration alone does not exist in prod, `record()` swallows the resulting
    UndefinedColumnError, and shadow mode stops recording with nothing but a dropped log line.
    Shadow is armed right now, so this fails closed on the one thing it is there to measure.
    """
    from db.catalog import checkout_preflight_observations as t

    cols = {c.name: c for c in t.columns}
    assert "source" in cols, "the model is what builds the prod table"
    assert "run_id" in cols
    assert cols["source"].nullable is False
    assert cols["source"].server_default is not None, (
        "an existing prod row has no source; without a default the ALTER cannot be NOT NULL")


def test_the_schema_guard_heals_a_table_that_predates_the_columns():
    """create_all does not ALTER an existing table, and prod's already exists."""
    src = open("db/schema_guard.py", encoding="utf-8").read()
    assert "checkout_preflight_observations" in src
    assert "ADD COLUMN IF NOT EXISTS source" in src
    assert "ADD COLUMN IF NOT EXISTS run_id" in src


def test_the_offer_names_the_merchant_the_way_the_route_does():
    """MUTANT: write the URL hostname into `merchant_id`.

    Live rows carry the pivota merchant id parsed from the product key. A hostname in the same
    indexed column makes the two sources unjoinable per merchant, which is most of what the
    source split is for — and it is the kind of divergence nobody notices until a per-merchant
    read silently returns nothing.
    """
    offer = sweep._offer_for(
        _row(attached_product_key="prod::m_brand::shopify::serum"), VID, {"snapshot": {}})
    assert offer["merchant_id"] == "m_brand"
    assert sweep._merchant_id_of(None) is None
    assert sweep._merchant_id_of("garbage") is None


@pytest.mark.asyncio
async def test_a_dead_host_is_dropped_from_the_streak_not_merely_tolerated(monkeypatch):
    """MUTANT: leave the dead host in the streak instead of pruning it.

    Without pruning, one dead brand's rows stay in the window and a couple of unrelated blips on
    OTHER hosts push the distinct count over the threshold — so the sweep still aborts because of
    the dead brand, just more slowly and with a misleading `aborted_on_hosts`.
    """
    # 6 rows on one dead host, then 3 single blips on three other hosts, then healthy rows.
    urls = (["https://dead.example/products/x"] * 6
            + [f"https://blip{i}.example/products/x" for i in range(3)]
            + ["https://good.example/products/x"] * 5)
    rows = [_row(id=f"eps_{i}", external_product_id=f"p{i}", url=u) for i, u in enumerate(urls)]
    blocked_upto = 9

    calls = {"n": 0}

    async def verdicts(offer):
        calls["n"] += 1
        bad = calls["n"] <= blocked_upto
        return _v(cp.R_UNVERIFIABLE if bad else cp.R_OK,
                  cp.UNVERIFIABLE if bad else cp.OK)

    _wire(monkeypatch, rows, {f"p{i}": VID for i in range(len(urls))}, verdicts)
    monkeypatch.setattr(sweep, "CONSECUTIVE_BLOCK_ABORT", 5)
    monkeypatch.setattr(sweep, "_ABORT_DISTINCT_HOSTS", 4)
    out = await sweep.run(limit=50, after=None, apply=False, run_id="t")
    assert out["aborted_on_block"] is False, (
        "one dead brand plus three unrelated blips is not a cross-domain block")
    assert out["gated"] == len(urls)


def test_it_refuses_to_sweep_out_of_the_payment_address(monkeypatch, capsys):
    """MUTANT: drop the egress-address check.

    `SUBNET` defaults to `default`, so the documented command with SUBNET forgotten sweeps the
    whole corpus out of 8.231.167.230 — the address payment partners allowlist, and the incident
    the fence exists to prevent. An earlier docstring claimed this script "refuses otherwise"
    while nothing in it looked at the address at all.
    """
    import sys as _sys

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", "true")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    monkeypatch.setattr(_sys, "argv", ["measure_checkout_preflight.py"])
    monkeypatch.setattr(sweep, "_egress_ip", lambda: _async(sweep.PAYMENT_EGRESS_IP))
    assert sweep.main() == 2
    assert "egress_leaves_by_the_payment_address" in capsys.readouterr().out


def test_it_refuses_when_it_cannot_tell_which_address_it_has(monkeypatch, capsys):
    """Unknown is not "probably fine". A sweep that cannot show it is NOT on the payment address
    must not run, or the guard is decorative."""
    import sys as _sys

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", "true")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    monkeypatch.setattr(_sys, "argv", ["measure_checkout_preflight.py"])
    monkeypatch.setattr(sweep, "_egress_ip", lambda: _async(None))
    assert sweep.main() == 2
    assert "egress_ip_unknown" in capsys.readouterr().out
