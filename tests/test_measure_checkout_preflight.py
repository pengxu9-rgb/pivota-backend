"""The measurement lane. Decision logic only — the SQL is exercised by the Postgres gate file.

The failure this file is built against is not a crash. It is a sweep that runs to completion,
reports a clean 100% refusal rate, and is read as a fact about merchants when it is a fact about
our own fence being shut. So the guards get as much attention as the arithmetic.
"""

from __future__ import annotations

import json
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


# The schema_guard heal is asserted by EXECUTING it, in
# tests/test_checkout_preflight_postgres.py::test_the_schema_guard_adds_the_columns_to_an_old_table.
# A grep for "ADD COLUMN IF NOT EXISTS run_id" is a substring ratchet that `run_idx` satisfies,
# and the thing being claimed is that a real table gains real columns.


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
async def test_a_dead_prefix_does_not_mask_a_real_block(monkeypatch):
    """MUTANT: prune `block_streak[-1]` instead of using a bounded window.

    THE ROUND-2 P1, and it was worse than having no distinction at all. `[-1]` is whatever host
    just ARRIVED, so once a dead brand's rows filled the streak they stayed forever and every
    genuinely new blocked host was the one thrown away. Review reproduced 40 distinct blocked
    hosts after a 9-row dead prefix with `aborted_on_block: false`, a clean `would_block_rate` of
    1.0, exit 0 and a resume cursor — while still firing one request per row into a live 429.

    A sliding window needs no pruning rule: the dead brand ages out as new hosts arrive.
    """
    urls = (["https://dead.example/products/x"] * 9
            + [f"https://shop{i}.example/products/x" for i in range(40)])
    rows = [_row(id=f"eps_{i:03d}", external_product_id=f"p{i}", url=u)
            for i, u in enumerate(urls)]
    _wire(monkeypatch, rows, {f"p{i}": VID for i in range(len(urls))},
          lambda offer: _async(_v(cp.R_UNVERIFIABLE, cp.UNVERIFIABLE)))
    monkeypatch.setattr(sweep, "CONSECUTIVE_BLOCK_ABORT", 10)
    monkeypatch.setattr(sweep, "_ABORT_DISTINCT_HOSTS", 4)
    out = await sweep.run(limit=100, after=None, apply=False, run_id="t")
    assert out["aborted_on_block"] is True, (
        "a dead prefix must not make the sweep blind to a cross-domain block behind it")
    assert out["gated"] < len(urls), "and it must stop rather than walk the whole page"


@pytest.mark.asyncio
async def test_one_good_answer_clears_the_window(monkeypatch):
    """MUTANT: never clear the window on a good answer.

    A dead handle between two live ones is ordinary catalog rot — 6.7% of a live sample — not
    evidence about our address. Without the clear, scattered rot accumulates distinct hosts across
    an entire healthy sweep and eventually trips the abort.
    """
    urls = [f"https://shop{i}.example/products/x" for i in range(40)]
    rows = [_row(id=f"eps_{i:03d}", external_product_id=f"p{i}", url=u)
            for i, u in enumerate(urls)]
    calls = {"n": 0}

    async def alternating(offer):
        calls["n"] += 1
        bad = calls["n"] % 2 == 1
        return _v(cp.R_UNVERIFIABLE if bad else cp.R_OK,
                  cp.UNVERIFIABLE if bad else cp.OK)

    _wire(monkeypatch, rows, {f"p{i}": VID for i in range(len(urls))}, alternating)
    monkeypatch.setattr(sweep, "CONSECUTIVE_BLOCK_ABORT", 5)
    monkeypatch.setattr(sweep, "_ABORT_DISTINCT_HOSTS", 3)
    out = await sweep.run(limit=100, after=None, apply=False, run_id="t")
    assert out["aborted_on_block"] is False
    assert out["gated"] == len(urls), "every row on a healthy sweep must still be asked"


@pytest.mark.asyncio
async def test_the_egress_probe_returns_an_address_or_nothing(monkeypatch):
    """MUTANT: return the response body unchecked.

    An HTML error page from the echo service is not equal to the payment address, so unchecked it
    counted as proof it was safe to sweep. That string is the only thing between "SUBNET
    forgotten" and a corpus crawl out of the payment IP, so anything unparseable must read as
    unknown — and unknown refuses.
    """
    import httpx

    class _Resp:
        def __init__(self, text, status=200):
            self.text = text
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)

    def _client_returning(resp):
        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return resp

        return lambda **kw: _C()

    monkeypatch.setattr(httpx, "AsyncClient", _client_returning(_Resp("34.82.199.35")))
    assert await sweep._egress_ip() == "34.82.199.35"

    for junk in ("<html><body>503 Service Unavailable</body></html>", "", "not-an-ip", "1.2.3"):
        monkeypatch.setattr(httpx, "AsyncClient", _client_returning(_Resp(junk)))
        assert await sweep._egress_ip() is None, f"{junk!r} is not an address"

    monkeypatch.setattr(httpx, "AsyncClient", _client_returning(_Resp("34.82.199.35", 503)))
    assert await sweep._egress_ip() is None, "a 503 body is not an address either"


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


def test_the_population_is_resolved_per_variant_like_the_route():
    """MUTANT: resolve once per row with `offer_variant_id=None`.

    Review showed that answers differently in BOTH directions against the real resolver: a product
    with two live merchant-issued SKUs is refused as `multiple_merchant_issued_skus` while the
    route names each by exact match, and a product with one candidate whose seed names a DIFFERENT
    id is refused by the route as a contradiction while the once-per-row call returned the
    candidate. Under-measuring one cohort while over-measuring another is the population drift
    this lane exists to avoid.
    """
    import inspect

    src = inspect.getsource(sweep.run)
    assert "_route_variants(seed_data)" in src, "the route walks variants; so must this"
    assert "offer_variant_id=_seed_offer_variant_id(v)" in src
    assert "named_variant_id=_seed_variant_identity_claim(v)" in src


def test_the_cart_lane_is_measured_too():
    """MUTANT: measure only the resolved half of `_gate_vid = _handover_id or _cart_vid`.

    The attach branch ships `_catalog_vid or _operator_vid` for a seed labelled shopify and the
    route gates that cart even with no `catalog_skus` row — 67.1% of the corpus has none, and the
    route's own comment calls this cohort the only carts that exist today. Measuring the resolved
    half alone reports a refusal rate for the cohort that is NOT shipping carts and says nothing
    about the one that is.
    """
    shopify = {"snapshot": {"storefront_platform": "shopify"}}
    assert sweep._cart_lane_variant_id({"attached_variant_id": VID}, shopify) == VID
    # ...and only where the route would: a seed it does not call shopify ships no permalink.
    assert sweep._cart_lane_variant_id({"attached_variant_id": VID}, {"snapshot": {}}) is None
    assert sweep._cart_lane_variant_id({"attached_variant_id": None}, shopify) is None


def test_a_recorded_row_names_the_key_the_resolver_used():
    """MUTANT: read `attached_product_key` off the row instead.

    `_handover_product_key` falls back to the seed document, so the column is empty for
    hand-overs the resolver keyed perfectly well — and those rows were being written with a null
    product key, unjoinable to anything.
    """
    row = _row(attached_product_key=None)
    offer = sweep._offer_for(row, VID, {"snapshot": {}}, product_key="prod::m_b::shopify::x")
    assert offer["product_key"] == "prod::m_b::shopify::x"
    assert offer["merchant_id"] == "m_b"


def test_a_malformed_product_key_yields_no_merchant_rather_than_a_wrong_one():
    """A wrong value in this column is worse than a null one: live rows fill it with a merchant
    id, so anything else makes the two sources unjoinable per merchant."""
    for bad in (None, "", "garbage", "a::b", "notprod::m::shopify::x"):
        assert sweep._merchant_id_of(bad) is None, bad
    assert sweep._merchant_id_of("prod::m_brand::shopify::serum") == "m_brand"


@pytest.mark.asyncio
async def test_the_cart_lane_reaches_the_merchant_through_run(monkeypatch):
    """MUTANT: never consult the cart lane inside `run`.

    Testing `_cart_lane_variant_id` alone passes for a `run` that never calls it — the helper and
    its use are two different claims, and the second is the one that decides what gets measured.
    """
    row = _row(external_product_id="p0", attached_product_key=None,
               attached_variant_id=VID,
               seed_data={"snapshot": {"storefront_platform": "shopify"}})
    asked = []

    async def verdicts(offer):
        asked.append(offer["execution_spec"]["variant_id"])
        return _v(cp.R_OK)

    # The resolver declines: this is the 67.1% with no catalog row, which is the whole cohort.
    _wire(monkeypatch, [row], {}, verdicts)
    out = await sweep.run(limit=10, after=None, apply=False, run_id="t")
    assert asked == [VID], "a seed the resolver declines but the route ships a cart for"
    assert out["by_lane"] == {"cart_operator_id": 1}


@pytest.mark.asyncio
async def test_the_pacing_floor_is_honoured(monkeypatch):
    """MUTANT: delete the pacing sleep.

    One ask is up to three outbound requests, against a measured cross-domain threshold of about
    50 a minute. Unpaced, a sweep is the incident.
    """
    rows = [_row(id=f"eps_{i}", external_product_id=f"p{i}") for i in range(3)]
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    _wire(monkeypatch, rows, {f"p{i}": VID for i in range(3)},
          lambda offer: _async(_v(cp.R_OK)))
    monkeypatch.setattr(sweep, "GLOBAL_MIN_INTERVAL_S", 4.0)
    monkeypatch.setattr(sweep.asyncio, "sleep", fake_sleep)
    await sweep.run(limit=10, after=None, apply=False, run_id="t")
    assert len([s for s in slept if s > 0]) >= 2, f"asks were not paced: {slept}"


@pytest.mark.asyncio
async def test_a_seed_document_that_arrives_as_text_is_still_read(monkeypatch):
    """MUTANT: treat a JSON string as unreadable.

    `databases`+asyncpg hands JSONB back as a dict OR a string depending on the codec. Treated as
    unreadable, every such row is skipped and the sweep reports a clean run over a fraction of the
    corpus — the failure this whole lane is built against, and one this repo has hit before.
    """
    row = _row(external_product_id="p0",
               seed_data=json.dumps({"snapshot": {"storefront_platform": "shopify"}}))
    asked = []

    async def verdicts(offer):
        asked.append(1)
        return _v(cp.R_OK)

    _wire(monkeypatch, [row], {"p0": VID}, verdicts)
    out = await sweep.run(limit=10, after=None, apply=False, run_id="t")
    assert asked, f"a text seed_data was skipped: {out['not_gated']}"
    assert out["gated"] == 1


@pytest.mark.asyncio
async def test_the_cursor_starts_past_the_row_it_names(monkeypatch):
    """MUTANT: `>=` instead of `>`.

    The resume cursor is the LAST row of the previous page, so `>=` re-asks it forever — a sweep
    that never advances while reporting a full page of work each time, and paying a merchant
    request for every repeat.
    """
    seen = {}

    async def fetch_all(sql, values=None):
        if "catalog_skus" in str(sql):
            return []
        seen["sql"] = str(sql)
        seen["values"] = dict(values or {})
        return []

    monkeypatch.setattr(sweep.database, "fetch_all", fetch_all)
    await sweep.run(limit=10, after="eps_5", apply=False, run_id="t")
    assert "e.id > :after" in seen["sql"], seen["sql"]
    assert "e.id >= :after" not in seen["sql"]


def test_an_aborted_run_exits_non_zero(monkeypatch, capsys):
    """MUTANT: always exit 0.

    A wrapper chaining pages reads the exit code. Zero on an abort means the next page starts from
    a cursor the aborted run never reached, and the block is walked straight back into.
    """
    import sys as _sys

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", "true")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    monkeypatch.setattr(_sys, "argv", ["measure_checkout_preflight.py"])
    monkeypatch.setattr(sweep, "_egress_ip", lambda: _async("34.82.199.35"))

    async def aborted(**kw):
        return {"aborted_on_block": True, "gated": 3}

    monkeypatch.setattr(sweep, "run", aborted)

    async def noop():
        return None

    monkeypatch.setattr(sweep.database, "connect", noop)
    monkeypatch.setattr(sweep.database, "disconnect", noop)
    assert sweep.main() == 1
    assert "aborted_on_block" in capsys.readouterr().out
