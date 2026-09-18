"""Decide whether one `onboard_curated_brands.py --apply` run finished cleanly, from its log.

A multi-host apply runs one host at a time and must only start the next host when the previous one
finished cleanly — a partial first apply means the second would ingest into a half-written state.
This is the check that decides that, and it reads the POST-APPLY REPORT, never the last `status`
token in the log.

WHY THIS FILE EXISTS. The shell gate every canary apply used was

    grep -o '"status": "[a-z_]*"' "$LOG" | tail -1

and the post-apply report nests its readiness status INSIDE `applied.primary_readiness`, while the
report's own top-level `status` is `"applied"`. `sort_keys=True` serialises `applied` first and
`status` last, so the last match is always the OUTER `"applied"` — and a gate waiting for
`"complete"` stopped every clean run. Measured 2026-09-18 on the Pyunkang Yul canary re-ingest:
eyurs finished with `primary_readiness.status = "complete"`, 0 missing and 0 identity conflicts,
and the gate refused to start ohlolly. The Phase 4 log has the same shape, which is why ohlolly
had been run by hand then. A gate that stops every good run gets bypassed, and a bypassed gate
protects nothing.

`onboard_curated_brands.py` prints `primary ingestion: <json>` twice on an apply: first the plan
inspection (`status: ready_to_apply`, no `applied` key), then — only if the write returned — the
result of `require_primary_apply` (`status: applied`, with `applied.primary_readiness`). A crawl or
validation failure goes to stderr as `{"crawl": ...}` / `{"error": ...}`.

Usage:
    python -m scripts.curated_apply_gate LOGFILE        # prints a JSON verdict; exit 0 only if clean
"""

from __future__ import annotations

import json
import sys
from typing import Any

MARKER = "primary ingestion: "

# Every counter here was 0 on both clean canary applies, and each is a way for an apply to have
# "succeeded" while writing something other than what was planned.
_MUST_BE_ZERO = (
    "product_groups_failed",
    "skus_identity_conflict",
    "pdps_skipped_identity",
    "offers_dropped_for_refused_sku",
)


def _json_after(line: str, marker: str) -> Any:
    index = line.find(marker)
    if index < 0:
        return None
    return json.loads(line[index + len(marker):])


def evaluate_apply_log(text: str) -> dict:
    """Return `{ok, reasons, apply_status, readiness_status, product_keys, applied}` for one log.

    `ok` is True only when the log carries a post-apply report and every check on it passes.
    Anything the log does not say is a reason to stop — an absent report is not a clean one.
    """
    reasons: list[str] = []
    reports: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if MARKER in line:
            try:
                parsed = _json_after(line, MARKER)
            except ValueError:
                reasons.append("unparsable_primary_ingestion_line")
                continue
            if isinstance(parsed, dict):
                reports.append(parsed)
            continue
        if line.startswith("Traceback"):
            reasons.append("traceback")
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and "error" in parsed:
                reasons.append("apply_error")
            if isinstance(parsed, dict) and "crawl" in parsed:
                reasons.append("crawl_incomplete")

    # The POST-APPLY report is the one that carries `applied`. The plan line never does, so a dry
    # run — or an apply that died before the write returned — has none.
    post = [r for r in reports if isinstance(r.get("applied"), dict)]
    report = post[-1] if post else None
    if report is None:
        reasons.append("no_post_apply_report")
        return {
            "ok": False,
            "reasons": sorted(set(reasons)),
            "apply_status": reports[-1].get("status") if reports else None,
            "readiness_status": None,
            "product_keys": [],
            "applied": None,
        }

    applied = report["applied"]
    readiness = applied.get("primary_readiness") if isinstance(applied.get("primary_readiness"), dict) else {}
    apply_status = report.get("status")
    readiness_status = readiness.get("status")

    if apply_status != "applied":
        reasons.append(f"apply_status_{apply_status or 'missing'}")
    if readiness_status != "complete":
        reasons.append(f"readiness_{readiness_status or 'missing'}")

    missing = report.get("missing")
    if not isinstance(missing, dict):
        reasons.append("missing_counts_absent")
    else:
        for kind, count in sorted(missing.items()):
            if count:
                reasons.append(f"missing_{kind}")
    for key in ("missing_commerce_product_keys", "missing_native_product_keys", "unresolved_product_keys"):
        if report.get(key):
            reasons.append(key)
    if report.get("unresolved_category_count"):
        reasons.append("unresolved_category_count")
    if report.get("reasons"):
        reasons.append("report_reasons")
    for key in _MUST_BE_ZERO:
        if applied.get(key):
            reasons.append(key)

    products = readiness.get("products") if isinstance(readiness.get("products"), list) else []
    return {
        "ok": not reasons,
        "reasons": sorted(set(reasons)),
        "apply_status": apply_status,
        "readiness_status": readiness_status,
        "product_keys": [p.get("product_key") for p in products if isinstance(p, dict)],
        "applied": {k: applied.get(k) for k in ("pdps", "skus", "offers", "inci_written", *_MUST_BE_ZERO)},
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m scripts.curated_apply_gate LOGFILE", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8", errors="replace") as handle:
        verdict = evaluate_apply_log(handle.read())
    print(json.dumps(verdict, sort_keys=True))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
