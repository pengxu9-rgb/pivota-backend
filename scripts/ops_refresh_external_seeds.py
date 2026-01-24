"""
Batch refresh external product seeds (external seeds).

This is useful after improving the external offer extractor so existing seeds
can pick up richer variants (e.g. Tom Ford multiple sizes).

Usage (from repo root):

    # Option A: use an existing employee JWT
    export PIVOTA_EMPLOYEE_JWT="..."
    python3 scripts/ops_refresh_external_seeds.py --base-url https://YOUR_BACKEND

    # Option B: login to fetch a token (avoid pasting JWT)
    export PIVOTA_EMPLOYEE_EMAIL="you@example.com"
    export PIVOTA_EMPLOYEE_PASSWORD="..."
    python3 scripts/ops_refresh_external_seeds.py --base-url https://YOUR_BACKEND

    # Refresh only seeds matching a domain/title, and only those with <= 1 variant
    python3 scripts/ops_refresh_external_seeds.py \
        --base-url https://YOUR_BACKEND \
        --query tomfordbeauty.com \
        --only-if-variants-le 1 \
        --concurrency 2

Environment variables:
    PIVOTA_BACKEND_BASE_URL   Base URL for the FastAPI service (default: http://localhost:8000)
    PIVOTA_EMPLOYEE_JWT       Employee/admin JWT for Authorization: Bearer
    PIVOTA_EMPLOYEE_EMAIL     Email for /auth/signin (optional)
    PIVOTA_EMPLOYEE_PASSWORD  Password for /auth/signin (optional)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch refresh /employee/products/external-seeds/{id}/refresh")
    parser.add_argument(
        "--base-url",
        default=os.getenv("PIVOTA_BACKEND_BASE_URL", "http://localhost:8000"),
        help="Base URL for the Pivota backend (default: %(default)s)",
    )

    auth = parser.add_argument_group("auth")
    auth.add_argument(
        "--token",
        default=os.getenv("PIVOTA_EMPLOYEE_JWT"),
        help="Employee/admin JWT (Authorization: Bearer ...). If omitted, script will try --email/--password.",
    )
    auth.add_argument("--email", default=os.getenv("PIVOTA_EMPLOYEE_EMAIL"), help="Email for /auth/signin")
    auth.add_argument("--password", default=os.getenv("PIVOTA_EMPLOYEE_PASSWORD"), help="Password for /auth/signin")

    selection = parser.add_argument_group("selection")
    selection.add_argument(
        "--seed-id",
        action="append",
        default=[],
        help="Explicit seed id to refresh (repeatable). If provided, list endpoint is skipped.",
    )
    selection.add_argument(
        "--seed-ids-file",
        default=None,
        help="Path to a file containing seed ids (one per line).",
    )
    selection.add_argument(
        "--query",
        default=None,
        help="Filter for GET /employee/products/external-seeds?q=... (matches url/domain/title/ids/variant ids).",
    )
    selection.add_argument(
        "--status",
        default="active",
        help="Seed status filter for list endpoint (default: %(default)s)",
    )
    selection.add_argument(
        "--attached",
        choices=["any", "true", "false"],
        default="any",
        help="Filter by attached_product_key (default: %(default)s)",
    )
    selection.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max seeds returned by list endpoint (default: %(default)s; API caps at 200)",
    )
    selection.add_argument(
        "--only-if-variants-le",
        type=int,
        default=None,
        help="Only refresh seeds whose current variants_count <= N (client-side).",
    )

    runtime = parser.add_argument_group("runtime")
    runtime.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Concurrent refresh workers (default: %(default)s)",
    )
    runtime.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout seconds per request (default: %(default)s)",
    )
    runtime.add_argument("--dry-run", action="store_true", help="List target seeds but do not refresh")
    runtime.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Print JSON summary at the end (for piping).",
    )
    return parser.parse_args()


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login_for_token(client: httpx.AsyncClient, base_url: str, email: str, password: str, timeout: float) -> str:
    url = f"{base_url.rstrip('/')}/auth/signin"
    resp = await client.post(url, json={"email": email, "password": password}, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Signin failed: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    token = (data.get("token") or "").strip()
    if not token:
        raise RuntimeError("Signin response missing token")
    return token


def _load_seed_ids_from_file(path: str) -> List[str]:
    out: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
    return out


async def _list_seeds(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    *,
    query: Optional[str],
    attached: str,
    status: str,
    limit: int,
    timeout: float,
) -> List[Dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/employee/products/external-seeds"
    params: Dict[str, Any] = {"status": status, "limit": limit}
    if query:
        params["q"] = query
    if attached == "true":
        params["attached"] = True
    elif attached == "false":
        params["attached"] = False
    resp = await client.get(url, headers=_auth_headers(token), params=params, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"List seeds failed: HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Unexpected list response shape: missing items[]")
    return [i for i in items if isinstance(i, dict) and i.get("id")]


async def _get_seed(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    seed_id: str,
    timeout: float,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/employee/products/external-seeds/{seed_id}"
    resp = await client.get(url, headers=_auth_headers(token), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Get seed {seed_id} failed: HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    seed = payload.get("seed")
    if not isinstance(seed, dict):
        raise RuntimeError(f"Unexpected get seed response for {seed_id}")
    return seed


async def _refresh_seed(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    seed_id: str,
    timeout: float,
) -> Tuple[bool, str]:
    url = f"{base_url.rstrip('/')}/employee/products/external-seeds/{seed_id}/refresh"
    resp = await client.post(url, headers=_auth_headers(token), timeout=timeout)
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    payload = resp.json()
    status = str(payload.get("status") or "")
    if status == "success":
        return True, "success"
    if status == "degraded":
        return False, f"degraded: {payload.get('error')}"
    return False, f"unexpected_status: {status}"


async def main_async(args: argparse.Namespace) -> int:
    base_url = str(args.base_url or "").strip()
    if not base_url:
        raise SystemExit("--base-url is required")

    async with httpx.AsyncClient() as client:
        token = (args.token or "").strip()
        if not token:
            email = (args.email or "").strip()
            password = (args.password or "").strip()
            if not email or not password:
                raise SystemExit("Missing auth: set --token or provide --email/--password (or env vars).")
            token = await _login_for_token(client, base_url, email, password, args.timeout)

        seed_ids: List[str] = []
        seed_ids.extend([s.strip() for s in (args.seed_id or []) if str(s).strip()])
        if args.seed_ids_file:
            seed_ids.extend(_load_seed_ids_from_file(args.seed_ids_file))

        selected: List[Dict[str, Any]] = []
        if seed_ids:
            # Convert to seed-like dicts; we may fetch variants_count later if needed.
            selected = [{"id": sid} for sid in seed_ids]
        else:
            selected = await _list_seeds(
                client,
                base_url,
                token,
                query=args.query,
                attached=args.attached,
                status=args.status,
                limit=args.limit,
                timeout=args.timeout,
            )

        if args.only_if_variants_le is not None:
            threshold = int(args.only_if_variants_le)
            filtered: List[Dict[str, Any]] = []
            for item in selected:
                vc = item.get("variants_count")
                if vc is None:
                    # For explicit seed ids, fetch the current seed state to get variants_count.
                    seed = await _get_seed(client, base_url, token, str(item["id"]), args.timeout)
                    vc = seed.get("variants_count")
                    item["variants_count"] = vc
                    item["title"] = item.get("title") or seed.get("title")
                    item["domain"] = item.get("domain") or seed.get("domain")
                if isinstance(vc, int) and vc <= threshold:
                    filtered.append(item)
            selected = filtered

        if args.dry_run:
            for item in selected:
                sid = str(item.get("id"))
                vc = item.get("variants_count")
                title = item.get("title")
                domain = item.get("domain")
                print(f"{sid}\tvariants={vc}\tdomain={domain}\ttitle={title}")
            return 0

        start = time.time()
        sem = asyncio.Semaphore(max(1, int(args.concurrency)))

        results: List[Dict[str, Any]] = []

        async def _worker(item: Dict[str, Any]) -> None:
            seed_id = str(item.get("id"))
            if not seed_id:
                return
            async with sem:
                before_vc = item.get("variants_count")
                try:
                    if before_vc is None:
                        seed_before = await _get_seed(client, base_url, token, seed_id, args.timeout)
                        before_vc = seed_before.get("variants_count")

                    ok, msg = await _refresh_seed(client, base_url, token, seed_id, args.timeout)

                    after_vc = None
                    try:
                        seed_after = await _get_seed(client, base_url, token, seed_id, args.timeout)
                        after_vc = seed_after.get("variants_count")
                    except Exception:
                        after_vc = None

                    results.append(
                        {
                            "id": seed_id,
                            "ok": bool(ok),
                            "message": msg,
                            "variants_before": before_vc,
                            "variants_after": after_vc,
                        }
                    )
                except Exception as exc:
                    results.append({"id": seed_id, "ok": False, "message": str(exc)[:200]})

        await asyncio.gather(*[_worker(item) for item in selected])

    elapsed = time.time() - start
    ok_count = sum(1 for r in results if r.get("ok"))
    fail_count = len(results) - ok_count

    if args.json_output:
        print(
            json.dumps(
                {
                    "status": "success" if fail_count == 0 else "partial",
                    "total": len(results),
                    "ok": ok_count,
                    "failed": fail_count,
                    "elapsed_s": round(elapsed, 3),
                    "results": results,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"Done. total={len(results)} ok={ok_count} failed={fail_count} elapsed={elapsed:.1f}s")
        for r in sorted(results, key=lambda x: str(x.get("id"))):
            vb = r.get("variants_before")
            va = r.get("variants_after")
            print(f"- {r.get('id')}: {r.get('message')} variants {vb} -> {va}")

    return 0 if fail_count == 0 else 2


def main() -> None:
    args = _parse_args()
    try:
        code = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(code)


if __name__ == "__main__":
    main()

