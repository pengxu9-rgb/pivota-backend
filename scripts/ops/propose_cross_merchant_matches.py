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

SELECTION (four flags, mutually exclusive). Each entry is "<proposal_id>=<content fingerprint>":
  --only-proposals PATH          keep only the proposals a LOCAL file names (laptop dry runs);
  --only-proposal-ids a=f,b=g    the same, inline (what a Cloud Run one-off job takes: it cannot see a
                                 laptop path; run_oneoff_job.sh picks an --args delimiter absent from
                                 the payload, so the commas survive);
  --exclude-proposals PATH       DRY RUNS ONLY: keep all but those;
  --exclude-proposal-ids a=f,... DRY RUNS ONLY.
  The content fingerprint (cfp_, identity_cross_merchant_match.content_fingerprint; spec in that
  module's docstring) covers every (moved listing, anchor) pair of the proposal as the audit rows
  show it: both sides' product_key, canonical_url, title, size, price, currency, plus tier and kind.
  The dry run prints it on every audit row, every sample and in a "fingerprints" line; the review
  page recomputes it in JavaScript from the audit rows. Every given fingerprint is recomputed and a
  mismatch refuses (exit 2): a write can only include a proposal whose listings and anchor are
  content-identical to what was reviewed.
  A file holds one entry per line (blank and '#' lines ignored) or a JSON list. gs:// paths are
  REFUSED. Every form goes through ONE normalisation (normalise_proposal_ids: strip, refuse empty /
  whitespace-only / malformed entries -- irp_ + 32 lowercase hex = cfp_ + 24 lowercase hex -- or one
  id with two fingerprints, then de-dup and sort), and the digest marks only the MODE, so
  --only-proposals FILE and --only-proposal-ids LIST naming the same entries give the same digest.
  Any read or parse failure refuses (exit 2), never falling back to no selection. An id that is not
  among THIS run's proposals refuses (exit 2, naming it); so does an empty list or a selection that
  keeps nothing. The selection is FOLDED INTO THE DIGEST (proposal_set_digest: each selected
  proposal's id and proposal_fingerprint, plus a line carrying the mode and the sorted id=cfp
  entries). The dry run prints the subset's digest, count and a next_step carrying the flag in the
  form used (shell-quoted path, or the sorted inline entries); --write-proposals takes the same
  selection, recomputes, and refuses on a mismatch. A SELECTED write also refuses while ANY
  'proposed' proposal of this strategy exists (an earlier run an approve could take whole), and when
  any selected proposal already exists (it would dedupe into another run); its run then holds exactly
  the selection. Every write takes pg_advisory_xact_lock(hashtext('cross_merchant_same_size')) before
  those checks, in the insert's transaction. Counts, samples, proposals.jsonl and the audit sample are
  of the subset and say so.
  A REJECTED (or approved / applied) proposal CANNOT be re-written by a selected write: its
  proposal_id is deterministic, so it already exists and the write refuses (would dedupe). A changed
  listing mints a new id; otherwise the old row must be dealt with in the engine first.
  AUDIT COMPLETENESS: the dry run's outputs line carries audit_complete (every moving pair is in the
  audit; false when --audit-size sampled). Any SELECTED run (dry or write) prints a FULL audit and
  refuses a selected proposal with a pair missing from it. The review page needs every pair of a
  proposal to compute its fingerprint: label from a run whose audit_complete is true.

SCOPE: --market US (default), --limit-groups N, and EXACTLY ONE of
  --brands a,b,c   canonical brand match (services.product_match_normalizer.canonical_brand): every
                   stored spelling of the family ("Dr.Jart+", "Dr. Jart+", "DR JART" for drjart) is
                   found through the trigram index, chosen exactly in Python, and read with
                   `brand = ANY(...)` -- a scoped run equals the full run restricted to those brands;
  --all-brands     every brand of the market: a full catalog_products scan. Refused unless asked for
                   by name, and the run prints a WARN line.

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
import shlex
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
    brand_blind_spot_misses,
    brand_exact_regex,
    brand_preselect_regexes,
    gtin_lookup_values,
    evidence_problems,
    plan_market,
    normalise_proposal_ids,
    select_brand_spellings,
    SelectionError,
    stamp_run,
)

CENSUS = "cross_merchant_match"
#: Markets whose marketplace feeds carry the spellings the --brands pre-select is weakest on (full-width
#: Latin in JP, bracketed brands in KR): every run there prints a WARN line.
WARN_MARKETS = ("JP", "KR")
AUDIT_SAMPLE_DEFAULT = 600
AUDIT_SEED_DEFAULT = 20261016
OUT_DEFAULT = "/tmp/cross_merchant_match"

# PHASE 0 (--brands only) -- the stored spellings of the requested brands. $1 is one chunk of a
# family's LOOSE regex (identity_cross_merchant_match.brand_preselect_regexes: together a superset of
# its ASCII-keyed spellings, each chunk small enough that the trigram GIN index
# idx_catalog_products_brand_trgm, migration 059, on lower(coalesce(brand, '')), serves it); one
# statement per chunk. DISTINCT spellings only; select_brand_spellings then keeps
# exactly those whose canonical_brand is the family.
BRAND_SPELLINGS_SQL = """
SELECT DISTINCT p.brand, lower(coalesce(p.brand, '')) AS lowered
FROM catalog_products p
WHERE lower(coalesce(p.brand, '')) ~ $1
"""
# PHASE 0, the blind-spot guard (--brands only; controller MEDIUM 10-17): every DISTINCT brand of the
# two shapes the loose regex cannot see -- starting with a non-alphanumeric character, or holding a
# non-ASCII one (identity_cross_merchant_match.in_brand_blind_spot is the same predicate in Python).
# Neither shape has a trigram to extract, so this is ONE sequential scan of catalog_products (the
# trigram index cannot serve it even with seq scans disabled). Measured locally on 300k rows / 180 MB
# with the 059 index: a parallel seq scan, 36 ms warm, reading every heap page -- its prod cost scales
# with the table, so it runs once per scoped run, inside the same read-only transaction and
# statement_timeout, and the SAFETY window above applies to scoped runs too. A requested family with a spelling here that
# the pre-select did not find refuses the run (brand_blind_spot_misses).
BRAND_BLIND_SPOT_SQL = r"""
SELECT DISTINCT p.brand
FROM catalog_products p
WHERE p.brand ~ '^[^[:alnum:]]' OR p.brand ~ '[^\x01-\x7F]'
"""
# PHASE 1 -- the run market's candidates. $1 market. With --brands, the EXACT filter is
# `p.brand = ANY($3)` over phase 0's spellings; $2 (brand_exact_regex: `^(s1|s2|...)$` of exactly
# those spellings, lowered by PostgreSQL itself) is the same condition spelled so the trigram index
# can find the rows -- there is no btree on brand, and no new index is added. --all-brands reads
# every candidate of the market: a full catalog_products scan (refused unless named; WARN line).
_PRODUCT_FILTER = """
    p.pivota_signature_id IS NOT NULL AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
    AND p.sync_status = 'live'{brand}
    AND EXISTS (SELECT 1 FROM catalog_offers o0
                WHERE o0.product_key = p.product_key AND o0.suppressed_at IS NULL
                  AND o0.suppression_reason IS NULL AND o0.market = $1)
"""
_BRAND_CLAUSE = "\n    AND lower(coalesce(p.brand, '')) ~ $2 AND p.brand = ANY($3::text[])"


def _products_sql(branded: bool) -> str:
    return f"""
SELECT p.product_key, p.merchant_id, p.platform, p.source_product_id, p.source_ref, p.content_key,
       p.pivota_signature_id, p.brand, p.title, p.gtin, p.source_system, p.seed_kind, p.created_at,
       p.image_url, p.canonical_url, p.source_domain
FROM catalog_products p
WHERE {_PRODUCT_FILTER.format(brand=_BRAND_CLAUSE if branded else "")}
"""


_HOST_EXPR = """lower(coalesce(nullif(btrim(o.source_domain), ''),
                      substring(o.offer_payload->>'destination_url' from '^[A-Za-z][A-Za-z0-9+.-]*://([^/:?#]+)')))"""


def _hosts_sql(branded: bool) -> str:
    return f"""
SELECT o.product_key, o.market, {_HOST_EXPR} AS host, bool_or(o.offer_type = 'brand_direct') AS brand_direct
FROM catalog_offers o
JOIN catalog_products p ON p.product_key = o.product_key
WHERE {_PRODUCT_FILTER.format(brand=_BRAND_CLAUSE if branded else "")}
  AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL AND o.market = $1
GROUP BY 1, 2, 3
"""


PRODUCTS_SQL, HOSTS_SQL = _products_sql(False), _hosts_sql(False)
PRODUCTS_BRAND_SQL, HOSTS_BRAND_SQL = _products_sql(True), _hosts_sql(True)
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
# This strategy's live proposals touching the keys, every market, as two lookups: as TARGET through
# idx_identity_resolution_proposals_content_key, and as SOURCE within this strategy's live proposals
# (idx_identity_resolution_proposals_status on (status, strategy): there is no expression index on
# evidence->>'from_content_key', so that half reads this strategy's live rows only, never the table).
_PROPOSAL_COLS = """r.proposal_id, r.status, r.kind, r.content_key, r.keeper_product_key,
       jsonb_build_object('matcher_verdict', r.evidence->'matcher_verdict',
                          'from_content_key', r.evidence->'from_content_key') AS evidence"""
PROPOSALS_SQL = f"""
SELECT {_PROPOSAL_COLS} FROM identity_resolution_proposals r
WHERE r.content_key = ANY($3::text[]) AND r.strategy = $1 AND r.status = ANY($2::text[])
UNION
SELECT {_PROPOSAL_COLS} FROM identity_resolution_proposals r
WHERE r.status = ANY($2::text[]) AND r.strategy = $1 AND r.evidence->>'from_content_key' = ANY($3::text[])
"""
# EVERY live row, any brand, any market, carrying one of the candidates' GTINs (every stored spelling:
# 14/13/12 digits), through idx_catalog_products_gtin: GTIN pollution is computed over these too, so a
# brand-scoped run and a full run give a pair the same tier.
GTIN_ROWS_SQL = """
SELECT g.product_key, g.merchant_id, g.source_domain, g.brand, g.title, g.gtin, g.content_key
FROM catalog_products g
WHERE g.gtin = ANY($1::text[]) AND g.suppression_reason IS NULL AND g.suppressed_at IS NULL
"""
PHASE1: Tuple[Tuple[str, str], ...] = (("products", PRODUCTS_SQL), ("hosts", HOSTS_SQL))
PHASE1_BRANDS: Tuple[Tuple[str, str], ...] = (("products", PRODUCTS_BRAND_SQL), ("hosts", HOSTS_BRAND_SQL))
PHASE2: Tuple[Tuple[str, str], ...] = (
    ("rows", ROWS_SQL), ("offers", OFFERS_SQL), ("skus", SKUS_SQL), ("memberships", MEMBERSHIPS_SQL),
    ("group_sizes", GROUP_SIZES_SQL), ("seeds", SEEDS_SQL), ("proposals", PROPOSALS_SQL),
    ("gtin_rows", GTIN_ROWS_SQL))


def _line(section: str, **fields: Any) -> Dict[str, Any]:
    return {"census": CENSUS, "section": section, **fields}


def _phase1(spellings: Optional[Sequence[Tuple[str, str]]]) -> Tuple[Tuple[Tuple[str, str], ...], Tuple[Any, ...]]:
    """The phase-1 statements and their parameters after $1 (the market): `spellings` None for
    --all-brands, else phase 0's chosen (brand, lowered) pairs (never empty here)."""
    if spellings is None:
        return PHASE1, ()
    return PHASE1_BRANDS, (brand_exact_regex(lo for _b, lo in spellings), sorted({b for b, _lo in spellings}))


async def brand_spellings(conn: Any, brands: Sequence[str]) -> List[Tuple[str, str]]:
    """Phase 0: (stored brand, lower(coalesce(brand, ''))) for every spelling whose canonical_brand is
    one of `brands`' families -- loose index-served pre-select per family, exact choice in Python.
    Raises Refused when a stored spelling of a requested family sits in the pre-select's blind spot
    (BRAND_BLIND_SPOT_SQL): the scoped run would silently differ from the full run."""
    found: Dict[str, str] = {}
    for fam, regexes in brand_preselect_regexes(brands).items():
        lowered: Dict[str, str] = {}
        for regex in regexes:  # chunks small enough for pg_trgm (PG_REGEX_COST_BUDGET)
            for r in await conn.fetch(BRAND_SPELLINGS_SQL, regex):
                if r["brand"] is not None:
                    lowered[str(r["brand"])] = str(r["lowered"])
        for b in select_brand_spellings([fam], lowered):
            found[b] = lowered[b]
    blind = [r["brand"] for r in await conn.fetch(BRAND_BLIND_SPOT_SQL)]
    misses = brand_blind_spot_misses(brands, blind, found)
    if misses:
        n = sum(len(v) for v in misses.values())
        raise Refused(
            f"--brands would miss {n} stored spelling(s) its index pre-select cannot see "
            f"(leading punctuation or non-ASCII letters): {misses}. A scoped run must equal the full run "
            "restricted to the brand: rerun with --all-brands, or fix those brand spellings")
    return sorted(found.items())


def _phase2_params(name: str, cks: List[str], rows: List[Dict[str, Any]],
                   memberships: List[Dict[str, Any]], gtins: Sequence[str] = ()) -> Tuple[Any, ...]:
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
    if name == "gtin_rows":
        return (sorted(gtins),)
    raise KeyError(name)


async def export_snapshot(conn: Any, market: str, *, brands: Sequence[str] = (),
                          limit_groups: Optional[int] = None) -> Tuple[Snapshot, Dict[str, Any]]:
    """The only read: ONE read-only transaction, statement_timeout 60s. Phase 1 reads the market's
    candidates (brand-filtered in SQL); Python computes the groups this run judges (Planner.scope);
    phase 2 reads the details for those content_keys only. `brands` empty = every brand (the caller
    refuses that unless --all-brands was given)."""
    spellings: Optional[List[Tuple[str, str]]] = None
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '60s'")
        if brands:
            spellings = await brand_spellings(conn, brands)
        if spellings == []:
            products, hosts = [], []  # no stored spelling of any requested brand
        else:
            phase1, extra = _phase1(spellings)
            products = [dict(r) for r in await conn.fetch(phase1[0][1], market, *extra)]
            hosts = [dict(r) for r in await conn.fetch(phase1[1][1], market, *extra)]
        pks, cks = Planner(Snapshot(products=products, offers=[], skus=[], ck_rows={}, memberships={},
                                    group_sizes={}, hosts=hosts), market,
                           brands=brands, limit_groups=limit_groups).scope()
        in_scope = set(pks)
        gtins = gtin_lookup_values(p.get("gtin") for p in products if str(p["product_key"]) in in_scope)
        got: Dict[str, List[Dict[str, Any]]] = {"rows": [], "memberships": []}
        for name, sql in PHASE2:
            if not cks:
                got[name] = []
                continue
            params = _phase2_params(name, cks, got["rows"], got["memberships"], gtins)
            got[name] = [dict(r) for r in await conn.fetch(sql, *params)]
    ck_rows: Dict[str, List[Dict[str, Any]]] = {}
    for r in got["rows"]:
        ck_rows.setdefault(r["content_key"], []).append(r)
    memberships = {(str(r["merchant_id"]), str(r["platform"]), str(r["platform_product_id"])): r["product_group_id"]
                   for r in got["memberships"]}
    group_sizes = {r["product_group_id"]: int(r["n"]) for r in got["group_sizes"]}
    snap = Snapshot(products=products, hosts=hosts, offers=got["offers"], skus=got["skus"], ck_rows=ck_rows,
                    memberships=memberships, group_sizes=group_sizes, seeds=got["seeds"],
                    proposals=got["proposals"], gtin_rows=got["gtin_rows"])
    counts: Dict[str, Any] = {"products": len(products), "candidate_content_keys": len(cks),
                              **{k: len(v) for k, v in got.items()}}
    if spellings is not None:
        counts["brand_spellings"] = [b for b, _lo in spellings]
    return snap, counts


async def explain(conn: Any, market: str, brands: Sequence[str]) -> List[Dict[str, Any]]:
    """EXPLAIN (no ANALYZE: nothing is executed) of every statement, for review before the timed
    run. Phase 0 is planned with each family's real regex; phase 1 with the requested brands as
    placeholder spellings (phase 0 is not run); phase 2 with 500 placeholder keys."""
    phase1, extra = _phase1([(b, b.lower()) for b in brands] if brands else None)
    keys = [f"ck_explain_{i}" for i in range(500)]
    fake_rows = [{"product_key": f"pk_{i}", "merchant_id": "m", "platform": "p", "source_product_id": f"s{i}",
                  "source_ref": f"r{i}"} for i in range(500)]
    fake_members = [{"product_group_id": f"g{i}"} for i in range(500)]
    out = []
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '60s'")
        for fam, regexes in brand_preselect_regexes(brands).items():
            for n, regex in enumerate(regexes):
                plan = [r[0] for r in await conn.fetch("EXPLAIN " + BRAND_SPELLINGS_SQL, regex)]
                out.append(_line("explain", statement="brand_spellings", family=fam, chunk=n, plan=plan))
        if brands:
            plan = [r[0] for r in await conn.fetch("EXPLAIN " + BRAND_BLIND_SPOT_SQL)]
            out.append(_line("explain", statement="brand_blind_spot", plan=plan))
        for name, sql in phase1:
            plan = [r[0] for r in await conn.fetch("EXPLAIN " + sql, market, *extra)]
            out.append(_line("explain", statement=name, plan=plan))
        for name, sql in PHASE2:
            params = _phase2_params(name, keys, fake_rows, fake_members, [f"{i:014d}" for i in range(500)])
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


#: (mode, normalised proposal_ids, the selection flag as next_step prints it)
#: (mode, {proposal_id: reviewed content fingerprint}, the selection flag as next_step prints it)
Selection = Tuple[str, Dict[str, str], str]


def parse_proposal_ids(text: str, *, source: str = "the selection file") -> Dict[str, str]:
    """{proposal_id: cfp} from a selection file's text: a JSON list of "id=cfp" strings, or one
    "id=cfp" per line (blank lines and lines starting with '#' ignored), through
    normalise_proposal_ids. Raises Refused."""
    body = text.strip()
    if body.startswith("["):
        try:
            raw = json.loads(body)
        except ValueError as exc:
            raise Refused(f"{source} is not a valid JSON list: {exc}") from exc
    else:
        raw = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    try:
        return normalise_proposal_ids(raw, source=source)
    except SelectionError as exc:
        raise Refused(str(exc)) from exc


def _load_selection(args: argparse.Namespace) -> Optional[Selection]:
    for mode in ("only", "exclude"):
        path = getattr(args, f"{mode}_proposals", None)
        inline = getattr(args, f"{mode}_proposal_ids", None)
        if path is not None:
            flag = f"--{mode}-proposals"
            if path.strip().lower().startswith("gs://"):
                raise Refused(f"{flag} {path}: gs:// is not read (no untested bucket path); pass the "
                              f"selection inline with --{mode}-proposal-ids id1=cfp1,id2=cfp2,... instead")
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            entries = parse_proposal_ids(text, source=f"{flag} {path}")
            shown = f"{flag} {shlex.quote(path)}"
        elif inline is not None:
            flag = f"--{mode}-proposal-ids"
            try:
                entries = normalise_proposal_ids(inline.split(","), source=flag) if inline.strip() else {}
            except SelectionError as exc:
                raise Refused(str(exc)) from exc
            shown = f"{flag} {','.join(f'{pid}={fp}' for pid, fp in entries.items())}"
        else:
            continue
        if not entries:
            raise Refused(f"{flag} names no proposal_id: an empty selection is refused")
        return mode, entries, shown
    return None


def load_selection(args: argparse.Namespace) -> Optional[Selection]:
    """The selection one of the four flags names, else None. EVERY failure -- a gs:// path, an
    unreadable or undecodable file, any malformed entry, an empty selection, anything unexpected --
    is Refused (exit 2) before the database is read; it never falls back to no selection and never
    escapes as a traceback."""
    try:
        return _load_selection(args)
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001 -- by design: any failure refuses, nothing falls through
        raise Refused(f"the proposal selection could not be read: {type(exc).__name__}: {exc}") from exc


def selection_flag(selection: Optional[Selection]) -> str:
    return "" if selection is None else f" {selection[2]}"


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
    if plan.selection is not None and plan.selection[0] != "only":
        raise Refused("--write-proposals writes only an --only selection; --exclude is for dry runs")
    run_id = run_id or new_proposal_run_id()
    stamped = stamp_run(plan.proposals, run_id)
    bad = {p["proposal_id"]: errs for p in stamped if (errs := evidence_problems(p))}
    if bad:
        raise Refused(f"proposals fail the engine's module rule: {dict(list(bad.items())[:5])}")
    inserted: List[Dict[str, Any]] = []
    async with conn.transaction():
        # one writer of this strategy at a time: the pending / dedupe checks below and the inserts
        # are one critical section (released at commit or rollback)
        await conn.execute(LOCK_SQL, STRATEGY)
        if plan.selection is not None:
            await refuse_unsafe_selected_write(conn, [p["proposal_id"] for p in stamped])
        for p in stamped:
            # #2637 (L3): only this module may write this strategy's proposals, and says so. module_writer
            # is a CONVENTION the engine's Python writer checks, not a database constraint: nothing in
            # the schema stops other code with a connection inserting such a row. Fine for phase 1 --
            # every write is a one-off job run by hand, and approval still needs #2637's module-run
            # approval with its digest.
            counts = await upsert_proposals(conn, [p], module_writer=STRATEGY)
            if counts["inserted"]:
                inserted.append(p)
        if plan.selection is not None and len(inserted) != len(stamped):
            # a concurrent writer since the check above: roll the whole selected write back
            raise Refused(f"{len(stamped) - len(inserted)} selected proposal(s) deduped onto existing rows; "
                          "a selected write must insert every proposal into its own run (rolled back)")
    return {"proposal_run_id": run_id, "proposed": len(stamped), "inserted": len(inserted),
            "deduped": len(stamped) - len(inserted), "dry_run_digest": digest,
            **({"selection": plan.selection_info} if plan.selection_info else {})}


LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext($1))"
PENDING_SQL = """
SELECT r.proposal_id, r.evidence->>'proposal_run_id' AS proposal_run_id
FROM identity_resolution_proposals r
WHERE r.strategy = $1 AND r.status = 'proposed'
ORDER BY r.proposal_id
LIMIT 20
"""
EXISTING_SQL = """
SELECT r.proposal_id, r.status, r.evidence->>'proposal_run_id' AS proposal_run_id
FROM identity_resolution_proposals r
WHERE r.proposal_id = ANY($1::text[])
ORDER BY r.proposal_id
LIMIT 20
"""


async def refuse_unsafe_selected_write(conn: Any, proposal_ids: Sequence[str]) -> None:
    """A selected write must leave its run holding EXACTLY the selection, and no other pending run of
    this strategy may exist: #2637 approves a module run as a whole, so an earlier full-set write
    still 'proposed' (or a selected proposal deduping onto an older run) would let an approve take
    more than was labelled. Raises Refused."""
    pending = [dict(r) for r in await conn.fetch(PENDING_SQL, STRATEGY)]
    if pending:
        runs = sorted({str(r["proposal_run_id"]) for r in pending})
        raise Refused(f"{STRATEGY} already has pending ('proposed') proposals under run(s) {runs} "
                      f"(first: {[r['proposal_id'] for r in pending][:5]}): reject or resolve them before a "
                      "selected write, or an approve of that run could take more than was labelled")
    existing = [dict(r) for r in await conn.fetch(EXISTING_SQL, list(proposal_ids))]
    if existing:
        raise Refused(f"{len(existing)}+ selected proposal(s) already exist and would dedupe (deduped > 0): "
                      f"{[(r['proposal_id'], r['status'], r['proposal_run_id']) for r in existing][:5]}")


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
    select = ap.add_mutually_exclusive_group()
    select.add_argument("--only-proposals", metavar="PATH",
                        help="keep only the proposals this LOCAL file names as id=cfp (one per line or a JSON "
                             "list; gs:// refused); each content fingerprint is verified; folded into the digest")
    select.add_argument("--exclude-proposals", metavar="PATH",
                        help="DRY RUNS ONLY: keep every proposal except those this LOCAL file names (id=cfp)")
    select.add_argument("--only-proposal-ids", metavar="ID=CFP,...",
                        help="keep only these id=cfp proposals (comma-separated; same digest as the file form)")
    select.add_argument("--exclude-proposal-ids", metavar="ID=CFP,...",
                        help="DRY RUNS ONLY: keep every proposal except these id=cfp (comma-separated)")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--brands", default="", help="comma-separated brands (canonical brand match)")
    scope.add_argument("--all-brands", action="store_true",
                       help="every brand of the market: a FULL catalog_products scan (prints a WARN line)")
    ap.add_argument("--limit-groups", type=int)
    ap.add_argument("--out", default=OUT_DEFAULT,
                    help="local directory (default) or gs://bucket/prefix (UNTESTED upload)")
    ap.add_argument("--audit-size", type=int, default=AUDIT_SAMPLE_DEFAULT)
    ap.add_argument("--audit-seed", type=int, default=AUDIT_SEED_DEFAULT)
    args = ap.parse_args(list(argv))
    if args.write_proposals and (not args.expect_digest or not args.max_proposals or args.max_proposals < 1):
        ap.error("--write-proposals needs --expect-digest (from a dry run) and --max-proposals >= 1")
    if args.write_proposals and (args.exclude_proposals is not None or args.exclude_proposal_ids is not None):
        ap.error("--write-proposals writes only an --only selection; --exclude-proposals / "
                 "--exclude-proposal-ids are for dry runs")
    if args.explain and any(getattr(args, a) is not None for a in (
            "only_proposals", "exclude_proposals", "only_proposal_ids", "exclude_proposal_ids")):
        ap.error("--explain plans the statements only; it takes no proposal selection")
    if args.limit_groups is not None and args.limit_groups < 1:
        ap.error("--limit-groups must be >= 1")
    args.market = args.market.strip().upper()
    args.brand_list = [b.strip() for b in args.brands.split(",") if b.strip()]
    if not args.brand_list and not args.all_brands:
        # M3: no silent full scan of catalog_products on the 2-vCPU primary.
        ap.error("give --brands a,b,c, or --all-brands to scan every brand of the market (full table scan)")
    return args


def _print(line: Mapping[str, Any]) -> None:
    print(json.dumps(line, default=str, separators=(",", ":"), ensure_ascii=False), flush=True)


async def run(conn: Any, args: argparse.Namespace, *,
              uploader: Callable[[str, str, bytes], None] = _gcs_upload) -> int:
    if getattr(args, "all_brands", False):
        _print(_line("warning", level="WARN",
                     message=("WARN --all-brands: phase 1 reads every signed live product of market "
                              f"{args.market} -- a full catalog_products scan on the primary. Start only "
                              "inside the SAFETY window (CPU under 35%).")))
    if args.explain:
        for line in await explain(conn, args.market, args.brand_list):
            _print(line)
        return 0
    try:
        selection = load_selection(args)
    except Refused as exc:  # gs://, unreadable, malformed or empty selection: refused before any read
        _print(_line("refused", reason=str(exc)))
        return 2
    try:
        snap, counts = await export_snapshot(conn, args.market, brands=args.brand_list,
                                             limit_groups=args.limit_groups)
    except Refused as exc:  # the --brands blind-spot guard
        _print(_line("refused", reason=str(exc)))
        return 2
    non_ascii = [b for b in counts.get("brand_spellings") or () if not str(b).isascii()]
    if args.market in WARN_MARKETS or non_ascii:
        # The brand pre-select's known blind spots are full-width / non-ASCII spellings and bracketed
        # leads (JP and KR marketplace data); the guard refuses a run that provably misses one, and this
        # line says the run is in that territory even when it did not.
        _print(_line("warning", level="WARN", market=args.market, non_ascii_brand_spellings=non_ascii,
                     message=(f"WARN market {args.market} / non-ASCII brand spellings {non_ascii}: the --brands "
                              "pre-select does not index full-width or multi-character-lead spellings (a scoped "
                              "run passed the blind-spot guard); review brand_spellings in the loaded line")))
    _print(_line("loaded", market=args.market, brands=args.brand_list, **counts))
    plan = plan_market(snap, args.market, brands=args.brand_list, limit_groups=args.limit_groups)
    if selection is not None:
        try:
            plan.select(selection[0], selection[1])
        except SelectionError as exc:
            _print(_line("refused", reason=str(exc), selection=selection[2]))
            return 2
    for line in report_lines(plan):
        _print(line)
    # a selected run (dry or write) prints EVERY pair: the page fingerprints whole proposals
    audit_size = len(plan.pairs) if selection is not None else args.audit_size
    audit = plan.audit_rows(size=audit_size, seed=args.audit_seed)
    audit_complete = len(audit) == len(plan.pairs)
    if selection is not None:
        gaps = plan.audit_gaps(audit)
        if gaps:
            _print(_line("refused", selection=selection[2], reason=(
                f"{len(gaps)} selected proposal(s) have pairs missing from the audit, so the review page "
                f"cannot have fingerprinted them whole: {dict(list(gaps.items())[:5])}")))
            return 2
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
        _print(_line("written", strategy=STRATEGY, **result, audit_complete=audit_complete,
                     **approve_commands(result["proposal_run_id"]),
                     next_step="after Peng reviewed the audit sample: approve_dry_run, then approve_execute"))
        return 0
    if args.out.startswith("gs://"):
        _print(_line("warning", gcs_upload="UNTESTED against GCS; the audit sample is also in the log lines"))
    written = write_outputs(args.out, output_files(plan, audit_size=audit_size, audit_seed=args.audit_seed),
                            uploader=uploader)
    _print(_line("outputs", files=written, digest=plan.digest(), proposals=len(plan.proposals),
                 audit_complete=audit_complete, audit_rows=len(audit), pairs=len(plan.pairs),
                 **({"selection": plan.selection_info} if plan.selection_info else {}),
                 next_step=(f"--write-proposals --expect-digest {plan.digest()} --max-proposals "
                            f"{len(plan.proposals)}{selection_flag(selection)} "
                            "(same --market, --brands or --all-brands, --limit-groups)")))
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
