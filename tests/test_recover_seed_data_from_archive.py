"""Tests for scripts/recover_seed_data_from_archive.py.

The recovery script is the bridge between a restored Postgres backup
(operator stands up via Railway dashboard) and the live DB. It must:

  - Skip rows where the archive doesn't actually beat live (cheap
    no-op; we don't want 4,500 useless proposal rows).
  - Pass archive seed_data through services.seed_data_writer.upsert_seed_data
    when archive scores higher on at least one lockable field — never
    write to seed_data directly.
  - Honor --dry-run by NOT calling the writer at all.
  - Emit a deterministic per-row outcome that codex / operators can
    grep for in the JSON report.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import recover_seed_data_from_archive as recover  # noqa: E402


# ---------------------------------------------------------------------------
# _archive_beats_live_on_any_lockable_field
# ---------------------------------------------------------------------------


def test_archive_beats_when_archive_has_richer_description() -> None:
    """The recovery scenario: archive has the original 800-char
    description, live has a 200-char polluted one. Score check picks
    that up."""
    archive = {"description": "A" * 800, "pdp_ingredients_raw": "AQUA, GLYCERIN"}
    live = {"description": "Short.", "pdp_ingredients_raw": "AQUA, GLYCERIN"}
    higher, breakdown = recover._archive_beats_live_on_any_lockable_field(archive, live)
    assert higher is True
    assert breakdown["description"]["archive"] > breakdown["description"]["live"]


def test_archive_does_not_beat_when_identical() -> None:
    """Identical content — return False so we don't waste a proposal
    row on a known no-op. This is the common case (4,500 rows; only a
    fraction were polluted)."""
    same = {"description": "Same description.", "pdp_ingredients_raw": "AQUA"}
    higher, _ = recover._archive_beats_live_on_any_lockable_field(same, same)
    assert higher is False


def test_archive_does_not_beat_when_live_is_better() -> None:
    """If live is *richer* than archive (e.g. live had a richer scrape
    after the backup date), don't propose. The writer would reject
    anyway, but skipping at the archive-side saves the round-trip."""
    archive = {"description": "Short."}
    live = {"description": "A" * 800}
    higher, _ = recover._archive_beats_live_on_any_lockable_field(archive, live)
    assert higher is False


def test_archive_beats_on_one_field_even_if_worse_on_another() -> None:
    """Field-level granularity: archive can win on description even if
    live has a better pdp_ingredients_raw. The writer will merge
    description and reject the ingredient regression."""
    archive = {"description": "A" * 800, "pdp_ingredients_raw": "short"}
    live = {"description": "Short.", "pdp_ingredients_raw": "AQUA, GLYCERIN, NIACINAMIDE"}
    higher, _ = recover._archive_beats_live_on_any_lockable_field(archive, live)
    assert higher is True


# ---------------------------------------------------------------------------
# _process_one — per-row dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_skips_when_no_live_row(monkeypatch) -> None:
    """A row that exists in archive but not in live (live deleted /
    re-id'd) is skipped — repopulating is a different concern."""
    async def fake_fetch_live_row(seed_id):
        return None
    monkeypatch.setattr(recover, "_fetch_live_row", fake_fetch_live_row)

    out = await recover._process_one(
        {"id": "eps_x", "external_product_id": "ext_x", "seed_data": {"description": "x"}},
        proposer="test", apply=True,
    )
    assert out["outcome"] == "skipped_no_live_row"


@pytest.mark.asyncio
async def test_process_one_dry_run_does_not_call_writer(monkeypatch) -> None:
    """--dry-run must not call upsert_seed_data — operators want to
    eyeball the score breakdown first."""
    async def fake_fetch_live_row(seed_id):
        return {"seed_data": {"description": "Short."}, "content_lock": {}}
    monkeypatch.setattr(recover, "_fetch_live_row", fake_fetch_live_row)

    writer_calls: List[Dict[str, Any]] = []

    async def fake_upsert(**kwargs):
        writer_calls.append(kwargs)
        from services.seed_data_writer import WriteResult
        return WriteResult(seed_id="x", proposal_id=1, proposer="x", status="merged")
    monkeypatch.setattr(
        recover.seed_data_writer, "upsert_seed_data", fake_upsert
    )

    archive_row = {
        "id": "eps_1", "external_product_id": "ext_1",
        "seed_data": {"description": "A" * 800},
    }
    out = await recover._process_one(archive_row, proposer="test", apply=False)
    assert out["outcome"] == "dry_run_archive_higher"
    assert writer_calls == []


@pytest.mark.asyncio
async def test_process_one_apply_calls_writer_with_archive_data(monkeypatch) -> None:
    """--apply path: archive beats live → writer is called with the
    archive's seed_data, the proposer tag, and source='archive_restore'.
    Result is surfaced in the per-row outcome."""
    async def fake_fetch_live_row(seed_id):
        return {"seed_data": {"description": "Short."}, "content_lock": {}}
    monkeypatch.setattr(recover, "_fetch_live_row", fake_fetch_live_row)

    captured: List[Dict[str, Any]] = []

    async def fake_upsert(*, seed_id, external_product_id, proposed_seed_data,
                           proposer, source, **kwargs):
        captured.append({
            "seed_id": seed_id,
            "external_product_id": external_product_id,
            "proposed_seed_data": proposed_seed_data,
            "proposer": proposer,
            "source": source,
        })
        from services.seed_data_writer import WriteResult, FieldDecision
        return WriteResult(
            seed_id=seed_id, proposal_id=42, proposer=proposer, status="merged",
            field_decisions=[FieldDecision(
                field="description", decision="merge", reason="unlocked",
                old_value="Short.", new_value="A" * 800,
                old_score=6.0, new_score=800.0,
            )],
        )
    monkeypatch.setattr(
        recover.seed_data_writer, "upsert_seed_data", fake_upsert
    )

    archive_row = {
        "id": "eps_1", "external_product_id": "ext_1",
        "seed_data": {"description": "A" * 800, "brand": "Glow Recipe"},
    }
    out = await recover._process_one(
        archive_row, proposer="recovery_archive_20260506", apply=True
    )

    # Writer was called exactly once with the right payload
    assert len(captured) == 1
    call = captured[0]
    assert call["seed_id"] == "eps_1"
    assert call["proposer"] == "recovery_archive_20260506"
    assert call["source"] == "archive_restore"
    assert call["proposed_seed_data"]["description"] == "A" * 800

    # Outcome surfaces what merged
    assert out["outcome"] == "merged"
    assert "description" in out["merged_fields"]


@pytest.mark.asyncio
async def test_process_one_skips_when_archive_not_better(monkeypatch) -> None:
    """If archive scores ≤ live on every lockable field, skip — don't
    even call the writer (saves a proposal-row insert)."""
    async def fake_fetch_live_row(seed_id):
        return {"seed_data": {"description": "Long live description." * 50}, "content_lock": {}}
    monkeypatch.setattr(recover, "_fetch_live_row", fake_fetch_live_row)

    writer_calls: List[Dict[str, Any]] = []

    async def fake_upsert(**kwargs):
        writer_calls.append(kwargs)
        from services.seed_data_writer import WriteResult
        return WriteResult(seed_id="x", proposal_id=1, proposer="x", status="merged")
    monkeypatch.setattr(
        recover.seed_data_writer, "upsert_seed_data", fake_upsert
    )

    archive_row = {
        "id": "eps_1", "external_product_id": "ext_1",
        "seed_data": {"description": "Short."},
    }
    out = await recover._process_one(archive_row, proposer="test", apply=True)
    assert out["outcome"] == "skipped_archive_not_better"
    assert writer_calls == []


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------


def test_dry_run_flag_overrides_apply(monkeypatch) -> None:
    """--dry-run --apply → dry-run wins (safety default). Operators
    sometimes pass both by accident."""
    monkeypatch.setattr(sys, "argv", [
        "recover", "--archive-url", "postgresql://x", "--apply", "--dry-run",
    ])
    args = recover._parse_args()
    assert args.apply is False
