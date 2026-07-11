"""enqueue_deposit_audit pre-flight logic: product-key resolution, trust
coverage read, and the depositability threshold that gates whether the run is
worth enqueueing. DB access is faked; no network / no real run is enqueued here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.enqueue_deposit_audit as enq  # noqa: E402


class _FakeDB:
    def __init__(self, fetch_all_result=None):
        self.calls = []
        self._fetch_all_result = fetch_all_result or []

    async def fetch_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return self._fetch_all_result


# ---- _depositable: mirrors the deposit gate (identity_confidence >= threshold) ----

def test_depositable_true_at_or_above_threshold():
    assert enq._depositable({"identity_confidence": 0.85}, 0.85) is True
    assert enq._depositable({"identity_confidence": 0.92}, 0.85) is True


def test_depositable_false_below_threshold_or_null_or_missing():
    assert enq._depositable({"identity_confidence": 0.84}, 0.85) is False
    assert enq._depositable({"identity_confidence": None}, 0.85) is False
    assert enq._depositable(None, 0.85) is False
    assert enq._depositable({"identity_confidence": "junk"}, 0.85) is False


# ---- _resolve_product_keys: explicit list wins + dedupes; else queries catalog ----

async def test_resolve_keys_explicit_dedupes_keeps_order(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(enq, "database", db)
    keys = await enq._resolve_product_keys("merch_obs_x", ["a", "b", "a", "c"])
    assert keys == ["a", "b", "c"]
    assert db.calls == []  # explicit list => no DB query


async def test_resolve_keys_falls_back_to_merchant_catalog(monkeypatch):
    db = _FakeDB(fetch_all_result=[{"product_key": "pk1"}, {"product_key": "pk2"}])
    monkeypatch.setattr(enq, "database", db)
    keys = await enq._resolve_product_keys("merch_obs_x", [])
    assert keys == ["pk1", "pk2"]
    sql, params = db.calls[0]
    assert "catalog_products" in sql and "suppression_reason IS NULL" in sql
    assert params["mid"] == "merch_obs_x"


# ---- _trust_preflight: builds {product_key: {status, confidence}} ----

async def test_trust_preflight_maps_rows(monkeypatch):
    db = _FakeDB(fetch_all_result=[
        {"product_key": "pk1", "identity_confidence": 0.9, "identity_status": "approved"},
    ])
    monkeypatch.setattr(enq, "database", db)
    out = await enq._trust_preflight(["pk1", "pk2"])
    assert out["pk1"]["identity_confidence"] == 0.9
    assert out["pk1"]["identity_status"] == "approved"
    assert "pk2" not in out  # no trust row => absent => not depositable


async def test_trust_preflight_empty_keys_short_circuits(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(enq, "database", db)
    assert await enq._trust_preflight([]) == {}
    assert db.calls == []


# ---- end-to-end pre-flight verdict over a mixed cohort ----

def test_mixed_cohort_depositable_count():
    threshold = 0.85
    trust = {
        "pk_ok": {"identity_confidence": 0.90, "identity_status": "approved"},
        "pk_low": {"identity_confidence": 0.50, "identity_status": "review_required"},
        # pk_missing absent entirely
    }
    keys = ["pk_ok", "pk_low", "pk_missing"]
    depositable = [k for k in keys if enq._depositable(trust.get(k), threshold)]
    assert depositable == ["pk_ok"]


# ---- _drive: the load-bearing enqueue contract (launch payload + refusal) ----

import argparse  # noqa: E402


class _DriveDB:
    """Fake app DB for _drive: connect/disconnect + the two fetch_all queries
    (catalog_products key resolution, catalog_row_trust preflight)."""

    def __init__(self, keys, conf_by_key):
        self._keys = keys
        self._conf = conf_by_key

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def fetch_all(self, sql, params=None):
        if "catalog_row_trust" in sql:
            return [
                {"product_key": k, "identity_confidence": c, "identity_status": "approved"}
                for k, c in self._conf.items()
            ]
        if "catalog_products" in sql:
            return [{"product_key": k} for k in self._keys]
        return []


def _drive_args(**over):
    base = dict(
        merchant_id="merch_obs_x", product_key=None,
        prompts_per_sku=12, apply=False, force=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


async def test_drive_apply_enqueues_per_sku_non_synthetic_no_debits(monkeypatch):
    monkeypatch.setattr(enq, "database", _DriveDB(["pk1"], {"pk1": 0.92}))
    captured = {}

    async def fake_enqueue(*, merchant_id, product_keys, subject_type, request_options_jsonb):
        captured.update(
            merchant_id=merchant_id, product_keys=product_keys,
            subject_type=subject_type, request_options_jsonb=request_options_jsonb,
        )
        return "run_123"

    monkeypatch.setattr("db.merchant_audit_runs.enqueue_audit_run", fake_enqueue)
    await enq._drive(_drive_args(apply=True))

    assert captured["merchant_id"] == "merch_obs_x"
    assert captured["product_keys"] == ["pk1"]
    assert captured["subject_type"] == "merchant"
    launch = captured["request_options_jsonb"]["launch"]
    # per-SKU mode (builds authority_map.skus -> citations deposit)...
    assert launch["audit_mode"] == "per_sku"
    # ...NOT synthetic (else is_synthetic short-circuits persist_canonical_evidence)...
    assert "synthetic_products" not in launch
    # ...and no debit keys (observed seller has no wallet).
    assert "debited" not in launch
    assert not any(k.startswith("estimated_") for k in launch)


async def test_drive_refuses_when_zero_depositable(monkeypatch):
    monkeypatch.setattr(enq, "database", _DriveDB(["pk1"], {"pk1": 0.50}))  # < 0.85

    async def must_not_enqueue(**_):
        raise AssertionError("must not enqueue when 0 depositable")

    monkeypatch.setattr("db.merchant_audit_runs.enqueue_audit_run", must_not_enqueue)
    with pytest.raises(SystemExit):
        await enq._drive(_drive_args(apply=True))


async def test_drive_force_enqueues_despite_zero_depositable(monkeypatch):
    monkeypatch.setattr(enq, "database", _DriveDB(["pk1"], {"pk1": 0.50}))
    called = {}

    async def fake_enqueue(**_):
        called["enqueued"] = True
        return "run_x"

    monkeypatch.setattr("db.merchant_audit_runs.enqueue_audit_run", fake_enqueue)
    await enq._drive(_drive_args(apply=True, force=True))
    assert called.get("enqueued") is True
