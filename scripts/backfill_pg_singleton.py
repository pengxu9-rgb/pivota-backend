#!/usr/bin/env python3
"""pg-singleton backfill (ADR-009 ratified decision 1 — the no-fallback amendment).

Makes `product_group_id` TOTAL for the decision layer: every catalog product that
carries a `content_key` but is NOT yet a member of any product group gets a
deterministic SINGLETON group (`pg_<content_key hex>`), materialized as a
`product_group_members` row — the table `offers.resolve` reads
(`routes/agent_shop_gateway.py`). Once pg is total the offer path keys on pg with
ZERO branching and the former merchant-scoped `pc:` fallback is dead code.

DRY-RUN BY DEFAULT; `--execute` is required for any write, and dry-run is strictly
READ-ONLY (safe against prod). Mirrors A9-4's shape
(`scripts/backfill_seller_of_record.py`): resumable checkpoint + per-batch parity
+ JSON report.

Required reading (cited throughout):
  - `docs/adr/ADR-009-seller-of-record-identity.md` — D1 (product layer is
    cross-merchant, seller-free) + "Resolved decisions" 1 (offer keys on pg
    UNCONDITIONALLY; where a product has no pg, mint a deterministic singleton
    from its content_key; content_key is a derivation INPUT, never a runtime
    alternative).
  - `docs/IDENTITY_REFERENCE.md` — §2 (`content_key` cross-merchant / `pg`),
    Trap T5 (sigs write-once & public — FROZEN here), Trap T6 (content_key is
    cross-merchant → the singleton is keyed on content_key ALONE, so two
    merchants' listings of one physical product share the singleton — correct
    grouping, NOT a merge of distinct products; `brand_verified_graduation.py:71`
    "mis-merge is worse than fragmentation").

INVARIANTS (loud over silent — founder no-fallback directive):
  - We ONLY ever INSERT into `product_group_members` (ON CONFLICT DO NOTHING).
    We NEVER touch `catalog_products` — so sigs/canonical URLs are FROZEN by
    construction; the per-batch sig snapshot is the tripwire that proves it.
  - We NEVER overwrite an existing group membership: the plan filters
    `NOT EXISTS (a membership row)` AND the write is `ON CONFLICT DO NOTHING`.
    A row already in a real/curated group (autogrouper cluster, variant
    promoter, manual governance) is SKIPPED — no auto-merge, no re-key.
  - Two rows that share a `content_key` legitimately share the singleton pg —
    that is CORRECT grouping of ONE physical product, not a merge. Distinct
    content_keys always map to distinct pgs.
  - Rows with `content_key IS NULL` CANNOT be minted (no canonical identity yet).
    They are COUNTED to a review number and LEFT pg-NULL — never force-minted
    from nothing. Downstream (`offers.resolve`) treats them as
    `no_canonical_identity`, honestly.

Usage:
  # dry-run (read-only; safe against prod)
  .venv/bin/python -m scripts.backfill_pg_singleton --db-url "$DATABASE_URL"

  # execute (writes) — required flag
  .venv/bin/python -m scripts.backfill_pg_singleton --db-url ... --execute --batch-size 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backfill_pg_singleton")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class SingletonBackfillReport:
    mode: str
    started_at: str
    batch_size: int
    counts: Dict[str, int] = field(default_factory=dict)
    planned: int = 0
    minted: int = 0
    skipped_already_grouped: int = 0
    review_null_content_key: int = 0
    batches: int = 0
    parity: List[Dict[str, Any]] = field(default_factory=list)
    epilogue: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "started_at": self.started_at,
            "batch_size": self.batch_size,
            "counts": self.counts,
            "planned": self.planned,
            "minted": self.minted,
            "skipped_already_grouped": self.skipped_already_grouped,
            "review_null_content_key": self.review_null_content_key,
            "batches": self.batches,
            "parity": self.parity,
            "epilogue": self.epilogue,
        }


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


# A product-scoped SELECT. `NOT EXISTS` gives us exactly the pg-NULL rows that
# CAN be minted (content_key present, no membership row yet). Ordered by
# product_key for a stable, resumable batch order.
_PLAN_SQL = """
    SELECT cp.product_key, cp.merchant_id, cp.platform, cp.source_product_id,
           cp.content_key, cp.pivota_signature_id, cp.pivota_canonical_url
      FROM catalog_products cp
     WHERE cp.content_key IS NOT NULL
       AND btrim(cp.content_key) <> ''
       AND NOT EXISTS (
             SELECT 1 FROM product_group_members m
              WHERE m.merchant_id = cp.merchant_id
                AND m.platform = cp.platform
                AND m.platform_product_id = cp.source_product_id
           )
     ORDER BY cp.product_key
"""


class SingletonBackfill:
    def __init__(self, *, database: Any, mint_mod: Any, execute: bool, batch_size: int):
        self.db = database
        self.mint = mint_mod  # services.product_group_autogrouper (reused, not reimplemented)
        self.execute = execute
        self.batch_size = max(1, int(batch_size))

    # -- discovery -----------------------------------------------------------

    async def scan_counts(self) -> Dict[str, int]:
        async def _count(sql: str) -> int:
            row = await self.db.fetch_one(sql)
            return int(dict(row)["c"]) if row else 0

        return {
            # rows that CAN be minted (content_key present, no membership yet)
            "catalog_missing_pg": await _count(
                "SELECT count(*) AS c FROM catalog_products cp "
                "WHERE cp.content_key IS NOT NULL AND btrim(cp.content_key) <> '' "
                "AND NOT EXISTS (SELECT 1 FROM product_group_members m "
                "WHERE m.merchant_id=cp.merchant_id AND m.platform=cp.platform "
                "AND m.platform_product_id=cp.source_product_id)"
            ),
            # rows that CANNOT be minted — no canonical identity yet (review)
            "catalog_null_content_key": await _count(
                "SELECT count(*) AS c FROM catalog_products "
                "WHERE content_key IS NULL OR btrim(content_key) = ''"
            ),
            # rows already carrying a membership row (skipped — never overwritten)
            "catalog_already_grouped": await _count(
                "SELECT count(*) AS c FROM catalog_products cp "
                "WHERE EXISTS (SELECT 1 FROM product_group_members m "
                "WHERE m.merchant_id=cp.merchant_id AND m.platform=cp.platform "
                "AND m.platform_product_id=cp.source_product_id)"
            ),
        }

    async def plan(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(_PLAN_SQL)
        return [dict(r) for r in rows or []]

    # -- parity --------------------------------------------------------------

    async def _membership_count(self, batch: List[Dict[str, Any]]) -> int:
        """How many of this batch's listings currently have a membership row."""
        if not batch:
            return 0
        # Match by the (merchant, platform, platform_product_id) PK tuple. We
        # count via a VALUES join so we don't have to trust any single column.
        n = 0
        for b in batch:
            row = await self.db.fetch_one(
                "SELECT count(*) AS c FROM product_group_members "
                "WHERE merchant_id=:m AND platform=:p AND platform_product_id=:s",
                {"m": b["merchant_id"], "p": b["platform"], "s": b["source_product_id"]},
            )
            n += int(dict(row)["c"]) if row else 0
        return n

    async def _sig_snapshot(self, product_keys: List[str]) -> Dict[str, tuple]:
        """FROZEN-sig tripwire (IDENTITY_REFERENCE T5): capture the batch's
        (pivota_signature_id, pivota_canonical_url). We never write
        catalog_products, so this MUST be byte-identical before/after."""
        if not product_keys:
            return {}
        rows = await self.db.fetch_all(
            "SELECT product_key, pivota_signature_id, pivota_canonical_url "
            "FROM catalog_products WHERE product_key = ANY(:pks)",
            {"pks": product_keys},
        )
        return {
            str(dict(r)["product_key"]): (
                dict(r).get("pivota_signature_id"),
                dict(r).get("pivota_canonical_url"),
            )
            for r in rows or []
        }

    # -- execution -----------------------------------------------------------

    async def _mint_batch(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        pks = [str(b["product_key"]) for b in batch]
        # BEFORE: every planned row has NO membership (plan filtered NOT EXISTS);
        # snapshot sigs to prove we never touch catalog_products.
        before_members = await self._membership_count(batch)
        before_sigs = await self._sig_snapshot(pks)
        expected_pgs = {
            str(b["product_key"]): self.mint.make_singleton_product_group_id(b["content_key"])
            for b in batch
        }

        if not self.execute:
            return {
                "ok": True,
                "mode": "dry_run",
                "product_count": len(pks),
                "members_before": before_members,
                "would_mint": len(pks),
                "distinct_pgs": len(set(expected_pgs.values())),
            }

        # Materialize each singleton via the SAME helper ingestion uses (reuse,
        # not reimplement). ON CONFLICT DO NOTHING inside the helper.
        async with self.db.transaction():
            for b in batch:
                await self.mint.ensure_singleton_group_membership(
                    merchant_id=str(b["merchant_id"]),
                    platform=str(b["platform"]),
                    source_product_id=str(b["source_product_id"]),
                    content_key=b["content_key"],
                    db=self.db,
                )

        # AFTER: parity assertions — loud abort on any violation.
        after_members = await self._membership_count(batch)
        after_sigs = await self._sig_snapshot(pks)
        # Each planned listing must now have exactly its one membership row.
        members_ok = after_members == before_members + len(batch)
        sigs_frozen = before_sigs == after_sigs
        # The pg each row now points at must be the deterministic singleton.
        pg_correct = True
        placed = await self.db.fetch_all(
            "SELECT cp.product_key, m.product_group_id "
            "FROM catalog_products cp JOIN product_group_members m "
            "ON m.merchant_id=cp.merchant_id AND m.platform=cp.platform "
            "AND m.platform_product_id=cp.source_product_id "
            "WHERE cp.product_key = ANY(:pks)",
            {"pks": pks},
        )
        for r in placed or []:
            d = dict(r)
            if expected_pgs.get(str(d["product_key"])) != str(d["product_group_id"]):
                pg_correct = False
                break

        parity = {
            "ok": members_ok and sigs_frozen and pg_correct,
            "product_count": len(pks),
            "members_before": before_members,
            "members_after": after_members,
            "members_ok": members_ok,
            "sigs_frozen": sigs_frozen,
            "pg_correct": pg_correct,
            "distinct_pgs": len(set(expected_pgs.values())),
        }
        if not parity["ok"]:
            raise RuntimeError(
                f"pg-singleton batch parity FAILED (loud abort, no silent absorb): {parity}"
            )
        for pk in pks:
            await self._checkpoint(pk, expected_pgs[pk])
        return parity

    async def run(self, report: SingletonBackfillReport) -> None:
        report.counts = await self.scan_counts()
        report.review_null_content_key = report.counts.get("catalog_null_content_key", 0)
        report.skipped_already_grouped = report.counts.get("catalog_already_grouped", 0)

        planned = await self.plan()
        report.planned = len(planned)
        if report.review_null_content_key:
            logger.warning(
                "pg-singleton: %d catalog rows have NULL content_key — CANNOT mint "
                "(no canonical identity yet); routed to review count, left pg-NULL.",
                report.review_null_content_key,
            )

        for i in range(0, len(planned), self.batch_size):
            batch = planned[i:i + self.batch_size]
            parity = await self._mint_batch(batch)
            report.parity.append(parity)
            report.batches += 1
            if self.execute:
                report.minted += parity.get("product_count", 0)

    # -- checkpoint / resume -------------------------------------------------

    async def ensure_checkpoint_table(self) -> None:
        if not self.execute:
            return
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS pg_singleton_backfill_checkpoint (
                product_key TEXT PRIMARY KEY,
                product_group_id TEXT,
                status TEXT NOT NULL DEFAULT 'done',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    async def _checkpoint(self, product_key: str, pg: Optional[str]) -> None:
        if not self.execute:
            return
        await self.db.execute(
            """
            INSERT INTO pg_singleton_backfill_checkpoint
                (product_key, product_group_id, status, updated_at)
            VALUES (:pk, :pg, 'done', NOW())
            ON CONFLICT (product_key)
            DO UPDATE SET product_group_id = EXCLUDED.product_group_id,
                          status = EXCLUDED.status, updated_at = NOW()
            """,
            {"pk": product_key, "pg": pg},
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_backfill(*, database: Any, mint_mod: Any, execute: bool,
                       batch_size: int) -> SingletonBackfillReport:
    report = SingletonBackfillReport(
        mode="execute" if execute else "dry_run",
        started_at=datetime.now(timezone.utc).isoformat(),
        batch_size=batch_size,
    )
    bf = SingletonBackfill(database=database, mint_mod=mint_mod, execute=execute,
                           batch_size=batch_size)
    await bf.ensure_checkpoint_table()
    await bf.run(report)
    report.epilogue = [
        "pg-singleton backfill is ADDITIVE: it ONLY inserts product_group_members "
        "rows (ON CONFLICT DO NOTHING) and NEVER touches catalog_products — sigs "
        "are FROZEN by construction (per-batch byte-identical sig parity is the "
        "tripwire).",
        "A singleton pg is byte-identical to what the autogrouper would mint for "
        "the same content_key, so a singleton that later gains a second listing "
        "grows seamlessly into a real multi-member group under the SAME pg — no "
        "re-key, no fork, no merge of distinct products.",
        "NULL-content_key rows are LEFT pg-NULL (review count) — never force-minted; "
        "offers.resolve reports them as no_canonical_identity, honestly.",
    ]
    return report


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="pg-singleton backfill (dry-run default).")
    ap.add_argument("--db-url", default=os.getenv("DATABASE_URL", ""),
                    help="Postgres URL (defaults to $DATABASE_URL).")
    ap.add_argument("--execute", action="store_true",
                    help="Perform writes. WITHOUT this flag the run is READ-ONLY (dry-run).")
    ap.add_argument("--batch-size", type=int, default=200,
                    help="Listings per mint batch (default 200).")
    ap.add_argument("--report", default=None,
                    help="Path to write the JSON report.")
    return ap.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    if not args.db_url:
        print("ERROR: --db-url or $DATABASE_URL is required.", file=sys.stderr)
        return 2
    from db.database import DATABASE_URL as _BOUND_URL, database
    from services import product_group_autogrouper as mint_mod

    if args.db_url not in (_BOUND_URL, _BOUND_URL.replace("postgresql+asyncpg://", "postgresql://", 1)):
        print(
            f"ERROR: db.database is bound to {_BOUND_URL[:60]!r} but --db-url is "
            f"{args.db_url[:60]!r}. Invoke with DATABASE_URL set in the environment, "
            f"or let this script re-exec.",
            file=sys.stderr,
        )
        return 3

    await database.connect()
    try:
        report = await run_backfill(
            database=database, mint_mod=mint_mod, execute=args.execute,
            batch_size=args.batch_size,
        )
    finally:
        await database.disconnect()

    out = json.dumps(report.to_json(), indent=2, default=str)
    print(out)
    report_path = args.report or str(
        Path(os.getenv("SCRATCHPAD_DIR", ".")) / f"pg_singleton_backfill_report_{report.mode}.json"
    )
    try:
        Path(report_path).write_text(out)
        print(f"\n[report written] {report_path}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[report write failed] {exc}", file=sys.stderr)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    if args.db_url and os.environ.get("DATABASE_URL", "") != args.db_url \
            and os.environ.get("_PG_SINGLETON_REEXEC") != "1":
        env = dict(os.environ)
        env["DATABASE_URL"] = args.db_url
        env["_PG_SINGLETON_REEXEC"] = "1"
        os.execve(sys.executable,
                  [sys.executable, "-m", "scripts.backfill_pg_singleton", *(argv or sys.argv[1:])],
                  env)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
