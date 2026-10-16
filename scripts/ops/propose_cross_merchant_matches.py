"""Propose cross-merchant same-size matches (`cross_merchant_same_size`, PR "M4"). PROPOSES ONLY.

The rule is services/identity_cross_merchant_match.py (tiers T0a/T0b/T0c + vetoes + the star anchor
+ same-size only). This script exports its inputs and either reports (dry run) or writes the
proposals with status 'proposed' through the identity engine's writer
(identity_resolution.upsert_proposals). It never approves and never applies. Approval is #2637's
module-run approval, after Peng reviewed the audit sample:
    python -m scripts.identity_engine approve --strategy cross_merchant_same_size --run <proposal_run_id> --by "<who>"
    python -m scripts.identity_engine approve --strategy cross_merchant_same_size --run <proposal_run_id> \
        --by "<who>" --expect-digest ira_... --execute
and the only apply path is, by the coordinator with Peng's go:
    python -m scripts.identity_engine apply --strategy cross_merchant_same_size --max-proposals N
    python -m scripts.identity_engine apply --strategy cross_merchant_same_size --max-proposals N \
        --expect-digest ird_... --execute

MODES
  --dry-run (default): ONE read-only transaction (SET LOCAL statement_timeout = '60s') exports the
      snapshot in TWO PHASES: phase 1 reads the market's candidate products and hosts, brand-filtered
      in SQL; Python computes the groups this run judges; phase 2 reads rows, every market's offers,
      SKUs, memberships, group sizes, seeds and this strategy's live proposals for THOSE content_keys
      only (one key array, passed as a parameter). Planning is in Python, off the database.
  --explain: EXPLAIN (no ANALYZE; nothing runs) every statement, for review before the timed run. Prints JSON lines (per-tier
      counts, held/deferred/vetoed by reason, top brands, host pairs, expected new multi-merchant
      groups, the proposal-set DIGEST, 20 samples per tier), then the AUDIT SAMPLE as one JSON line
      per pair (section "audit": a seeded RANDOM sample of (moved listing, anchor) pairs for
      side-by-side review: titles, brands, sizes, prices, image_url, canonical_url, tier) -- read back
      from Cloud Logging, see below. It also writes to --out (default /tmp/cross_merchant_match, a
      local path inside the job):
        proposals.jsonl     every proposal (exactly what --write-proposals would insert)
        audit_sample.jsonl  the same audit sample
      --out gs://bucket/prefix uploads with the job's metadata-server token: UNTESTED against GCS.
  --write-proposals --expect-digest D --max-proposals N: re-runs the dry run IN PROCESS, refuses
      unless its digest is D and it has at most N proposals, refuses any proposal that fails the
      engine's module rule (identity_resolution.validate_matcher_verdict / module_proposal_problems),
      then inserts them (status 'proposed', evidence.proposer, evidence.proposal_run_id = one id for
      this invocation) and prints the proposal run id and the approve DRY-RUN command, whose output
      carries the ira_ digest the --execute approval needs.

SCOPE: --market US (default), --brands a,b,c (canonical brand match), --limit-groups N.

SAFETY. pivota-pg has 2 vCPU and serves live search: start only while CPU is under 35%, never
05:15-06:15Z, 10:30-11:20Z, or during a drain or index build. One prod one-off at a time, through
scripts/ops/run_oneoff_job.sh. No merchant page is fetched.

    bash scripts/ops/run_oneoff_job.sh scripts/ops/propose_cross_merchant_matches.py --market US \\
        --brands laneige,cosrx --out gs://<bucket>/cross_merchant/2026-10-17

Cloud Run logs a bare JSON line as jsonPayload: read them back (the audit sample with
AND jsonPayload.section="audit") with
    gcloud logging read 'resource.type="cloud_run_job" AND jsonPayload.census="cross_merchant_match"'
The program text is kept free of the at-sign (run_oneoff_job.sh's --args delimiter choice).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:  # python -c has no __file__; /app is the image's repo root
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)

from services.identity_cross_merchant_match import (  # noqa: E402
    LIVE_PROPOSAL_STATUSES,
    SKU_PAYLOAD_SIZE_FIELDS,
    STRATEGY,
    Plan,
    Planner,
    Snapshot,
    brand_sql_patterns,
    evidence_problems,
    plan_market,
    stamp_run,
)

CENSUS = "cross_merchant_match"
AUDIT_SAMPLE_DEFAULT = 600
AUDIT_SEED_DEFAULT = 20261016
OUT_DEFAULT = "/tmp/cross_merchant_match"

# PHASE 1 -- the run market's candidates, brand-filtered IN SQL. $1 market, $2 brand LIKE patterns
# over the alphanumeric brand key (identity_cross_merchant_match.brand_sql_patterns: a superset; the
# exact canonical_brand filter runs in Python), NULL = every brand.
_PRODUCT_FILTER = """
    p.pivota_signature_id IS NOT NULL AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
    AND p.sync_status = 'live'
    AND ($2::text[] IS NULL
         OR regexp_replace(lower(coalesce(p.brand, '')), '[^[:alnum:]]+', '', 'g') LIKE ANY($2::text[]))
    AND EXISTS (SELECT 1 FROM catalog_offers o0
                WHERE o0.product_key = p.product_key AND o0.suppressed_at IS NULL
                  AND o0.suppression_reason IS NULL AND o0.market = $1)
"""
PRODUCTS_SQL = f"""
SELECT p.product_key, p.merchant_id, p.platform, p.source_product_id, p.source_ref, p.content_key,
       p.pivota_signature_id, p.brand, p.title, p.gtin, p.source_system, p.seed_kind, p.created_at,
       p.image_url, p.canonical_url
FROM catalog_products p
WHERE {_PRODUCT_FILTER}
"""
_HOST_EXPR = """lower(coalesce(nullif(btrim(o.source_domain), ''),
                      substring(o.offer_payload->>'destination_url' from '^[A-Za-z][A-Za-z0-9+.-]*://([^/:?#]+)')))"""
HOSTS_SQL = f"""
SELECT o.product_key, o.market, {_HOST_EXPR} AS host, bool_or(o.offer_type = 'brand_direct') AS brand_direct
FROM catalog_offers o
JOIN catalog_products p ON p.product_key = o.product_key
WHERE {_PRODUCT_FILTER} AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL AND o.market = $1
GROUP BY 1, 2, 3
"""
# PHASE 2 -- the details for the candidate content_keys ONLY ($1 = that array, computed once in
# Python from phase 1; no subquery re-runs, no full-table SKU scan). EVERY market's offers.
ROWS_SQL = """
SELECT c.product_key, c.content_key, c.title, c.brand, c.merchant_id, c.platform, c.source_product_id,
       c.source_ref
FROM catalog_products c
WHERE c.content_key = ANY($1::text[]) AND c.suppression_reason IS NULL
"""
_SIZE_FIELDS_SQL = "jsonb_strip_nulls(jsonb_build_object(" + ", ".join(
    f"'{f}', s.sku_payload->'{f}'" for f in SKU_PAYLOAD_SIZE_FIELDS) + "))"
OFFERS_SQL = f"""
SELECT o.offer_id, o.product_key, o.sku_key, o.market, o.offer_type, o.availability, o.currency,
       coalesce(o.merchant_effective_price, o.list_price) AS price, {_HOST_EXPR} AS host,
       s.title AS sku_title, s.visible_option_labels AS sku_labels, {_SIZE_FIELDS_SQL} AS sku_size_fields
FROM catalog_offers o
LEFT JOIN catalog_skus s ON s.sku_key = o.sku_key AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
WHERE o.product_key = ANY($1::text[]) AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL
"""
SKUS_SQL = """
SELECT s.product_key, s.sku_key, s.title, s.visible_option_labels AS labels
FROM catalog_skus s
WHERE s.product_key = ANY($1::text[]) AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
"""
MEMBERSHIPS_SQL = """
SELECT m.merchant_id, m.platform, m.platform_product_id, m.product_group_id
FROM product_group_members m
WHERE (m.merchant_id, m.platform, m.platform_product_id) IN (
    SELECT * FROM unnest($1::text[], $2::text[], $3::text[]))
"""
GROUP_SIZES_SQL = """
SELECT g.product_group_id, count(*) AS n
FROM product_group_members g
WHERE g.product_group_id = ANY($1::text[])
GROUP BY 1
"""
# Seeds linked the ways offers.resolve links them: attached_product_key, the row's source_ref (the
# seed id) or its source_product_id (the seed's external_product_id). The MAIN variant list only.
SEEDS_SQL = """
SELECT e.id, e.attached_product_key, e.external_product_id, e.title, e.seed_data->'variants' AS seed_variants
FROM external_product_seeds e
WHERE lower(coalesce(e.status, '')) = 'active'
  AND (e.attached_product_key = ANY($1::text[]) OR e.id = ANY($2::text[]) OR e.external_product_id = ANY($3::text[]))
"""
# This strategy's live proposals touching the keys (as target or source), every market.
PROPOSALS_SQL = """
SELECT r.proposal_id, r.status, r.kind, r.content_key, r.keeper_product_key,
       jsonb_build_object('matcher_verdict', r.evidence->'matcher_verdict',
                          'from_content_key', r.evidence->'from_content_key') AS evidence
FROM identity_resolution_proposals r
WHERE r.strategy = $1 AND r.status = ANY($2::text[])
  AND (r.content_key = ANY($3::text[]) OR r.evidence->>'from_content_key' = ANY($3::text[]))
"""
PHASE1: Tuple[Tuple[str, str], ...] = (("products", PRODUCTS_SQL), ("hosts", HOSTS_SQL))
PHASE2: Tuple[Tuple[str, str], ...] = (
    ("rows", ROWS_SQL), ("offers", OFFERS_SQL), ("skus", SKUS_SQL), ("memberships", MEMBERSHIPS_SQL),
    ("group_sizes", GROUP_SIZES_SQL), ("seeds", SEEDS_SQL), ("proposals", PROPOSALS_SQL))


def _line(section: str, **fields: Any) -> Dict[str, Any]:
    return {"census": CENSUS, "section": section, **fields}


def _phase2_params(name: str, cks: List[str], rows: List[Dict[str, Any]],
                   memberships: List[Dict[str, Any]]) -> Tuple[Any, ...]:
    pks = sorted({str(r["product_key"]) for r in rows})
    if name == "rows":
        return (cks,)
    if name in ("offers", "skus"):
        return (pks,)
    if name == "memberships":
        return ([str(r["merchant_id"]) for r in rows], [str(r["platform"]) for r in rows],
                [str(r["source_product_id"]) for r in rows])
    if name == "group_sizes":
        return (sorted({str(m["product_group_id"]) for m in memberships}),)
    if name == "seeds":
        return (pks, sorted({str(r["source_ref"]) for r in rows if r.get("source_ref")}),
                sorted({str(r["source_product_id"]) for r in rows if r.get("source_product_id")}))
    if name == "proposals":
        return (STRATEGY, list(LIVE_PROPOSAL_STATUSES), cks)
    raise KeyError(name)


async def export_snapshot(conn: Any, market: str, *, brands: Sequence[str] = (),
                          limit_groups: Optional[int] = None) -> Tuple[Snapshot, Dict[str, int]]:
    """The only read: ONE read-only transaction, statement_timeout 60s. Phase 1 reads the market's
    candidates (brand-filtered in SQL); Python computes the groups this run judges (Planner.scope);
    phase 2 reads the details for those content_keys only."""
    patterns = brand_sql_patterns(brands) if brands else None
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '60s'")
        products = [dict(r) for r in await conn.fetch(PRODUCTS_SQL, market, patterns)]
        hosts = [dict(r) for r in await conn.fetch(HOSTS_SQL, market, patterns)]
        _pks, cks = Planner(Snapshot(products=products, offers=[], skus=[], ck_rows={}, memberships={},
                                     group_sizes={}, hosts=hosts), market,
                            brands=brands, limit_groups=limit_groups).scope()
        got: Dict[str, List[Dict[str, Any]]] = {"rows": [], "memberships": []}
        for name, sql in PHASE2:
            if not cks:
                got[name] = []
                continue
            params = _phase2_params(name, cks, got["rows"], got["memberships"])
            got[name] = [dict(r) for r in await conn.fetch(sql, *params)]
    ck_rows: Dict[str, List[Dict[str, Any]]] = {}
    for r in got["rows"]:
        ck_rows.setdefault(r["content_key"], []).append(r)
    memberships = {(str(r["merchant_id"]), str(r["platform"]), str(r["platform_product_id"])): r["product_group_id"]
                   for r in got["memberships"]}
    group_sizes = {r["product_group_id"]: int(r["n"]) for r in got["group_sizes"]}
    snap = Snapshot(products=products, hosts=hosts, offers=got["offers"], skus=got["skus"], ck_rows=ck_rows,
                    memberships=memberships, group_sizes=group_sizes, seeds=got["seeds"],
                    proposals=got["proposals"])
    counts = {"products": len(products), "candidate_content_keys": len(cks),
              **{k: len(v) for k, v in got.items()}}
    return snap, counts


async def explain(conn: Any, market: str, brands: Sequence[str]) -> List[Dict[str, Any]]:
    """EXPLAIN (no ANALYZE: nothing is executed) of every statement, for review before the timed
    run. Phase-2 statements are planned with 500 placeholder keys."""
    patterns = brand_sql_patterns(brands) if brands else None
    keys = [f"ck_explain_{i}" for i in range(500)]
    fake_rows = [{"product_key": f"pk_{i}", "merchant_id": "m", "platform": "p", "source_product_id": f"s{i}",
                  "source_ref": f"r{i}"} for i in range(500)]
    fake_members = [{"product_group_id": f"g{i}"} for i in range(500)]
    out = []
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '60s'")
        for name, sql in PHASE1:
            plan = [r[0] for r in await conn.fetch("EXPLAIN " + sql, market, patterns)]
            out.append(_line("explain", statement=name, plan=plan))
        for name, sql in PHASE2:
            params = _phase2_params(name, keys, fake_rows, fake_members)
            plan = [r[0] for r in await conn.fetch("EXPLAIN " + sql, *params)]
            out.append(_line("explain", statement=name, plan=plan))
    return out


def report_lines(plan: Plan, *, samples_per_tier: int = 20) -> List[Dict[str, Any]]:
    return [_line(d.pop("section"), **d) for d in plan.summary(samples_per_tier=samples_per_tier)]


def _jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(r, default=str, sort_keys=True, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")


def _gcs_upload(bucket: str, name: str, body: bytes) -> None:  # pragma: no cover - needs GCP
    """Upload with the job's service account (metadata-server token), stdlib only. UNTESTED against
    GCS: the default --out is local and the audit sample is in the job's log lines."""
    import urllib.parse
    import urllib.request

    tok_req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(tok_req, timeout=10) as r:
        token = json.loads(r.read())["access_token"]
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o"
           f"?uploadType=media&name={urllib.parse.quote(name, safe='')}")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/x-ndjson"})
    with urllib.request.urlopen(req, timeout=60):
        pass


def write_outputs(out: str, files: Mapping[str, bytes], *,
                  uploader: Callable[[str, str, bytes], None] = _gcs_upload) -> List[str]:
    written = []
    if out.startswith("gs://"):
        bucket, _, prefix = out[len("gs://"):].partition("/")
        for name, body in files.items():
            obj = f"{prefix.rstrip('/')}/{name}" if prefix else name
            uploader(bucket, obj, body)
            written.append(f"gs://{bucket}/{obj}")
        return written
    os.makedirs(out, exist_ok=True)
    for name, body in files.items():
        path = os.path.join(out, name)
        with open(path, "wb") as fh:
            fh.write(body)
        written.append(path)
    return written


def output_files(plan: Plan, *, audit_size: int, audit_seed: int) -> Dict[str, bytes]:
    return {"proposals.jsonl": _jsonl(plan.proposals),
            "audit_sample.jsonl": _jsonl(plan.audit_rows(size=audit_size, seed=audit_seed))}


def new_proposal_run_id(now: Optional[datetime] = None) -> str:
    return "cmss_" + (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


class Refused(RuntimeError):
    pass


async def write_proposals(conn: Any, plan: Plan, *, expect_digest: str, max_proposals: int,
                          run_id: Optional[str] = None) -> Dict[str, Any]:
    """Insert `plan`'s proposals (status 'proposed') through the engine's writer, after re-checking
    the digest, the cap and every proposal's matcher contract. Never approves, never applies."""
    from services.identity_resolution import upsert_proposals

    digest = plan.digest()
    if digest != expect_digest:
        raise Refused(f"the proposal set changed since the dry run: expected {expect_digest}, now {digest}")
    if len(plan.proposals) > max_proposals:
        raise Refused(f"{len(plan.proposals)} proposals exceed --max-proposals {max_proposals}; "
                      "narrow with --brands / --limit-groups")
    run_id = run_id or new_proposal_run_id()
    stamped = stamp_run(plan.proposals, run_id)
    bad = {p["proposal_id"]: errs for p in stamped if (errs := evidence_problems(p))}
    if bad:
        raise Refused(f"proposals fail the engine's module rule: {dict(list(bad.items())[:5])}")
    inserted: List[Dict[str, Any]] = []
    async with conn.transaction():
        for p in stamped:
            # #2637 (L3): only this module may write this strategy's proposals, and says so.
            counts = await upsert_proposals(conn, [p], module_writer=STRATEGY)
            if counts["inserted"]:
                inserted.append(p)
    return {"proposal_run_id": run_id, "proposed": len(stamped), "inserted": len(inserted),
            "deduped": len(stamped) - len(inserted), "dry_run_digest": digest}


def approve_commands(run_id: str) -> Dict[str, str]:
    """#2637's module-run approval: the dry run prints the ira_ digest the --execute run needs."""
    base = f'python -m scripts.identity_engine approve --strategy {STRATEGY} --run {run_id} --by "<who>"'
    return {"approve_dry_run": base, "approve_execute": base + " --expect-digest <ira_ from the dry run> --execute"}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--write-proposals", action="store_true")
    mode.add_argument("--explain", action="store_true",
                      help="print EXPLAIN (no ANALYZE) of every statement and exit; nothing else runs")
    ap.add_argument("--expect-digest")
    ap.add_argument("--max-proposals", type=int)
    ap.add_argument("--market", default="US")
    ap.add_argument("--brands", default="")
    ap.add_argument("--limit-groups", type=int)
    ap.add_argument("--out", default=OUT_DEFAULT,
                    help="local directory (default) or gs://bucket/prefix (UNTESTED upload)")
    ap.add_argument("--audit-size", type=int, default=AUDIT_SAMPLE_DEFAULT)
    ap.add_argument("--audit-seed", type=int, default=AUDIT_SEED_DEFAULT)
    args = ap.parse_args(list(argv))
    if args.write_proposals and (not args.expect_digest or not args.max_proposals or args.max_proposals < 1):
        ap.error("--write-proposals needs --expect-digest (from a dry run) and --max-proposals >= 1")
    if args.limit_groups is not None and args.limit_groups < 1:
        ap.error("--limit-groups must be >= 1")
    args.market = args.market.strip().upper()
    args.brand_list = [b.strip() for b in args.brands.split(",") if b.strip()]
    return args


def _print(line: Mapping[str, Any]) -> None:
    print(json.dumps(line, default=str, separators=(",", ":"), ensure_ascii=False), flush=True)


async def run(conn: Any, args: argparse.Namespace, *,
              uploader: Callable[[str, str, bytes], None] = _gcs_upload) -> int:
    if args.explain:
        for line in await explain(conn, args.market, args.brand_list):
            _print(line)
        return 0
    snap, counts = await export_snapshot(conn, args.market, brands=args.brand_list, limit_groups=args.limit_groups)
    _print(_line("loaded", market=args.market, brands=args.brand_list, **counts))
    plan = plan_market(snap, args.market, brands=args.brand_list, limit_groups=args.limit_groups)
    for line in report_lines(plan):
        _print(line)
    audit = plan.audit_rows(size=args.audit_size, seed=args.audit_seed)
    if not args.write_proposals:
        for row in audit:
            _print(_line("audit", **row))
    if args.write_proposals:
        try:
            result = await write_proposals(conn, plan, expect_digest=args.expect_digest,
                                           max_proposals=args.max_proposals)
        except Refused as exc:
            _print(_line("refused", reason=str(exc)))
            return 2
        _print(_line("written", strategy=STRATEGY, **result, **approve_commands(result["proposal_run_id"]),
                     next_step="after Peng reviewed the audit sample: approve_dry_run, then approve_execute"))
        return 0
    if args.out.startswith("gs://"):
        _print(_line("warning", gcs_upload="UNTESTED against GCS; the audit sample is also in the log lines"))
    written = write_outputs(args.out, output_files(plan, audit_size=args.audit_size, audit_seed=args.audit_seed),
                            uploader=uploader)
    _print(_line("outputs", files=written, digest=plan.digest(), proposals=len(plan.proposals),
                 next_step=(f"--write-proposals --expect-digest {plan.digest()} --max-proposals "
                            f"{len(plan.proposals)} (same --market/--brands/--limit-groups)")))
    return 0


async def _main(argv: Sequence[str]) -> int:
    import asyncpg

    args = parse_args(argv)
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn, timeout=30, command_timeout=300)
    try:
        return await run(conn, args)
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
