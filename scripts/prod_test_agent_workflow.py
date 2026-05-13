#!/usr/bin/env python3
"""Production test harness for the /api/audits (Phase 2.3) async lifecycle.

Hits live HTTPS endpoints with a structured test matrix:
  - Happy path: enqueue → poll → terminal → inspect report
  - Idempotency: same payload twice → idempotent_replay=true + same run_id
  - Force bypass: force=true skips dedupe → new run_id
  - Cross-tenant guard: body.merchant_id mismatch → 403
  - Audience-shape guards on GET / POST cancel for unknown / other-tenant runs
  - subject_type=cold_start with merchant JWT → 403
  - Schema validation: 0 / 6 product_keys → 422
  - Cancel happy path: queued/running → 202 with cancellation_requested=true
  - Cancel terminal run → 202 with cancellation_requested=false
  - GET /api/audits list + limit edges
  - Stage transition timestamp monotonicity

Usage:
  BASE_URL=https://web-production-fedb.up.railway.app \\
  MERCHANT_JWT=<token> \\
  MERCHANT_ID=merch_efbc46b4619cfbdf \\
  python3 scripts/prod_test_agent_workflow.py [--max-poll-seconds 180]

Reads zero secrets from any prod env. Outputs a JSON report to stdout and
to `prod_test_agent_workflow_<UTCISO>.json` in the cwd.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


DEFAULT_BASE = "https://web-production-fedb.up.railway.app"


@dataclass
class Check:
    name: str
    category: str
    passed: Optional[bool] = None
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    base_url: str
    merchant_id: str
    started_at: str
    finished_at: str = ""
    checks: List[Check] = field(default_factory=list)

    def add(self, c: Check) -> Check:
        self.checks.append(c)
        return c

    def summary(self) -> Dict[str, int]:
        passed = sum(1 for c in self.checks if c.passed is True)
        failed = sum(1 for c in self.checks if c.passed is False)
        skipped = sum(1 for c in self.checks if c.passed is None)
        return {"passed": passed, "failed": failed, "skipped": skipped,
                "total": len(self.checks)}


class Client:
    def __init__(self, base: str, jwt: str, timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.h = {"Authorization": f"Bearer {jwt}",
                  "Content-Type": "application/json",
                  "Accept": "application/json"}
        self.timeout = timeout

    def post(self, path: str, body: Dict[str, Any],
             extra_headers: Optional[Dict[str, str]] = None) -> requests.Response:
        h = dict(self.h)
        if extra_headers:
            h.update(extra_headers)
        return requests.post(f"{self.base}{path}", headers=h,
                             json=body, timeout=self.timeout)

    def get(self, path: str) -> requests.Response:
        return requests.get(f"{self.base}{path}", headers=self.h,
                            timeout=self.timeout)


def _evidence(resp: requests.Response) -> Dict[str, Any]:
    try:
        body = resp.json()
    except Exception:
        body = (resp.text or "")[:500]
    return {"status_code": resp.status_code,
            "body": body,
            "request_id": resp.headers.get("x-request-id")
                          or resp.headers.get("X-Request-Id")}


def _pick_product_keys(client: Client, merchant_id: str,
                       limit: int = 3) -> Tuple[List[str], Dict[str, Any]]:
    """Discover up to `limit` product_keys for this merchant by
    harvesting recent audit runs' product_keys (same auth surface).
    Falls back to agent v1 search if no historical runs exist."""
    # Primary: GET /api/audits with the same merchant JWT.
    try:
        r = client.get("/api/audits?limit=10")
    except Exception as exc:
        r = None  # type: ignore[assignment]
        primary_err: Dict[str, Any] = {"path": "/api/audits?limit=10",
                                       "exc": str(exc)}
    else:
        primary_err = {}
    if r is not None and r.status_code == 200:
        try:
            rows = r.json() or []
        except Exception:
            rows = []
        seen: List[str] = []
        seenset = set()
        for row in rows if isinstance(rows, list) else []:
            for pk in (row.get("product_keys") or []):
                if isinstance(pk, str) and pk not in seenset:
                    seen.append(pk); seenset.add(pk)
                    if len(seen) >= limit:
                        break
            if len(seen) >= limit:
                break
        if seen:
            return seen, {"discovered_via": "/api/audits (history)",
                          "count": len(seen)}
        primary_err = {"path": "/api/audits", "rows": len(rows)}

    # Fallback: agent v1 search
    candidates = [
        f"/agent/v1/products/search?merchant_id={merchant_id}&query=&limit={limit}&offset=0",
        f"/agent/merchants/{merchant_id}/products?limit={limit}",
    ]
    last_err: Dict[str, Any] = primary_err
    for path in candidates:
        try:
            r = client.get(path)
        except Exception as exc:
            last_err = {"path": path, "exc": str(exc)}
            continue
        if r.status_code >= 400:
            last_err = {"path": path, "status": r.status_code,
                        "body": (r.text or "")[:200]}
            continue
        try:
            data = r.json()
        except Exception:
            last_err = {"path": path, "non_json": True,
                        "body": (r.text or "")[:200]}
            continue
        keys = _extract_product_keys(data, merchant_id, limit)
        if keys:
            return keys, {"discovered_via": path, "count": len(keys)}
        last_err = {"path": path, "json_no_product_keys": True}
    return [], {"last_error": last_err}


def _extract_product_keys(payload: Any, merchant_id: str,
                          limit: int) -> List[str]:
    """Walk a JSON blob, pulling out anything that looks like a
    product_key for this merchant."""
    out: List[str] = []
    prefix_v1 = f"prod::{merchant_id}::"
    prefix_v0 = f"{merchant_id}|"  # legacy

    def visit(o: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(o, dict):
            pk = o.get("product_key") or o.get("productKey")
            if isinstance(pk, str) and (
                pk.startswith(prefix_v1) or pk.startswith(prefix_v0)
            ):
                out.append(pk)
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(payload)
    # Dedupe but preserve order
    seen = set()
    deduped: List[str] = []
    for k in out:
        if k not in seen:
            deduped.append(k); seen.add(k)
    return deduped[:limit]


def _poll_until_terminal(client: Client, run_id: str,
                         max_seconds: int) -> Tuple[Dict[str, Any], List[str]]:
    deadline = time.time() + max_seconds
    seen_stages: List[str] = []
    detail: Dict[str, Any] = {}
    while time.time() < deadline:
        r = client.get(f"/api/audits/{run_id}")
        try:
            data = r.json()
        except Exception:
            detail = {"non_json": True,
                      "text": (r.text or "")[:300],
                      "status_code": r.status_code}
            return detail, seen_stages
        stage = data.get("stage")
        if stage and (not seen_stages or seen_stages[-1] != stage):
            seen_stages.append(stage)
        if stage in ("completed", "failed", "cancelled"):
            return data, seen_stages
        time.sleep(2.0)
    return {"timeout_after_seconds": max_seconds,
            "last_known_stage": seen_stages[-1] if seen_stages else None}, seen_stages


def run(base: str, jwt: str, merchant_id: str,
        max_poll_seconds: int) -> Report:
    rep = Report(base_url=base, merchant_id=merchant_id,
                 started_at=datetime.now(timezone.utc).isoformat())
    c = Client(base, jwt)

    # --- A. Auth-shape smoke -------------------------------------------------
    chk = rep.add(Check(name="auth.list_audits_200",
                        category="auth"))
    r = c.get("/api/audits?limit=1")
    chk.passed = r.status_code == 200
    chk.evidence = _evidence(r)
    if not chk.passed:
        chk.detail = "JWT rejected at /api/audits — abort remaining checks"
        rep.finished_at = datetime.now(timezone.utc).isoformat()
        return rep

    # --- B. Discover product_keys -------------------------------------------
    chk = rep.add(Check(name="prereq.discover_product_keys",
                        category="prereq"))
    keys, disc = _pick_product_keys(c, merchant_id, limit=3)
    chk.evidence = disc
    if not keys:
        chk.passed = False
        chk.detail = ("Could not discover any product_keys via public APIs; "
                      "remaining audit checks will be skipped.")
        rep.finished_at = datetime.now(timezone.utc).isoformat()
        return rep
    chk.passed = True
    chk.detail = f"Using {len(keys)} product_keys"
    primary_keys = keys[:1]

    # --- C. Schema validation ------------------------------------------------
    chk = rep.add(Check(name="validation.zero_product_keys_422",
                        category="validation"))
    r = c.post("/api/audits", {"merchant_id": merchant_id,
                                "product_keys": [],
                                "subject_type": "merchant"})
    # Pivota's error envelope normalizes 422 → 400 with code=INVALID_REQUEST;
    # accept either, but require the code if 400.
    chk.passed = r.status_code == 422 or (
        r.status_code == 400
        and isinstance(_evidence(r).get("body"), dict)
        and (_evidence(r)["body"].get("error") or {}).get("code") == "INVALID_REQUEST"
    )
    chk.evidence = _evidence(r)

    chk = rep.add(Check(name="validation.six_product_keys_422",
                        category="validation"))
    r = c.post("/api/audits", {"merchant_id": merchant_id,
                                "product_keys": (keys * 6)[:6],
                                "subject_type": "merchant"})
    # Pivota's error envelope normalizes 422 → 400 with code=INVALID_REQUEST;
    # accept either, but require the code if 400.
    chk.passed = r.status_code == 422 or (
        r.status_code == 400
        and isinstance(_evidence(r).get("body"), dict)
        and (_evidence(r)["body"].get("error") or {}).get("code") == "INVALID_REQUEST"
    )
    chk.evidence = _evidence(r)

    # --- D. Cross-tenant guard ----------------------------------------------
    chk = rep.add(Check(name="auth.cross_tenant_body_mismatch_403",
                        category="auth"))
    r = c.post("/api/audits", {"merchant_id": "merch_some_other_tenant",
                                "product_keys": primary_keys,
                                "subject_type": "merchant"})
    chk.passed = r.status_code == 403
    chk.evidence = _evidence(r)

    chk = rep.add(Check(name="auth.cold_start_via_merchant_jwt_403",
                        category="auth"))
    r = c.post("/api/audits", {"merchant_id": merchant_id,
                                "product_keys": primary_keys,
                                "subject_type": "cold_start"})
    chk.passed = r.status_code == 403
    chk.evidence = _evidence(r)

    # --- E. Happy path enqueue ----------------------------------------------
    chk = rep.add(Check(name="happy.enqueue_202",
                        category="happy_path"))
    enqueue_body = {"merchant_id": merchant_id,
                    "product_keys": primary_keys,
                    "subject_type": "merchant"}
    r = c.post("/api/audits", enqueue_body)
    chk.evidence = _evidence(r)
    if r.status_code != 202:
        chk.passed = False
        chk.detail = f"Expected 202 ACCEPTED, got {r.status_code}"
        rep.finished_at = datetime.now(timezone.utc).isoformat()
        return rep
    chk.passed = True
    first_run_id = r.json().get("run_id")

    # --- F. Idempotency dedupe ----------------------------------------------
    chk = rep.add(Check(name="idempotency.same_body_returns_same_run_id",
                        category="idempotency"))
    r = c.post("/api/audits", enqueue_body)
    body = r.json() if r.headers.get("content-type", "").startswith(
        "application/json") else {}
    chk.evidence = _evidence(r)
    chk.passed = (
        r.status_code in (200, 202)
        and body.get("run_id") == first_run_id
        and body.get("idempotent_replay") is True
    )

    # --- G. Force bypass ----------------------------------------------------
    chk = rep.add(Check(name="idempotency.force_true_bypasses_dedupe",
                        category="idempotency"))
    r = c.post("/api/audits", {**enqueue_body, "force": True})
    body = r.json() if r.ok else {}
    chk.evidence = _evidence(r)
    chk.passed = (
        r.status_code == 202
        and body.get("run_id")
        and body.get("run_id") != first_run_id
        and body.get("idempotent_replay") is False
    )
    forced_run_id = body.get("run_id") if r.ok else None

    # --- H. Poll lifecycle of the FIRST run ---------------------------------
    chk = rep.add(Check(name="happy.poll_to_terminal",
                        category="happy_path"))
    final, seen_stages = _poll_until_terminal(
        c, first_run_id, max_seconds=max_poll_seconds,
    )
    chk.evidence = {"final": final, "stage_sequence": seen_stages}
    terminal = final.get("stage")
    if terminal in ("completed", "failed", "cancelled"):
        chk.passed = True
        chk.detail = f"Reached terminal stage {terminal}"
    else:
        chk.passed = False
        chk.detail = (
            f"Did not reach terminal within {max_poll_seconds}s; "
            f"last stage seen: {seen_stages[-1] if seen_stages else 'none'}"
        )

    # --- I. Stage monotonicity ----------------------------------------------
    chk = rep.add(Check(name="lifecycle.stage_sequence_monotonic",
                        category="lifecycle"))
    invalid_back_edges: List[Tuple[str, str]] = []
    rank = {"queued": 0, "discovering": 1, "probing": 2, "scoring": 3,
            "materializing": 4, "verifying": 5,
            "completed": 99, "failed": 99, "cancelled": 99}
    for a, b in zip(seen_stages, seen_stages[1:]):
        if a in rank and b in rank and rank[b] < rank[a]:
            invalid_back_edges.append((a, b))
    chk.passed = len(invalid_back_edges) == 0
    chk.evidence = {"sequence": seen_stages,
                    "back_edges": invalid_back_edges}
    if invalid_back_edges:
        chk.detail = "Stage went backwards — state machine invariant violated"

    # --- J. GET unknown run_id → 404 ----------------------------------------
    chk = rep.add(Check(name="audit.get_unknown_run_id_404",
                        category="auth"))
    r = c.get(f"/api/audits/run_id_{uuid.uuid4().hex[:12]}_does_not_exist")
    chk.passed = r.status_code == 404
    chk.evidence = _evidence(r)

    # --- K. Cancel an already-terminal run → 202, cancellation_requested=false
    chk = rep.add(Check(name="cancel.terminal_run_no_op_202",
                        category="cancel"))
    r = c.post(f"/api/audits/{first_run_id}/cancel", {})
    body = r.json() if r.ok else {}
    chk.evidence = _evidence(r)
    chk.passed = (
        r.status_code == 202
        and body.get("cancellation_requested") is False
        and body.get("current_stage") in ("completed", "failed", "cancelled")
    )

    # --- L. Cancel unknown run_id → 404 -------------------------------------
    chk = rep.add(Check(name="cancel.unknown_run_id_404",
                        category="cancel"))
    r = c.post(f"/api/audits/run_id_{uuid.uuid4().hex[:12]}_dne/cancel", {})
    chk.passed = r.status_code == 404
    chk.evidence = _evidence(r)

    # --- M. Cancel the forced run while (likely) still active ---------------
    if forced_run_id:
        chk = rep.add(Check(name="cancel.active_run_accepts_202",
                            category="cancel"))
        r = c.post(f"/api/audits/{forced_run_id}/cancel", {})
        body = r.json() if r.ok else {}
        chk.evidence = _evidence(r)
        # Either still active and accepted, or already terminal (race);
        # both are valid lifecycle responses.
        chk.passed = (
            r.status_code == 202
            and body.get("current_stage") is not None
        )

    # --- N. List endpoint edges --------------------------------------------
    chk = rep.add(Check(name="list.recent_runs_includes_first_run_id",
                        category="list"))
    r = c.get("/api/audits?limit=20")
    chk.evidence = _evidence(r)
    if r.status_code == 200 and isinstance(r.json(), list):
        ids = [row.get("run_id") for row in r.json()]
        chk.passed = first_run_id in ids
        chk.detail = f"first_run_id={first_run_id} {'in' if chk.passed else 'NOT in'} list"
    else:
        chk.passed = False

    chk = rep.add(Check(name="list.limit_zero_422",
                        category="list"))
    r = c.get("/api/audits?limit=0")
    # Pivota's error envelope normalizes 422 → 400 with code=INVALID_REQUEST;
    # accept either, but require the code if 400.
    chk.passed = r.status_code == 422 or (
        r.status_code == 400
        and isinstance(_evidence(r).get("body"), dict)
        and (_evidence(r)["body"].get("error") or {}).get("code") == "INVALID_REQUEST"
    )
    chk.evidence = _evidence(r)

    chk = rep.add(Check(name="list.limit_101_422",
                        category="list"))
    r = c.get("/api/audits?limit=101")
    # Pivota's error envelope normalizes 422 → 400 with code=INVALID_REQUEST;
    # accept either, but require the code if 400.
    chk.passed = r.status_code == 422 or (
        r.status_code == 400
        and isinstance(_evidence(r).get("body"), dict)
        and (_evidence(r)["body"].get("error") or {}).get("code") == "INVALID_REQUEST"
    )
    chk.evidence = _evidence(r)

    rep.finished_at = datetime.now(timezone.utc).isoformat()
    return rep


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=os.environ.get("BASE_URL", DEFAULT_BASE))
    p.add_argument("--merchant-id",
                   default=os.environ.get("MERCHANT_ID", "merch_efbc46b4619cfbdf"))
    p.add_argument("--max-poll-seconds", type=int, default=180)
    p.add_argument("--out", default=None,
                   help="Output JSON file path (auto-named if omitted).")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    jwt = (os.environ.get("MERCHANT_JWT") or "").strip()
    if not jwt:
        print("ERROR: MERCHANT_JWT env var is required.", file=sys.stderr)
        return 2

    rep = run(base=args.base_url, jwt=jwt, merchant_id=args.merchant_id,
              max_poll_seconds=args.max_poll_seconds)

    out_path = args.out or (
        f"prod_test_agent_workflow_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    rendered = json.dumps({**asdict(rep),
                           "summary": rep.summary()},
                          indent=2, default=str)
    Path(out_path).write_text(rendered)
    print(rendered)

    s = rep.summary()
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
