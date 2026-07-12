"""Fix Plan B — wiring tests for the two catalog_products write sites.

Covers:
  T1  resolved_vertical is written by BOTH lanes (the mirror lane previously
      omitted it entirely — the root cause of the 83% NULL cohort).
  T3  each lane accounts unresolved-vertical rows and exposes a configurable
      fail brake (env threshold parsing).
  T4  each lane case/trim-normalizes the category it writes.

The INSERT/UPSERT paths need a live DB, so the behavioral column-write is pinned
at source level (the same style as the existing mirror signature/category
tests), plus the pure threshold-parsing helpers are exercised directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.mirror_external_seeds_to_catalog_products as mirror_module  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_MIRROR_SRC = (_ROOT / "scripts" / "mirror_external_seeds_to_catalog_products.py").read_text()
_SYNC_SRC = (_ROOT / "services" / "catalog_sync_service.py").read_text()


# ---------------------------------------------------------------------------
# T1 — resolved_vertical written by BOTH lanes
# ---------------------------------------------------------------------------


def test_mirror_insert_includes_resolved_vertical_column_and_bind() -> None:
    """The root-cause fix: the external-seed mirror INSERT must now list
    resolved_vertical and bind :resolved_vertical (it omitted the column,
    leaving every mirrored row NULL)."""
    assert "resolved_vertical," in _MIRROR_SRC
    assert ":resolved_vertical," in _MIRROR_SRC
    assert '"resolved_vertical": resolved_vertical' in _MIRROR_SRC
    # Still healed-by-backfill, never overwritten on re-mirror.
    assert "ON CONFLICT (merchant_id, platform, source_product_id) DO NOTHING" in _MIRROR_SRC


def test_mirror_resolves_vertical_with_full_signal_set() -> None:
    """Matches the ingest_standard_products signal set: category/product_type/
    category_path + a title blob folding title + description + tags."""
    assert "resolved_vertical = resolve_vertical(" in _MIRROR_SRC
    assert "from services.vertical_profiles import" in _MIRROR_SRC
    assert "resolve_vertical" in _MIRROR_SRC


def test_sync_writes_resolved_vertical_once_into_local() -> None:
    """The sync lane computes the vertical once into a local (so the brake counts
    the same value it persists) and writes that local."""
    assert "_resolved_vertical = resolve_vertical(" in _SYNC_SRC
    assert '"resolved_vertical": _resolved_vertical' in _SYNC_SRC


# ---------------------------------------------------------------------------
# T4 — category normalization at both write sites
# ---------------------------------------------------------------------------


def test_mirror_normalizes_category_before_write() -> None:
    assert "normalized_category = normalize_category(" in _MIRROR_SRC
    assert '"category": normalized_category' in _MIRROR_SRC
    # The raw mirrored_category must no longer be written unnormalized.
    assert '"category": row_dict.get("mirrored_category")' not in _MIRROR_SRC


def test_sync_normalizes_category_before_write() -> None:
    assert "_normalized_category = normalize_category(" in _SYNC_SRC
    assert '"category": _normalized_category' in _SYNC_SRC
    # The raw product_type must no longer be written straight into category.
    assert '"category": product.product_type' not in _SYNC_SRC


# ---------------------------------------------------------------------------
# T3 — unresolved accounting + configurable brake at both sites
# ---------------------------------------------------------------------------


def test_mirror_emits_summary_and_can_fail_the_run() -> None:
    assert "summarize_unresolved_vertical(" in _MIRROR_SRC
    assert "is_vertical_unresolved(" in _MIRROR_SRC
    # The guard is surfaced in the report and flips ok -> False (non-zero exit).
    assert 'report["vertical_guard"] = vertical_guard' in _MIRROR_SRC
    assert 'report["ok"] = False' in _MIRROR_SRC


def test_sync_surfaces_guard_in_stats_without_raising() -> None:
    """A LIVE merchant sync must not raise mid-write, so the sync lane surfaces
    the guard in the returned stats + logs, rather than exiting non-zero."""
    assert "summarize_unresolved_vertical(" in _SYNC_SRC
    assert 'stats["vertical_guard"] = _vertical_guard' in _SYNC_SRC


def test_mirror_threshold_env_parsing() -> None:
    """MIRROR_UNRESOLVED_VERTICAL_FAIL_THRESHOLD overrides the default; bad /
    out-of-range values fall back to the shared default."""
    from services.vertical_profiles import DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD as DEFAULT

    fn = mirror_module._unresolved_vertical_fail_threshold
    env = mirror_module.os.environ
    key = "MIRROR_UNRESOLVED_VERTICAL_FAIL_THRESHOLD"
    prior = env.get(key)
    try:
        env.pop(key, None)
        assert fn() == DEFAULT
        env[key] = "0.35"
        assert fn() == 0.35
        env[key] = "not-a-number"
        assert fn() == DEFAULT
        env[key] = "1.5"  # out of [0,1]
        assert fn() == DEFAULT
        env[key] = "-0.1"
        assert fn() == DEFAULT
    finally:
        if prior is None:
            env.pop(key, None)
        else:
            env[key] = prior
