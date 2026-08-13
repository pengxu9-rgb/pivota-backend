"""pick_matches is the safety core of enrichment re-attribution — pin it.

A wrong re-attribution publishes one product's curated claims, disclaimers and
selling copy onto a DIFFERENT product's PDP. That is strictly worse than the
gap it repairs, so the matcher must be conservative: hypothesis priority (the
strongest evidence wins, never a vote), ambiguity rejection within a
hypothesis, and bilateral-uniqueness across targets. Each rule here is verified
to kill its own mutant; see the commit message.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest  # noqa: F401 — parametrize below

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.reattribute_orphaned_enrichment import pick_matches  # noqa: E402


def _hit(old: str, new: str, hyp: str, merchant: str = "m1") -> Dict[str, Any]:
    return {"merchant_id": merchant, "platform": "external_seed",
            "old_id": old, "new_id": new, "hypothesis": hyp,
            "content_key": f"ck-{new}", "title": f"T {new}"}


def test_a_single_match_is_accepted() -> None:
    accepted, rejected = pick_matches([_hit("old-1", "new-1", "H1_seed_external_id")])
    assert [(m["old_id"], m["new_id"]) for m in accepted] == [("old-1", "new-1")]
    assert rejected == []


def test_hypothesis_priority_wins_over_list_order() -> None:
    """H1 evidence (seed's own external id) beats H4 (content fingerprint) even
    when they disagree — the stronger identity evidence decides, never a vote
    and never whichever the query happened to return first. The H4 hit is
    deliberately FIRST in the list to catch a naive hits[0] pick."""
    accepted, rejected = pick_matches([
        _hit("old-1", "new-WRONG", "H4_surviving_apv_bullets"),
        _hit("old-1", "new-RIGHT", "H1_seed_external_id"),
    ])
    assert len(accepted) == 1
    assert accepted[0]["new_id"] == "new-RIGHT"
    assert rejected == []


def test_two_targets_within_one_hypothesis_is_ambiguous() -> None:
    accepted, rejected = pick_matches([
        _hit("old-1", "new-A", "H2_seed_url_slug"),
        _hit("old-1", "new-B", "H2_seed_url_slug"),
    ])
    assert accepted == []
    assert len(rejected) == 1 and "ambiguous" in rejected[0]["reason"]


def test_two_orphans_claiming_one_target_are_both_rejected() -> None:
    """Publishing two products' curated claims onto one row is exactly the
    failure the bilateral rule blocks — neither side can be trusted, so
    NEITHER is applied, rather than picking a winner."""
    accepted, rejected = pick_matches([
        _hit("old-1", "new-shared", "H1_seed_external_id"),
        _hit("old-2", "new-shared", "H1_seed_external_id"),
    ])
    assert accepted == []
    assert len(rejected) == 2
    assert all("collision" in r["reason"] for r in rejected)


def test_a_weak_hypothesis_alone_still_matches() -> None:
    accepted, _ = pick_matches([_hit("old-1", "new-1", "H4_surviving_apv_bullets")])
    assert len(accepted) == 1


def test_duplicate_hits_for_the_same_pair_are_not_ambiguous() -> None:
    """H1 and H2 often re-derive the SAME pair (the seed's external id also
    appears in its URL). Agreement is corroboration, not ambiguity."""
    accepted, rejected = pick_matches([
        _hit("old-1", "new-1", "H1_seed_external_id"),
        _hit("old-1", "new-1", "H2_seed_url_slug"),
    ])
    assert len(accepted) == 1 and accepted[0]["new_id"] == "new-1"
    assert rejected == []


def test_merchants_are_isolated() -> None:
    """The same slug under two merchants is two separate decisions — neither a
    collision nor corroboration."""
    accepted, rejected = pick_matches([
        _hit("old-x", "new-1", "H1_seed_external_id", merchant="m1"),
        _hit("old-x", "new-2", "H1_seed_external_id", merchant="m2"),
    ])
    assert len(accepted) == 2
    assert rejected == []
