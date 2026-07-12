"""Backfill catalog_products.llm_attributes with structural-depth beauty
attributes (Fix Plan G — T1).

Two-pass, deterministic-first (services.structural_attributes):
  * the deterministic pass reconciles existing signal (beauty_sku_ingredients,
    beauty_product_profiles, crawled seed_data INCI) + regex/lexicon extractors —
    NO LLM, NO network;
  * the LLM residual pass fills ONLY the unresolved judgment fields (skin_type,
    texture, finish, concerns) via the shared services.llm_synthesis client.

COST GATE (hard, per the plan): the deterministic pass may run broadly in
--mode dry-run (it is free); the LLM pass is capped to a --limit pilot cohort.
The FULL 9,249-row run is a FOUNDER decision — this script STOPS after the pilot
and prints the cost projection + go/no-go inputs. It will refuse an LLM run whose
--limit exceeds --max-pilot (default 100) unless --i-understand-full-cost is
given.

Modes:
  --mode dry-run   (default) deterministic pass over the cohort; reports per-field
                   deterministic coverage + residual-field frequency + a full-run
                   LLM cost ESTIMATE (char-based token model). Writes NOTHING,
                   calls NO LLM.
  --mode pilot     deterministic + LLM residual for the first --limit rows;
                   writes the versioned envelope guarded on
                   (llm_attributes IS NULL OR '{}'); reports ACTUAL cost (from
                   provider usage), parse-failure rate (FAILS LOUDLY above
                   --max-parse-fail-rate), and the exact written product_keys
                   (reversibility).

Reuses the Fix-Plan-B backfill template: keyset pagination on product_key
(resumable), set-based unnest batch writes (the public proxy is ~1 write/s
per statement), bounded retry that STOPS loudly rather than thrashing.

Usage (prod, read-only estimate):
  railway run bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" PYTHONPATH="$PWD" \
      python3.11 -m scripts.backfill_llm_attributes --mode dry-run'
  railway run bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" PYTHONPATH="$PWD" \
      python3.11 -m scripts.backfill_llm_attributes --mode pilot --limit 100'
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from db.database import database
from services.structural_attributes import (
    CORE_FIELDS,
    LLM_RESIDUAL_FIELDS,
    build_envelope,
    build_residual_prompt,
    extract_deterministic,
    run_llm_residual,
)

# Demo/test merchants excluded from the metrics + pilot cohort (Fix Plan E;
# scripts/_utils/demoExclusions.cjs — no Python twin, ported here).
_DEMO_MERCHANTS: List[str] = [
    "merch_efbc46b4619cfbdf", "merch_bbd34645bc1950cc", "merch_test_ownist_001",
    "merch_shopify_00d4a720d67d96c5dcba", "merch_shopify_0584b37f7a8be00a5223",
    "merch_shopify_b20b5797f4181983c177",
]

# Live, non-demo, beauty, un-enriched cohort. suppression_reason IS NULL already
# drops the demo_retired_2026_07 cohort; the domain-prefix + merchant-id filters
# drop the review-demo stores. The llm_attributes guard makes the pass
# resumable + idempotent (a written row leaves the cohort).
_COHORT_WHERE = """
    cp.suppression_reason IS NULL
    AND COALESCE(cp.source_domain, '') NOT LIKE 'pivota-review-demo%'
    AND cp.merchant_id <> ALL(:demo_merchants)
    AND cp.resolved_vertical = 'beauty'
    AND (cp.llm_attributes IS NULL OR cp.llm_attributes = '{}'::jsonb)
"""

# Keyset SELECT joining the existing beauty signal to reconcile (never
# regenerate). The ingredient LATERAL picks the single richest sku row per
# product so multi-sku fan-out cannot inflate the scan; the shade LATERAL
# aggregates finishes.
_SELECT_SQL = f"""
    SELECT
        cp.product_key, cp.merchant_id, cp.platform, cp.source_product_id,
        cp.title, cp.description, cp.product_type, cp.category, cp.category_path,
        cp.category_kind, cp.tags,
        bpp.concerns_json AS concerns_json,
        bsi.active_ingredients_json AS active_ingredients_json,
        bsi.raw_inci AS raw_inci,
        bsi.concentration_notes_json AS concentration_notes_json,
        COALESCE(
            NULLIF(cp.product_payload -> 'seed_data' ->> 'inci_list', ''),
            cp.product_payload -> 'seed_data' ->> 'pdp_ingredients_raw'
        ) AS seed_inci,
        sh.shade_json AS shade_json
    FROM catalog_products cp
    LEFT JOIN beauty_product_profiles bpp ON bpp.product_key = cp.product_key
    LEFT JOIN LATERAL (
        SELECT b.active_ingredients_json, b.raw_inci, b.concentration_notes_json
        FROM beauty_sku_ingredients b
        WHERE b.product_key = cp.product_key
        ORDER BY (b.active_ingredients_json IS NOT NULL) DESC,
                 length(COALESCE(b.raw_inci, '')) DESC
        LIMIT 1
    ) bsi ON TRUE
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object('finish', s.finish)) AS shade_json
        FROM beauty_shades s
        WHERE s.product_key = cp.product_key AND s.finish IS NOT NULL
    ) sh ON TRUE
    WHERE {_COHORT_WHERE}
      AND cp.product_key > :cursor
    ORDER BY cp.product_key ASC
    LIMIT :batch_size
"""

# One-shot random sample for a REPRESENTATIVE pilot quality read (the keyset
# order is alphabetical by product_key, which clusters a single brand). Same
# joins/cohort; no cursor — a single bounded fetch of :limit rows.
_SELECT_RANDOM_SQL = _SELECT_SQL.replace(
    "AND cp.product_key > :cursor\n    ORDER BY cp.product_key ASC\n    LIMIT :batch_size",
    "ORDER BY random()\n    LIMIT :limit",
)

# Set-based batch write: ONE round-trip updates the whole batch by zipping
# (product_keys, json payloads) through unnest. Writes ONLY llm_attributes, and
# guards (IS NULL OR '{}') so a concurrently-written row / an existing extractor
# cache is NEVER clobbered (idempotent + additive).
_UPDATE_BATCH_SQL = """
    UPDATE catalog_products AS c
    SET llm_attributes = CAST(v.payload AS jsonb)
    FROM unnest(CAST(:keys AS text[]), CAST(:payloads AS text[])) AS v(pk, payload)
    WHERE c.product_key = v.pk
      AND (c.llm_attributes IS NULL OR c.llm_attributes = '{}'::jsonb)
"""

# Gemini-flash list rates from services.llm_providers.provider_registry (the
# codebase's canonical numbers for cost telemetry). USD per 1K tokens.
_RATE_INPUT_PER_1K = 0.00035
_RATE_OUTPUT_PER_1K = 0.00105
# Rough output-token budget a residual answer uses (the ask is 4 short fields);
# used only for the dry-run ESTIMATE — the pilot reports ACTUAL usage.
_EST_OUTPUT_TOKENS = 60


def _est_tokens(text: str) -> int:
    """Cheap char->token estimate (~4 chars/token) for the dry-run projection.
    The pilot uses real provider usage; this only sizes the pre-spend estimate."""
    return max(1, len(text) // 4)


async def _connect_if_needed(db: Any, *, max_retries: int = 5, base_delay: float = 1.5) -> bool:
    was = bool(getattr(db, "is_connected", False))
    if not was and callable(getattr(db, "connect", None)):
        # The public DB proxy flakes on connect; retry with backoff rather than
        # dying on the first stall (the documented ad-hoc-read pattern).
        await _with_retry(
            lambda: db.connect(), max_retries=max_retries, base_delay=base_delay,
            label="db.connect",
        )
    return was


async def _disconnect_if_needed(db: Any, was: bool) -> None:
    if not was and bool(getattr(db, "is_connected", False)) and callable(getattr(db, "disconnect", None)):
        await db.disconnect()


async def _with_retry(coro_factory, *, max_retries: int, base_delay: float, label: str):
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001 — transient proxy stalls are opaque
            attempt += 1
            if attempt > max_retries:
                raise RuntimeError(f"{label} failed after {max_retries} retries: {exc!r}") from exc
            delay = base_delay * (2 ** (attempt - 1))
            print(f"WARN: {label} errored (attempt {attempt}/{max_retries}): {exc!r}; "
                  f"retrying in {delay:.1f}s", flush=True)
            await asyncio.sleep(delay)


async def _fetch_batch(db: Any, cursor: str, batch_size: int) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        _SELECT_SQL,
        {"cursor": cursor, "batch_size": batch_size, "demo_merchants": _DEMO_MERCHANTS},
    )
    return [dict(r) for r in rows]


def _empty_field_coverage() -> Dict[str, int]:
    return {f: 0 for f in (*CORE_FIELDS, "format", "fragrance_free", "sulfate_free",
                           "silicone_free", "vegan_status", "cruelty_free_status")}


async def _drive(
    args: argparse.Namespace,
    *,
    db: Any = database,
    synthesize: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    was = await _connect_if_needed(db)

    scanned = 0
    batches = 0
    written = 0
    cursor = ""

    det_field_hits = _empty_field_coverage()
    residual_field_freq: Counter = Counter()
    provenance_freq: Counter = Counter()
    est_input_tokens = 0
    est_residual_calls = 0

    # pilot-only accounting
    llm_outcomes: Counter = Counter()
    actual_input_tokens = 0
    actual_output_tokens = 0
    written_keys: List[str] = []
    samples: List[Dict[str, Any]] = []

    is_pilot = args.mode == "pilot"
    if is_pilot and synthesize is None:
        from services.llm_synthesis import synthesize as _synth
        synthesize = _synth

    # Representative random pilot: one bounded fetch, served in write-batches.
    preloaded: Optional[List[Dict[str, Any]]] = None
    preload_idx = 0
    if is_pilot and getattr(args, "sample_order", "keyset") == "random":
        preloaded = await _with_retry(
            lambda: db.fetch_all(_SELECT_RANDOM_SQL,
                                 {"limit": args.limit, "demo_merchants": _DEMO_MERCHANTS}),
            max_retries=args.max_retries, base_delay=args.retry_base_delay,
            label="fetch_random",
        )
        preloaded = [dict(r) for r in preloaded]

    try:
        while True:
            if is_pilot and args.limit and scanned >= args.limit:
                break
            if args.max_batches and batches >= args.max_batches:
                break
            remaining = (args.limit - scanned) if (is_pilot and args.limit) else args.batch_size
            batch_size = max(1, min(args.batch_size, remaining)) if is_pilot and args.limit else args.batch_size
            if preloaded is not None:
                rows = preloaded[preload_idx:preload_idx + batch_size]
                preload_idx += len(rows)
            else:
                rows = await _with_retry(
                    lambda bs=batch_size: _fetch_batch(db, cursor, bs),
                    max_retries=args.max_retries, base_delay=args.retry_base_delay,
                    label="fetch_batch",
                )
            if not rows:
                break
            batches += 1
            batch_writes: List[Dict[str, str]] = []

            for row in rows:
                scanned += 1
                det = extract_deterministic(row)
                for fname in det_field_hits:
                    if det.attributes.get(fname) not in (None, [], ""):
                        det_field_hits[fname] += 1
                for fname in det.residual_fields:
                    residual_field_freq[fname] += 1
                for prov in det.provenance.values():
                    provenance_freq[prov.split(":", 1)[0]] += 1

                # dry-run: estimate the residual LLM cost without spending.
                if det.residual_fields:
                    from services.structural_attributes import _text_blob
                    system, user = build_residual_prompt(_text_blob(row), det.residual_fields)
                    est_input_tokens += _est_tokens(system) + _est_tokens(user)
                    est_residual_calls += 1

                if not is_pilot:
                    if len(samples) < args.sample_size:
                        samples.append({
                            "product_key": row.get("product_key"),
                            "title": row.get("title"),
                            "resolved": det.attributes,
                            "residual_fields": det.residual_fields,
                        })
                    continue

                # pilot: run the LLM residual and build the envelope.
                residual = await run_llm_residual(
                    row, det.residual_fields, synthesize=synthesize,
                    provider=args.provider, model=args.model,
                    max_tokens=args.max_tokens,
                )
                llm_outcomes[residual.outcome] += 1
                actual_input_tokens += int(residual.usage.get("input_tokens") or 0)
                actual_output_tokens += int(residual.usage.get("output_tokens") or 0)
                envelope = build_envelope(det, residual)
                if len(samples) < args.sample_size:
                    samples.append({
                        "product_key": row.get("product_key"),
                        "title": row.get("title"),
                        "envelope": envelope,
                        "llm_outcome": residual.outcome,
                    })
                batch_writes.append({
                    "product_key": row["product_key"],
                    "payload": json.dumps(envelope, ensure_ascii=False),
                })

            if is_pilot and batch_writes and not args.no_write:
                keys = [w["product_key"] for w in batch_writes]
                payloads = [w["payload"] for w in batch_writes]
                await _with_retry(
                    lambda k=keys, p=payloads: db.execute(_UPDATE_BATCH_SQL, {"keys": k, "payloads": p}),
                    max_retries=args.max_retries, base_delay=args.retry_base_delay,
                    label="update_batch",
                )
                written += len(batch_writes)
                written_keys.extend(keys)

            cursor = rows[-1]["product_key"]
            print(f"batch {batches}: scanned={scanned} written={written} cursor={cursor!r}", flush=True)
    finally:
        await _disconnect_if_needed(db, was)

    # ---- cost model ----
    det_coverage = {
        f: {"count": det_field_hits[f], "share": (det_field_hits[f] / scanned) if scanned else 0.0}
        for f in det_field_hits
    }
    est_output_tokens = est_residual_calls * _EST_OUTPUT_TOKENS
    est_cost = (est_input_tokens / 1000.0) * _RATE_INPUT_PER_1K + \
               (est_output_tokens / 1000.0) * _RATE_OUTPUT_PER_1K
    est_cost_per_row = (est_cost / scanned) if scanned else 0.0

    report: Dict[str, Any] = {
        "mode": args.mode,
        "scanned": scanned,
        "batches": batches,
        "deterministic_field_coverage": det_coverage,
        "residual_field_frequency": dict(residual_field_freq),
        "provenance_source_frequency": dict(provenance_freq),
        "estimate": {
            "residual_llm_calls": est_residual_calls,
            "est_input_tokens": est_input_tokens,
            "est_output_tokens": est_output_tokens,
            "rate_input_per_1k_usd": _RATE_INPUT_PER_1K,
            "rate_output_per_1k_usd": _RATE_OUTPUT_PER_1K,
            "est_cost_usd_for_scanned": round(est_cost, 4),
            "est_cost_usd_per_row": round(est_cost_per_row, 6),
        },
        "samples": samples,
    }

    if is_pilot:
        total_llm = sum(llm_outcomes.values())
        parse_fail = llm_outcomes.get("parse_fail", 0) + llm_outcomes.get("truncated", 0)
        parse_fail_rate = (parse_fail / total_llm) if total_llm else 0.0
        actual_cost = (actual_input_tokens / 1000.0) * _RATE_INPUT_PER_1K + \
                      (actual_output_tokens / 1000.0) * _RATE_OUTPUT_PER_1K
        report["pilot"] = {
            "written": written,
            "written_product_keys": written_keys,
            "llm_calls": total_llm,
            "llm_outcomes": dict(llm_outcomes),
            "parse_failures": parse_fail,
            "parse_fail_rate": round(parse_fail_rate, 4),
            "actual_input_tokens": actual_input_tokens,
            "actual_output_tokens": actual_output_tokens,
            "actual_cost_usd": round(actual_cost, 4),
            "actual_cost_usd_per_llm_call": round(actual_cost / total_llm, 6) if total_llm else 0.0,
        }
        # FAIL LOUDLY: the known systemic failure is truncation→swallow. Above the
        # threshold the run is not trustworthy and the operator must raise the cap.
        if total_llm and parse_fail_rate > args.max_parse_fail_rate:
            report["FATAL"] = (
                f"parse-failure rate {parse_fail_rate:.1%} exceeds "
                f"{args.max_parse_fail_rate:.1%} — LLM output is truncating/garbage; "
                f"raise --max-tokens and re-run. NOT SAFE for the full run."
            )

    return report


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("dry-run", "pilot"), default="dry-run")
    p.add_argument("--limit", type=int, default=100,
                   help="Pilot cohort size (LLM rows). Ignored in dry-run unless --max-batches set.")
    p.add_argument("--max-pilot", type=int, default=100,
                   help="Hard ceiling on --limit for pilot without --i-understand-full-cost.")
    p.add_argument("--i-understand-full-cost", action="store_true",
                   help="Required to run a pilot with --limit above --max-pilot (founder gate).")
    p.add_argument("--provider", default="gemini",
                   help="LLM provider (repo default for cheap extraction = gemini flash).")
    p.add_argument("--model", default="gemini-2.5-flash")
    p.add_argument("--max-tokens", type=int, default=512,
                   help="Per-call output cap. Small: the residual ask is 4 short fields.")
    p.add_argument("--max-parse-fail-rate", type=float, default=0.05,
                   help="Pilot fails loudly above this parse-failure rate (default 0.05).")
    p.add_argument("--sample-order", choices=("keyset", "random"), default="keyset",
                   help="Pilot cohort selection: keyset (resumable, alphabetical) or "
                        "random (one-shot representative sample for the quality read).")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--max-batches", type=int, default=0,
                   help="Stop after N batches (0 = run the whole cohort; dry-run only).")
    p.add_argument("--sample-size", type=int, default=15)
    p.add_argument("--no-write", action="store_true", help="Pilot: compute + cost but do not write.")
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("--retry-base-delay", type=float, default=1.0)
    return p.parse_args(argv)


def main() -> int:
    args = _parse_args()
    if args.mode == "pilot" and args.limit > args.max_pilot and not args.i_understand_full_cost:
        raise SystemExit(
            f"REFUSED: pilot --limit {args.limit} exceeds --max-pilot {args.max_pilot}. "
            f"The full run is a founder decision — pass --i-understand-full-cost to override."
        )
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 1 if report.get("FATAL") else 0


if __name__ == "__main__":
    raise SystemExit(main())
