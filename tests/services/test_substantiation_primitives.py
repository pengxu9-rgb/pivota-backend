"""P1 substantiation slice — durable lab-evidence storage (record_lab_evidence)
+ the attested -> substantiated lifecycle primitive
(advance_product_to_substantiated). Both mock the DB boundary (the real SQL is
postgres-specific; the local test DB is sqlite)."""

import asyncio

import db.brand_attestation_evidence as bae
import db.database
import services.claim_state as cs


def test_record_lab_evidence_inserts_and_returns_id(monkeypatch):
    captured = {}

    async def _ensure():
        return None

    async def _execute(query):
        captured["query"] = query
        return 7

    monkeypatch.setattr(bae, "ensure_brand_attestation_evidence_table", _ensure)
    monkeypatch.setattr(db.database.database, "execute", _execute)

    out = asyncio.run(
        bae.record_lab_evidence(
            merchant_id="m1",
            product_key="pk",
            content_key="ck_x",
            evidence_ref="https://lab/report.pdf",
        )
    )
    assert out == 7
    assert captured["query"] is not None  # an INSERT construct was executed


def test_record_lab_evidence_missing_inputs_short_circuits(monkeypatch):
    called = {"execute": False}

    async def _execute(query):
        called["execute"] = True
        return 1

    monkeypatch.setattr(db.database.database, "execute", _execute)
    out = asyncio.run(
        bae.record_lab_evidence(
            merchant_id="m1", product_key="pk", content_key=None, evidence_ref=""
        )
    )
    assert out is None  # no evidence_ref -> no DB work
    assert called["execute"] is False


def test_advance_to_substantiated_only_from_attested(monkeypatch):
    captured = {}

    async def _execute(query, params):
        captured["params"] = params
        return 1  # one row updated

    monkeypatch.setattr(db.database.database, "execute", _execute)
    ok = asyncio.run(
        cs.advance_product_to_substantiated(merchant_id="m1", product_key="pk")
    )
    assert ok is True
    # the UPDATE guard only matches attested -> substantiated (never skips a state)
    assert captured["params"]["attested"] == cs.CLAIM_STATE_ATTESTED
    assert captured["params"]["substantiated"] == cs.CLAIM_STATE_SUBSTANTIATED


def test_advance_to_substantiated_no_matching_row_returns_false(monkeypatch):
    async def _execute(query, params):
        return 0  # nothing matched (SKU not in 'attested')

    monkeypatch.setattr(db.database.database, "execute", _execute)
    ok = asyncio.run(
        cs.advance_product_to_substantiated(merchant_id="m1", product_key="pk")
    )
    assert ok is False


def test_advance_to_substantiated_missing_inputs_returns_false():
    ok = asyncio.run(
        cs.advance_product_to_substantiated(merchant_id="", product_key="pk")
    )
    assert ok is False
