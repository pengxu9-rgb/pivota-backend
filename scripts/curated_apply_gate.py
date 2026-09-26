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

WHAT THE LOG CAN AND CANNOT SAY (the runner fetches it with
`gcloud logging read ... --format='value(textPayload,jsonPayload.message)'`):
  * `onboard_curated_brands.py` prints `primary ingestion: <json>` twice on an apply: the plan
    inspection (`status: ready_to_apply`, no `applied` key), then the post-apply report — with
    `status: applied` when `require_primary_apply` accepted the write, or `status: partial` /
    `failed` when it refused it. Both post-apply shapes carry `applied`, `skipped_products` (one
    row per planned PDP that did not land: `reason` = identity_skip / identity_resolution_incomplete
    / insert_failed / product_group_failed, with the identity matcher and the conflicting row) and
    `skipped_by_reason`. The verdict repeats those, so a stop names the rows and why.
  * A refused apply then RAISES (`PrimaryIngestionIncomplete` is a ValueError), prints
    `{"error": ...}` to stderr and exits 2. That stderr line is bare JSON, which Cloud Run parses
    into `jsonPayload` with no `message` key, so the fetch prints it as a BLANK row
    (scripts/measure_checkout_preflight.py records the same). It can never be seen here; do not
    add a check for it — a check that cannot fire reads as protection that does not exist. That is
    why the CLI prints the refused report to STDOUT first. Logs from before 2026-09-22 have no
    refused report at all, and read as `no_post_apply_report` as they always did.
  * What CAN distinguish a failed apply from a lost log line is the job's exit code, which the
    phase runner appends to the log as `JOB=<id> RC=<n>`. A non-zero RC is a failure; RC=0 with
    no report means the report line was lost by the logging sink, not that the write failed.
  * One post-apply report line is ~1.2 KB plus ~1 KB per product. Past ~250 products on one host
    it can exceed Cloud Logging's 256 KiB entry limit and arrive truncated; the gate then STOPS
    with `unparsable_primary_ingestion_line`. That is a known limit, and it fails safe.

Usage:
    python -m scripts.curated_apply_gate LOGFILE [--domain HOST]
Prints a JSON verdict; exits 0 only if the run is clean.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any
from urllib.parse import urlparse

MARKER = "primary ingestion: "
# One line per domain from onboard_curated_brands.py --only-category / --only-resolved-category.
# Deliberately NOT a failure: narrowing is the operator's choice. It is reported so a gate that
# passes a filtered run cannot read as "the whole storefront was onboarded".
FILTER_MARKER = "category filter report: "

# Every counter here was 0 on both clean canary applies, and each is a way for an apply to have
# "succeeded" while writing something other than what was planned.
_MUST_BE_ZERO = (
    "product_groups_failed",
    "skus_identity_conflict",
    "pdps_skipped_identity",
    "pdps_skipped_insert",
    "products_fully_skipped",
    "offers_dropped_for_refused_sku",
)

# What the verdict repeats per skipped row: enough to act on without opening intake_identity_events.
_SKIPPED_FIELDS = (
    "product_key", "reason", "matcher", "detail", "conflict_product_key", "conflict_merchant_id",
    "action", "error", "sqlstate",
)

_RUNNER_RC = re.compile(r"^JOB=\S+\s+RC=(\d+)\s*$")
# Anywhere on the line: a fetch format with a timestamp prefix must not hide one. 3.11's
# "+ Exception Group Traceback (most recent call last):" contains this too, so it needs no case.
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")


def _json_after(line: str, marker: str) -> Any:
    index = line.find(marker)
    if index < 0:
        return None
    return json.loads(line[index + len(marker):])


def _host(url: str) -> str:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _skipped_rows(report: dict) -> list[dict]:
    rows = report.get("skipped_products")
    if not isinstance(rows, list):
        return []
    return [{k: row[k] for k in _SKIPPED_FIELDS if row.get(k) is not None} for row in rows if isinstance(row, dict)]


def _skip_key(row: dict) -> str:
    # Same bucket as primary_ingestion.skipped_reason_key; kept local so this gate stays importable
    # without the service tree (it runs against a fetched log file, often off the service image).
    reason = str(row.get("reason") or "unknown")
    return f"{reason}:{row['matcher']}" if reason == "identity_skip" and row.get("matcher") else reason


def _filter_summary(filters: list[dict]) -> dict | None:
    """None when the run was not filtered; otherwise what the filter kept OUT, summed and per domain."""
    if not filters:
        return None
    return {
        "left_out": sum(int(f.get("left_out") or 0) for f in filters),
        "kept": sum(int(f.get("kept") or 0) for f in filters),
        "domains": filters,
    }


def evaluate_apply_log(text: str, *, domain: str | None = None) -> dict:
    """Return `{ok, reasons, apply_status, readiness_status, runner_rc, product_keys, applied,
    skipped_products, skipped_by_reason, category_filter}`.

    `ok` is True only when the log carries exactly one post-apply report, every check on it passes,
    and the runner — when it said anything — said the job succeeded. Anything the log does not say
    is a reason to stop: an absent report is not a clean one.
    """
    reasons: list[str] = []
    reports: list[dict] = []
    filters: list[dict] = []
    runner_rc: int | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        rc_match = _RUNNER_RC.match(line)
        if rc_match:
            runner_rc = int(rc_match.group(1))
            continue
        if FILTER_MARKER in line:
            try:
                parsed_filter = _json_after(line, FILTER_MARKER)
            except ValueError:
                reasons.append("unparsable_category_filter_line")
                continue
            if isinstance(parsed_filter, dict):
                filters.append(parsed_filter)
            continue
        if MARKER in line:
            try:
                parsed = _json_after(line, MARKER)
            except ValueError:
                reasons.append("unparsable_primary_ingestion_line")
                continue
            if isinstance(parsed, dict):
                reports.append(parsed)
            continue
        if _TRACEBACK.search(line):
            reasons.append("traceback")

    if runner_rc is not None and runner_rc != 0:
        reasons.append("runner_failed")

    # The POST-APPLY report is the one that carries `applied`. The plan line never does, so a dry
    # run — or an apply that raised before the write was accepted — has none.
    post = [r for r in reports if isinstance(r.get("applied"), dict)]
    if len(post) > 1:
        # Two applies in one log: two hosts, or a failed attempt with a retry appended. Judging the
        # last one would pass a log whose earlier apply failed; the runner writes one log per host.
        reasons.append("multiple_apply_reports")
    report = post[-1] if post else None
    if report is None:
        # Distinguish the two causes, because they need different responses: a failed job needs
        # investigating, a lost line needs the log fetched again.
        reasons.append("report_line_missing_from_logs" if runner_rc == 0 else "no_post_apply_report")
        return {
            "ok": False,
            "reasons": sorted(set(reasons)),
            "apply_status": reports[-1].get("status") if reports else None,
            "readiness_status": None,
            "runner_rc": runner_rc,
            "product_keys": [],
            "applied": None,
            "skipped_products": [],
            "skipped_by_reason": {},
            "category_filter": _filter_summary(filters),
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
        # Mirrors `require_primary_apply` exactly: fewer SKUs than planned is acceptable ONLY up to
        # the number explicitly deduplicated on the natural key. Any other shortfall is partial.
        deduped = max(0, int(applied.get("skus_deduped_same_identity") or 0))
        for kind, count in sorted(missing.items()):
            shortfall = int(count or 0)
            if kind == "skus":
                shortfall = max(0, shortfall - deduped)
            if shortfall:
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
    skipped = _skipped_rows(report)
    skipped_tally: dict[str, int] = {}
    for row in skipped:
        skipped_tally[_skip_key(row)] = skipped_tally.get(_skip_key(row), 0) + 1
    if skipped:
        reasons.append("skipped_products")
        reasons.extend(f"skipped:{key}" for key in skipped_tally)

    products = [p for p in (readiness.get("products") or []) if isinstance(p, dict)]
    if domain:
        # The report must be for the host this runner applied — a stale or mis-pointed log must not
        # pass on another host's clean run.
        want = _host(f"https://{domain}")
        # One exception, recorded by the apply itself: products whose canonical copy ANOTHER brand-official
        # storefront owns, which this (off-canonical-market) apply attached offers to without taking over
        # (apply._guard_canonical_owner; multi-market storefronts ADR 3.4 item 4). Their canonical_url is
        # the owner's by design. Excused per owner host, at most as many products as the apply recorded it
        # kept under THAT owner FOR THIS HOST -- from the complete tally (canonical_owner_kept_by_host),
        # never from canonical_owner_kept, which is a 50-row display sample (review of #2358, D2).
        allowed: dict[str, int] = {}
        for r in applied.get("canonical_owner_kept_by_host") or []:
            if isinstance(r, dict) and _host(f"https://{r.get('writer')}") == want:
                owner = _host(f"https://{r.get('owner')}")
                allowed[owner] = allowed.get(owner, 0) + max(0, int(r.get("count") or 0))
        elsewhere: dict[str, int] = {}
        for p in products:
            host = _host(p.get("canonical_url"))
            if host != want:
                elsewhere[host] = elsewhere.get(host, 0) + 1
        if not products or any(n > allowed.get(host, 0) for host, n in elsewhere.items()):
            reasons.append("report_for_another_host")

    return {
        "ok": not reasons,
        "reasons": sorted(set(reasons)),
        "apply_status": apply_status,
        "readiness_status": readiness_status,
        "runner_rc": runner_rc,
        "product_keys": [p.get("product_key") for p in products],
        "applied": {k: applied.get(k) for k in ("pdps", "skus", "offers", "inci_written", *_MUST_BE_ZERO)},
        "skipped_products": skipped,
        "skipped_by_reason": skipped_tally,
        "category_filter": _filter_summary(filters),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    domain = None
    if "--domain" in args:
        index = args.index("--domain")
        if index + 1 >= len(args):
            print("usage: python -m scripts.curated_apply_gate LOGFILE [--domain HOST]", file=sys.stderr)
            return 2
        domain = args[index + 1]
        del args[index:index + 2]
    if len(args) != 1:
        print("usage: python -m scripts.curated_apply_gate LOGFILE [--domain HOST]", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8", errors="replace") as handle:
        verdict = evaluate_apply_log(handle.read(), domain=domain)
    print(json.dumps(verdict, sort_keys=True))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
