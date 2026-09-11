"""The crawl lane wrote a PROVENANCE string into the column the agent door filters recall on.

`external_product_seeds.tool` is a recall scope. PIVOTA-Agent's external-seed arm accepts exactly
`shopping_agents`, `creator_agents` and `*` (src/server.js:16851-16857; the legacy `''` scope sits
behind PIVOT_BEAUTY_LEGACY_TOOL_SCOPE_RECALL_ENABLED, default false). This lane wrote
`external_brand_crawl`, so every row it ever onboarded was invisible to that arm — a halving of
recall, not a blackout, because the canonical arm has no `tool` filter. That is why it survived.

The fix is not "change TOOL". TOOL is also `_seed_id`'s prefix (the seed's PRIMARY KEY) and the
`source_system`/`reason` provenance stamp, so changing it would re-key every seed this lane has
written. These tests pin the separation, in both directions — a future edit that re-merges the two
meanings fails here whichever way it merges them.
"""

from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "scripts/onboard_external_brand_from_crawl.py"

# The gateway's accept-list, transcribed. `''` is deliberately NOT here: it is admitted only when
# PIVOT_BEAUTY_LEGACY_TOOL_SCOPE_RECALL_ENABLED is on, and it defaults off.
_DOOR_ACCEPTS = {"shopping_agents", "creator_agents", "*"}


def _literal(name: str):
    tree = ast.parse(_SRC.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module-level literal in {_SRC.name}")


def test_the_tool_this_lane_writes_is_one_the_door_accepts():
    scope = _literal("SEED_TOOL_SCOPE")
    assert scope in _DOOR_ACCEPTS, (
        "this lane writes tool=%r, which PIVOTA-Agent's external-seed recall arm does not accept "
        "(%s). Every product onboarded by this script would be invisible to that arm — and "
        "visible via the canonical arm, so search would still return SOMETHING and nothing would "
        "look broken." % (scope, sorted(_DOOR_ACCEPTS))
    )


def test_provenance_and_recall_scope_are_still_different_constants():
    """The bug was ONE constant meaning two things. Re-merging them is the regression."""
    assert _literal("TOOL") != _literal("SEED_TOOL_SCOPE"), (
        "TOOL and SEED_TOOL_SCOPE are the same value again. TOOL is identity and provenance "
        "(`_seed_id` builds the seed PRIMARY KEY from it); SEED_TOOL_SCOPE is what the door "
        "filters on. Setting them equal either re-keys every seed this lane has written, or "
        "puts the provenance string back in front of the door."
    )


def test_the_seed_primary_key_is_still_built_from_the_PROVENANCE_constant():
    """The other direction: 'fixing' this by pointing `_seed_id` at the scope constant would
    silently re-key every seed — `*::<epid>` instead of `external_brand_crawl::<epid>` — and the
    upsert would then insert duplicates rather than update."""
    src = _SRC.read_text()
    start = src.index("def _seed_id(")
    body = src[start:src.index("\n\n", start)]
    assert "TOOL" in body and "SEED_TOOL_SCOPE" not in body, (
        "_seed_id no longer derives from TOOL alone:\n%s" % body
    )


def test_the_seed_insert_binds_the_scope_constant_not_the_provenance_one():
    """Pins the actual call site, not just the constants. The defect was one keyword argument."""
    src = _SRC.read_text()
    assert '"tool": SEED_TOOL_SCOPE,' in src, (
        "the seed upsert no longer binds SEED_TOOL_SCOPE to `tool`. If it binds TOOL again, every "
        "onboarded row goes back outside the door's recall arm."
    )
    assert '"tool": TOOL,' not in src, "the seed upsert binds the provenance constant to `tool`"
