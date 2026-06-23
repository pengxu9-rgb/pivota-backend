"""ARO slice 5 — conservative entity-resolution gate on the audit seed.

EXACT deterministic match (canonical-URL / source-id) auto-aligns the seed's
content_key to the existing entity; a FUZZY (trigram/LLM) match routes to human
review WITHOUT auto-merging; any error is safe (never blocks, never mis-merges).
Flag-gated + dark by default.
"""

import asyncio

import services.audit_index_intake as aii


def test_route_audit_match():
    assert (
        aii.route_audit_match({"product_key": "p", "matcher": "source_product_id_match"})
        == "align"
    )
    assert (
        aii.route_audit_match({"product_key": "p", "matcher": "canonical_url_match"})
        == "align"
    )
    assert (
        aii.route_audit_match({"product_key": "p", "matcher": "title_brand_match"})
        == "review"
    )
    assert aii.route_audit_match({"product_key": "p", "matcher": "llm_match_v1"}) == "review"
    assert aii.route_audit_match(None) == "none"
    assert aii.route_audit_match({"matcher": "source_product_id_match"}) == "none"  # no pk


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("ENABLE_AUDIT_ER_GATE", raising=False)
    assert aii.audit_er_gate_enabled() is False
    monkeypatch.setenv("ENABLE_AUDIT_ER_GATE", "true")
    assert aii.audit_er_gate_enabled() is True


def test_seed_shape():
    seed = aii._audit_seed_for_match(
        {
            "product_key": "prod::m::url_audit::x",
            "source_product_id": "x",
            "title": "T",
            "canonical_url": "https://b/p",
            "source_domain": "b",
            "brand": "B",
        }
    )
    assert seed["title"] == "T"
    assert seed["external_product_id"] == "x"
    assert seed["canonical_url"] == "https://b/p"
    assert seed["seed_data"]["brand"] == "B"


def _fields(ck="ck_orig"):
    return {
        "product_key": "prod::m::url_audit::x",
        "content_key": ck,
        "title": "T",
        "brand": "B",
        "canonical_url": "https://b/p",
        "source_domain": "b",
        "source_product_id": "x",
    }


def test_gate_disabled_leaves_content_key(monkeypatch):
    monkeypatch.setattr(aii, "audit_er_gate_enabled", lambda: False)
    out = asyncio.run(aii.apply_audit_er_gate(_fields("ck_orig")))
    assert out["action"] == "disabled"
    assert out["content_key"] == "ck_orig"


def test_gate_exact_match_aligns(monkeypatch):
    monkeypatch.setattr(aii, "audit_er_gate_enabled", lambda: True)

    async def _resolve(f):
        return {"product_key": "prod::other", "matcher": "canonical_url_match", "confidence": 0.9}

    async def _ck(pk):
        return "ck_matched"

    monkeypatch.setattr(aii, "_resolve_audit_identity", _resolve)
    monkeypatch.setattr(aii, "_content_key_for_product_key", _ck)
    out = asyncio.run(aii.apply_audit_er_gate(_fields("ck_orig")))
    assert out["action"] == "align"
    assert out["content_key"] == "ck_matched"  # re-aligned to the matched entity
    assert out["matched_product_key"] == "prod::other"


def test_gate_fuzzy_match_routes_to_hitl_without_merge(monkeypatch):
    monkeypatch.setattr(aii, "audit_er_gate_enabled", lambda: True)
    enq = {}

    async def _resolve(f):
        return {"product_key": "prod::other", "matcher": "title_brand_match", "confidence": 0.88}

    async def _enqueue(fields, match):
        enq["candidate"] = match["product_key"]
        return "task1"

    monkeypatch.setattr(aii, "_resolve_audit_identity", _resolve)
    monkeypatch.setattr(aii, "enqueue_audit_identity_review", _enqueue)
    out = asyncio.run(aii.apply_audit_er_gate(_fields("ck_orig")))
    assert out["action"] == "review"
    assert out["content_key"] == "ck_orig"  # NOT merged — fuzzy never auto-merges
    assert enq["candidate"] == "prod::other"  # the potential merge went to HITL


def test_gate_no_match_passthrough(monkeypatch):
    monkeypatch.setattr(aii, "audit_er_gate_enabled", lambda: True)

    async def _resolve(f):
        return None

    monkeypatch.setattr(aii, "_resolve_audit_identity", _resolve)
    out = asyncio.run(aii.apply_audit_er_gate(_fields("ck_orig")))
    assert out["action"] == "none"
    assert out["content_key"] == "ck_orig"


def test_gate_resolve_error_is_safe(monkeypatch):
    monkeypatch.setattr(aii, "audit_er_gate_enabled", lambda: True)

    async def _resolve(f):
        raise RuntimeError("boom")

    monkeypatch.setattr(aii, "_resolve_audit_identity", _resolve)
    out = asyncio.run(aii.apply_audit_er_gate(_fields("ck_orig")))
    assert out["action"] == "error"
    assert out["content_key"] == "ck_orig"  # never blocks, never mis-merges on error
