"""The scorer must not carry a dead component, nor punish long-form content.

Two defects, both measured on prod 2026-07-28:

1. `summary` scored 0.0 for 100% of rows — passing and blocked alike, across every
   rules_version, with exactly ONE non-zero row in 6,044. A permanently-zero term
   in an unweighted mean capped the achievable score at 6/7 = 85.7 and dragged
   every product down 14.3 points for a signal no ingest lane produces. The two
   most common blocked scores were exactly 4/7 = 57.1 and 3/7 = 42.9 — component
   counts, not a quality continuum.

2. `description` used the over-length decay, scoring a long description WORSE than
   a medium one. In the blocked cohort alone: 1,191 products at 600–1,799 chars
   decaying and 460 at >=1,800 pinned to the 0.4 floor. Backwards for an index
   whose purpose is being citable.

The rescale is a REAL WIDENING at a fixed threshold, so `test_removing_summary_
rescales_by_seven_sixths` pins the exact factor the floor decision depends on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.external_seed_servability import (  # noqa: E402
    build_servable_quality_payload,
)
from services.product_quality_service import (  # noqa: E402
    _text_length_score,
    preview_quality,
)

# Built through the real payload builder, as the sibling test
# tests/test_servable_quality_payload_sections.py does — a hand-rolled dict does
# not carry the derived keys (global_category_id in particular) and silently
# scores 1-of-6 for the wrong reason.
BASE = dict(
    title="Centella Calming Gel Cream",
    price=24.0,
    image_url="https://example.com/p.jpg",
    brand="ExampleBeauty",
    product_type=None,
    category="skincare",
)


def _payload(description: str):
    return build_servable_quality_payload(**BASE, description=description)


def _score(description: str) -> float:
    return preview_quality(_payload(description))["content_quality_score"]


def _components(description: str):
    return {
        c["name"]: c["score"]
        for c in preview_quality(_payload(description))["components"]
    }


# --- defect 1: the dead component --------------------------------------------

def test_summary_is_not_a_scored_component():
    names = list(_components("x" * 200))
    assert "summary" not in names, (
        "summary scored 0.0 for 100% of prod rows; a permanently-zero term in an "
        "unweighted mean is a flat penalty, not a neutral one"
    )


def test_six_components_remain_and_they_are_the_real_ones():
    names = set(_components("x" * 200))
    assert names == {
        "title",
        "description",
        "attributes",
        "images",
        "brand_category",
        "price",
    }


def test_removing_summary_rescales_by_seven_sixths():
    """The exact factor the floor re-baseline depends on.

    A row scoring title+description+images+brand_category+price and nothing else
    was 5/7 = 71.4; it is now 5/6 = 83.3. Any floor decision made against the old
    scale is off by this factor, which is why the threshold must move with it.
    """
    comps = _components("x" * 200)
    scored = [v for v in comps.values()]
    assert len(scored) == 6
    got = _score("x" * 200)
    expected = round(sum(scored) / 6.0, 1)
    assert abs(got - expected) < 0.05, f"{got} != mean of six components {expected}"


# --- defect 2: the decay curve ------------------------------------------------

def test_long_description_is_not_penalised():
    medium = _components("x" * 400)["description"]
    long_ = _components("x" * 1200)["description"]
    very_long = _components("x" * 5000)["description"]
    assert medium == 100.0
    assert long_ == 100.0, f"1,200-char description scored {long_}, expected no penalty"
    assert very_long == 100.0, (
        f"5,000-char description scored {very_long} — the 0.4 floor is exactly the "
        f"defect this removes"
    )


def test_short_description_ramp_is_untouched():
    # The SHORT side must still discriminate; only the upper penalty was removed.
    assert _components("x" * 20)["description"] < 100.0
    assert _components("")["description"] == 0.0
    assert _components("x" * 40)["description"] == 100.0


def test_over_length_penalty_still_applies_where_it_belongs():
    """Title and summary keep the decay — there, length IS a defect.

    Guards against "fix" by deleting the decay globally, which would stop
    penalising keyword-stuffed titles.
    """
    assert _text_length_score("x" * 5000, min_len=10, max_len=100) < 1.0
    assert _text_length_score("x" * 5000, min_len=10, max_len=100) == 0.4
    # ...and the opt-out is explicit, not the default.
    assert _text_length_score("x" * 5000, min_len=10, max_len=100,
                              penalize_over_max=False) == 1.0


def test_a_long_description_row_now_clears_a_stricter_bar():
    """End-to-end: the two fixes together.

    Old scale: description decayed to 0.4 and summary contributed 0, so a row
    with everything else present scored (1+0.4+0+0+1+1+1)/7 = 62.9 — blocked.
    New scale, same row: (1+1+0+1+1+1)/6 = 83.3.
    """
    score = _score("x" * 2000)
    assert score > 80.0, f"expected a content-rich row to score well; got {score}"
