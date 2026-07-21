"""
Smoke tests for scripts/backfill_pivota_canonical_pdp.py — focused on
the pure logic (chunking, identity validation, sig shape) since the
script's DB layer is exercised end-to-end via railway run.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

import pytest


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeDB:
    """Minimal async DB stand-in. Records every SQL execute so the
    test can assert exactly which rows were UPDATEd."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = list(rows)
        self.executed: List[Dict[str, Any]] = []
        self._connected = False

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self._connected = False

    def transaction(self):
        return _FakeTxn()

    async def fetch_one(self, query: str, values: Dict[str, Any] = None):
        # The COUNT(*) preflight.
        return {"n": len([r for r in self._rows if r.get("pivota_signature_id") is None])}

    async def fetch_all(self, query: str, values: Dict[str, Any] = None):
        # Only return rows with NULL sig (matching the WHERE clause).
        out = [
            {
                "product_key": r["product_key"],
                "merchant_id": r["merchant_id"],
                "platform": r["platform"],
                "source_product_id": r["source_product_id"],
            }
            for r in self._rows
            if r.get("pivota_signature_id") is None
        ]
        # Stable order for assertions.
        out.sort(key=lambda r: r["product_key"])
        return out[: int((values or {}).get("chunk_size", 100))]

    async def execute(self, query: str, values: Dict[str, Any] = None):
        # Simulate the UPDATE flipping NULL → sig.
        self.executed.append({"query": query, "values": values or {}})
        if values and "product_key" in values and "sig" in values:
            for r in self._rows:
                if r["product_key"] == values["product_key"] and r.get("pivota_signature_id") is None:
                    r["pivota_signature_id"] = values["sig"]
                    r["pivota_canonical_url"] = values["url"]


def _row(suffix: str, **overrides) -> Dict[str, Any]:
    base = {
        "product_key": f"prod::merch_a::shopify::{suffix}",
        "merchant_id": "merch_a",
        "platform": "shopify",
        "source_product_id": suffix,
        "pivota_signature_id": None,
        "pivota_canonical_url": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def fake_db_module(monkeypatch: pytest.MonkeyPatch):
    rows = [
        _row("p1"),
        _row("p2"),
        _row("p3"),
        _row("already_has_sig", pivota_signature_id="sig_existing", pivota_canonical_url="https://x"),
        # Row with empty source_product_id — must be skipped.
        _row("", source_product_id=""),
    ]
    fake_db = _FakeDB(rows)

    import scripts.backfill_pivota_canonical_pdp as mod
    monkeypatch.setattr(mod, "database", fake_db)
    return mod, fake_db, rows


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write(fake_db_module):
    mod, fake_db, _ = fake_db_module
    args = argparse.Namespace(
        apply=False, merchant_id=None, platform=None, chunk_size=10, max_rows=0
    )
    code = await mod._run(args)
    assert code == 0
    # No UPDATE statements emitted in dry-run.
    update_stmts = [e for e in fake_db.executed if "UPDATE" in e["query"]]
    assert update_stmts == []


@pytest.mark.asyncio
async def test_backfill_apply_writes_sigs_and_skips_invalid(fake_db_module):
    mod, fake_db, rows = fake_db_module
    args = argparse.Namespace(
        apply=True, merchant_id=None, platform=None, chunk_size=10, max_rows=0
    )
    code = await mod._run(args)
    assert code == 0

    # Three valid NULL-sig rows got UPDATEd; the empty-identity row was skipped.
    update_stmts = [e for e in fake_db.executed if "UPDATE" in e["query"]]
    assert len(update_stmts) == 3
    updated_keys = {e["values"]["product_key"] for e in update_stmts}
    assert updated_keys == {
        "prod::merch_a::shopify::p1",
        "prod::merch_a::shopify::p2",
        "prod::merch_a::shopify::p3",
    }
    # Each persisted sig has the sig_<32hex> shape (contract guard).
    for e in update_stmts:
        sig = e["values"]["sig"]
        assert sig.startswith("sig_")
        assert len(sig) == len("sig_") + 32
        assert e["values"]["url"].endswith(sig)


@pytest.mark.asyncio
async def test_backfill_apply_is_idempotent(fake_db_module):
    """Second --apply pass must be a no-op — every row already has a sig."""
    mod, fake_db, _ = fake_db_module
    args = argparse.Namespace(
        apply=True, merchant_id=None, platform=None, chunk_size=10, max_rows=0
    )
    await mod._run(args)
    fake_db.executed.clear()

    code = await mod._run(args)
    assert code == 0
    update_stmts = [e for e in fake_db.executed if "UPDATE" in e["query"]]
    assert update_stmts == []
