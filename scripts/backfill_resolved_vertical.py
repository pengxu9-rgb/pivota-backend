"""Backfill durable catalog_products.resolved_vertical (Fix Plan B — T2).

The external-seed mirror lane historically OMITTED resolved_vertical entirely,
so ~83% of the catalog (11,584 of 12,381 rows on 2026-07-12) had a NULL
top-level vertical and could not be filtered / grounded on by frontier agents.
Fix Plan B T1 wires resolve_vertical into the mirror lane go-forward; THIS script
heals the existing NULL rows.

What it does:
  - Selects catalog_products WHERE resolved_vertical IS NULL, INCLUDING suppressed
    rows (they may be un-suppressed later; resolving is cheap and deterministic).
  - Re-derives the vertical with services.vertical_profiles.resolve_vertical using
    the SAME signal set the two write sites use: category / product_type /
    category_path + a title blob folding title + description + tags.
  - Writes back ONLY the resolved_vertical column (never any other column). The
    resolver never returns NULL — it returns 'other' when nothing matches — so a
    full pass drives the NULL count to 0.

Properties:
  - DETERMINISTIC + cheap: pure keyword resolution, no LLM, no network.
  - RESUMABLE: keyset pagination on product_key (ORDER BY product_key ASC,
    product_key > :cursor). A crashed / stalled run is restarted by simply
    re-invoking — already-written rows drop out of the IS NULL filter and the
    cursor skips past processed keys.
  - BATCHED (default 500): the public DB proxy stalls on large single scans, so
    every batch is a bounded fetch+update with bounded retry. If a batch keeps
    failing after --max-retries, the script STOPS loudly rather than thrashing.
  - IDEMPOTENT: the UPDATE guards `resolved_vertical IS NULL`, so a row a
    concurrent writer already resolved is never overwritten.
  - --dry-run: prints the would-write distribution + a sample of resolutions and
    writes NOTHING.

Does NOT depend on migration 173 having run — db/schema_guard.py:663 self-heals
the column, matching the mirror/backfill convention (Railway skips migrations).

Usage:
  python -m scripts.backfill_resolved_vertical --dry-run
  python -m scripts.backfill_resolved_vertical            # apply, all NULL rows
  python -m scripts.backfill_resolved_vertical --batch-size 500 --max-batches 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional

from db.database import database
from services.vertical_profiles import resolve_vertical

# The verticals resolve_vertical can return; the report always lists all four so
# a zero bucket is explicit rather than missing.
_VERTICALS = ("beauty", "fashion", "electronics", "other")


async def _connect_if_needed(db: Any) -> bool:
    was = bool(getattr(db, "is_connected", False))
    if not was and callable(getattr(db, "connect", None)):
        await db.connect()
    return was


async def _disconnect_if_needed(db: Any, was: bool) -> None:
    if not was and bool(getattr(db, "is_connected", False)) and callable(getattr(db, "disconnect", None)):
        await db.disconnect()


def _coerce_tags(raw: Any) -> List[str]:
    """tags is a jsonb array; the driver may hand it back as a list OR a JSON
    string depending on the connection. Coerce to a list of strings; never raise."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(t) for t in raw if t is not None and str(t).strip()]
    return []


def _title_blob(row: Dict[str, Any]) -> str:
    """Same title signal the two write sites build: title + description + tags,
    space-joined, empties dropped."""
    parts = [row.get("title"), row.get("description"), *(_coerce_tags(row.get("tags")))]
    return " ".join(str(p) for p in parts if p)


def _resolve_for_row(row: Dict[str, Any]) -> str:
    return resolve_vertical(
        {
            "product_type": row.get("product_type"),
            "category": row.get("category"),
            "category_path": row.get("category_path"),
        },
        title=_title_blob(row),
    )


_SELECT_SQL = """
    SELECT product_key, category, product_type, category_path, title, description, tags
    FROM catalog_products
    WHERE resolved_vertical IS NULL
      AND product_key > :cursor
    ORDER BY product_key ASC
    LIMIT :batch_size
"""

# Writes ONLY resolved_vertical; guards IS NULL so a concurrently-resolved row is
# never clobbered.
_UPDATE_SQL = """
    UPDATE catalog_products
    SET resolved_vertical = :resolved_vertical
    WHERE product_key = :product_key AND resolved_vertical IS NULL
"""


async def _fetch_batch(db: Any, cursor: str, batch_size: int) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(_SELECT_SQL, {"cursor": cursor, "batch_size": batch_size})
    return [dict(r) for r in rows]


async def _with_retry(coro_factory, *, max_retries: int, base_delay: float, label: str):
    """Run an awaitable factory with bounded exponential backoff. Raises the last
    error if every attempt fails — the caller STOPS rather than thrashing."""
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001 — transient proxy stalls are opaque
            attempt += 1
            if attempt > max_retries:
                raise RuntimeError(
                    f"{label} failed after {max_retries} retries: {exc!r}"
                ) from exc
            delay = base_delay * (2 ** (attempt - 1))
            print(
                f"WARN: {label} errored (attempt {attempt}/{max_retries}): {exc!r}; "
                f"retrying in {delay:.1f}s",
                flush=True,
            )
            await asyncio.sleep(delay)


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was = await _connect_if_needed(db)
    counts: Dict[str, int] = {v: 0 for v in _VERTICALS}
    samples: List[Dict[str, Any]] = []
    scanned = 0
    written = 0
    batches = 0
    cursor = ""  # keyset start; product_key > '' matches all rows
    try:
        while True:
            if args.max_batches and batches >= args.max_batches:
                break
            rows = await _with_retry(
                lambda: _fetch_batch(db, cursor, args.batch_size),
                max_retries=args.max_retries,
                base_delay=args.retry_base_delay,
                label="fetch_batch",
            )
            if not rows:
                break
            batches += 1
            for row in rows:
                scanned += 1
                resolved = _resolve_for_row(row)
                counts[resolved] = counts.get(resolved, 0) + 1
                if len(samples) < args.sample_size:
                    samples.append(
                        {
                            "product_key": row.get("product_key"),
                            "title": row.get("title"),
                            "product_type": row.get("product_type"),
                            "category": row.get("category"),
                            "category_path": row.get("category_path"),
                            "resolved_vertical": resolved,
                        }
                    )
                if not args.dry_run:
                    await _with_retry(
                        lambda pk=row["product_key"], rv=resolved: db.execute(
                            _UPDATE_SQL, {"product_key": pk, "resolved_vertical": rv}
                        ),
                        max_retries=args.max_retries,
                        base_delay=args.retry_base_delay,
                        label="update_row",
                    )
                    written += 1
            # Advance the keyset cursor past this batch's last product_key.
            cursor = rows[-1]["product_key"]
            print(
                f"batch {batches}: scanned={scanned} written={written} "
                f"cursor={cursor!r}",
                flush=True,
            )
    finally:
        await _disconnect_if_needed(db, was)

    return {
        "dry_run": args.dry_run,
        "batch_size": args.batch_size,
        "batches_processed": batches,
        "scanned_null_vertical": scanned,
        "written": written,
        "distribution": counts,
        "other_share": (counts.get("other", 0) / scanned) if scanned else 0.0,
        "samples": samples,
    }


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Report the would-write distribution + samples; write nothing.")
    p.add_argument("--batch-size", type=int, default=500,
                   help="Rows per batch (default 500 — the proxy stalls on large scans).")
    p.add_argument("--max-batches", type=int, default=0,
                   help="Stop after N batches (0 = run until no NULL rows remain).")
    p.add_argument("--sample-size", type=int, default=25,
                   help="Number of resolution samples to include in the report.")
    p.add_argument("--max-retries", type=int, default=5,
                   help="Per-operation retry budget before the run STOPS.")
    p.add_argument("--retry-base-delay", type=float, default=1.0,
                   help="Base seconds for exponential backoff between retries.")
    return p.parse_args(argv)


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
