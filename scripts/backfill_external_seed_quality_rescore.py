"""Re-score already-onboarded external_brand_crawl products through the
ingredient-aware quality payload (this PR).

Products onboarded before this fix scored ~50 and stalled at low_quality: the
scorer never saw their ingredients and their product_type was null. This
backfill pulls each product's fields from catalog_products, its price from
external_product_seeds, and its INCI from beauty_sku_ingredients, rebuilds the
payload with build_servable_quality_payload(category=category_kind, raw_inci=...),
and re-runs make_external_seed_servable -> fresh product_quality_snapshot +
serving-eligibility recompute -> catalog_row_trust upsert.

TWO-STEP FLIP (the second step is why this script exists as a one-shot):
  1. make_external_seed_servable sets index_pipeline_state.serving_eligible.
  2. upsert_catalog_row_trust flips catalog_row_trust.serving_decision to
     `public` — the field public READERS actually gate on. Step 1 alone leaves a
     row serving_eligible-but-`blocked` until the phase-2d drift cron catches it
     (proven live 2026-07-23). The trust upsert runs in chunks for rows that
     actually became eligible, so a promotion is durable and a mid-run abort
     still publishes everything scored so far. Pass --skip-trust to do step 1
     only.

Idempotent + RESUMABLE (skips products already on the source-backed rules
version) + RESILIENT (one product's failure never aborts the run) + a
consecutive-failure circuit breaker for dead connections.

CONNECTION POISONING (fixed 2026-07-23): `asyncio.wait_for` cancels the in-flight
query on timeout, leaving the shared `databases` connection half-acquired, so every
LATER query dies with "Connection is already acquired". Because
make_external_seed_servable swallows its own errors, those deaths were invisible —
one timeout on row 1 silently voided the next 1,320 rows while still reporting
`ok=1322 fail=1`. Guards now:
  * the pool is recycled (BOUNDED — `pool.close()` can itself park on a dead
    socket, so it is capped and falls back to `terminate()`) after a hard failure,
    or after `_NO_WRITE_RECYCLE_AFTER` consecutive no-writes — not every no-write,
    which would tear the pool down per row and never stop under an alternating
    write/no-write pattern;
  * a write only counts when the snapshot PERSISTED *and* the identity lookup
    resolved a content_key, so a snapshot written under the fallback merchant
    (unfindable by the classifier, yet enough to make `_rescored_ids()` skip the
    product forever) is treated as a no-write and stays resumable;
  * hard failures and no-writes have SEPARATE breakers — legitimate `quality=False`
    rows cluster because FETCH is ORDER BY product_key, so sharing one breaker
    aborted healthy runs and blamed the connection;
  * progress reports `wrote` / `eligible` / `promoted` separately, so a step-1
    failure is distinguishable from a step-2 one.

Designed to run as a Railway job against the INTERNAL DB:
  Dry-run:  python -m scripts.backfill_external_seed_quality_rescore
  Apply:    python -m scripts.backfill_external_seed_quality_rescore --apply [--limit N]

Local run over the public proxy (db.database reads DATABASE_URL, which is the
internal railway.internal host under `railway run` and won't resolve locally):
  railway run -- bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" \
    python -m scripts.backfill_external_seed_quality_rescore --apply'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many
from services.external_seed_servability import (
    build_servable_quality_payload,
    make_external_seed_servable,
)
from services.product_quality_service import SOURCE_BACKED_COMPONENTS_RULES_VERSION

TOOL = "external_brand_crawl"

# Scope: ANY seed-attached product, not just `external_brand_crawl::%`. The
# original filter matched only one ingest path and silently skipped 2,404 of the
# 4,072 seed-attached beauty rows (measured 2026-07-22) — they carry the same
# payload shape and the same ~50 stalled score, they just arrived via a different
# source_ref. `--tool-prefix` restores the old narrow behavior when needed.
#
# Three safety properties are load-bearing here (review findings, 2026-07-23):
#   * `eps.id AS seed_id` — the seed id must come from the ROW, never be
#     rebuilt as `external_brand_crawl::{epid}`. Only the crawl path mints ids
#     in that shape; for every widened-scope row a fabricated id silently
#     matches no seed, so the backlink and the agent_pdp_view refresh become
#     no-ops that still report success (and could, worst case, re-point another
#     product's seed).
#   * `NOT ips.serving_eligible` — restrict to rows that are ALREADY dark. A
#     rescore can then only ever promote; a currently-public row cannot be
#     demoted by a thinner field set (e.g. a NULL eps.price_amount costing the
#     price component). `--include-eligible` opts out deliberately.
#   * `DISTINCT ON (p.product_key)` — `attached_product_key` has no unique
#     index, so a multi-seed product would otherwise be scored once per seed
#     with a non-deterministic winner. Match the eligibility reader and take the
#     most recently updated seed. (Postgres-only; this script is prod-only.)
FETCH = """
    SELECT DISTINCT ON (p.product_key)
           p.product_key, p.source_product_id, p.title, p.description, p.brand,
           p.product_type, p.category_kind, p.image_url,
           eps.id AS seed_id, eps.price_amount, bsi.raw_inci,
           COALESCE(
               eps.seed_data -> 'pdp_details_sections',
               eps.seed_data -> 'snapshot' -> 'pdp_details_sections'
           ) AS pdp_details_sections
    FROM catalog_products p
    JOIN external_product_seeds eps ON eps.attached_product_key = p.product_key
    JOIN index_pipeline_state ips ON ips.content_key = p.content_key
    LEFT JOIN beauty_sku_ingredients bsi
           ON bsi.sku_key = p.product_key || '::canonical'
    WHERE (
        CAST(:source_prefix AS TEXT) IS NULL
        OR p.source_ref LIKE CAST(:source_prefix AS TEXT)
    )
      -- snapshots are written under platform='external_seed'; the eligibility
      -- lateral matches on platform, so scoring any other platform's rows here
      -- writes snapshots nothing will ever read.
      AND p.platform = 'external_seed'
      AND (CAST(:include_eligible AS INTEGER) = 1 OR NOT ips.serving_eligible)
    ORDER BY p.product_key, eps.updated_at DESC NULLS LAST, eps.id
"""


def _coerce_sections(value: Any) -> Optional[list]:
    """`seed_data->'pdp_details_sections'` arrives as JSON (str or list)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    return list(value) if isinstance(value, (list, tuple)) else None


async def _rescored_ids() -> set:
    rows = await database.fetch_all(
        "SELECT DISTINCT platform_product_id AS pid FROM product_quality_snapshot "
        "WHERE rules_version = :v",
        {"v": SOURCE_BACKED_COMPONENTS_RULES_VERSION},
    )
    return {dict(r)["pid"] for r in rows}


# Bounds for the pool recycle (see _reset_connection).
_DISCONNECT_TIMEOUT_S = 15
_CONNECT_TIMEOUT_S = 30
# A single no-write can be a legitimately bad row; only a RUN of them looks like a
# poisoned connection. Recycling on every one would tear the pool down per row.
_NO_WRITE_RECYCLE_AFTER = 2
# Separate breaker for no-writes: legitimate quality=False rows cluster (FETCH is
# ORDER BY product_key, so same-brand rows are adjacent), so this must be looser
# than the hard-failure breaker or a healthy run aborts on a bad brand.
_NO_WRITE_ABORT_AFTER = 25


async def _public_count(product_keys: list[str]) -> int:
    """How many of these product_keys are currently `public`."""
    row = await database.fetch_one(
        "SELECT count(*) AS n FROM catalog_row_trust "
        "WHERE product_key = ANY(:product_keys) AND serving_decision = 'public'",
        {"product_keys": product_keys},
    )
    return int(dict(row).get("n") or 0) if row else 0


async def _reset_connection() -> bool:
    """Recycle the shared `databases` pool after a failed/cancelled query.

    `asyncio.wait_for` cancels the in-flight query on timeout, which leaves the
    shared connection half-acquired: every LATER query then dies with
    ``AssertionError: Connection is already acquired``. Because
    ``make_external_seed_servable`` swallows all of its own errors by contract,
    those deaths are invisible — it just returns a summary with nothing written,
    the ``ok`` counter keeps climbing, and the run silently no-ops to completion.
    Measured 2026-07-23: one timeout on row 1 poisoned the next 1,320 rows
    (0 writes, 0 promotions, reported ``ok=1322 fail=1``).

    Returns True if the pool is usable again.

    BOUNDED ON PURPOSE: `disconnect()` -> `asyncpg pool.close()` waits for every
    holder to release and asyncpg's own docs advise wrapping it in a timeout — on
    a dead socket the release we are recovering from can park forever, which would
    turn "one slow row" into "the run hangs silently", strictly worse than the bug.
    So the graceful close is capped and falls back to `pool.terminate()`.
    """
    try:
        await asyncio.wait_for(database.disconnect(), timeout=_DISCONNECT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — incl. TimeoutError on a parked close()
        # Force the pool down without waiting on holders.
        try:
            backend = getattr(database, "_backend", None)
            pool = getattr(backend, "_pool", None)
            if pool is not None and hasattr(pool, "terminate"):
                pool.terminate()
            if backend is not None:
                backend._pool = None
            database.is_connected = False
        except Exception:  # noqa: BLE001 — best-effort teardown, never raise
            pass
    try:
        await asyncio.wait_for(database.connect(), timeout=_CONNECT_TIMEOUT_S)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  RECONNECT FAILED: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
        return False


async def _flush_trust(product_keys: list[str]) -> int:
    """Recompute catalog_row_trust for the just-promoted product_keys.

    Rescoring sets index_pipeline_state.serving_eligible, but that is NOT what
    public readers gate on — catalog_row_trust.serving_decision is derived by a
    SEPARATE policy (services.catalog_trust_policy) and written only by the trust
    upserter. Without this step a rescored row is serving_eligible yet stays
    `blocked` until the phase-2d drift cron eventually catches it (proven live
    2026-07-23: 7/7 eligible rows went blocked->public the instant the trust
    upsert ran). Best-effort: a trust failure is logged inside the upserter and
    never rolls back the score.

    Returns the number of rows this flush actually PROMOTED to public — measured
    as an after-minus-before delta, not a post-state count. The upserter's own
    return value is policy WRITE ATTEMPTS, and a recomputed row legitimately lands
    `shadow` (IDENTITY_LIVE_READ_DISABLED when pdp_identity_listing.live_read_enabled
    is false) or stays `blocked`; reporting attempts as promotions is how a run
    showed "60 blocked->public" while public did not move at all. A post-state
    count would also over-report under --include-eligible, where rows can already
    be public going in.
    """
    if not product_keys:
        return 0
    try:
        before = await _public_count(product_keys)
        # Pass limit=len(keys) so the upserter's internal SELECT ... LIMIT (default
        # 5000) can never silently truncate an oversized chunk — self-consistent
        # regardless of --trust-flush-every.
        await upsert_catalog_row_trust_many(
            db=database, product_keys=product_keys, limit=len(product_keys)
        )
        after = await _public_count(product_keys)
        return max(0, after - before)
    except Exception as exc:  # noqa: BLE001 -- trust flush must never abort the run
        print(f"  TRUST-FLUSH FAIL ({len(product_keys)} keys): "
              f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)
        return 0


async def run(
    apply: bool,
    limit: Optional[int],
    *,
    source_prefix: Optional[str] = None,
    force: bool = False,
    include_eligible: bool = False,
    offset: int = 0,
    trust_flush_every: int = 200,
    skip_trust: bool = False,
) -> None:
    await database.connect()
    try:
        rows = [
            dict(r)
            for r in await database.fetch_all(
                FETCH,
                {
                    "source_prefix": source_prefix,
                    "include_eligible": 1 if include_eligible else 0,
                },
            )
        ]
        done = set() if force else await _rescored_ids()
        todo = [r for r in rows if r["source_product_id"] not in done]
        if offset:
            todo = todo[offset:]
        if limit:
            todo = todo[:limit]
        with_inci = sum(1 for r in rows if (r.get("raw_inci") or "").strip())
        with_sections = sum(1 for r in rows if _coerce_sections(r.get("pdp_details_sections")))
        # `rows` is DISTINCT ON (product_key), so these are product counts.
        print(
            f"batch={len(rows)} products  already-rescored={len(done)}  "
            f"to-rescore={len(todo)}  (with INCI: {with_inci}, "
            f"with pdp_details_sections: {with_sections})",
            flush=True,
        )

        if not apply:
            for r in todo[:5]:
                has_inci = "Y" if (r.get("raw_inci") or "").strip() else "N"
                has_sec = "Y" if _coerce_sections(r.get("pdp_details_sections")) else "N"
                print(f"  would rescore {r['source_product_id']} inci={has_inci} "
                      f"sections={has_sec} cat={r['category_kind']} "
                      f":: {(r['title'] or '')[:40]}")
            print("DRY-RUN — pass --apply to write.")
            return

        ok = fail = silent = 0
        eligible = 0  # total rows that reached serving_eligible (step 1 succeeded)
        consec_fail = 0      # hard exceptions in a row
        consec_no_write = 0  # summaries that persisted nothing, in a row
        promoted: list[str] = []  # product_keys that flipped serving_eligible
        trust_wrote = 0
        for i, r in enumerate(todo, 1):
            epid = r["source_product_id"]
            resume = (
                f"Re-run with --offset {offset + i - 1} to resume."
                if force
                else "Re-run to resume (already-rescored are skipped)."
            )
            if consec_fail >= 5:
                print(f"[ABORT] {consec_fail} consecutive hard failures — connection "
                      f"likely dead. {resume}", flush=True)
                break
            if consec_no_write >= _NO_WRITE_ABORT_AFTER:
                # Distinct message: nothing is raising, the writes just aren't
                # landing. Sending the operator to "check the connection" when the
                # real cause is per-row data would waste the diagnosis.
                print(f"[ABORT] {consec_no_write} consecutive rows persisted nothing "
                      f"(no exceptions raised — check the NO-WRITE summaries above "
                      f"for the cause, not the connection). {resume}", flush=True)
                break
            try:
                qp = build_servable_quality_payload(
                    title=r["title"], description=r["description"],
                    price=r["price_amount"], image_url=r["image_url"],
                    brand=r["brand"], product_type=r["product_type"],
                    category=r["category_kind"], raw_inci=r["raw_inci"],
                    pdp_details_sections=_coerce_sections(r.get("pdp_details_sections")),
                )
                # per-product timeout so a dead socket errors out, never hangs
                summary = await asyncio.wait_for(
                    make_external_seed_servable(
                        # seed_id comes from the row — NEVER rebuilt from TOOL.
                        product_key=r["product_key"], seed_id=r["seed_id"],
                        source_product_id=epid, quality_payload=qp,
                        reason="rescore_ingredient_aware",
                    ),
                    timeout=45,
                )
                # `make_external_seed_servable` swallows its OWN errors, so a
                # returned summary is NOT proof of a write. `quality` is the
                # authoritative signal: it is only True once the snapshot was
                # persisted. Counting "didn't raise" is what let a poisoned
                # connection report ok=1322 while writing nothing.
                # A write only counts when the snapshot persisted AND the identity
                # lookup resolved a content_key. `serving_eligible is None` means
                # that lookup failed, so the snapshot went under the FALLBACK
                # merchant — unfindable by the eligibility classifier, yet
                # _rescored_ids() would mark the product done forever. Treat it as
                # a no-write so a resume can still fix it.
                persisted = bool((summary or {}).get("quality"))
                resolved = (summary or {}).get("serving_eligible") is not None
                if persisted and resolved:
                    ok += 1
                    consec_fail = consec_no_write = 0
                    # Only rows that actually became serving_eligible need the
                    # trust flip — a row that stayed below the bar would just
                    # recompute to `blocked` again, so skip the needless work.
                    if (summary or {}).get("serving_eligible") is True:
                        eligible += 1
                        if not skip_trust:
                            promoted.append(r["product_key"])
                else:
                    # Nothing usable persisted. A single one is often just a bad
                    # row; a RUN of them looks like a poisoned connection — so
                    # recycle only after a couple in a row rather than tearing the
                    # pool down per row (which on a cross-region DB costs seconds
                    # each and, with an alternating write/no-write pattern, would
                    # never stop).
                    silent += 1
                    consec_no_write += 1
                    print(f"  NO-WRITE {epid}: persisted={persisted} "
                          f"identity_resolved={resolved} summary={summary}", flush=True)
                    if consec_no_write >= _NO_WRITE_RECYCLE_AFTER:
                        if not await _reset_connection():
                            print(f"[ABORT] reconnect failed. {resume}", flush=True)
                            break
            except Exception as e:  # noqa: BLE001 -- isolate per-product failures
                fail += 1
                consec_fail += 1
                print(f"  FAIL {epid}: {type(e).__name__}: {str(e)[:80]}", flush=True)
                # A cancelled (timeout) or errored query can leave the shared
                # connection unusable for every subsequent row — recycle before
                # continuing so one slow row can't silently void the whole run.
                if not await _reset_connection():
                    print(f"[ABORT] reconnect failed. {resume}", flush=True)
                    break
            # Flush trust in chunks so a long run promotes durably (and a mid-run
            # abort still publishes everything scored so far).
            if len(promoted) >= trust_flush_every:
                trust_wrote += await _flush_trust(promoted)
                promoted = []
            if i % 100 == 0:
                print(f"  {i}/{len(todo)} wrote={ok} eligible={eligible} "
                      f"fail={fail} no_write={silent} now_public={trust_wrote}",
                      flush=True)
        # Flush whatever is left — runs on normal completion AND after an abort break.
        trust_wrote += await _flush_trust(promoted)
        # `wrote` counts PERSISTED snapshots, not calls that merely didn't raise.
        # wrote = persisted snapshots; eligible = reached serving_eligible (step 1);
        # promoted = actually flipped to public (step 2). Keeping the three apart is
        # what makes "wrote=1322 eligible=0 promoted=0" diagnosable instead of a
        # mystery — step-1 and step-2 failures are otherwise indistinguishable.
        print(f"\nDONE: wrote={ok} eligible={eligible} fail={fail} "
              f"no_write={silent} (of {len(todo)}); PROMOTED to public: {trust_wrote}")
    finally:
        await database.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap rows this run")
    ap.add_argument(
        "--tool-prefix",
        default=None,
        help="restrict to one ingest path, e.g. 'external_brand_crawl::%%' "
             "(default: ALL seed-attached products)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-score even products already on the current rules version. NOT "
             "needed for a payload-shape change — bump "
             "SOURCE_BACKED_COMPONENTS_RULES_VERSION instead, which keeps the "
             "run resumable. --force restarts from row 1 every time.",
    )
    ap.add_argument(
        "--include-eligible",
        action="store_true",
        help="also re-score rows that are ALREADY serving-eligible. Off by "
             "default so a rescore can only promote, never demote a public row.",
    )
    ap.add_argument("--offset", type=int, default=0, help="skip the first N rows")
    ap.add_argument(
        "--trust-flush-every",
        type=int,
        default=200,
        help="chunk size for the catalog_row_trust upsert that flips promoted "
             "rows to public (default 200)",
    )
    ap.add_argument(
        "--skip-trust",
        action="store_true",
        help="rescore only; do NOT run the trust upsert. Promoted rows stay "
             "serving_eligible-but-blocked until the drift cron catches them.",
    )
    args = ap.parse_args()
    asyncio.run(
        run(
            args.apply,
            args.limit,
            source_prefix=args.tool_prefix,
            force=args.force,
            include_eligible=args.include_eligible,
            offset=args.offset,
            trust_flush_every=args.trust_flush_every,
            skip_trust=args.skip_trust,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
