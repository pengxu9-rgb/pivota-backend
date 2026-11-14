from dataclasses import asdict, replace
import json

import pytest

from services.external_seed_model_review import (
    ReviewReceipt,
    ReviewRow,
    ReviewTarget,
    build_review_snapshot,
    review_receipt_from_dict,
    review_receipt_to_dict,
    review_seed,
)

MODELS = ("provider/reviewer-a", "provider/reviewer-b", "provider/adjudicator")
COCO_BRAND = ReviewTarget("41341731438667", "brand_direct")
MAUVE_BRAND = ReviewTarget("41341731635275", "brand_direct")
COCO_RETAIL = ReviewTarget(
    "41341731438667", "exact_retailer_offer", "example.com:offer-1"
)
TARGETS = (COCO_BRAND, COCO_RETAIL, MAUVE_BRAND)


def packet(barcode, targets):
    return {
        "sources": {
            "brand-feed": {"native_barcode": barcode},
            "retailer-feed": {"native_barcode": "8809625246082"},
        },
        "target_sources": [
            asdict(target)
            | {
                "evidence_refs": [
                    "brand-feed" if target.scope == "brand_direct" else "retailer-feed"
                ]
            }
            for target in targets
        ],
    }


def snapshot(*, seed_id="seed-1", price=14, barcode="8800258082894", targets=TARGETS):
    return build_review_snapshot(
        seed_id=seed_id,
        seed_snapshot={"market": "US", "price": price},
        evidence_packet=packet(barcode, targets),
        expected_targets=targets,
    )


def receipt(current, stage, rows):
    index = ("primary", "secondary", "tie_break").index(stage)
    return ReviewReceipt(
        stage,
        f"run-{stage}",
        MODELS[index],
        current.seed_id,
        current.seed_digest,
        current.evidence_digest,
        tuple(rows),
    )


def bases(current, *, hold_coco=False):
    agree_rows = (
        ReviewRow(COCO_BRAND, "employee_review", ("brand-feed",)),
        ReviewRow(COCO_RETAIL, "hold", ("retailer-feed",)),
        ReviewRow(MAUVE_BRAND, "employee_review", ("brand-feed",)),
    )
    secondary_rows = (
        (ReviewRow(COCO_BRAND, "hold"), *agree_rows[1:]) if hold_coco else agree_rows
    )
    return receipt(current, "primary", reversed(agree_rows)), receipt(
        current, "secondary", secondary_rows
    )


def decide(current, first=None, second=None, third=None):
    return review_seed(
        current,
        expected_model_names=MODELS,
        primary_receipt=first,
        secondary_receipt=second,
        tie_break_receipt=third,
    )


def test_agreement_does_not_request_a_third_model_and_keeps_scopes_separate():
    current = snapshot()
    first, second = bases(current)
    result = decide(current, first, second)

    assert result.state == "completed"
    assert not result.tie_break_targets
    assert result.run_ids == (first.run_id, second.run_id)
    assert {row.target: (row.action, row.decided_by) for row in result.decisions} == {
        COCO_BRAND: ("employee_review", "agreement"),
        MAUVE_BRAND: ("employee_review", "agreement"),
        COCO_RETAIL: ("hold", "agreement"),
    }


def test_tie_break_uses_only_action_disagreement_and_does_not_change_other_scopes():
    current = snapshot()
    first, second = bases(current, hold_coco=True)
    pending = decide(current, first, second)
    assert pending.state == "requires_tie_break"
    assert pending.tie_break_targets == (COCO_BRAND,)
    assert {row.target: row.action for row in pending.decisions}[
        COCO_BRAND
    ] == "requires_tie_break"

    third = receipt(
        current,
        "tie_break",
        [ReviewRow(COCO_BRAND, "employee_review", ("brand-feed",))],
    )
    result = decide(current, first, second, third)
    assert result.state == "completed"
    assert result.tie_break_targets == ()
    assert result.run_ids == (first.run_id, second.run_id, third.run_id)
    assert {row.target: (row.action, row.decided_by) for row in result.decisions} == {
        COCO_BRAND: ("employee_review", "tie_break"),
        MAUVE_BRAND: ("employee_review", "agreement"),
        COCO_RETAIL: ("hold", "agreement"),
    }


def test_tie_break_can_adopt_hold():
    current = snapshot()
    first, second = bases(current, hold_coco=True)
    third = receipt(current, "tie_break", [ReviewRow(COCO_BRAND, "hold")])
    result = decide(current, first, second, third)
    assert (
        next(row for row in result.decisions if row.target == COCO_BRAND).action
        == "hold"
    )


def test_partial_review_never_produces_a_decision():
    current = snapshot()
    first, _ = bases(current)
    result = decide(current, first)
    assert result.state == "awaiting_review"
    assert not result.decisions
    assert not result.tie_break_targets


@pytest.mark.parametrize(
    "changed",
    [
        dict(price=15),
        dict(barcode="other"),
        dict(seed_id="other"),
        dict(targets=TARGETS[:2]),
    ],
)
def test_receipts_are_bound_to_exact_seed_evidence_and_target_roster(changed):
    old = snapshot()
    first, second = bases(old)
    result = decide(snapshot(**changed), first, second)
    assert result.state == "invalid_receipt"
    assert not result.decisions
    assert "does not match" in result.error


def test_target_order_and_json_key_order_do_not_change_digests():
    reordered = build_review_snapshot(
        seed_id="seed-1",
        seed_snapshot={"price": 14, "market": "US"},
        evidence_packet=packet("8800258082894", TARGETS),
        expected_targets=list(reversed(TARGETS)),
    )
    assert reordered == snapshot()


@pytest.mark.parametrize(
    "invalid_row",
    [
        ReviewRow(COCO_BRAND, "employee_review"),
        ReviewRow(COCO_BRAND, "employee_review", ("unknown-source",)),
        ReviewRow(COCO_BRAND, "hold", ("unknown-source",)),
        ReviewRow(COCO_BRAND, "publish", ("brand-feed",)),
        ReviewRow(
            ReviewTarget("41341731438667", "exact_retailer_offer", "other"), "hold"
        ),
        ReviewRow(COCO_RETAIL, "hold"),
    ],
)
def test_invalid_or_unbound_row_does_not_generate_any_decision(invalid_row):
    current = snapshot()
    first, second = bases(current)
    result = decide(
        current, first, replace(second, rows=(invalid_row, *second.rows[1:]))
    )
    assert result.state == "invalid_receipt"
    assert not result.decisions


@pytest.mark.parametrize(
    "changed",
    [
        dict(stage="primary"),
        dict(model_name=MODELS[0]),
        dict(run_id="run-primary"),
        dict(run_id=""),
    ],
)
def test_stage_model_and_run_identity_are_validated(changed):
    current = snapshot()
    first, second = bases(current)
    assert decide(current, first, replace(second, **changed)).state == "invalid_receipt"


def test_base_receipt_requires_complete_coverage():
    current = snapshot()
    first, second = bases(current)
    assert (
        decide(current, first, replace(second, rows=second.rows[:2])).state
        == "invalid_receipt"
    )


def test_tie_break_is_rejected_without_disagreement_or_if_it_covers_extra_targets():
    current = snapshot()
    first, second = bases(current)
    third = receipt(current, "tie_break", [ReviewRow(COCO_BRAND, "hold")])
    assert decide(current, first, second, third).state == "invalid_receipt"
    assert decide(current, first, None, third).state == "invalid_receipt"

    first, second = bases(current, hold_coco=True)
    too_many = replace(third, rows=(*third.rows, ReviewRow(COCO_RETAIL, "hold")))
    assert decide(current, first, second, too_many).state == "invalid_receipt"
    wrong_scope = replace(third, rows=(ReviewRow(COCO_RETAIL, "hold"),))
    assert decide(current, first, second, wrong_scope).state == "invalid_receipt"
    assert (
        decide(current, first, second, replace(third, run_id=first.run_id)).state
        == "invalid_receipt"
    )
    assert (
        decide(current, first, second, replace(third, model_name=MODELS[0])).state
        == "invalid_receipt"
    )


@pytest.mark.parametrize(
    "bad_targets",
    [
        (),
        (COCO_BRAND, COCO_BRAND),
        (ReviewTarget("41341731438667", "exact_retailer_offer"),),
    ],
)
def test_snapshot_rejects_invalid_target_roster(bad_targets):
    with pytest.raises(ValueError):
        snapshot(targets=bad_targets)


def test_configured_reviewers_must_be_three_distinct_models():
    with pytest.raises(ValueError, match="distinct"):
        review_seed(snapshot(), expected_model_names=(MODELS[0], MODELS[0], MODELS[2]))


def test_other_scope_evidence_cannot_support_retailer_approval():
    current = snapshot()
    first, second = bases(current)
    changed = replace(
        second,
        rows=(
            second.rows[0],
            ReviewRow(COCO_RETAIL, "employee_review", ("brand-feed",)),
            second.rows[2],
        ),
    )
    assert decide(current, first, changed).state == "invalid_receipt"


def test_unscoped_packet_rejected_if_retailer_offer_is_in_scope():
    with pytest.raises(ValueError, match="Retailer evidence"):
        build_review_snapshot(
            seed_id="seed-1",
            seed_snapshot={"market": "US"},
            evidence_packet={"sources": {"public": {}}},
            expected_targets=TARGETS,
        )


def test_archived_receipt_round_trips_in_json_and_is_revalidated():
    current = snapshot()
    first, second = bases(current)
    archive = json.loads(json.dumps(review_receipt_to_dict(first)))
    assert review_receipt_from_dict(archive) == first
    archive["rows"][0]["evidence_refs"] = ["unknown-source"]
    assert (
        decide(current, review_receipt_from_dict(archive), second).state
        == "invalid_receipt"
    )
