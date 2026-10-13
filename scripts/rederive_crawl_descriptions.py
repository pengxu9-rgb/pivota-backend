#!/usr/bin/env python3
"""Re-derive crawl-written catalog descriptions with the current description producer.

WHY. `_PDP_UPSERT_SQL` (services/catalog_enrichment_agent/apply.py) never rewrites an existing
row's `description`: a re-crawl writes the description only when it inserts the row. So a fix to
the storefront description producer (`curated_brand_feed.body_html_to_text` and the per-batch
repetition pass) reaches no row that already exists. This one-off re-derives the text the way a
retailer-ingest job derives it and rewrites ONLY copy the old crawl wrote.

WHAT IT DOES, per host:

  1. JOBS. Rebuild a host only from its completed `retailer_ingest_jobs` rows whose source is the
     Shopify `/products.json` crawl (`pipeline.job_source(options) == "storefront"`), the latest
     job per cohort (`scope_key`), newest first. `affiliate_feed` and `shopify_markets` jobs are
     excluded. A host with no such job, or whose crawl fails or is not proven complete, is
     reported and skipped.
  2. ROWS. Live, unsuppressed, crawl-minted (`catalog_enrichment_agent_v1`) rows with a
     description, a Shopify product URL and a stage other than hold/archived, created on or after
     `--since` (phase 1 by default; `--since all` drops the bound).
  3. REBUILD. `records_for_brand` exactly as the drain's `_crawl` builds it (`ingest_payload` =
     the normalized `_feed_payload`), with PDP INCI and GTIN fetches off, then the pure
     `ingest_validated_jsonl` with the job's market and each row's current listing. A row the plan
     does not produce is `not_in_feed` and untouched. The batch is TODAY's feed.
  4. GUARD. A row is written only when its stored description EQUALS (`=`) the crawl's own copy
     (`enrichment_meta.candidate_attribute_summary`) or `legacy_body_html_to_text` of the
     product's current body_html (a frozen copy of the old producer, below). Anything else was
     written by another lane or by hand: `not_crawl_owned`, never written.
  5. WRITE. One guarded UPDATE per row (`_UPDATE_SQL`): description, lifecycle stage and the
     description-derived taxonomy columns, only while the description and stage are still the
     ones read. 0 rows -> `raced`. Then the row's agent_pdp_view is refreshed.
  6. GATES. A row whose new text would drop its stage (`would_demote`) is skipped unless
     `--allow-demote`; a row whose new text loses a safety cue the old text had
     (`safety_cue_lost`) is never written.

OPERATION. Dry run by default: reads only, inside READ ONLY transactions, and writes nothing.
`--apply` refuses to start inside the 05:15-06:15Z sweep window, commits in batches of
`--batch-size` with a pause between them, and holds the drain's catalog write lock
(`db.retailer_ingest.catalog_write_lock`) around every batch so no drain apply writes alongside
it. Re-running is safe: a written row reads back `unchanged`.

    DATABASE_URL=... python scripts/rederive_crawl_descriptions.py                 # dry run
    DATABASE_URL=... python scripts/rederive_crawl_descriptions.py --hosts a,b      # some hosts
    DATABASE_URL=... python scripts/rederive_crawl_descriptions.py --apply          # write

It fetches from merchants: run it on the crawl egress, never the default one. The final line is
`SUMMARY {json}`; one `HOST {json}` line is printed per host. The file carries no at-sign so it
can also be handed to scripts/ops/run_oneoff_job.sh as an inline program.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import html
import json
import os
import re
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "__file__" in globals() else "/app"
sys.path.insert(0, _ROOT)

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.bulk_writer import is_transport_error  # noqa: E402

REFRESH_SOURCE = "crawl_description_rederive_v1"
PHASE1_SINCE = "2026-10-10"
BATCH_SIZE = 200
BATCH_SLEEP_S = 2.0
DEFAULT_BUDGET_S = 3000.0
LOCK_WAIT_S = 900.0
LOCK_POLL_S = 5.0
# The Shopify pacer's sweep window, UTC minutes [start, end): no --apply starts, and no crawl of an
# --apply run starts, inside it.
QUIET_WINDOW_UTC_MIN = (5 * 60 + 15, 6 * 60 + 15)
STAGE_RANK = {"draft": 0, "candidate": 1, "validated": 2, "published": 3}

SUMMARY_KEYS = ("rows", "would_rewrite", "written", "raced", "unchanged", "not_crawl_owned",
                "not_in_feed", "would_demote", "skipped_demote", "safety_rows", "safety_cue_lost")
EXTRA_KEYS = ("host_skipped", "would_empty", "not_in_feed_url_in_plan", "update_failed",
              "refresh_failed", "lock_unwritten", "jobs_crawled", "jobs_failed")

# Outcomes of classify_row.
NOT_IN_FEED = "not_in_feed"
UNCHANGED = "unchanged"
NOT_CRAWL_OWNED = "not_crawl_owned"
WOULD_EMPTY = "would_empty"
WRITE = "write"
SKIP_SAFETY = "skip_safety"
SKIP_DEMOTE = "skip_demote"

HOST_SQL = "lower(regexp_replace(split_part(split_part(canonical_url, '://', 2), '/', 1), '^www[.]', ''))"

_ROWS_SQL = f"""
SELECT product_key, content_key, canonical_url, source_domain, {HOST_SQL} AS host,
       title, description, image_url, category_path, tags, demographic, use_case_tags,
       lifestyle_tags, pdp_scope, source_system, pdp_lifecycle_stage,
       product_payload->'enrichment_meta'->>'candidate_attribute_summary' AS crawled_copy
  FROM catalog_products
 WHERE source_system = 'catalog_enrichment_agent_v1'
   AND sync_status = 'live' AND suppressed_at IS NULL
   AND description IS NOT NULL
   AND COALESCE(pdp_lifecycle_stage, '') NOT IN ('hold', 'archived')
   AND canonical_url LIKE 'https://%/products/%'
"""
# created_at is a timestamp WITHOUT time zone: bound as a naive UTC datetime (asyncpg refuses a str).
_ROWS_SINCE_SQL = "   AND created_at >= :since\n"

_JOBS_SQL = """
SELECT id, domain, brand, options, scope_key, status, updated_at,
       lower(regexp_replace(domain, '^www[.]', '')) AS host
  FROM retailer_ingest_jobs
 WHERE status = 'done' AND lower(regexp_replace(domain, '^www[.]', '')) = ANY(:hosts)
 ORDER BY updated_at DESC, id
"""

# THE WRITE. Every predicate is part of the guard: the row is still unsuppressed, its description
# and its stage are still the ones this run read and judged, and it has not been held or archived
# since. A row that moved updates nothing, and RETURNING says so -- `databases` on asyncpg runs
# `execute` as fetchval, which reports no rowcount, so the readback is `fetch_one` on RETURNING.
# price_tier is NOT set: derive_price_tier reads only the price, which this run does not change.
# content_changed_at is bumped by trg_catalog_products_content_changed_at (migration 138).
_UPDATE_SQL = """
UPDATE catalog_products
   SET description = :new, pdp_lifecycle_stage = :stage,
       use_case_tags = CAST(:use_case_tags AS jsonb),
       lifestyle_tags = CAST(:lifestyle_tags AS jsonb), demographic = :demographic,
       updated_at = NOW()
 WHERE product_key = :pk AND suppressed_at IS NULL
   AND description = :old
   AND pdp_lifecycle_stage IS NOT DISTINCT FROM :old_stage
   AND COALESCE(pdp_lifecycle_stage, '') NOT IN ('hold', 'archived')
RETURNING product_key
"""


# --- the OLD producer, frozen ----------------------------------------------------------------------
# curated_brand_feed.body_html_to_text as it was before the description-cleanup fix. Rows minted by
# it (and rows another backfill filled from body_html through it) hold exactly this text, which is
# the second arm of the ownership guard. Never "update" it to the current producer.
_LEGACY_BLOCK_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>|<!--.*?-->")
_LEGACY_TAG_RE = re.compile(r"<[^>]+>")


def legacy_body_html_to_text(body_html: Optional[str]) -> str:
    if not body_html:
        return ""
    text = _LEGACY_BLOCK_RE.sub(" ", str(body_html))
    text = _LEGACY_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 2000:
        cut = text[:2000]
        text = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return text


# --- pure helpers ----------------------------------------------------------------------------------

def norm_url(url: Any) -> str:
    """host (no www.) + lower-cased path without a trailing slash: one product page, however spelled."""
    parts = urlsplit(str(url or "").strip())
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{parts.path.rstrip('/').lower()}"


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value:
        return _as_utc(datetime.fromisoformat(str(value)))
    return datetime.min.replace(tzinfo=timezone.utc)


def in_quiet_window(now: datetime) -> bool:
    now = _as_utc(now).astimezone(timezone.utc)
    minute = now.hour * 60 + now.minute
    return QUIET_WINDOW_UTC_MIN[0] <= minute < QUIET_WINDOW_UTC_MIN[1]


def _job_options(job: Dict[str, Any]) -> Dict[str, Any]:
    options = job.get("options")
    if isinstance(options, str):
        options = json.loads(options) if options.strip() else {}
    return dict(options or {})


def select_jobs(jobs: Iterable[Dict[str, Any]], *,
                jobs_since: Optional[datetime]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """-> (the jobs a host is rebuilt from, newest first; the excluded sources seen).

    Completed jobs only; storefront (`/products.json`) jobs only; the LATEST job per cohort
    (`scope_key`): an older run of the same cohort crawls the same feed today. Jobs older than
    `jobs_since` are left out (None: no bound)."""
    from services.retailer_ingest.pipeline import job_source

    picked: List[Dict[str, Any]] = []
    scopes: set = set()
    excluded: set = set()
    for job in sorted(jobs, key=lambda j: _as_utc(j.get("updated_at")), reverse=True):
        if job.get("status") != "done":
            continue
        source = job_source(_job_options(job))
        if source != "storefront":
            excluded.add(source)
            continue
        if jobs_since is not None and _as_utc(job.get("updated_at")) < jobs_since:
            continue
        scope = job.get("scope_key") or job.get("id")
        if scope in scopes:
            continue
        scopes.add(scope)
        picked.append({**job, "options": _job_options(job)})
    return picked, sorted(excluded)


# Footnote markers glued onto a word ("irritation2", "eyes¹") are not part of the word: without
# this, a cue the new text still carries reads as lost (or a glued cue is never seen at all).
_GLUED_FOOTNOTE_RE = re.compile(r"(?<=[^\W\d_])[\d²³¹⁰-⁹]+")


def safety_cue_keys(text: Optional[str]) -> set:
    """The safety cues (`curated_brand_feed._SAFETY_CUE_RE`) a text carries, normalized."""
    from services.curated_brand_feed import _SAFETY_CUE_RE

    if not text:
        return set()
    spaced = _GLUED_FOOTNOTE_RE.sub(" ", str(text))
    return {re.sub(r"\s+", " ", m.group(0)).casefold() for m in _SAFETY_CUE_RE.finditer(spaced)}


def lost_safety_cues(old: Optional[str], new: Optional[str]) -> List[str]:
    return sorted(safety_cue_keys(old) - safety_cue_keys(new))


def taxonomy_for(row: Dict[str, Any], description: Optional[str]) -> Dict[str, Any]:
    from services.pdp_taxonomy import derive_taxonomy_v1

    return derive_taxonomy_v1(price=None, title=row.get("title"), description=description,
                              tags=[str(t) for t in as_list(row.get("tags"))])


def stage_for(row: Dict[str, Any], description: Optional[str], taxonomy: Dict[str, Any]) -> str:
    from services.pdp_lifecycle import compute_lifecycle_stage

    return compute_lifecycle_stage({
        "title": row.get("title"), "description": description, "image_url": row.get("image_url"),
        "category_path": row.get("category_path"), "tags": as_list(row.get("tags")),
        "demographic": taxonomy.get("demographic"), "use_case_tags": taxonomy.get("use_case_tags"),
        "lifestyle_tags": taxonomy.get("lifestyle_tags"), "pdp_scope": row.get("pdp_scope"),
        "source_system": row.get("source_system"),
    })


def crawl_owned(old: Optional[str], crawled_copy: Optional[str], legacy_text: Optional[str]) -> bool:
    """The write guard: the stored text is EXACTLY what the crawl wrote, by either arm."""
    if old is None:
        return False
    return (crawled_copy is not None and old == crawled_copy) or (legacy_text is not None and old == legacy_text)


class Decision:
    """What one row gets, and why."""

    __slots__ = ("row", "outcome", "new", "stage", "taxonomy", "would_demote", "safety_row", "lost_cues")

    def __init__(self, row: Dict[str, Any], outcome: str, *, new: Optional[str] = None,
                 stage: Optional[str] = None, taxonomy: Optional[Dict[str, Any]] = None,
                 would_demote: bool = False, safety_row: bool = False,
                 lost_cues: Optional[List[str]] = None) -> None:
        self.row = row
        self.outcome = outcome
        self.new = new
        self.stage = stage
        self.taxonomy = taxonomy or {}
        self.would_demote = would_demote
        self.safety_row = safety_row
        self.lost_cues = lost_cues or []

    def pk(self) -> str:
        return str(self.row["product_key"])

    def update_params(self) -> Dict[str, Any]:
        return {
            "pk": self.pk(), "new": self.new, "stage": self.stage,
            "use_case_tags": json.dumps(list(self.taxonomy.get("use_case_tags") or []), ensure_ascii=False),
            "lifestyle_tags": json.dumps(list(self.taxonomy.get("lifestyle_tags") or []), ensure_ascii=False),
            "demographic": self.taxonomy.get("demographic"),
            "old": self.row["description"], "old_stage": self.row.get("pdp_lifecycle_stage"),
        }


def classify_row(row: Dict[str, Any], planned: Optional[Dict[str, Any]], legacy_text: Optional[str], *,
                 allow_demote: bool) -> Decision:
    """Pure: the planned pdp row for this product_key (None: the plan does not produce it) and the
    old producer's text of the product's current body_html -> what this row gets."""
    if planned is None:
        return Decision(row, NOT_IN_FEED)
    old = row.get("description")
    new = planned.get("description") or None
    if new is not None and new == old:
        return Decision(row, UNCHANGED)
    if not crawl_owned(old, row.get("crawled_copy"), legacy_text):
        return Decision(row, NOT_CRAWL_OWNED)
    if new is None:
        return Decision(row, WOULD_EMPTY)
    taxonomy = taxonomy_for(row, new)
    stage = stage_for(row, new, taxonomy)
    demote = STAGE_RANK.get(stage, 0) < STAGE_RANK.get(row.get("pdp_lifecycle_stage"), 0)
    lost = lost_safety_cues(old, new)
    if lost:
        outcome = SKIP_SAFETY
    elif demote and not allow_demote:
        outcome = SKIP_DEMOTE
    else:
        outcome = WRITE
    return Decision(row, outcome, new=new, stage=stage, taxonomy=taxonomy, would_demote=demote,
                    safety_row=bool(safety_cue_keys(old)), lost_cues=lost)


def count_decision(counts: collections.Counter, d: Decision) -> None:
    counts["rows"] += 1
    if d.outcome in (NOT_IN_FEED, UNCHANGED, NOT_CRAWL_OWNED, WOULD_EMPTY):
        counts[d.outcome] += 1
        return
    counts["would_rewrite"] += 1
    counts["would_demote"] += int(d.would_demote)
    counts["safety_rows"] += int(d.safety_row)
    counts["safety_cue_lost"] += int(d.outcome == SKIP_SAFETY)
    counts["skipped_demote"] += int(d.outcome == SKIP_DEMOTE)


# --- the crawl and the plan --------------------------------------------------------------------------

class CrawlRefused(RuntimeError):
    """The crawl ran but its batch cannot be trusted as the job's batch."""


class _CaptureProducts:
    """Keeps the raw products records_for_brand fetched (the legacy arm of the guard reads their
    body_html), passing every result through unchanged."""

    def __init__(self, feed: Any) -> None:
        self.feed = feed
        self.products: List[Dict[str, Any]] = []

    def _wrap(self, fn: Callable) -> Callable:
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = await fn(*args, **kwargs)
            self.products.extend(p for p in (result or []) if isinstance(p, dict))
            return result
        return wrapped

    def __enter__(self) -> "_CaptureProducts":
        self._orig = (self.feed.fetch_shopify_products, self.feed.fetch_shopify_collections)
        self.feed.fetch_shopify_products = self._wrap(self._orig[0])
        self.feed.fetch_shopify_collections = self._wrap(self._orig[1])
        return self

    def __exit__(self, *exc: Any) -> None:
        self.feed.fetch_shopify_products, self.feed.fetch_shopify_collections = self._orig


async def crawl_job(job: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """records_for_brand as the drain's `_crawl` builds it, PDP INCI and GTIN fetches off ->
    (records, the raw products fetched). Refuses a batch the drain would refuse."""
    import services.curated_brand_feed as feed
    from services.retailer_ingest import pipeline

    options = job.get("options") or {}
    payload = pipeline.ingest_payload(job)
    if options.get("collections"):
        payload["collection_handles"] = list(options["collections"])
    if payload.get("base_listings_only"):
        payload["fold_extended"] = True
    if options.get("multi_brand"):
        payload["brand_by_vendor"] = {" ".join(k.split()).casefold(): " ".join(v.split())
                                      for k, v in options["brands"].items()}
    payload.update(enrich_missing_inci=False, max_pdp_inci_fetches=0, enrich_missing_gtin=False,
                   max_pdp_identity_fetches=0)
    evidence = contextlib.ExitStack()
    if options.get("lip_title_evidence"):
        evidence.enter_context(feed.lip_title_evidence())
    if options.get("lash_nail_title_evidence"):
        evidence.enter_context(feed.lash_nail_title_evidence())
    with evidence, _CaptureProducts(feed) as capture:
        records = await feed.records_for_brand(**{k: v for k, v in payload.items() if k != "market"})
    report = getattr(records, "crawl_report", None)
    if not isinstance(report, dict) or report.get("status") != "complete":
        raise CrawlRefused("crawl completeness was not proven")
    currency = payload["require_currency"]
    for record in records:
        seen = (record.get("pdp") or {}).get("currency")
        if seen != currency:
            raise CrawlRefused(f"record currency {seen!r} is not {currency}")
    return records, capture.products


def plan_rows(job: Dict[str, Any], records: List[Dict[str, Any]],
              host_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The pure plan of this batch -> product_key -> its planned pdp row. Each row's current listing
    is handed in, so the plan keeps a row on the listing it names (as the drain plans)."""
    from services.catalog_enrichment_agent.ingestion import content_listing, ingest_validated_jsonl
    from services.retailer_ingest.pipeline import job_market

    current = {}
    for row in host_rows:
        pk = str(row["product_key"])
        listing = content_listing(row.get("canonical_url"))
        if listing and not pk.startswith("ext:retailer:"):
            current[pk] = listing
    plan = ingest_validated_jsonl(list(records), market=job_market(job.get("options")),
                                  current_listings=current)
    return {str(p["product_key"]): p for p in plan.get("pdps") or [] if p.get("product_key")}


# --- DB plumbing -------------------------------------------------------------------------------------

def _should_heal(exc: BaseException) -> bool:
    return is_transport_error(exc) or "already acquired" in str(exc)


async def _reconnect(db: Any = None) -> None:
    db = db or database
    with contextlib.suppress(Exception):
        await db.disconnect()
    for attempt in range(1, 6):
        try:
            await db.connect()
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  (reconnect attempt {attempt} failed: {str(exc)[:100]})", flush=True)
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("could not re-establish DB connection")


async def _read_only(deps: Any, sql: str, values: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """One SELECT inside a READ ONLY transaction that is rolled back, healed and retried."""
    for attempt in range(1, 4):
        try:
            tx = deps.db.transaction()
            await tx.start()
            try:
                await deps.db.execute("SET TRANSACTION READ ONLY")
                return [dict(r) for r in await deps.db.fetch_all(sql, values or {})]
            finally:
                await tx.rollback()
        except Exception as exc:  # noqa: BLE001
            if attempt == 3 or not _should_heal(exc):
                raise
            print(f"  (read attempt {attempt} failed: {str(exc)[:100]} -- reconnecting)", flush=True)
            await deps.reconnect()
    return []


async def _write_batch(batch: List[Decision], deps: Any) -> Tuple[List[Decision], List[Decision], int]:
    """-> (written, raced, failed). One transaction per batch; a transport failure is healed and the
    batch retried once (a row whose first attempt did commit then reads back as raced)."""
    for attempt in (1, 2):
        written: List[Decision] = []
        raced: List[Decision] = []
        try:
            async with deps.db.transaction():
                for d in batch:
                    hit = await deps.db.fetch_one(_UPDATE_SQL, d.update_params())
                    (written if hit is not None else raced).append(d)
            return written, raced, 0
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and _should_heal(exc):
                await deps.reconnect()
                continue
            print(f"  WARN batch of {len(batch)} not written: {type(exc).__name__}: {str(exc)[:150]}",
                  flush=True)
            return [], [], len(batch)
    return [], [], len(batch)


async def _refresh_views(written: List[Decision], deps: Any, counts: collections.Counter) -> None:
    for ck in sorted({str(d.row.get("content_key")) for d in written if d.row.get("content_key")}):
        for attempt in (1, 2):
            try:
                await deps.refresh(ck, refresh_source=REFRESH_SOURCE, db=deps.db)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1 and _should_heal(exc):
                    await deps.reconnect()
                    continue
                counts["refresh_failed"] += 1
                print(f"  WARN view refresh failed for {ck}: {str(exc)[:120]}", flush=True)
                break


class StopRun(RuntimeError):
    """The write phase cannot go on (the catalog write lock was not obtained)."""


async def write_rows(writes: List[Decision], deps: Any, counts: collections.Counter, opts: Any) -> None:
    """Write in batches, each under the drain's catalog write lock, with a pause between batches."""
    for start in range(0, len(writes), opts.batch_size):
        batch = writes[start:start + opts.batch_size]
        try:
            async with deps.lock():
                written, raced, failed = await _write_batch(batch, deps)
                counts["written"] += len(written)
                counts["raced"] += len(raced)
                counts["update_failed"] += failed
                await _refresh_views(written, deps, counts)
        except Exception as exc:  # the lock was busy or unavailable: nothing of this batch ran
            if type(exc).__name__ not in ("CatalogWriteLockBusy", "CatalogWriteLockUnavailable"):
                raise
            counts["lock_unwritten"] += len(writes) - start
            raise StopRun(f"catalog write lock: {type(exc).__name__}") from exc
        if start + opts.batch_size < len(writes):
            await deps.sleep(opts.batch_sleep_s)


# --- the run -----------------------------------------------------------------------------------------

def _emit(tag: str, payload: Dict[str, Any]) -> None:
    print(f"{tag} {json.dumps(payload, default=str, ensure_ascii=False)}", flush=True)


async def process_host(host: str, host_rows: List[Dict[str, Any]], host_jobs: List[Dict[str, Any]],
                       deps: Any, opts: Any, totals: collections.Counter) -> None:
    from services.catalog_enrichment_agent.ingestion import content_listing
    from services.curated_brand_feed import _clean_domain

    counts: collections.Counter = collections.Counter()
    jobs, excluded = select_jobs(host_jobs, jobs_since=opts.jobs_since)
    pending = {str(r["product_key"]) for r in host_rows}
    matched: Dict[str, Dict[str, Any]] = {}
    legacy: Dict[str, str] = {}
    plan_listings: set = set()
    skip = None if jobs else "no completed storefront job"
    errors: List[str] = []
    rows_by_pk = {str(r["product_key"]): r for r in host_rows}
    for job in jobs:
        if len(matched) == len(pending):
            break
        if deps.clock() - deps.started > opts.budget_s:
            skip = skip or "time budget reached; rerun this host"
            break
        if opts.apply and in_quiet_window(deps.now()):
            skip = skip or "inside the sweep window; rerun this host"
            break
        try:
            records, products = await deps.crawl(job)
            planned = deps.plan(job, records, host_rows)
        except Exception as exc:  # noqa: BLE001 -- a refused or failed crawl is reported, never retried
            counts["jobs_failed"] += 1
            errors.append(f"{job.get('id')}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        counts["jobs_crawled"] += 1
        clean = _clean_domain(job["domain"])
        for product in products:
            handle = str(product.get("handle") or "").strip()
            if handle:
                legacy.setdefault(norm_url(f"https://{clean}/products/{handle}"),
                                  legacy_body_html_to_text(product.get("body_html")))
        for pk, pdp in planned.items():
            plan_listings.add(content_listing(pdp.get("canonical_url")))
            row = rows_by_pk.get(pk)
            if row is None or pk in matched:
                continue
            # The plan keeps each row on the listing it names; a key planned on another listing is
            # another page's copy, never this row's.
            if content_listing(pdp.get("canonical_url")) != content_listing(row.get("canonical_url")):
                continue
            matched[pk] = pdp
    if not matched and (skip or errors):
        counts["rows"] += len(host_rows)
        counts["host_skipped"] += len(host_rows)
        totals.update(counts)
        _emit("HOST", {"host": host, **counts, "skip": skip or "every crawl failed", "errors": errors,
                       "excluded_sources": excluded})
        return
    writes: List[Decision] = []
    for row in host_rows:
        pk = str(row["product_key"])
        d = classify_row(row, matched.get(pk), legacy.get(norm_url(row.get("canonical_url"))),
                         allow_demote=opts.allow_demote)
        count_decision(counts, d)
        if d.outcome == NOT_IN_FEED and content_listing(row.get("canonical_url")) in plan_listings:
            counts["not_in_feed_url_in_plan"] += 1
        if d.outcome == WRITE:
            writes.append(d)
        elif d.outcome in (SKIP_SAFETY, SKIP_DEMOTE):
            _emit("ROW_SKIPPED", {"host": host, "pk": pk, "why": d.outcome, "lost_cues": d.lost_cues,
                                  "stored_stage": row.get("pdp_lifecycle_stage"), "new_stage": d.stage,
                                  "len_old": len(row.get("description") or ""), "len_new": len(d.new or "")})
    try:
        if opts.apply and writes:
            await write_rows(writes, deps, counts, opts)
    finally:
        totals.update(counts)
        _emit("HOST", {"host": host, **counts, "to_write": len(writes), "skip": skip, "errors": errors,
                       "excluded_sources": excluded})


async def run(opts: Any, deps: Any) -> Tuple[int, Dict[str, Any]]:
    """-> (exit code, summary)."""
    if opts.apply and in_quiet_window(deps.now()):
        print("REFUSED: --apply does not start inside the 05:15-06:15Z sweep window", flush=True)
        return 2, {}
    deps.started = deps.clock()
    await deps.reconnect()
    sql, values = _ROWS_SQL, {}
    if opts.since is not None:
        sql, values = _ROWS_SQL + _ROWS_SINCE_SQL, {"since": opts.since}
    rows = await _read_only(deps, sql, values)
    if opts.hosts:
        rows = [r for r in rows if r.get("host") in opts.hosts]
    by_host: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        by_host[r["host"]].append(r)
    jobs = await _read_only(deps, _JOBS_SQL, {"hosts": sorted(by_host)}) if by_host else []
    jobs_by_host: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for j in jobs:
        jobs_by_host[j["host"]].append(j)
    _emit("START", {"rows": len(rows), "hosts": len(by_host), "apply": opts.apply,
                    "allow_demote": opts.allow_demote, "since": opts.since, "jobs_since": opts.jobs_since,
                    "budget_s": opts.budget_s})
    totals: collections.Counter = collections.Counter()
    stopped = None
    for host in sorted(by_host, key=lambda h: (-len(by_host[h]), h)):
        try:
            await process_host(host, by_host[host], jobs_by_host.get(host, []), deps, opts, totals)
        except StopRun as exc:
            stopped = str(exc)
            break
    summary = {k: totals.get(k, 0) for k in SUMMARY_KEYS + EXTRA_KEYS}
    summary.update(apply=opts.apply, allow_demote=opts.allow_demote, stopped=stopped,
                   runtime_s=round(deps.clock() - deps.started, 1))
    _emit("SUMMARY", summary)
    failed = stopped or summary["update_failed"] or summary["refresh_failed"]
    return (3 if failed else 0), summary


def production_deps(opts: Any) -> Any:
    from db.retailer_ingest import catalog_write_lock
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key

    return types.SimpleNamespace(
        db=database, crawl=crawl_job, plan=plan_rows,
        lock=lambda: catalog_write_lock(wait_s=opts.lock_wait_s, poll_s=LOCK_POLL_S),
        refresh=refresh_agent_pdp_view_for_content_key,
        reconnect=lambda: _reconnect(database), sleep=asyncio.sleep,
        now=lambda: datetime.now(timezone.utc), clock=time.monotonic, started=0.0,
    )


def _date_arg(value: str) -> Optional[datetime]:
    """'YYYY-MM-DD' -> naive UTC midnight; 'all' -> None (no bound)."""
    if value.strip().lower() == "all":
        return None
    return datetime.fromisoformat(value.strip()).replace(tzinfo=None)


def parse_args(argv: Optional[List[str]] = None) -> Any:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write (default: dry run, writes nothing)")
    p.add_argument("--allow-demote", action="store_true",
                   help="with --apply, also write rows whose new text lowers their lifecycle stage")
    p.add_argument("--since", default=PHASE1_SINCE,
                   help=f"rows created on/after this UTC date (default {PHASE1_SINCE}, phase 1); 'all' = phase 2")
    p.add_argument("--jobs-since", default=None,
                   help="jobs updated on/after this UTC date (default: one day before --since; 'all' = no bound)")
    p.add_argument("--hosts", default="", help="comma-separated hosts (no www.) to limit the run to")
    p.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S,
                   help="no new crawl starts after this many seconds")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--batch-sleep-s", type=float, default=BATCH_SLEEP_S)
    p.add_argument("--lock-wait-s", type=float, default=LOCK_WAIT_S)
    args = p.parse_args(argv)
    if args.allow_demote and not args.apply:
        p.error("--allow-demote only means something with --apply")
    if args.batch_size < 1:
        p.error("--batch-size must be at least 1")
    args.since = _date_arg(args.since)
    if args.jobs_since is None:
        args.jobs_since = None if args.since is None else args.since - timedelta(days=1)
    else:
        args.jobs_since = _date_arg(args.jobs_since)
    if args.jobs_since is not None:
        args.jobs_since = args.jobs_since.replace(tzinfo=timezone.utc)
    args.hosts = {h.strip().lower().removeprefix("www.") for h in args.hosts.split(",") if h.strip()}
    return args


def main(argv: Optional[List[str]] = None) -> int:
    opts = parse_args(argv)
    deps = production_deps(opts)

    async def go() -> int:
        try:
            code, _ = await run(opts, deps)
            return code
        finally:
            with contextlib.suppress(Exception):
                await database.disconnect()

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
