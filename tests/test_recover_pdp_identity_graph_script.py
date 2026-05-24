from __future__ import annotations

import argparse

import pytest

import scripts.recover_pdp_identity_graph as module


def _proposal(**overrides):
    row = {
        "action": "upsert_product_group_member",
        "reason": "ext_identity_attached_key_group_member",
        "product_key": "prod::external_seed::external_seed::ext_1",
        "product_group_id": "pg_ext_1",
        "source_product_id": "ext_1",
        "seed_id": "seed_1",
        "high_confidence": True,
    }
    row.update(overrides)
    return row


def _args(**overrides):
    values = {
        "apply": False,
        "dry_run": True,
        "limit": 500,
        "offset": 0,
        "proposer": module.DEFAULT_PROPOSER,
        "reason_allowlist": [],
        "action_allowlist": [],
        "max_apply": None,
        "confirm": "",
        "export_path": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_select_recovery_proposals_filters_reason_and_action() -> None:
    selected, selection = module.select_recovery_proposals(
        [
            _proposal(),
            _proposal(reason="internal_product_missing_group", product_key="prod::m::shopify::1"),
            _proposal(action="repair_external_seed_attachment", source_product_id="ext_2"),
            _proposal(high_confidence=False, source_product_id="ext_3"),
        ],
        reason_allowlist=["ext_identity_attached_key_group_member"],
        action_allowlist=["upsert_product_group_member"],
    )

    assert [row["source_product_id"] for row in selected] == ["ext_1"]
    assert selection["high_confidence_count"] == 3
    assert selection["selected_count"] == 1
    assert selection["unselected_reason_counts"] == {
        "ext_identity_attached_key_group_member": 1,
        "internal_product_missing_group": 1,
    }
    assert selection["unselected_skip_counts"] == {
        "action_not_allowed": 1,
        "reason_not_allowed": 1,
    }


def test_select_recovery_proposals_applies_stable_max_cap() -> None:
    selected, selection = module.select_recovery_proposals(
        [
            _proposal(product_group_id="pg_ext_b", source_product_id="ext_b"),
            _proposal(product_group_id="pg_ext_a", source_product_id="ext_a"),
        ],
        reason_allowlist=["ext_identity_attached_key_group_member"],
        max_apply=1,
    )

    assert [row["source_product_id"] for row in selected] == ["ext_a"]
    assert selection["selected_before_truncation"] == 2
    assert selection["selected_count"] == 1
    assert selection["selection_truncated"] is True
    assert selection["unselected_skip_counts"] == {"max_apply_cap": 1}


@pytest.mark.asyncio
async def test_apply_requires_confirm_token_before_db_connection() -> None:
    with pytest.raises(SystemExit, match=module.CONFIRM_TOKEN):
        await module._run(_args(apply=True, dry_run=False, confirm="WRONG"))
