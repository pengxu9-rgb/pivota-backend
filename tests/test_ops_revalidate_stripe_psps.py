import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ops_revalidate_stripe_psps import (  # noqa: E402
    _load_psp_ids_from_report,
    _should_target_row,
)


def test_load_psp_ids_from_report_reads_changed_rows_from_json_payload(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "summary": {"changed": 2},
                "rows": [
                    {"psp_id": "psp_1", "changed": True},
                    {"psp_id": "psp_1", "changed": True},
                    {"psp_id": "psp_2", "changed": False},
                    {"psp_id": "psp_3", "changed": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert _load_psp_ids_from_report(report_path) == ["psp_1", "psp_3"]


def test_load_psp_ids_from_report_reads_changed_rows_from_jsonl_payload(tmp_path: Path) -> None:
    report_path = tmp_path / "report.jsonl"
    report_path.write_text(
        "\n".join(
            [
                json.dumps({"psp_id": "psp_a", "changed": True}),
                json.dumps({"psp_id": "psp_b", "changed": False}),
                json.dumps({"psp_id": "psp_c", "changed": True}),
            ]
        ),
        encoding="utf-8",
    )

    assert _load_psp_ids_from_report(report_path) == ["psp_a", "psp_c"]


def test_should_target_row_skips_ready_live_row_by_default() -> None:
    report = {
        "environment": "live",
        "validation_status": "valid",
        "webhook_ready": True,
        "live_charge_ready": True,
    }

    assert _should_target_row(report, include_test=False, include_ready=False) is False


def test_should_target_row_targets_live_row_missing_webhook() -> None:
    report = {
        "environment": "live",
        "validation_status": "valid",
        "webhook_ready": False,
        "live_charge_ready": False,
    }

    assert _should_target_row(report, include_test=False, include_ready=False) is True


def test_should_target_row_can_include_test_rows_when_requested() -> None:
    report = {
        "environment": "test",
        "validation_status": "invalid",
        "webhook_ready": False,
        "live_charge_ready": False,
    }

    assert _should_target_row(report, include_test=False, include_ready=False) is False
    assert _should_target_row(report, include_test=True, include_ready=False) is True
