"""P1 — claim_state syndicate-after-claim gate + the unclaimed->claimed promotion."""

import asyncio

from services.claim_state import (
    CLAIM_STATE_ATTESTED,
    CLAIM_STATE_CLAIMED,
    CLAIM_STATE_SUBSTANTIATED,
    CLAIM_STATE_UNCLAIMED,
    may_publish_brand_attested,
    promote_merchant_skus_to_claimed,
)


def test_gate_blocks_unclaimed_allows_claimed_plus():
    # Observed seed: brand-attested copy is NOT publishable.
    assert may_publish_brand_attested(CLAIM_STATE_UNCLAIMED) is False
    # Claimed and beyond: publishable.
    assert may_publish_brand_attested(CLAIM_STATE_CLAIMED) is True
    assert may_publish_brand_attested(CLAIM_STATE_ATTESTED) is True
    assert may_publish_brand_attested(CLAIM_STATE_SUBSTANTIATED) is True
    # Unknown / empty -> not publishable (fail closed).
    assert may_publish_brand_attested(None) is False
    assert may_publish_brand_attested("") is False
    assert may_publish_brand_attested("garbage") is False
    assert may_publish_brand_attested("  CLAIMED ") is True  # tolerant of case/space


def test_promote_updates_only_unclaimed(monkeypatch):
    import db.database

    captured = {}

    async def _fake_execute(query, values=None):
        captured["query"] = str(query)
        captured["values"] = values
        return 3  # rows updated

    monkeypatch.setattr(db.database.database, "execute", _fake_execute)
    n = asyncio.run(promote_merchant_skus_to_claimed("m1"))
    assert n == 3
    assert "claim_state = :claimed" in captured["query"]
    assert "claim_state = :unclaimed" in captured["query"]  # never downgrades attested
    assert captured["values"]["merchant_id"] == "m1"
    assert captured["values"]["claimed"] == CLAIM_STATE_CLAIMED


def test_promote_best_effort_never_raises(monkeypatch):
    import db.database

    async def _boom(query, values=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(db.database.database, "execute", _boom)
    assert asyncio.run(promote_merchant_skus_to_claimed("m1")) == 0
    assert asyncio.run(promote_merchant_skus_to_claimed("")) == 0  # no merchant -> 0
