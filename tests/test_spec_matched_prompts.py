"""Spec-matched discovery prompts for medium/long-tail merchants.

Head terms ("best headphones") are dominated by top brands; a niche merchant can
only win SPEC-anchored long-tail. Three defects used to sabotage that:

  1. A one-directional substring guard let a use-case that CONTAINS the category
     noun through, producing malformed "best headphones for bone conduction
     headphones" / "best headphones for golf headphones".
  2. The beauty problem-frame "what helps with X" leaked into electronics, whose
     use-cases are activities/product types → "what helps with bone conduction
     headphones".
  3. The budgeter filled leftover slots from the category-template tail BEFORE
     the LLM (Gemini) winnable/scenario prompts, so at the wedge target of 14 the
     spec-matched prompts were dropped — wasting the whole point of generation.

These tests pin all three, and assert beauty behavior is preserved.
"""

from services import agent_center_bd_report_service as m
from services.vertical_profiles import (
    BEAUTY_PROFILE,
    ELECTRONICS_AUDIO_PROFILE,
)


# --- unit: bidirectional category-noun redundancy guard --------------------

def test_term_repeats_category_catches_nested_head_noun():
    # category ⊆ term: the case the old `term not in category` guard MISSED.
    assert m._term_repeats_category(
        "bone conduction headphones", "headphones", profile=ELECTRONICS_AUDIO_PROFILE
    )
    assert m._term_repeats_category(
        "golf headphones", "headphones", profile=ELECTRONICS_AUDIO_PROFILE
    )
    # cross-type nesting via the vertical's product-type head nouns.
    assert m._term_repeats_category(
        "wireless earbuds", "headphones", profile=ELECTRONICS_AUDIO_PROFILE
    )


def test_term_repeats_category_allows_real_use_cases():
    # genuine activity/concern use-cases must survive.
    assert not m._term_repeats_category(
        "golf", "headphones", profile=ELECTRONICS_AUDIO_PROFILE
    )
    assert not m._term_repeats_category(
        "swimming", "headphones", profile=ELECTRONICS_AUDIO_PROFILE
    )
    # beauty concern use-cases don't contain product-form head nouns.
    assert not m._term_repeats_category(
        "damaged hair", "hair oil", profile=BEAUTY_PROFILE
    )
    assert not m._term_repeats_category(
        "dry skin", "serum", profile=BEAUTY_PROFILE
    )


# --- electronics: no malformed, no problem-frame ---------------------------

def _electronics_specs():
    graph = {
        "classes": {
            "use_case": [
                "bone conduction headphones",  # redundant with category -> drop
                "swimming",                     # real activity -> keep
                "sports",                       # real activity -> keep
            ],
            "audience": ["athletes"],
        }
    }
    return m._unbranded_category_specs(
        category="headphones",
        graph=graph,
        topics=["golf headphones", "open water swimming"],
        bullets=[],
        profile=ELECTRONICS_AUDIO_PROFILE,
    )


def test_electronics_no_malformed_headphones_for_headphones():
    joined = " || ".join(q for q, _ in _electronics_specs())
    assert "headphones for bone conduction headphones" not in joined
    assert "headphones for golf headphones" not in joined
    # the real activities still produce clean spec queries.
    assert "best headphones for swimming" in joined


def test_electronics_no_problem_framed_prompts():
    # "what helps with X" is nonsensical for a device — gated off by profile.
    joined = " || ".join(q for q, _ in _electronics_specs())
    assert "what helps with" not in joined


# --- beauty: problem-frame + concern queries preserved ---------------------

def test_beauty_problem_framed_prompts_preserved():
    graph = {
        "classes": {
            "use_case": ["damaged hair", "split ends"],
            "audience": ["women"],
        }
    }
    specs = m._unbranded_category_specs(
        category="hair oil",
        graph=graph,
        topics=[],
        bullets=[],
        profile=BEAUTY_PROFILE,
    )
    joined = " || ".join(q for q, _ in specs)
    assert "what helps with damaged hair" in joined
    assert "best hair oil for damaged hair" in joined


# --- budgeter: LLM winnable prompts win the leftover slots ------------------

def test_winnable_prompts_win_budget_over_template_tail():
    # base_records: 2 nav + 2 head + 3 trust + generic category tail, then the
    # LLM winnable prompts LAST (their real position in base order).
    winnable = [
        "bone conduction headphones for golfers",
        "open ear headphones for triathletes",
    ]
    base_records = m._query_tuple_records(
        [
            ("where can I buy X", "intent"),
            ("shop X online", "intent"),
            ("best headphones", "category"),
            ("what headphones should I buy", "category"),
            ("is X legit", "review"),
            ("X reviews", "review"),
            ("does X actually work", "review"),
            ("headphones for athletes", "category"),
            ("top headphones", "category"),
            ("recommended headphones", "category"),
        ]
        + [(w, "category") for w in winnable]
    )
    # Exactly 2 sidewalk rows so the budgeter's leftover (mid-reserve) region is
    # scarce: at target=10 there are precisely 2 leftover slots, which must go to
    # the winnable prompts, NOT the generic category-template tail.
    sidewalk_records = m._query_tuple_records(
        [(f"ip68 waterproof bone conduction headphones {i}", "sidewalk") for i in range(2)]
    )
    recs = m._budgeted_wedge_query_records(
        base_records=base_records,
        sidewalk_records=sidewalk_records,
        target=10,
        title="X",
        priority_queries=frozenset(w.lower() for w in winnable),
    )
    final = {r["query"].lower() for r in recs}
    # The Gemini winnable prompts must survive the tight budget...
    for w in winnable:
        assert w.lower() in final, f"winnable prompt dropped from budget: {w}"
    # ...and beat the generic category-template tail for the scarce leftover slots.
    assert "top headphones" not in final and "recommended headphones" not in final
