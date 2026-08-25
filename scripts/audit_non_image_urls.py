#!/usr/bin/env python3
"""Audit served image fields for URLs that are not images.

Dry-run by default. Finds rows whose image field holds something that is not an
image — most often the product's own PDP page URL — and optionally prunes them.

Why this exists
---------------
/api/image-proxy used to relay the upstream content-type verbatim, so a page URL
sitting in an image field was served as ``200 text/html`` and every consumer
treated it as an image. The proxy now falls back to a placeholder for a non-image
upstream (pivota-agent-ui#312), so nothing is *served* wrong any more. This
script cleans the underlying rows so the gallery stops carrying a dead slot.

Detection is two-stage, and BOTH stages must agree before --apply touches a row:

  1. structural  -- the URL looks like a page (``.html``/``.php``/...), or it is
     the row's own canonical/destination URL. Cheap, runs in SQL+Python.
  2. probe       -- fetch it and read the content-type. This is the ground truth
     and it is what makes the audit trustworthy: plenty of perfectly good CDN
     images carry no file extension at all (media.ultainc.com/i/ulta/2609862),
     so a "does not look like an image" rule alone would be a false-positive
     machine. Stage 1 only decides WHAT to probe; stage 2 decides what is broken.

Repair is deliberately conservative:

  * A bad entry is dropped from ``agent_pdp_view.image_urls``.
  * A bad scalar ``image_url`` is replaced by the first surviving gallery entry,
    never blindly NULLed.
  * If a row would be left with NO image at all, it is reported as
    ``needs_review`` and SKIPPED. Emptying a gallery can flip serving-eligibility
    downstream, and that is not a decision an image-hygiene script gets to make.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402

CONFIRM_TOKEN = "AUDIT_NON_IMAGE_URL_PRUNE"

# A URL is a CANDIDATE if it looks like a document. This is intentionally a
# denylist: an allowlist ("must end in .jpg") would flag every extensionless CDN
# image in the catalog. Candidates are then probed before anything is believed.
_PAGE_PATH_RE = re.compile(r"\.(?:html?|php|aspx?|jspx?|cfm)$", re.IGNORECASE)

_IMAGE_CONTENT_TYPE_RE = re.compile(r"^image/", re.IGNORECASE)

# Serving fields, in the order a reader falls back through them.
_SCAN_SQL = """
    SELECT
        v.content_key,
        v.pivota_signature_id,
        v.product_group_id,
        v.brand,
        v.title,
        v.image_url,
        v.image_urls,
        v.primary_merchant_id,
        s.canonical_url,
        s.destination_url
    FROM agent_pdp_view v
    LEFT JOIN LATERAL (
        SELECT canonical_url, destination_url
        FROM external_product_seeds
        WHERE attached_product_key = v.content_key
        ORDER BY updated_at DESC NULLS LAST
        LIMIT 1
    ) s ON TRUE
    WHERE (
        v.image_url IS NOT NULL
        OR (v.image_urls IS NOT NULL AND jsonb_typeof(v.image_urls) = 'array'
            AND jsonb_array_length(v.image_urls) > 0)
    )
    ORDER BY v.content_key
"""


def _build_scan_sql(args: argparse.Namespace) -> Tuple[str, Dict[str, Any]]:
    """Compose the scan query and its params.

    The base WHERE is a parenthesised OR group ON PURPOSE: AND binds tighter than
    OR, so appending `AND content_key = :x` to a bare `a OR b` would parse as
    `a OR (b AND ...)` and silently return every row with an image_url, ignoring
    the filter entirely.
    """
    sql = _SCAN_SQL
    params: Dict[str, Any] = {}
    filters: List[str] = []
    if args.content_key:
        filters.append("v.content_key = :content_key")
        params["content_key"] = args.content_key
    if args.brand:
        filters.append("lower(v.brand) = lower(:brand)")
        params["brand"] = args.brand
    if filters:
        sql = sql.replace(
            "ORDER BY v.content_key",
            "AND " + " AND ".join(filters) + "\n    ORDER BY v.content_key",
            1,
        )
    if args.limit:
        sql += f"\n    LIMIT {int(args.limit)}"
    return sql, params


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=0, help="0 = all rows")
    p.add_argument("--brand", default=None, help="restrict to one brand")
    p.add_argument("--content-key", default=None,
                   help="audit a single agent_pdp_view row")
    p.add_argument("--no-probe", action="store_true",
                   help="skip the network probe. Reports structural candidates "
                        "only and REFUSES --apply, since nothing is confirmed.")
    p.add_argument("--probe-timeout", type=float, default=15.0)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--out-json", default=None)
    p.add_argument("--apply", action="store_true",
                   help="prune confirmed non-image URLs")
    p.add_argument("--confirm", default="",
                   help=f"required with --apply: {CONFIRM_TOKEN}")
    p.add_argument("--max-changes", type=int, default=25,
                   help="refuse --apply if more rows than this would change (0=off)")
    return p.parse_args(argv)


def _as_list(value: Any) -> List[str]:
    """agent_pdp_view.image_urls is JSONB and arrives as list OR json string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _strip_query(url: str) -> str:
    return str(url or "").split("?", 1)[0].split("#", 1)[0]


def _structural_reason(url: str, page_urls: Tuple[str, ...]) -> Optional[str]:
    """Why this URL is worth probing. None = looks fine, do not probe."""
    bare = _strip_query(url)
    if not bare:
        return None
    try:
        path = urlparse(bare).path
    except ValueError:
        return "unparseable"
    if _PAGE_PATH_RE.search(path):
        return "page_extension"
    if bare and bare in page_urls:
        return "equals_product_page_url"
    return None


async def _probe(url: str, timeout: float) -> Dict[str, Any]:
    """Fetch and report the content-type. Never raises."""
    import httpx

    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # GET, not HEAD: several merchant CDNs answer HEAD with 405 or with a
            # content-type that differs from the real GET response.
            resp = await client.get(url, headers=headers)
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            return {
                "ok": True,
                "status": resp.status_code,
                "content_type": ctype,
                "is_image": bool(ctype and _IMAGE_CONTENT_TYPE_RE.match(ctype)),
            }
    except Exception as exc:  # noqa: BLE001 - a probe failure is data, not a crash
        return {
            "ok": False,
            "status": None,
            "content_type": None,
            "is_image": None,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


def _plan_repair(row: Dict[str, Any], bad: set) -> Dict[str, Any]:
    """Decide the new (image_url, image_urls) for a row, or refuse."""
    gallery = _as_list(row.get("image_urls"))
    scalar = (row.get("image_url") or "").strip()

    kept = [u for u in gallery if u not in bad]
    scalar_bad = bool(scalar) and scalar in bad

    # Never leave a row with no image at all.
    if not kept and (scalar_bad or not scalar):
        return {"action": "needs_review",
                "why": "pruning would leave the row with no image"}

    new_scalar = scalar
    if scalar_bad:
        new_scalar = kept[0] if kept else scalar

    changed_gallery = kept != gallery
    changed_scalar = new_scalar != scalar
    if not changed_gallery and not changed_scalar:
        return {"action": "noop"}

    return {
        "action": "prune",
        "image_url": {"from": scalar, "to": new_scalar} if changed_scalar else None,
        "image_urls": {"removed": [u for u in gallery if u in bad],
                       "remaining": len(kept)} if changed_gallery else None,
        "_new_scalar": new_scalar,
        "_new_gallery": kept,
    }


async def _run(args: argparse.Namespace) -> int:
    if args.apply and args.no_probe:
        print(f"REFUSED: --apply needs the probe to confirm a URL is not an image.\n"
              f"         Drop --no-probe.")
        return 2
    if args.apply and args.confirm != CONFIRM_TOKEN:
        print(f"REFUSED: --apply requires --confirm {CONFIRM_TOKEN}")
        return 2

    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        sql, params = _build_scan_sql(args)

        rows = [dict(r) for r in await database.fetch_all(sql, params)]
        print(f"scanned {len(rows)} agent_pdp_view rows carrying at least one image")

        # ---- stage 1: structural candidates -------------------------------
        candidates: List[Tuple[Dict[str, Any], str, str]] = []
        for row in rows:
            page_urls = tuple(
                _strip_query(u) for u in (row.get("canonical_url"), row.get("destination_url")) if u
            )
            seen = set()
            for url in ([row["image_url"]] if row.get("image_url") else []) + _as_list(row.get("image_urls")):
                if url in seen:
                    continue
                seen.add(url)
                reason = _structural_reason(url, page_urls)
                if reason:
                    candidates.append((row, url, reason))

        by_reason = Counter(r for _, _, r in candidates)
        print(f"stage 1 (structural): {len(candidates)} candidate URLs across "
              f"{len({r['content_key'] for r, _, _ in candidates})} rows  {dict(by_reason)}")

        if not candidates:
            print("\nnothing to do — no structural candidates found")
            if args.out_json:
                Path(args.out_json).write_text(json.dumps(
                    {"scanned": len(rows), "candidates": [], "rows": []}, indent=2))
            return 0

        # ---- stage 2: probe -----------------------------------------------
        findings: List[Dict[str, Any]] = []
        if args.no_probe:
            print("stage 2 (probe): SKIPPED (--no-probe) — nothing is confirmed")
            for row, url, reason in candidates:
                findings.append({"content_key": row["content_key"], "url": url,
                                 "reason": reason, "probe": None, "confirmed": None})
        else:
            sem = asyncio.Semaphore(max(1, args.concurrency))
            probe_cache: Dict[str, Dict[str, Any]] = {}

            async def one(url: str) -> None:
                async with sem:
                    probe_cache[url] = await _probe(url, args.probe_timeout)

            await asyncio.gather(*(one(u) for u in {c[1] for c in candidates}))
            for row, url, reason in candidates:
                pr = probe_cache[url]
                findings.append({
                    "content_key": row["content_key"],
                    "sig": row.get("pivota_signature_id"),
                    "brand": row.get("brand"),
                    "title": row.get("title"),
                    "url": url,
                    "reason": reason,
                    "probe": pr,
                    "confirmed": (pr["is_image"] is False) if pr["ok"] else None,
                })
            confirmed_n = sum(1 for f in findings if f["confirmed"] is True)
            unknown_n = sum(1 for f in findings if f["confirmed"] is None)
            cleared_n = sum(1 for f in findings if f["confirmed"] is False)
            print(f"stage 2 (probe): {confirmed_n} confirmed non-image, "
                  f"{cleared_n} cleared (really are images), {unknown_n} unreachable")

        # ---- plan ----------------------------------------------------------
        bad_by_row: Dict[str, set] = {}
        for f in findings:
            if f["confirmed"] is True:
                bad_by_row.setdefault(f["content_key"], set()).add(f["url"])

        rows_by_key = {r["content_key"]: r for r in rows}
        plans: List[Dict[str, Any]] = []
        for key, bad in bad_by_row.items():
            plan = _plan_repair(rows_by_key[key], bad)
            if plan["action"] == "noop":
                continue
            row = rows_by_key[key]
            plans.append({"content_key": key, "sig": row.get("pivota_signature_id"),
                          "brand": row.get("brand"), "title": row.get("title"), **plan})

        prunable = [p for p in plans if p["action"] == "prune"]
        review = [p for p in plans if p["action"] == "needs_review"]

        print(f"\nplan: {len(prunable)} row(s) prunable, {len(review)} need review")
        for p in prunable[:20]:
            print(f"  PRUNE  {p['content_key']}  {(p.get('brand') or '?')} — {(p.get('title') or '')[:52]}")
            if p.get("image_urls"):
                for u in p["image_urls"]["removed"]:
                    print(f"           - drop gallery entry: {u[:104]}")
                print(f"           gallery {p['image_urls']['remaining']} entries remain")
            if p.get("image_url"):
                print(f"           image_url: {p['image_url']['from'][:60]}")
                print(f"                  ->  {p['image_url']['to'][:60]}")
        for p in review[:20]:
            print(f"  REVIEW {p['content_key']}  {(p.get('brand') or '?')} — {p['why']}")

        if args.out_json:
            Path(args.out_json).write_text(json.dumps(
                {"scanned": len(rows), "findings": findings, "plans": plans}, indent=2, default=str))
            print(f"\nwrote {args.out_json}")

        # ---- apply ----------------------------------------------------------
        if not args.apply:
            print(f"\nDRY RUN — nothing written. To apply:\n"
                  f"  python scripts/audit_non_image_urls.py --apply --confirm {CONFIRM_TOKEN}")
            return 0

        if args.max_changes and len(prunable) > args.max_changes:
            print(f"\nREFUSED: {len(prunable)} rows would change, ceiling is "
                  f"--max-changes {args.max_changes}. Re-run with a higher ceiling "
                  f"once you have read the plan above.")
            return 2

        applied = 0
        for p in prunable:
            await database.execute(
                """
                UPDATE agent_pdp_view
                   SET image_url  = :image_url,
                       image_urls = CAST(:image_urls AS jsonb)
                 WHERE content_key = :content_key
                """,
                {"content_key": p["content_key"],
                 "image_url": p["_new_scalar"] or None,
                 "image_urls": json.dumps(p["_new_gallery"])},
            )
            applied += 1
            print(f"  applied {p['content_key']}")
        print(f"\napplied {applied} row(s); {len(review)} left for review")
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
