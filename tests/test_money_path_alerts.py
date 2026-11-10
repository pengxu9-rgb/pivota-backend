"""Alert lines (utils/money_alerts) and the alert DEFINITIONS that page on them (FIX_PLAN R-03).

Two things are pinned, the same way tests/test_reap_rail_alerts.py pins the Reap poller's alerts:

  THE LINE FORMAT. utils/money_alerts renders the lines; emit() is read off the "pivota" logger's
  own stream under production's logging state. The producer itself (the protocol-directory
  freshness check) is driven in tests/test_freshness_directory_check.py.

  THE FILTERS ARE THE DEFINITION FILE'S. infra/gcp/monitoring/money_path_alerts.json is loaded and
  each metric's filter is EVALUATED (the whole expression) against those lines.

    .venv/bin/python -m pytest tests/test_money_path_alerts.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from utils import money_alerts  # noqa: E402

from test_reap_rail_alerts import matches, worker_log  # noqa: E402

DEFS_PATH = ROOT / "infra" / "gcp" / "monitoring" / "money_path_alerts.json"
APPLY = ROOT / "infra" / "gcp" / "apply_money_path_alerts.sh"


@pytest.fixture(scope="module")
def defs():
    return json.loads(DEFS_PATH.read_text(encoding="utf-8"))


def _metric(defs, name):
    return next(m["filter"] for m in defs["log_metrics"] if m["name"] == name)


def _entry(line, service="web"):
    return {"resource.type": "cloud_run_revision", "resource.labels.service_name": service, "textPayload": line}


def _hits(defs, line):
    return sorted(m["name"] for m in defs["log_metrics"] if matches(m["filter"], _entry(line)))


def _alerts(lines):
    return [line for line in lines if "money_alert: " in line]


# ── the helper ─────────────────────────────────────────────────────────────────────────────────


def test_a_line_is_compact_sorted_json_after_the_prefix():
    line = money_alerts.render("freshness_directory_past_limit", status="stale", domain="shop-a.example",
                               age_seconds=259201, limit_seconds=259200, market="US", clock="card_fact")
    assert line == (
        'money_alert: {"age_seconds":259201,"clock":"card_fact","domain":"shop-a.example",'
        '"event":"freshness_directory_past_limit","limit_seconds":259200,"market":"US","status":"stale"}')


def test_emit_writes_the_rendered_line_through_the_pivota_logger_at_its_level():
    with worker_log() as lines:
        money_alerts.emit("freshness_directory_past_limit", level="warning", status="missing", age_seconds=None)
    [line] = lines
    assert " WARNING - " in line
    assert line.endswith('money_alert: {"age_seconds":null,"event":"freshness_directory_past_limit","status":"missing"}')


@pytest.mark.parametrize("bad", ["email", "buyer_email", "shipping_address", "Event", "event", "a-b", "x" * 49])
def test_contact_keys_and_bad_names_are_refused(bad):
    with pytest.raises(ValueError):
        money_alerts.render("freshness_directory_past_limit", **{bad: "v"})


def test_an_unknown_event_is_refused_and_emit_never_raises():
    with pytest.raises(ValueError):
        money_alerts.render("not_an_event")
    with worker_log() as lines:
        money_alerts.emit("not_an_event", email="someone@example.com")
    assert lines and all("someone@example.com" not in line for line in lines)
    assert any("render failed" in line for line in lines)


def test_values_are_scalars_without_control_characters_and_bounded():
    line = money_alerts.render("freshness_directory_past_limit", reason="a\nb\x00c" + "z" * 500, obj={"k": 1})
    body = json.loads(line[len(money_alerts.PREFIX):])
    assert body["reason"].startswith("abc") and len(body["reason"]) == money_alerts.MAX_TEXT
    assert body["obj"] == "{'k': 1}"[: money_alerts.MAX_TEXT]


# ── the definitions ────────────────────────────────────────────────────────────────────────────


def test_every_event_has_exactly_the_metric_meant_for_it(defs):
    expected = {
        "freshness_directory_past_limit": ["freshness_directory_past_limit"],
    }
    for event, metrics in expected.items():
        assert _hits(defs, "[ts] ERROR - " + money_alerts.render(event, x=1)) == metrics, event
    stale = "[ts] WARNING - " + money_alerts.render("freshness_directory_past_limit", status="stale")
    assert _hits(defs, stale) == ["freshness_directory_past_limit"]
    missing = "[ts] WARNING - " + money_alerts.render("freshness_directory_past_limit", status="missing")
    assert _hits(defs, missing) == ["freshness_directory_missing", "freshness_directory_past_limit"]
    beat = '[ts] INFO - freshness_directory_check: {"fresh":1,"missing":0,"negative":0,"pairs":1}'
    assert _hits(defs, beat) == ["freshness_directory_check_heartbeat"]
    assert set(expected) == set(money_alerts.EVENTS)


def test_the_file_holds_only_the_directory_freshness_definitions(defs):
    assert sorted(m["name"] for m in defs["log_metrics"]) == [
        "freshness_directory_check_heartbeat", "freshness_directory_missing", "freshness_directory_past_limit"]
    assert sorted(p["displayName"] for p in defs["alert_policies"]) == [
        "prod: directory freshness check went silent", "prod: directory merchant fact past its limit"]


def test_an_unrelated_line_feeds_no_metric(defs):
    for line in ("[ts] INFO - reap_agentic_poll: PollReport(errors=0)",
                 "[ts] ERROR - merchant_order_gap: 1 paid order(s)",
                 "event stuck_paid_orders mentioned in prose"):
        assert _hits(defs, line) == []


def test_every_policy_reads_a_defined_metric_and_goes_to_the_channel(defs):
    names = {m["name"] for m in defs["log_metrics"]}
    displays = [p["displayName"] for p in defs["alert_policies"]]
    assert len(displays) == len(set(displays))
    for policy in defs["alert_policies"]:
        assert policy["notificationChannels"] == ["${CHANNEL}"]
        assert policy["displayName"].startswith("prod: ")
        for condition in policy["conditions"]:
            text = json.dumps(condition)
            used = set(re.findall(r"logging\.googleapis\.com/user/([a-z0-9_]+)", text)) | set(
                re.findall(r"logging_googleapis_com:user_([a-z0-9_]+)", text))
            assert used and used <= names, (policy["displayName"], used)
            threshold = condition.get("conditionThreshold")
            if threshold:
                assert threshold["duration"] == "0s", "a log counter writes no points between lines"


def test_the_apply_script_parses_and_never_deletes():
    subprocess.run(["bash", "-n", str(APPLY)], check=True)
    source = APPLY.read_text(encoding="utf-8")
    assert "money_path_alerts.json" in source
    assert "DELETE" not in source and "metrics delete" not in source
