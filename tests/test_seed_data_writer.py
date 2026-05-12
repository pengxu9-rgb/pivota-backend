"""Tests for services/seed_data_writer.py — the canonical write path.

Anchored to the 2026-05-09 incident chain: codex skills + mirror jobs
were silently overwriting vetted PDP content with lower-quality
re-extracts. Writer service is the structural fix; tests pin the
decision policy so the next "small refactor" can't accidentally
re-open the door to pollution.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import seed_data_writer as writer  # noqa: E402
from services.seed_data_writer import (  # noqa: E402
    FieldDecision,
    LOCKABLE_FIELDS,
    _build_merged_seed_data,
    _build_updated_lock,
    _decide_field,
    _hash_value,
    _score_field,
)


# ---------------------------------------------------------------------------
# _score_field
# ---------------------------------------------------------------------------


def test_score_field_empty_or_none_is_zero() -> None:
    """Empty / None / whitespace-only fields score 0 — the lowest
    possible — so any real content beats them."""
    assert _score_field("description", None) == 0.0
    assert _score_field("description", "") == 0.0
    assert _score_field("description", "   ") == 0.0


def test_score_field_length_drives_score() -> None:
    """Length is the primary signal: longer clean text scores higher."""
    short = _score_field("description", "Short text.")
    long = _score_field("description", "Short text." * 50)
    assert long > short


def test_score_field_html_entity_penalty() -> None:
    """A description with HTML entities scores LOWER than the same
    description cleaned. Otherwise the writer would prefer the dirty
    version when both have similar length."""
    clean = "Hydrate, plump & balance"  # 25 chars
    dirty = "Hydrate, plump &amp; balance"  # 29 chars but dirty
    assert _score_field("description", clean) > _score_field("description", dirty)


def test_score_field_shade_prefix_penalty_only_for_ingredient_fields() -> None:
    """Shade-name prefix is a known contamination pattern in
    pdp_ingredients_raw / raw_ingredient_text_clean. Penalize there
    but NOT in description (where a colon-led list is normal text)."""
    contaminated = "TWO'LIP KISS, SORTA $ELFISH: AQUA, GLYCERIN"
    clean = "AQUA, GLYCERIN"
    # Ingredient field penalizes
    assert _score_field("pdp_ingredients_raw", clean) > _score_field(
        "pdp_ingredients_raw", contaminated
    )
    # Description field does NOT penalize this pattern
    desc_score = _score_field("description", contaminated)
    desc_clean_score = _score_field("description", clean)
    assert desc_score > desc_clean_score  # length wins; no shade penalty


# ---------------------------------------------------------------------------
# _decide_field — the core policy
# ---------------------------------------------------------------------------


def _decision(
    *,
    field_name: str = "description",
    current_value: str = None,
    proposed_value: str = None,
    current_lock: Dict[str, Any] = None,
    proposed_audit_clean: bool = True,
) -> FieldDecision:
    return _decide_field(
        field_name=field_name,
        current_value=current_value,
        proposed_value=proposed_value,
        current_lock=current_lock or {},
        proposed_audit_clean=proposed_audit_clean,
    )


def test_decide_field_unlocked_field_merges_any_change() -> None:
    """Brand-new content into an unlocked field — always merges. The
    field will auto-lock on merge so subsequent writes get gated."""
    d = _decision(current_value=None, proposed_value="A clean description.")
    assert d.decision == "merge"
    assert "unlocked" in d.reason


def test_decide_field_no_change_is_no_op() -> None:
    """Proposed value identical to current → no_change. Never produces
    a write or relock noise."""
    d = _decision(
        current_value="Same text",
        proposed_value="Same text",
        current_lock={"description": {"hash": "...", "quality_score": 9.0}},
    )
    assert d.decision == "no_change"


def test_decide_field_locked_field_rejects_lower_score() -> None:
    """The whole point of the lock: a *worse* re-extract cannot
    overwrite a vetted field. Score is the gate."""
    long_clean = "A" * 500  # score 500
    short = "B" * 100  # score 100
    d = _decision(
        current_value=long_clean,
        proposed_value=short,
        current_lock={"description": {"hash": _hash_value(long_clean)}},
    )
    assert d.decision == "reject"
    assert "locked" in d.reason
    assert "score" in d.reason


def test_decide_field_locked_field_accepts_higher_score_clean() -> None:
    """A *better* re-extract (longer + audit-clean) beats the lock and
    re-locks. This is how recovery from a richer source replaces a
    polluted-but-locked value."""
    short = "Short description."  # score ~18
    long_clean = "A much longer, richer description with full detail." * 5
    d = _decision(
        current_value=short,
        proposed_value=long_clean,
        current_lock={"description": {"hash": _hash_value(short)}},
        proposed_audit_clean=True,
    )
    assert d.decision == "merge"
    assert "beats the lock" in d.reason or "beats lock" in d.reason


def test_decide_field_locked_field_rejects_audit_failing_proposal() -> None:
    """A re-extract that fails audit (e.g. has HTML entities) cannot
    win even if it is longer. Audit-pass is a prerequisite for
    overriding a lock."""
    short_clean = "Short clean text."
    long_dirty = ("Long description with &amp; entities " * 50)
    d = _decision(
        current_value=short_clean,
        proposed_value=long_dirty,
        current_lock={"description": {"hash": _hash_value(short_clean)}},
        proposed_audit_clean=False,
    )
    assert d.decision == "reject"
    assert "audit" in d.reason


# ---------------------------------------------------------------------------
# _build_merged_seed_data
# ---------------------------------------------------------------------------


def test_build_merged_seed_data_passes_through_non_lockable_fields() -> None:
    """Identity / non-content fields (price, image_url, brand) overwrite
    on every re-extract — we don't lock them."""
    current = {
        "description": "old desc",
        "brand": "Old Brand",
        "price_amount": 10.00,
    }
    proposed = {
        "description": "new desc (rejected by decision)",
        "brand": "New Brand",
        "price_amount": 12.00,
    }
    decisions = [
        FieldDecision(
            field="description",
            decision="reject",
            reason="locked",
            old_value="old desc",
            new_value="new desc",
            old_score=8.0,
            new_score=4.0,
        ),
    ]
    merged = _build_merged_seed_data(
        current_seed_data=current,
        proposed_seed_data=proposed,
        decisions=decisions,
    )
    # Non-lockable fields took the proposal values
    assert merged["brand"] == "New Brand"
    assert merged["price_amount"] == 12.00
    # Lockable field with reject decision kept current value
    assert merged["description"] == "old desc"


def test_build_merged_seed_data_applies_merge_decisions() -> None:
    """Lockable fields with merge decisions take the new value."""
    current = {"description": "old"}
    proposed = {"description": "new"}
    decisions = [
        FieldDecision(
            field="description",
            decision="merge",
            reason="unlocked",
            old_value="old",
            new_value="new",
            old_score=3.0,
            new_score=3.0,
        ),
    ]
    merged = _build_merged_seed_data(
        current_seed_data=current,
        proposed_seed_data=proposed,
        decisions=decisions,
    )
    assert merged["description"] == "new"


# ---------------------------------------------------------------------------
# _build_updated_lock
# ---------------------------------------------------------------------------


def test_build_updated_lock_records_hash_and_proposer() -> None:
    """A merged field auto-locks with hash, timestamp, proposer, score.
    This is what makes the next bad-write attempt rejectable."""
    decisions = [
        FieldDecision(
            field="description",
            decision="merge",
            reason="unlocked",
            old_value=None,
            new_value="A clean description.",
            old_score=0.0,
            new_score=20.0,
        ),
    ]
    lock = _build_updated_lock(
        current_lock={},
        decisions=decisions,
        proposer="test_proposer",
    )
    assert "description" in lock
    assert lock["description"]["hash"] == _hash_value("A clean description.")
    assert lock["description"]["locked_by"] == "test_proposer"
    assert lock["description"]["quality_score"] == 20.0
    assert "locked_at" in lock["description"]


def test_build_updated_lock_preserves_existing_locks_for_other_fields() -> None:
    """Locking field A must not unlock field B."""
    existing = {
        "pdp_ingredients_raw": {
            "hash": "sha256-existing",
            "locked_at": "2026-05-08T00:00:00+00:00",
            "locked_by": "earlier",
            "quality_score": 50.0,
        }
    }
    decisions = [
        FieldDecision(
            field="description",
            decision="merge",
            reason="unlocked",
            old_value=None,
            new_value="new desc",
            old_score=0.0,
            new_score=8.0,
        ),
    ]
    lock = _build_updated_lock(
        current_lock=existing,
        decisions=decisions,
        proposer="newer",
    )
    # Old lock preserved
    assert lock["pdp_ingredients_raw"]["hash"] == "sha256-existing"
    # New lock added
    assert "description" in lock


def test_build_updated_lock_skips_rejected_and_no_change_fields() -> None:
    """Reject / no_change decisions don't create new locks."""
    decisions = [
        FieldDecision(field="description", decision="reject", reason="x",
                      old_value="a", new_value="b", old_score=1, new_score=0),
        FieldDecision(field="title", decision="no_change", reason="y",
                      old_value="t", new_value="t", old_score=1, new_score=1),
    ]
    lock = _build_updated_lock(current_lock={}, decisions=decisions, proposer="p")
    assert lock == {}


# ---------------------------------------------------------------------------
# upsert_seed_data — integration with mocked database
# ---------------------------------------------------------------------------


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDB:
    """Minimal stand-in for db.database.database. Captures every
    execute() call so tests can assert the writer set the token, did
    the right UPDATE, and updated the proposal status."""

    def __init__(self, current_row=None, next_proposal_id: int = 1) -> None:
        self.current_row = current_row
        self.next_proposal_id = next_proposal_id
        self.executed: List[Dict[str, Any]] = []

    async def fetch_one(self, sql: str, params: Dict[str, Any]):
        if "RETURNING id" in sql and "INSERT INTO seed_data_proposals" in sql:
            self.executed.append({"sql": sql, "params": params, "kind": "insert_proposal"})
            pid = self.next_proposal_id
            self.next_proposal_id += 1
            return {"id": pid}
        if "FROM external_product_seeds" in sql and "WHERE id = :seed_id" in sql:
            return self.current_row
        return None

    async def execute(self, sql: str, params: Dict[str, Any] = None) -> int:
        self.executed.append({"sql": sql, "params": params or {}, "kind": "execute"})
        return 1

    def transaction(self):
        return _FakeTxn()


def _install_fake_db(monkeypatch, fake: _FakeDB) -> None:
    monkeypatch.setattr(writer, "database", fake)


@pytest.mark.asyncio
async def test_upsert_writes_proposal_then_applies_merge_with_token(monkeypatch) -> None:
    """Happy path: unlocked row, clean proposal. Writer should:
      1. Insert into seed_data_proposals (status='merged' upfront since
         we know the merge will happen)
      2. Open transaction, SET LOCAL pivota.seed_write_token, UPDATE
         external_product_seeds, mark proposal merged."""
    fake = _FakeDB(current_row={
        "seed_data": {"description": "old desc"},
        "content_lock": {},
    })
    _install_fake_db(monkeypatch, fake)

    proposal = {
        "description": "A new, much richer description with detail.",
        "brand": "Glow Recipe",
    }
    result = await writer.upsert_seed_data(
        seed_id="eps_1",
        external_product_id="ext_1",
        proposed_seed_data=proposal,
        proposer="test_caller",
        source="test",
    )

    # Verify the writer actually merged
    assert result.status == "merged"
    assert result.merged_field_count() >= 1
    # Find the proposal insert + UPDATE + token SET in execution order
    sqls = [e["sql"] for e in fake.executed]
    sqls_joined = "\n".join(sqls)
    assert "INSERT INTO seed_data_proposals" in sqls_joined
    assert "SET LOCAL pivota.seed_write_token" in sqls_joined
    assert "UPDATE external_product_seeds" in sqls_joined
    # Token must be SET *before* the UPDATE (so trigger sees it)
    set_idx = next(i for i, s in enumerate(sqls) if "SET LOCAL pivota.seed_write_token" in s)
    upd_idx = next(i for i, s in enumerate(sqls) if "UPDATE external_product_seeds" in s)
    assert set_idx < upd_idx, "token must be set before UPDATE so trigger lets it through"


@pytest.mark.asyncio
async def test_upsert_rejects_lower_quality_proposal_against_locked_field(monkeypatch) -> None:
    """The pollution scenario: codex re-backfill produces a SHORT
    description. Field is locked. Writer must reject — the locked
    long description survives."""
    long_locked = "A" * 800
    fake = _FakeDB(current_row={
        "seed_data": {"description": long_locked},
        "content_lock": {
            "description": {
                "hash": _hash_value(long_locked),
                "locked_at": "2026-05-01T00:00:00+00:00",
                "locked_by": "earlier_audit",
                "quality_score": 800.0,
            }
        },
    })
    _install_fake_db(monkeypatch, fake)

    proposal = {"description": "Short re-extract."}
    result = await writer.upsert_seed_data(
        seed_id="eps_1",
        external_product_id="ext_1",
        proposed_seed_data=proposal,
        proposer="codex_backfill",
    )

    assert result.status == "rejected"
    # Critically: NO UPDATE to external_product_seeds
    update_calls = [e for e in fake.executed if "UPDATE external_product_seeds" in e["sql"]]
    assert update_calls == [], "rejected proposal must NOT touch external_product_seeds"
    # But the proposal row was still inserted (forensic record)
    assert any("INSERT INTO seed_data_proposals" in e["sql"] for e in fake.executed)


@pytest.mark.asyncio
async def test_upsert_accepts_higher_quality_recovery_against_locked_field(monkeypatch) -> None:
    """The recovery scenario: archive_20260506 has the original good
    long description; current value is the polluted short version.
    Lock on current is weak (low score) — the recovery proposal beats
    it on length AND audit-pass. Writer should merge."""
    short_polluted = "Short polluted desc."
    fake = _FakeDB(current_row={
        "seed_data": {"description": short_polluted},
        "content_lock": {
            "description": {
                "hash": _hash_value(short_polluted),
                "locked_at": "2026-05-09T00:00:00+00:00",
                "locked_by": "audit_after_pollution",
                "quality_score": _score_field("description", short_polluted),
            }
        },
    })
    _install_fake_db(monkeypatch, fake)

    long_clean = ("Hydrate, plump and balance skin with our whipped gel cream "
                  "moisturizer that fills in fine lines while delivering 24-hour "
                  "hydration. " * 8)
    proposal = {"description": long_clean}
    result = await writer.upsert_seed_data(
        seed_id="eps_1",
        external_product_id="ext_1",
        proposed_seed_data=proposal,
        proposer="recovery_archive_20260506",
        source="archive_restore",
    )

    assert result.status == "merged"
    # Field gets re-locked with the new (higher) score
    update_calls = [e for e in fake.executed if "UPDATE external_product_seeds" in e["sql"]]
    assert len(update_calls) == 1
    payload = json.loads(update_calls[0]["params"]["content_lock"])
    assert payload["description"]["locked_by"] == "recovery_archive_20260506"
    assert payload["description"]["quality_score"] > _score_field("description", short_polluted)


@pytest.mark.asyncio
async def test_upsert_no_op_when_proposal_identical_to_current(monkeypatch) -> None:
    """Re-running a backfill that produces the exact same content on
    every field — lockable AND non-lockable — yields status='no_change'
    and no UPDATE on external_product_seeds. Proposal row IS still
    inserted for forensic completeness ('codex tried, content already
    correct'). 'no_change' must be in the seed_data_proposals.status
    CHECK constraint (migration 082)."""
    fake = _FakeDB(current_row={
        "seed_data": {"description": "Same text."},
        "content_lock": {},
    })
    _install_fake_db(monkeypatch, fake)

    result = await writer.upsert_seed_data(
        seed_id="eps_1",
        external_product_id="ext_1",
        proposed_seed_data={"description": "Same text."},
        proposer="idempotent_caller",
    )

    assert result.status == "no_change"
    # No UPDATE on external_product_seeds when nothing changed
    update_calls = [e for e in fake.executed if "UPDATE external_product_seeds" in e["sql"]]
    assert update_calls == []
    # Proposal row still inserted (forensic record)
    proposal_inserts = [e for e in fake.executed if "INSERT INTO seed_data_proposals" in e["sql"]]
    assert len(proposal_inserts) == 1


@pytest.mark.asyncio
async def test_upsert_writes_when_only_non_lockable_fields_change(monkeypatch) -> None:
    """Regression 2026-05-12: codex's MAC recovery proposal only
    touched non-lockable fields (canonical_url, image_url, variants —
    skipping description which was already correct). The original
    writer marked this as 'no_change' which failed the
    seed_data_proposals.status CHECK constraint and bombed before any
    write. After fix: this case correctly yields status='merged',
    inserts the proposal as 'merged' upfront, and applies the
    non-lockable changes to external_product_seeds."""
    fake = _FakeDB(current_row={
        "seed_data": {
            "description": "Lipstick in Russian Red.",  # lockable, unchanged
            "image_url": "https://cdn/old.jpg",          # NON-lockable, changing
            "price_amount": 22.00,                         # NON-lockable, changing
        },
        "content_lock": {},
    })
    _install_fake_db(monkeypatch, fake)

    proposal = {
        "description": "Lipstick in Russian Red.",   # identical
        "image_url": "https://cdn/new.jpg",            # new image
        "price_amount": 24.00,                          # new price
        "variants": [{"sku": "v1"}, {"sku": "v2"}],   # new variants
    }
    result = await writer.upsert_seed_data(
        seed_id="eps_mac",
        external_product_id="mac_russian_red",
        proposed_seed_data=proposal,
        proposer="recovery_archive_20260506",
        source="archive_restore",
    )

    assert result.status == "merged"
    # The UPDATE must fire (non-lockable fields changed)
    update_calls = [e for e in fake.executed if "UPDATE external_product_seeds" in e["sql"]]
    assert len(update_calls) == 1
    persisted = json.loads(update_calls[0]["params"]["seed_data"])
    assert persisted["image_url"] == "https://cdn/new.jpg"
    assert persisted["price_amount"] == 24.00
    assert persisted["variants"] == [{"sku": "v1"}, {"sku": "v2"}]
    # Lockable field unchanged (no merge decision fired for it)
    assert persisted["description"] == "Lipstick in Russian Red."
    # Proposal was inserted with status='merged' (not 'no_change') —
    # otherwise the CHECK constraint would reject the INSERT.
    proposal_inserts = [e for e in fake.executed if "INSERT INTO seed_data_proposals" in e["sql"]]
    assert len(proposal_inserts) == 1
    assert proposal_inserts[0]["params"]["status"] == "merged"


@pytest.mark.asyncio
async def test_upsert_audits_and_cleans_proposal_before_decision(monkeypatch) -> None:
    """The proposal goes through audit_seed_data first — HTML entities
    and shade prefixes are cleaned before scoring. Otherwise a dirty
    proposal would lose to a clean current value purely on entity
    penalty, even if its underlying content is identical."""
    fake = _FakeDB(current_row={
        "seed_data": {"description": "A clean description."},
        "content_lock": {},
    })
    _install_fake_db(monkeypatch, fake)

    # Same content but with HTML entity — auditor should decode it
    proposal = {"description": "A clean description &amp; more."}
    result = await writer.upsert_seed_data(
        seed_id="eps_1",
        external_product_id="ext_1",
        proposed_seed_data=proposal,
        proposer="cleanup",
    )

    # The persisted seed_data must NOT contain &amp;
    update_calls = [e for e in fake.executed if "UPDATE external_product_seeds" in e["sql"]]
    if update_calls:
        persisted = json.loads(update_calls[0]["params"]["seed_data"])
        assert "&amp;" not in persisted["description"]


def test_lockable_fields_covers_known_content_fields() -> None:
    """Pin the lock surface — if someone adds a new content field to
    seed_data, they need to consciously decide whether it's lockable."""
    expected_minimum = {"title", "description", "pdp_ingredients_raw",
                        "pdp_how_to_use_raw"}
    assert expected_minimum.issubset(set(LOCKABLE_FIELDS))
