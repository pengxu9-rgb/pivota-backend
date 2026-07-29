"""The scorer's rules_version must move whenever the component set moves.

WHY THIS TEST EXISTS. `backfill_external_seed_quality_rescore._rescored_ids()`
skips any product already carrying `SOURCE_BACKED_COMPONENTS_RULES_VERSION`. So a
component-set change WITHOUT a version bump does not merely mislabel snapshots —
it makes the corrective rescore a silent no-op for every row already on the old
value, leaving them on the previous scale while the serving gate compares against
the new floor.

That is not hypothetical: #1612 removed `summary` (rescaling every score by 7/6)
and raised the floor 65.0 -> 71.4 without bumping the version. The ~2,700+ rows
scored during the 2026-07-28 drain were already on v2, so the follow-up rescore
would have skipped them, and `jobs/nightly_index_health_job` — which reclassifies
in batches at 04:00 UTC from stored scores — would have demoted them wholesale.

A plain "the constant equals v3" assertion would go stale on the next legitimate
bump, so this pins the INVARIANT instead: the version string must encode the
component count that the scorer actually produces.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.external_seed_servability import (  # noqa: E402
    build_servable_quality_payload,
)
from services.product_quality_service import (  # noqa: E402
    SOURCE_BACKED_COMPONENTS_RULES_VERSION,
    preview_quality,
)

_WORD_TO_INT = {
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}


def _scored_component_count() -> int:
    payload = build_servable_quality_payload(
        title="Centella Calming Gel Cream",
        description="A soothing gel cream for sensitive, dehydrated skin.",
        price=24.0,
        image_url="https://example.com/p.jpg",
        brand="ExampleBeauty",
        product_type=None,
        category="skincare",
    )
    return len(preview_quality(payload)["components"])


def test_rules_version_encodes_the_actual_component_count():
    """If you change the component set, change the version — and say the count.

    The version is the resumability key for the rescore. Encoding the count makes
    a mismatch a test failure rather than a silent skip of the whole corpus.
    """
    n = _scored_component_count()
    version = SOURCE_BACKED_COMPONENTS_RULES_VERSION
    found = None
    for word, value in _WORD_TO_INT.items():
        if word in version:
            found = value
            break
    m = re.search(r"(\d+)[-_]?components?", version)
    if m:
        found = int(m.group(1))

    assert found is not None, (
        f"rules_version {version!r} does not state its component count. It is the "
        f"key `_rescored_ids()` de-duplicates on; name it so a component-set change "
        f"cannot silently reuse the previous value (e.g. 'v3-six-components')."
    )
    assert found == n, (
        f"rules_version {version!r} claims {found} components but the scorer emits "
        f"{n}. Bump the version to match — otherwise the rescore skips every row "
        f"already on the old value and they keep scores from the previous scale."
    )


def test_summary_is_not_among_the_scored_components():
    """Guards the specific removal the v3 bump records."""
    payload = build_servable_quality_payload(
        title="X",
        description="y" * 100,
        price=1.0,
        image_url="https://example.com/p.jpg",
        brand="B",
        product_type=None,
        category="skincare",
    )
    names = {c["name"] for c in preview_quality(payload)["components"]}
    assert "summary" not in names
    assert len(names) == _scored_component_count()


def test_version_differs_from_the_superseded_one():
    # v2 is the value the 2026-07-28 drain wrote. Reusing it would re-create the
    # exact silent-skip this bump exists to prevent.
    assert SOURCE_BACKED_COMPONENTS_RULES_VERSION != "v2-source-backed-components"
