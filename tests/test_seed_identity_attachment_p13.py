"""Convergence P1.3 — flag-gated deterministic-EXACT seed identity attachment.

The attach service is DARK by default (co-gated with the Phase-2 pivot cutover;
enabling it before the pivot serves canonical rows drops products from the live
seed lane). Covers:
  - flag OFF → no-op (never attaches), even with an exact candidate present;
  - force=True (operator backfill) bypasses the flag;
  - ONLY exact matchers (source_product_id / canonical_url) auto-attach — a
    fuzzy title-trigram match must NOT (leaves the seed unattached for review);
  - idempotent (already-attached seed skipped);
  - the best-effort write-time hook never raises and is a no-op when dark.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import services.seed_identity_attachment as sia  # noqa: E402


def _seed(**over: Any) -> Dict[str, Any]:
    base = {
        "id": "seed_1",
        "external_product_id": "SKU-12345",
        "canonical_url": "https://brand.example/products/x",
        "destination_url": "https://brand.example/products/x",
        "title": "Widget",
        "seed_data": {"brand": "BrandX"},
        "attached_product_key": None,
    }
    base.update(over)
    return base


@pytest.fixture()
def _wired(monkeypatch: pytest.MonkeyPatch):
    """Stub the matcher's candidate fetch + apply so no DB is needed; capture
    what would be written."""
    state: Dict[str, Any] = {"candidates": [], "match": None, "applied": []}

    async def fake_candidates(seed):
        return state["candidates"]

    def fake_matches(*, seed, candidates):
        return state["match"]

    async def fake_apply(*, seed_id, product_key, matcher, confidence, evidence, dry_run):
        state["applied"].append({
            "seed_id": seed_id, "product_key": product_key,
            "matcher": matcher, "dry_run": dry_run,
        })

    # matches_for_seed is imported into the module namespace
    monkeypatch.setattr(sia, "matches_for_seed", fake_matches)
    # runner primitives are imported lazily inside the function
    import services.pdp_matcher.runner as runner
    monkeypatch.setattr(runner, "_candidates_for_seed", fake_candidates)
    monkeypatch.setattr(runner, "_apply_attachment", fake_apply)
    return state


def _exact_match() -> Dict[str, Any]:
    return {"product_key": "pk_a", "matcher": "source_product_id_match", "confidence": 0.95}


def _fuzzy_match() -> Dict[str, Any]:
    return {"product_key": "pk_b", "matcher": "title_brand_match", "confidence": 0.9}


@pytest.mark.asyncio
async def test_flag_off_is_noop_even_with_exact_match(monkeypatch, _wired):
    monkeypatch.delenv("SEED_IDENTITY_ATTACHMENT_ENABLED", raising=False)
    _wired["candidates"] = [{"product_key": "pk_a"}]
    _wired["match"] = _exact_match()

    result = await sia.attach_seed_identity_exact(_seed())
    assert result is None
    assert _wired["applied"] == []  # DARK: nothing written


@pytest.mark.asyncio
async def test_flag_on_attaches_exact(monkeypatch, _wired):
    monkeypatch.setenv("SEED_IDENTITY_ATTACHMENT_ENABLED", "true")
    _wired["candidates"] = [{"product_key": "pk_a"}]
    _wired["match"] = _exact_match()

    result = await sia.attach_seed_identity_exact(_seed())
    assert result and result["product_key"] == "pk_a"
    assert len(_wired["applied"]) == 1
    assert _wired["applied"][0]["dry_run"] is False


@pytest.mark.asyncio
async def test_force_bypasses_flag(monkeypatch, _wired):
    monkeypatch.delenv("SEED_IDENTITY_ATTACHMENT_ENABLED", raising=False)
    _wired["candidates"] = [{"product_key": "pk_a"}]
    _wired["match"] = _exact_match()

    result = await sia.attach_seed_identity_exact(_seed(), force=True)
    assert result is not None
    assert len(_wired["applied"]) == 1


@pytest.mark.asyncio
async def test_fuzzy_match_never_auto_attaches(monkeypatch, _wired):
    monkeypatch.setenv("SEED_IDENTITY_ATTACHMENT_ENABLED", "true")
    _wired["candidates"] = [{"product_key": "pk_b"}]
    _wired["match"] = _fuzzy_match()  # title_brand_match — fuzzy

    result = await sia.attach_seed_identity_exact(_seed())
    assert result is None  # left unattached for HITL review
    assert _wired["applied"] == []


@pytest.mark.asyncio
async def test_dry_run_computes_but_does_not_write(monkeypatch, _wired):
    _wired["candidates"] = [{"product_key": "pk_a"}]
    _wired["match"] = _exact_match()

    result = await sia.attach_seed_identity_exact(_seed(), force=True, dry_run=True)
    assert result is not None  # would attach
    assert _wired["applied"][0]["dry_run"] is True  # apply called in dry-run mode


@pytest.mark.asyncio
async def test_already_attached_seed_skipped(monkeypatch, _wired):
    monkeypatch.setenv("SEED_IDENTITY_ATTACHMENT_ENABLED", "true")
    _wired["candidates"] = [{"product_key": "pk_a"}]
    _wired["match"] = _exact_match()

    result = await sia.attach_seed_identity_exact(_seed(attached_product_key="pk_existing"))
    assert result is None
    assert _wired["applied"] == []


@pytest.mark.asyncio
async def test_best_effort_hook_noop_when_dark_and_never_raises(monkeypatch):
    monkeypatch.delenv("SEED_IDENTITY_ATTACHMENT_ENABLED", raising=False)

    async def boom(*a, **k):
        raise RuntimeError("should not be called when dark")

    monkeypatch.setattr(sia, "attach_seed_identity_exact", boom)
    # must return without invoking attach (flag off) and without raising
    await sia.attach_seed_identity_best_effort(_seed())


def test_enabled_flag_parsing(monkeypatch):
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv("SEED_IDENTITY_ATTACHMENT_ENABLED", val)
        assert sia.seed_identity_attachment_enabled() is True
    for val in ("0", "false", "", "no"):
        monkeypatch.setenv("SEED_IDENTITY_ATTACHMENT_ENABLED", val)
        assert sia.seed_identity_attachment_enabled() is False
