"""The per-host apply gate must pass a clean apply and stop every other kind.

The fixture is the REAL eyurs log from the Pyunkang Yul canary re-ingest (2026-09-18, job
oneoff-61425-664): a clean apply that the old shell gate — the last `"status"` token in the log —
stopped, because that token is the report's outer `"applied"`, not the nested readiness
`"complete"`. Every negative case below is that same log with exactly one thing made wrong, so each
test isolates one reason and a mutant that drops a check has a test that notices.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.curated_apply_gate import MARKER, evaluate_apply_log, main

FIXTURE = Path(__file__).parent / "fixtures" / "curated_apply_gate" / "eyurs_clean_apply_2026_09_18.log"


def _clean_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _with_post_apply(mutate) -> str:
    """The clean log with the POST-APPLY report (the one carrying `applied`) edited by `mutate`."""
    lines = _clean_text().splitlines()
    for index, line in enumerate(lines):
        if MARKER not in line:
            continue
        head, _, body = line.partition(MARKER)
        report = json.loads(body)
        if isinstance(report.get("applied"), dict):
            mutate(report)
            lines[index] = head + MARKER + json.dumps(report, sort_keys=True)
            return "\n".join(lines)
    raise AssertionError("fixture has no post-apply report")


def test_the_real_clean_apply_passes():
    verdict = evaluate_apply_log(_clean_text())

    assert verdict["ok"] is True, verdict["reasons"]
    assert verdict["reasons"] == []
    assert verdict["apply_status"] == "applied"
    assert verdict["readiness_status"] == "complete"
    assert verdict["product_keys"] == ["ext:retailer:1aed0be4ea653d0b3a0d8557ecf6a2c5"]
    assert verdict["applied"]["offers"] == 2


def test_the_old_shell_gate_would_have_stopped_this_same_clean_log():
    """Pins the defect this module replaces: the last `"status"` token IS the outer `"applied"`.

    If the report format ever changes so that this stops being true, this test fails, and whoever
    touches the format learns why the gate reads the nested readiness instead."""
    tokens = re.findall(r'"status": "([a-z_]*)"', _clean_text())
    assert tokens[-1] == "applied"
    assert "complete" in tokens
    assert evaluate_apply_log(_clean_text())["ok"] is True


def test_a_dry_run_has_no_post_apply_report_and_does_not_pass():
    plan_only = "\n".join(
        line for line in _clean_text().splitlines()
        if not (MARKER in line and '"applied"' in line)
    )
    verdict = evaluate_apply_log(plan_only)

    assert verdict["ok"] is False
    assert "no_post_apply_report" in verdict["reasons"]
    assert verdict["apply_status"] == "ready_to_apply", "the plan line is still read and reported"


@pytest.mark.parametrize("text", ["", "   \n", "nothing relevant here\n"])
def test_an_empty_or_irrelevant_log_does_not_pass(text):
    verdict = evaluate_apply_log(text)
    assert verdict["ok"] is False
    assert verdict["reasons"] == ["no_post_apply_report"]


@pytest.mark.parametrize("status", ["incomplete", "failed", "pending", None])
def test_readiness_other_than_complete_stops_the_run(status):
    def mutate(report):
        report["applied"]["primary_readiness"]["status"] = status

    verdict = evaluate_apply_log(_with_post_apply(mutate))
    assert verdict["ok"] is False
    assert f"readiness_{status or 'missing'}" in verdict["reasons"]


def test_a_missing_readiness_block_stops_the_run():
    verdict = evaluate_apply_log(_with_post_apply(lambda r: r["applied"].pop("primary_readiness")))
    assert verdict["ok"] is False
    assert "readiness_missing" in verdict["reasons"]


def test_an_outer_status_other_than_applied_stops_the_run():
    def mutate(report):
        report["status"] = "partial"

    verdict = evaluate_apply_log(_with_post_apply(mutate))
    assert verdict["ok"] is False
    assert "apply_status_partial" in verdict["reasons"]


@pytest.mark.parametrize("kind", ["offers", "pdps", "skus"])
def test_anything_planned_but_not_written_stops_the_run(kind):
    def mutate(report):
        report["missing"][kind] = 1

    verdict = evaluate_apply_log(_with_post_apply(mutate))
    assert verdict["ok"] is False
    assert f"missing_{kind}" in verdict["reasons"]


def test_absent_missing_counts_are_not_read_as_zero():
    verdict = evaluate_apply_log(_with_post_apply(lambda r: r.pop("missing")))
    assert verdict["ok"] is False
    assert "missing_counts_absent" in verdict["reasons"]


@pytest.mark.parametrize(
    "key",
    ["product_groups_failed", "skus_identity_conflict", "pdps_skipped_identity", "offers_dropped_for_refused_sku"],
)
def test_an_identity_or_group_failure_stops_the_run(key):
    def mutate(report):
        report["applied"][key] = 1

    verdict = evaluate_apply_log(_with_post_apply(mutate))
    assert verdict["ok"] is False
    assert key in verdict["reasons"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("missing_commerce_product_keys", ["ext:retailer:x"]),
        ("missing_native_product_keys", ["ext:retailer:x"]),
        ("unresolved_product_keys", ["ext:retailer:x"]),
        ("unresolved_category_count", 1),
        ("reasons", ["something"]),
    ],
)
def test_unresolved_or_explained_leftovers_stop_the_run(key, value):
    def mutate(report):
        report[key] = value

    verdict = evaluate_apply_log(_with_post_apply(mutate))
    assert verdict["ok"] is False
    expected = "report_reasons" if key == "reasons" else key
    assert expected in verdict["reasons"]


@pytest.mark.parametrize(
    "extra",
    [
        "Traceback (most recent call last):",
        "2026-09-18T02:59:12Z\tTraceback (most recent call last):",
        "  + Exception Group Traceback (most recent call last):",
    ],
)
def test_a_traceback_anywhere_stops_even_a_clean_looking_report(extra):
    """Anywhere on the line: a fetch format with a timestamp prefix must not hide one."""
    verdict = evaluate_apply_log(_clean_text() + "\n" + extra + "\n")
    assert verdict["ok"] is False
    assert "traceback" in verdict["reasons"]


def test_a_failed_job_stops_the_run_whatever_the_log_says():
    """The runner appends `JOB=<id> RC=<n>`. A partial apply raises and exits 2, and its stderr
    `{"error": ...}` line never reaches a fetched log (Cloud Run parses bare JSON into jsonPayload,
    which the fetch prints blank) -- so the exit code is the only witness of the failure."""
    verdict = evaluate_apply_log(_clean_text() + "\nJOB=oneoff-1-2 RC=2\n")
    assert verdict["ok"] is False
    assert "runner_failed" in verdict["reasons"]
    assert verdict["runner_rc"] == 2


def test_a_succeeded_job_with_a_clean_report_passes_and_records_the_rc():
    verdict = evaluate_apply_log(_clean_text() + "\nJOB=oneoff-61425-664 RC=0\n")
    assert verdict["ok"] is True, verdict["reasons"]
    assert verdict["runner_rc"] == 0


def test_a_lost_report_line_is_told_apart_from_a_failed_apply():
    """Cloud Logging drops lines, and the report is the LAST line printed. With RC=0 the write
    succeeded and the log is incomplete -- fetch it again; with RC!=0 the apply failed."""
    plan_only = "\n".join(
        line for line in _clean_text().splitlines()
        if not (MARKER in line and '"applied"' in line)
    )
    lost = evaluate_apply_log(plan_only + "\nJOB=oneoff-1-2 RC=0\n")
    assert lost["ok"] is False
    assert lost["reasons"] == ["report_line_missing_from_logs"]

    failed = evaluate_apply_log(plan_only + "\nJOB=oneoff-1-2 RC=2\n")
    assert failed["ok"] is False
    assert set(failed["reasons"]) == {"runner_failed", "no_post_apply_report"}


def test_two_apply_reports_in_one_log_stop_the_run():
    """Two hosts, or a failed attempt with a clean retry appended: judging only the last report
    would pass a log whose earlier apply failed."""
    post = [line for line in _clean_text().splitlines() if MARKER in line and '"applied"' in line]
    verdict = evaluate_apply_log(_clean_text() + "\n" + post[0] + "\n")
    assert verdict["ok"] is False
    assert "multiple_apply_reports" in verdict["reasons"]


def test_the_report_must_be_for_the_host_this_runner_applied():
    assert evaluate_apply_log(_clean_text(), domain="eyurs.com")["ok"] is True
    assert evaluate_apply_log(_clean_text(), domain="www.eyurs.com")["ok"] is True

    wrong = evaluate_apply_log(_clean_text(), domain="ohlolly.com")
    assert wrong["ok"] is False
    assert "report_for_another_host" in wrong["reasons"]

    def no_products(report):
        report["applied"]["primary_readiness"]["products"] = []

    empty = evaluate_apply_log(_with_post_apply(no_products), domain="eyurs.com")
    assert empty["ok"] is False
    assert "report_for_another_host" in empty["reasons"]


def _owned_elsewhere(kept):
    """The first product's canonical_url is us.eyurs.com's: another brand-official storefront owns that copy,
    and an off-canonical-market apply keeps it (apply._guard_canonical_owner, multi-market storefronts ADR
    Phase 2). `kept` is the apply's own record of that (owner, writer), or None when it recorded nothing."""
    def mutate(report):
        first = report["applied"]["primary_readiness"]["products"][0]
        first["canonical_url"] = "https://us.eyurs.com/products/x"
        if kept is not None:
            owner, writer = kept
            report["applied"]["canonical_owner_kept"] = [
                {"product_key": first["product_key"], "owner": owner, "writer": writer, "market": "AU"}]
    return _with_post_apply(mutate)


def test_a_product_the_apply_kept_under_its_owner_is_this_hosts_report():
    assert evaluate_apply_log(_owned_elsewhere(("us.eyurs.com", "eyurs.com")), domain="eyurs.com")["ok"] is True
    assert evaluate_apply_log(_owned_elsewhere(("us.eyurs.com", "www.eyurs.com")), domain="eyurs.com")["ok"] is True


@pytest.mark.parametrize("kept", [
    None,                                   # another host's URL the apply never said it kept
    ("us.eyurs.com", "ohlolly.com"),        # kept, but by ANOTHER host's apply: a stale or mis-pointed log
    ("somewhere-else.com", "eyurs.com"),    # the URL is not the owner the apply recorded
])
def test_only_a_product_kept_for_this_host_under_that_owner_is_excused(kept):
    verdict = evaluate_apply_log(_owned_elsewhere(kept), domain="eyurs.com")
    assert verdict["ok"] is False and "report_for_another_host" in verdict["reasons"]


def test_skus_explained_by_natural_key_dedupe_are_not_missing():
    """Mirrors `require_primary_apply`: fewer SKUs than planned is acceptable ONLY up to the number
    explicitly deduplicated on the natural key. The producer returns `applied` for this run; a gate
    that stopped it would stop a clean multi-variant host."""
    def deduped(report):
        report["missing"]["skus"] = 1
        report["applied"]["skus_deduped_same_identity"] = 1

    assert evaluate_apply_log(_with_post_apply(deduped))["ok"] is True

    def over(report):
        report["missing"]["skus"] = 2
        report["applied"]["skus_deduped_same_identity"] = 1

    verdict = evaluate_apply_log(_with_post_apply(over))
    assert verdict["ok"] is False
    assert "missing_skus" in verdict["reasons"]


def test_an_unparsable_report_line_is_a_reason_not_a_skip():
    text = _clean_text() + "\n" + MARKER + "{not json\n"
    verdict = evaluate_apply_log(text)
    assert verdict["ok"] is False
    assert "unparsable_primary_ingestion_line" in verdict["reasons"]


def test_the_cli_exits_zero_only_for_a_clean_log(tmp_path, capsys):
    assert main([str(FIXTURE)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    bad = tmp_path / "bad.log"
    bad.write_text(_with_post_apply(lambda r: r["missing"].__setitem__("offers", 1)), encoding="utf-8")
    assert main([str(bad)]) == 1
    assert "missing_offers" in json.loads(capsys.readouterr().out)["reasons"]

    assert main([]) == 2
