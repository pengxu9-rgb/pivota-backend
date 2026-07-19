"""Decision-intelligence lane — the EXTRACTIVE anti-fabrication gate, the
generator, and the content_key-driven CLI orchestration.

No network, no DB: the LLM is the injected GenerateFn (a fake model) and the
CLI's fetch/persist/publish calls are monkeypatched. The invariant these lock in
is the re-review's: the gate must PUBLISH SOURCE-TRUTH (matched evidence
claim_text / quoted source span) and drop anything else — so a fabricated clause,
a grade-escalation, or an attribute swap can never reach the PDP even when it
rides real fragments.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import scripts.author_decision_intelligence as cli
from services.decision_intelligence import (
    DecisionCopy,
    author_decision_copy,
    build_context,
    evaluate_bullet,
    is_grounded,
)


def _fake_gen(payload: Dict[str, Any]):
    async def _gen(_prompt: str) -> Optional[Dict[str, Any]]:
        return payload

    return _gen


# ==========================================================================
# The 6 reproductions the re-review reproduced against evaluate_bullet — all
# must DROP. (source = brand marketing; substantiated_claims mostly empty.)
# ==========================================================================

def test_depuff_missing_from_allowlist_still_drops():
    ctx = build_context(description="A cooling eye gel for mornings.", substantiated_claims=[])
    kept, reason, pub = evaluate_bullet("De-puffs tired under-eyes", ctx)
    assert kept is False and pub is None
    assert reason == "efficacy_unsubstantiated"


def test_anti_aging_powerhouse_drops():
    ctx = build_context(description="A rich night cream.", substantiated_claims=[])
    kept, reason, _ = evaluate_bullet("An anti-aging powerhouse", ctx)
    assert kept is False and reason == "efficacy_unsubstantiated"


def test_minimizes_redness_stemmer_drift_drops():
    # 'minimize'->'minimiz' must be in the vocab (stemmer drift fix, #5).
    ctx = build_context(description="A calming facial mist.", substantiated_claims=[])
    kept, reason, _ = evaluate_bullet("Visibly minimizes redness", ctx)
    assert kept is False and reason == "efficacy_unsubstantiated"


def test_fabricated_rider_clause_drops():
    # Only 'a refreshing gel moisturizer' is owned; the 'reverses sun damage'
    # clause is fabricated and must sink the whole bullet.
    ctx = build_context(
        description="A refreshing gel moisturizer with green tea extract.", substantiated_claims=[]
    )
    kept, reason, _ = evaluate_bullet(
        "A refreshing gel moisturizer that reverses sun damage", ctx
    )
    assert kept is False
    assert reason == "efficacy_unsubstantiated"  # 'reverses' is a claim, unbacked


def test_grade_escalation_ride_drops():
    # Bullet rides a hedged evidence item but escalates the grade -> drop.
    ctx = build_context(
        description="A serum with niacinamide.",
        substantiated_claims=["Niacinamide may help reduce hyperpigmentation and dark spots"],
    )
    kept, reason, _ = evaluate_bullet("Clinically proven to reduce hyperpigmentation", ctx)
    assert kept is False and reason == "grade_escalation"


def test_skin_type_attribute_swap_drops():
    ctx = build_context(description="A mattifying toner for oily skin.", substantiated_claims=[])
    kept, reason, _ = evaluate_bullet("Great for dry skin", ctx)
    assert kept is False and reason == "attribute_swap"


# ==========================================================================
# The 2 positives — a real substantiated claim publishes its claim_text
# VERBATIM, and a real quoted source phrase publishes the source span.
# ==========================================================================

def test_substantiated_claim_publishes_claim_text_verbatim():
    claim = "Niacinamide helps reduce the look of hyperpigmentation and dark spots"
    ctx = build_context(description="A serum with niacinamide.", substantiated_claims=[claim])
    kept, reason, pub = evaluate_bullet("Helps reduce hyperpigmentation and dark spots", ctx)
    assert kept is True and reason == "efficacy_substantiated"
    assert pub == claim  # publishes the graded evidence phrasing, not the model line


def test_quoted_source_phrase_publishes_span():
    ctx = build_context(
        description="A refreshing gel moisturizer with green tea extract.", substantiated_claims=[]
    )
    kept, reason, pub = evaluate_bullet("A refreshing gel moisturizer with green tea extract", ctx)
    assert kept is True and reason == "descriptive_quote"
    assert pub is not None and "refreshing" in pub.lower()
    assert "reverses" not in pub.lower()


def test_negation_and_free_inversions_still_drop():
    ctx1 = build_context(description="Suitable for sensitive skin.", substantiated_claims=[])
    assert evaluate_bullet("Not suitable — may cause irritation", ctx1)[1] == "polarity_flip"
    ctx2 = build_context(description="An alcohol-free formula.", substantiated_claims=[])
    assert evaluate_bullet("Formulated with alcohol", ctx2)[1] == "polarity_flip"


# ==========================================================================
# author_decision_copy end-to-end (fake model) — publishes source-truth.
# ==========================================================================

_DESC = (
    "A gentle daily toner formulated with niacinamide and panthenol. "
    "Fragrance-free and suitable for sensitive skin. "
    "Lightweight watery texture that absorbs quickly."
)
_SUBS = [
    "Helps brighten the look of dull, uneven skin tone",
    "Hydrates and helps replenish the skin's moisture",
]


async def test_author_publishes_source_truth_and_drops_the_rest():
    gen = _fake_gen({
        "bullet_points": [
            "Helps brighten the look of dull, uneven skin tone",  # verbatim claim -> claim_text
            "A gentle daily toner",                               # source quote
            "Lightweight watery texture that absorbs quickly",    # source quote
            "Contains 24k gold flakes and free shipping",         # not owned -> drop
            "Clinically proven to reduce wrinkles",               # unbacked efficacy -> drop
        ],
        "usage_scenarios": [
            "Suitable for sensitive skin",                        # source quote
            "Doubles as a car engine degreaser",                 # not owned -> drop
        ],
    })
    copy = await author_decision_copy(
        description=_DESC, title="Daily Toner",
        actives=[{"label": "Niacinamide", "source": "inci"}],
        substantiated_claims=_SUBS, generate_fn=gen,
    )
    assert copy.is_publishable()
    assert "Helps brighten the look of dull, uneven skin tone" in copy.bullet_points
    joined = " ".join(copy.bullet_points).lower()
    assert "gold" not in joined and "wrinkles" not in joined
    assert len(copy.bullet_points) == 3
    assert [s.lower() for s in copy.usage_scenarios] == ["suitable for sensitive skin"]
    assert {d["reason"] for d in copy.dropped} >= {"descriptive_not_quote", "efficacy_unsubstantiated"}


async def test_author_no_source_authors_nothing():
    gen = _fake_gen({"bullet_points": ["Anything at all", "More invented copy"]})
    copy = await author_decision_copy(title=None, description=None, generate_fn=gen)
    assert copy.bullet_points == [] and copy.usage_scenarios == []
    assert copy.is_publishable() is False


async def test_author_none_from_model_is_safe():
    async def _none(_p):
        return None

    copy = await author_decision_copy(title="Toner", description=_DESC, generate_fn=_none)
    assert isinstance(copy, DecisionCopy)
    assert copy.generated is False and copy.bullet_points == []


def test_is_grounded_boolean_alias():
    ctx = build_context(description="A refreshing gel moisturizer.", substantiated_claims=[])
    assert is_grounded("A refreshing gel moisturizer", ctx) is True
    assert is_grounded("Comes with a free gold watch", ctx) is False


# ==========================================================================
# CLI — content_key-driven: overlay lands on the pick_canonical winner (S1),
# no double refresh (S3), per-ck isolation.
# ==========================================================================

def _canonical(**kw) -> Dict[str, Any]:
    base = {
        "product_key": "pk_canon",
        "content_key": "ck1",
        "merchant_id": "external_seed",
        "platform": "seed",
        "source_product_id": "sp_canon",
        "title": "Daily Toner",
        "description": _DESC,
        "brand": "Anuko",
        "category": "beauty/skincare",
        "category_kind": "skincare",
        "tags": ["hydrating", "sensitive"],
        "pivota_signature_id": "sig_1",
        "group_is_primary": True,
    }
    base.update(kw)
    return base


class _FakeDB:
    async def fetch_one(self, _sql, _params=None):
        return {"raw_inci": "Aqua, Niacinamide, Panthenol", "active_ingredients_json": None}


_APPLY_GEN = {
    "bullet_points": [
        "A gentle daily toner",
        "Fragrance-free and suitable for sensitive skin",
        "Lightweight watery texture that absorbs quickly",
        "Contains 24k gold flakes",
    ],
    "usage_scenarios": ["Suitable for sensitive skin"],
}


@pytest.fixture
def patched_cli(monkeypatch):
    calls: Dict[str, List[Any]] = {"enrich": [], "evidence": [], "refresh": []}
    sibling = _canonical(product_key="pk_other", source_product_id="sp_other",
                         pivota_signature_id=None, group_is_primary=False)
    products = [sibling, _canonical()]

    async def fake_fetch_products(ck, db=None):
        return products

    async def fake_upsert(mid, platform, ppid, geo, data):
        calls["enrich"].append({"key": (mid, platform, ppid, geo), "data": data})

    async def fake_evidence(pk, dry_run=False):
        calls["evidence"].append(pk)
        return {"status": "ok", "written": {"evidence_claims": True, "concerns": [], "actives_skus": []}}

    async def fake_refresh(ck, refresh_source=""):
        calls["refresh"].append(ck)
        return True

    monkeypatch.setattr(cli, "database", _FakeDB())
    monkeypatch.setattr(cli, "fetch_products_for_key", fake_fetch_products)
    monkeypatch.setattr(cli, "upsert_enrichment", fake_upsert)
    monkeypatch.setattr(cli, "enrich_and_persist_product", fake_evidence)
    monkeypatch.setattr(cli, "refresh_agent_pdp_view_for_content_key", fake_refresh)
    return calls


def _patch_gen(monkeypatch, payload):
    real = author_decision_copy
    gen = _fake_gen(payload)
    monkeypatch.setattr(cli, "author_decision_copy",
                        lambda **kw: real(generate_fn=gen, **kw))


async def test_apply_ck_writes_to_pick_canonical_winner(patched_cli, monkeypatch):
    _patch_gen(monkeypatch, _APPLY_GEN)
    res = await cli._apply_ck("ck1")

    assert res["copy_written"] is True
    assert len(patched_cli["enrich"]) == 1
    write = patched_cli["enrich"][0]
    # S1: overlay keyed to pk_canon's identity triple (pick_canonical winner).
    assert write["key"] == ("external_seed", "seed", "sp_canon", "default")
    assert len(write["data"]["bullet_points"]) >= 3
    assert patched_cli["evidence"] == ["pk_canon"]
    # S3: evidence writer already refreshed -> CLI must NOT double-refresh.
    assert patched_cli["refresh"] == []
    assert res["published"] is True


async def test_apply_ck_refreshes_when_evidence_did_not(patched_cli, monkeypatch):
    async def fake_evidence(pk, dry_run=False):
        patched_cli["evidence"].append(pk)
        return {"status": "skipped_non_beauty", "written": {}}

    monkeypatch.setattr(cli, "enrich_and_persist_product", fake_evidence)
    _patch_gen(monkeypatch, _APPLY_GEN)
    res = await cli._apply_ck("ck1")
    assert res["copy_written"] is True
    assert patched_cli["refresh"] == ["ck1"]
    assert res["published"] is True


async def test_apply_ck_skips_copy_when_not_publishable(patched_cli, monkeypatch):
    _patch_gen(monkeypatch, {"bullet_points": ["A gentle daily toner"]})  # 1 quote only
    res = await cli._apply_ck("ck1")
    assert res["copy_written"] is False
    assert patched_cli["enrich"] == []
    assert patched_cli["evidence"] == ["pk_canon"]


async def test_apply_ck_isolates_copy_failure(patched_cli, monkeypatch):
    async def _boom(**_kw):
        raise ValueError("model exploded")

    monkeypatch.setattr(cli, "author_decision_copy", _boom)
    res = await cli._apply_ck("ck1")
    assert "copy_error" in res
    assert patched_cli["evidence"] == ["pk_canon"]
    assert res.get("copy_written") is not True
