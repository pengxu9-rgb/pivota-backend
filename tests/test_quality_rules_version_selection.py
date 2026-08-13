"""Which rules_version a quality snapshot is written under — pinned, finally.

MEASURED 2026-08-13 (scripts/report_quality_scale_population.py, prod): 3,689 of
14,979 products — 24.6% of the corpus — carry a CURRENT score on a superseded
scale, across five stale rules_versions. v3-six-components scores exist only in
a two-day window (the 2026-07-28/29 #1612 drain); v1-lite has a snapshot from
the day before the measurement. The corpus was drifting BACK to the old scale,
and nothing noticed for two weeks, because the version-selection logic below had
zero test coverage:

  * `full_quality_eval` promotes DEFAULT_QUALITY_RULES_VERSION ("v1-lite") to
    SOURCE_BACKED_COMPONENTS_RULES_VERSION ("v3-six-components") only when the
    source-backed optional components were actually scored;
  * whether they are scored defaults to the env flag
    PDP_QUALITY_SCORE_SOURCE_BACKED_OPTIONAL_COMPONENTS — which was UNSET in
    prod, so every scoring path that did not explicitly pass
    score_source_backed_components=True regressed to v1-lite;
  * and the one explicit True (make_external_seed_servable) was asserted by no
    test repo-wide — flipping it to False survived the entire suite.

These tests drive the REAL full_quality_eval (not a stub of it) with a fake
database, and read the rules_version off the compiled INSERT — so any future
change to the promotion logic, the flag helper, or the env var name fails here
rather than silently re-draining the corpus onto the wrong scale.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.product_quality_service as pqs  # noqa: E402

# A payload rich enough to score without tripping any guard; content is
# irrelevant here — only the VERSION the row is written under is.
_PAYLOAD: Dict[str, Any] = {
    "title_canonical": "Glow Serum",
    "title_local": "Glow Serum",
    "brand": "AuraGlow",
    "description_local": "A perfectly ordinary description of adequate length.",
    "price_local_value": 42.0,
    "image_url": "https://example.com/x.jpg",
}


class _RecordingDB:
    """Records the INSERT; answers the post-insert catalog lookup with None so
    the serving-eligibility hook no-ops."""

    def __init__(self) -> None:
        self.inserts: List[Any] = []

    async def execute(self, stmt: Any) -> None:
        self.inserts.append(stmt)

    async def fetch_one(self, sql: str, params: Optional[Dict[str, Any]] = None):
        return None


def _written_rules_version(db: _RecordingDB) -> str:
    assert len(db.inserts) == 1, f"expected exactly one snapshot insert, got {len(db.inserts)}"
    # The row travels inside the SQLAlchemy Insert; compile() exposes its params.
    return db.inserts[0].compile().params["rules_version"]


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> _RecordingDB:
    fake = _RecordingDB()
    monkeypatch.setattr(pqs, "database", fake)
    return fake


@pytest.mark.asyncio
async def test_flag_unset_writes_v1_lite(monkeypatch: pytest.MonkeyPatch, db) -> None:
    """THE DRIFT MECHANISM. Flag unset + no explicit override = the old scale.
    This is what prod was doing to every ordinarily-scored product for two weeks."""
    monkeypatch.delenv(pqs.SOURCE_BACKED_OPTIONAL_COMPONENTS_FLAG, raising=False)

    await pqs.full_quality_eval(
        merchant_id="m1", platform="shopify", platform_product_id="p1",
        geo_code="default", payload=dict(_PAYLOAD),
    )
    assert _written_rules_version(db) == pqs.DEFAULT_QUALITY_RULES_VERSION


@pytest.mark.asyncio
async def test_flag_on_writes_v3(monkeypatch: pytest.MonkeyPatch, db) -> None:
    """The premise of the prod flag flip: setting the env var must change which
    scale new snapshots land on, with no code change."""
    monkeypatch.setenv(pqs.SOURCE_BACKED_OPTIONAL_COMPONENTS_FLAG, "1")

    await pqs.full_quality_eval(
        merchant_id="m1", platform="shopify", platform_product_id="p1",
        geo_code="default", payload=dict(_PAYLOAD),
    )
    assert _written_rules_version(db) == pqs.SOURCE_BACKED_COMPONENTS_RULES_VERSION


@pytest.mark.asyncio
async def test_explicit_true_beats_an_unset_flag(monkeypatch: pytest.MonkeyPatch, db) -> None:
    """make_external_seed_servable's path: score_source_backed_components=True
    must produce v3 regardless of the env — external seeds ARE the source-backed
    case, and were the only writes landing as v3 while the flag was unset."""
    monkeypatch.delenv(pqs.SOURCE_BACKED_OPTIONAL_COMPONENTS_FLAG, raising=False)

    await pqs.full_quality_eval(
        merchant_id="m1", platform="external_seed", platform_product_id="p1",
        geo_code="default", payload=dict(_PAYLOAD),
        score_source_backed_components=True,
    )
    assert _written_rules_version(db) == pqs.SOURCE_BACKED_COMPONENTS_RULES_VERSION


@pytest.mark.asyncio
async def test_an_explicit_rules_version_is_never_promoted(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """The promotion is gated on rules_version == DEFAULT. A caller that names
    its own version owns it — promoting it would silently misfile snapshots
    under a version the caller never chose, which is exactly the comparability
    corruption the version column exists to prevent."""
    monkeypatch.setenv(pqs.SOURCE_BACKED_OPTIONAL_COMPONENTS_FLAG, "1")

    await pqs.full_quality_eval(
        merchant_id="m1", platform="shopify", platform_product_id="p1",
        geo_code="default", payload=dict(_PAYLOAD),
        rules_version="v9-experimental",
    )
    assert _written_rules_version(db) == "v9-experimental"


def test_the_env_var_name_matches_what_prod_has_set() -> None:
    """The literal is load-bearing: this exact spelling was set on the Railway
    web service on 2026-08-13. Renaming the constant without migrating the prod
    variable silently reverts the corpus to v1-lite — the two-week drift again,
    with the flag apparently 'on'."""
    assert pqs.SOURCE_BACKED_OPTIONAL_COMPONENTS_FLAG == (
        "PDP_QUALITY_SCORE_SOURCE_BACKED_OPTIONAL_COMPONENTS"
    )


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("", False), ("enabled", False),
])
def test_flag_value_parsing(value: str, expected: bool,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pqs.SOURCE_BACKED_OPTIONAL_COMPONENTS_FLAG, value)
    assert pqs.quality_source_backed_optional_components_enabled() is expected
