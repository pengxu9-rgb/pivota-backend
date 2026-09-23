"""Decision logic of the partner adjustment adapter, on a fake DB. SQL is proven on Postgres in
tests/test_partner_order_adjustments_postgres.py."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from services import partner_order_adjustments as adj

T0 = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def _edge(**over):
    base = {"edge_id": "cae_ext_1", "order_id": "ext_1", "merchant_id": "brand.example",
            "agent_id": "agent_minds", "currency": "USD", "created_at": T0,
            "gross_attributed_gmv_cents": 4500, "refund_amount_cents": 0, "refund_ids": []}
    base.update(over)
    return base


class FakeDB:
    def __init__(self, edges):
        self.edges = edges
        self.executed = []
        self.in_txn = False
        self.committed = 0
        self.rolled_back = 0

    @asynccontextmanager
    async def transaction(self):
        self.in_txn = True
        try:
            yield
        except BaseException:
            self.rolled_back += 1
            raise
        else:
            self.committed += 1
        finally:
            self.in_txn = False

    async def fetch_all(self, sql, params=None):
        assert "FOR UPDATE" in sql and self.in_txn
        return list(self.edges)

    async def execute(self, sql, params=None):
        assert self.in_txn, "the metadata append must be inside the locked transaction"
        self.executed.append((sql, params))


@pytest.fixture
def world(monkeypatch):
    state = {"applied": [], "events": [], "recomputed": [], "apply_rows": None, "recompute_raises": False}

    async def fake_apply(order_id, refund_id, amount):
        state["applied"].append((order_id, refund_id, amount))
        return state["apply_rows"] if state["apply_rows"] is not None else [{"edge_id": "cae_ext_1"}]

    async def fake_emit(rows, order_id, refund_id, amount):
        state["events"].append((order_id, refund_id))

    async def fake_recompute(edge):
        state["recomputed"].append(edge["edge_id"])
        return not state["recompute_raises"]

    monkeypatch.setattr(adj, "_apply_refund", fake_apply)
    monkeypatch.setattr(adj, "_emit_refund_event", fake_emit)
    monkeypatch.setattr(adj, "_recompute_rollup", fake_recompute)
    return state


def _use(monkeypatch, *edges):
    db = FakeDB(list(edges))
    monkeypatch.setattr(adj, "database", db)
    return db


def _call(**over):
    kw = dict(partner="reap", purchase_id="rp_abc", event_id="evt_1", kind="refund",
              currency="USD", amount_minor=1500, occurred_at=T0)
    kw.update(over)
    return adj.record_partner_order_adjustment(**kw)


async def test_a_partial_refund_is_applied_recorded_and_rerolled(monkeypatch, world):
    db = _use(monkeypatch, _edge())
    r = await _call()
    assert (r.status, r.applied_minor, r.edge_id, r.agent_id) == ("applied", 1500, "cae_ext_1", "agent_minds")
    assert world["applied"] == [("ext_1", "reap:evt_1", adj.Decimal("15"))]
    assert db.committed == 1
    (sql, params), = db.executed
    assert "partner_adjustments" in sql
    recorded = json.loads(params["adjustment"])
    assert recorded == {**recorded, "partner": "reap", "event_id": "evt_1", "kind": "refund",
                        "amount_minor": 1500, "currency": "USD", "occurred_at": T0.isoformat()}
    # The event and the recompute happen AFTER commit, once.
    assert world["events"] == [("ext_1", "reap:evt_1")]
    assert world["recomputed"] == ["cae_ext_1"] and r.rollup_recomputed is True


async def test_a_cancellation_reverses_whatever_is_left(monkeypatch, world):
    _use(monkeypatch, _edge(refund_amount_cents=1000))
    r = await _call(kind="cancellation", amount_minor=None)
    assert (r.status, r.applied_minor, r.refunded_before_minor) == ("applied", 3500, 1000)
    assert world["applied"][0][2] == adj.Decimal("35")


async def test_a_redelivered_event_changes_nothing(monkeypatch, world):
    db = _use(monkeypatch, _edge(refund_ids=["reap:evt_1"], refund_amount_cents=1500))
    r = await _call()
    assert r.status == "replayed"
    assert world["applied"] == [] and db.executed == [] and world["recomputed"] == []


async def test_refund_ids_stored_as_json_text_still_dedupe(monkeypatch, world):
    _use(monkeypatch, _edge(refund_ids=json.dumps(["reap:evt_1"])))
    assert (await _call()).status == "replayed"


async def test_no_edge_yet_is_a_result_not_an_error(monkeypatch, world):
    _use(monkeypatch)
    r = await _call()
    assert r.status == "no_edge" and world["applied"] == []


async def test_more_than_is_left_is_refused_and_nothing_is_written(monkeypatch, world):
    db = _use(monkeypatch, _edge(refund_amount_cents=4000))
    with pytest.raises(adj.AdjustmentRefused) as e:
        await _call(amount_minor=501)
    assert e.value.code == "exceeds_remaining"
    assert world["applied"] == [] and db.executed == [] and db.rolled_back == 1


async def test_exactly_what_is_left_is_allowed(monkeypatch, world):
    _use(monkeypatch, _edge(refund_amount_cents=4000))
    assert (await _call(amount_minor=500)).status == "applied"


async def test_a_fully_refunded_edge_has_nothing_remaining(monkeypatch, world):
    _use(monkeypatch, _edge(refund_amount_cents=4500))
    r = await _call(kind="cancellation", amount_minor=None)
    assert r.status == "nothing_remaining" and world["applied"] == []


async def test_a_different_currency_is_refused(monkeypatch, world):
    _use(monkeypatch, _edge(currency="SGD"))
    with pytest.raises(adj.AdjustmentRefused) as e:
        await _call(currency="USD")
    assert e.value.code == "currency_mismatch"


@pytest.mark.parametrize("cur", ["JPY", "KWD"])
async def test_a_currency_whose_minor_unit_is_not_a_cent_is_refused(monkeypatch, world, cur):
    # The shared refund SQL stores major x 100: a JPY or KWD amount would be recorded wrong.
    _use(monkeypatch, _edge(currency=cur))
    with pytest.raises(adj.AdjustmentRefused) as e:
        await _call(currency=cur)
    assert e.value.code == "unsupported_currency"


async def test_two_partner_edges_for_one_purchase_are_refused(monkeypatch, world):
    _use(monkeypatch, _edge(), _edge(edge_id="cae_ext_2", order_id="ext_2"))
    with pytest.raises(adj.AdjustmentRefused) as e:
        await _call()
    assert e.value.code == "ambiguous_edge"


async def test_dry_run_checks_everything_and_writes_nothing(monkeypatch, world):
    db = _use(monkeypatch, _edge())
    r = await _call(apply=False)
    assert (r.status, r.applied_minor) == ("would_apply", 1500)
    assert world["applied"] == [] and db.executed == [] and world["recomputed"] == []


async def test_the_refund_must_land_on_exactly_the_locked_edge(monkeypatch, world):
    db = _use(monkeypatch, _edge())
    world["apply_rows"] = []
    with pytest.raises(RuntimeError):
        await _call()
    assert db.rolled_back == 1 and db.executed == []


async def test_a_failed_recompute_is_reported_not_raised(monkeypatch, world):
    _use(monkeypatch, _edge())
    world["recompute_raises"] = True
    r = await _call()
    assert r.status == "applied" and r.rollup_recomputed is False


@pytest.mark.parametrize(
    "over, code",
    [
        (dict(partner="Reap"), "invalid_partner"),
        (dict(partner=""), "invalid_partner"),
        (dict(purchase_id=" "), "invalid_purchase_id"),
        (dict(event_id="a b"), "invalid_event_id"),
        (dict(event_id="e" * 65), "invalid_event_id"),
        (dict(event_id="e" * 60), "event_id_too_long"),
        (dict(kind="chargeback"), "invalid_kind"),
        (dict(currency="usdx"), "invalid_currency"),
        (dict(amount_minor=0), "invalid_amount"),
        (dict(amount_minor=-5), "invalid_amount"),
        (dict(amount_minor=True), "invalid_amount"),
        (dict(amount_minor=12.5), "invalid_amount"),
        (dict(amount_minor=None), "amount_required"),
        (dict(kind="chargeback_lost", amount_minor=None), "amount_required"),
        (dict(occurred_at=datetime(2026, 9, 20)), "invalid_occurred_at"),
    ],
)
async def test_bad_input_is_refused_before_the_database_is_touched(monkeypatch, world, over, code):
    db = _use(monkeypatch, _edge())
    with pytest.raises(adj.AdjustmentRefused) as e:
        await _call(**over)
    assert e.value.code == code
    assert db.committed == 0 and db.rolled_back == 0


async def test_currency_is_normalised_before_the_comparison(monkeypatch, world):
    _use(monkeypatch, _edge(currency="usd"))
    assert (await _call(currency=" usd ")).status == "applied"


def test_the_ops_script_is_a_dry_run_unless_told_otherwise():
    from scripts import record_partner_order_adjustment as script

    args = script.build_parser().parse_args(
        ["--partner", "reap", "--purchase-id", "rp_1", "--event-id", "e1", "--kind", "refund",
         "--currency", "USD", "--amount-minor", "100"])
    assert args.apply is False


def test_the_ops_script_refuses_a_naive_timestamp():
    from scripts import record_partner_order_adjustment as script

    with pytest.raises(SystemExit):
        script.build_parser().parse_args(
            ["--partner", "reap", "--purchase-id", "rp_1", "--event-id", "e1", "--kind", "refund",
             "--currency", "USD", "--amount-minor", "100", "--occurred-at", "2026-10-01T09:00:00"])
