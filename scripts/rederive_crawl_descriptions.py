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
     `--since` (phase 1 by default; `--since all` drops the bound). A host's rows are read AFTER its
     crawl, so the stage, taxonomy and guards judge what the row holds at write time.
  3. REBUILD. `records_for_brand` exactly as the drain's `_crawl` builds it (`ingest_payload` =
     the normalized `_feed_payload`), with PDP INCI and GTIN fetches off, then the pure
     `ingest_validated_jsonl` with the job's market and each row's current listing. A row the plan
     does not produce is `not_in_feed` and untouched. The batch is TODAY's feed.
  4. GUARD. A row is written only when its stored description EQUALS (`=`) the crawl's own copy
     (`enrichment_meta.candidate_attribute_summary`), `legacy_body_html_to_text` of the
     product's current body_html (a frozen copy of the old producer, below), or the new text with
     its sub/superscript numerals back in ASCII (what the producer wrote before the numeric sup/sub
     fix; meaningful only on an image that has that fix -- without it the arm equals the new text).
     Anything else was written by another lane or by hand: `not_crawl_owned`, never written.
  5. WHAT IS WRITTEN is what the drain's next re-crawl of the row would leave on it once its copy is
     the new text: the stage from the drain's own rule (`apply._stage_from_kept_copy`, which keeps a
     row whose category does not resolve at the plan's stage), and the plan's four taxonomy columns
     (derived from the new text at ingest). So nothing flips back on the next re-crawl.
  6. GATES. A row whose new text drops its stage is skipped unless `--allow-demote`, one whose stage
     rises unless `--allow-promote`. A row whose new text loses a safety SENTENCE the old text had
     (every old sentence carrying a safety cue must survive, counted) is never written. A sentence
     survives when it is a new sentence, or is re-split into consecutive new ones, or is merged
     with its neighbour into one, compared as letters only (`sentence_survival`).
  7. WRITE. One guarded UPDATE per row (`_UPDATE_SQL`), only while the row's description, stage and
     content_changed_at are still the ones read; 0 rows -> `raced`. Each batch's written rows are
     STORED as a manifest event in identity_resolution_events INSIDE the batch's transaction, so a
     manifest exists exactly for what committed. After the commit, outside the lock: the
     agent_pdp_view refresh, and serving eligibility for rows whose stage changed.

THE MANIFEST. Every would-write and every written row is one JSON entry (pk, content_key, host,
old/new text, old/new stage, the drain's stage with the old copy kept, removed sentences, old/new
taxonomy, decision), printed as a `ROW {json}` line and appended to `--manifest-out` (JSONL). A
Cloud Run job keeps no disk: the log lines (and, for written rows, the stored events) are the copy.
An entry is the two texts plus what was removed, about 2-3 KB on average and under 10 KB at the
2000-character cap: far below a log entry's limit.

BINDING. `--apply` writes only what a reviewed dry run decided: the dry run prints per-host plan
digests (`expect_plan` in its SUMMARY); `--apply --expect-plan host=digest,...` re-derives, and a
host whose decisions are not byte-identical to the reviewed ones (old text, new text, stages,
taxonomy) writes nothing and is reported `plan_changed`. Hosts not named are not touched.
A digest binds ONE apply. Any partial apply -- a `--max-writes` canary, a failed batch, a lock or
window stop -- leaves the host's plan different from the reviewed one (its written rows now read
back `unchanged`), so the remainder needs a FRESH dry run and its fresh digest, never a re-use.

REVERT. `--revert <run_id | manifest.jsonl>` restores written rows (text, stage, taxonomy), each
guarded on still holding the text and stage this run wrote; a taxonomy column a later writer has
moved since is left as it is. Dry run unless `--apply`.

OPERATION. Dry run by default: reads only, inside READ ONLY transactions, and writes nothing.
`--apply` refuses to start, and stops writing, inside the 05:15-06:15Z sweep window; it commits in
batches of `--batch-size` with a pause between them, holds the drain's catalog write lock
(`db.retailer_ingest.catalog_write_lock`) around each batch's transaction only, sets its own
statement and lock timeouts, and stops after `--max-writes` rows (a one-host canary).

    python scripts/rederive_crawl_descriptions.py --hosts a.example.com            # dry run
    python scripts/rederive_crawl_descriptions.py --apply --expect-plan a.example.com=<digest> --max-writes 20
    python scripts/rederive_crawl_descriptions.py --revert rederive_<hex>            # revert dry run

It fetches from merchants: run it on the crawl egress, never the default one. The final line is
`SUMMARY {json}`; one `HOST {json}` line is printed per host. The file carries no at-sign so it
can also be handed to scripts/ops/run_oneoff_job.sh as an inline program.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import sys
import tempfile
import time
import types
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "__file__" in globals() else "/app"
sys.path.insert(0, _ROOT)

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.bulk_writer import is_transport_error  # noqa: E402

REFRESH_SOURCE = "crawl_description_rederive_v1"
APPLIED_ACTION = "crawl_description_rederive_applied"
REVERTED_ACTION = "crawl_description_rederive_reverted"
RUN_PREFIX = "rederive_"
PHASE1_SINCE = "2026-10-10"
BATCH_SIZE = 200
# Between batches. The drain polls the lock every 5 s, so a pause longer than that lets a waiting
# apply in between two batches instead of starving it.
BATCH_SLEEP_S = 8.0
DEFAULT_BUDGET_S = 3000.0
LOCK_WAIT_S = 900.0
LOCK_POLL_S = 5.0
# SET LOCAL in every transaction: a pooled connection's session settings are not this script's.
READ_STATEMENT_TIMEOUT = "120s"
WRITE_STATEMENT_TIMEOUT = "30s"
WRITE_LOCK_TIMEOUT = "5s"
# The Shopify pacer's sweep window, UTC minutes [start, end): no --apply starts, no crawl of an
# --apply run starts, and no write batch starts inside it.
QUIET_WINDOW_UTC_MIN = (5 * 60 + 15, 6 * 60 + 15)
STAGE_RANK = {"draft": 0, "candidate": 1, "validated": 2, "published": 3}
TAXONOMY_COLUMNS = ("price_tier", "use_case_tags", "lifestyle_tags", "demographic")

SUMMARY_KEYS = ("rows", "would_rewrite", "written", "raced", "unchanged", "not_crawl_owned",
                "not_in_feed", "would_demote", "skipped_demote", "would_promote", "skipped_promote",
                "safety_rows", "safety_cue_lost")
EXTRA_KEYS = ("host_skipped", "would_empty", "not_in_feed_url_in_plan", "plan_changed", "unwritten",
              "update_failed", "refresh_failed", "eligibility_failed", "eligibility_recomputed",
              "jobs_crawled", "jobs_failed", "transition_due_to_description")

# Outcomes.
NOT_IN_FEED = "not_in_feed"
UNCHANGED = "unchanged"
NOT_CRAWL_OWNED = "not_crawl_owned"
WOULD_EMPTY = "would_empty"
WRITE = "write"
SKIP_SAFETY = "skip_safety"
SKIP_DEMOTE = "skip_demote"
SKIP_PROMOTE = "skip_promote"
REWRITE_OUTCOMES = (WRITE, SKIP_SAFETY, SKIP_DEMOTE, SKIP_PROMOTE)
TOP_REMOVED = 10

HOST_SQL = "lower(regexp_replace(split_part(split_part(canonical_url, '://', 2), '/', 1), '^www[.]', ''))"

_POPULATION_WHERE = """
 WHERE source_system = 'catalog_enrichment_agent_v1'
   AND sync_status = 'live' AND suppressed_at IS NULL
   AND description IS NOT NULL
   AND COALESCE(pdp_lifecycle_stage, '') NOT IN ('hold', 'archived')
   AND canonical_url LIKE 'https://%/products/%'
"""
# created_at is a timestamp WITHOUT time zone: bound as a naive UTC datetime (asyncpg refuses a str).
_SINCE_SQL = "   AND created_at >= :since\n"
_HOSTS_SQL = f"   AND {HOST_SQL} = ANY(:hosts)\n"
_KEYS_SQL = "   AND product_key = ANY(:pks)\n"
_COLUMNS_SQL = f"""
SELECT product_key, content_key, canonical_url, source_domain, {HOST_SQL} AS host,
       title, description, image_url, category_path, tags, price_tier, demographic, use_case_tags,
       lifestyle_tags, pdp_scope, source_system, pdp_lifecycle_stage, content_changed_at,
       product_payload->'enrichment_meta'->>'candidate_attribute_summary' AS crawled_copy
  FROM catalog_products"""


def _population_filters(opts: Any) -> Tuple[str, Dict[str, Any]]:
    sql, values = _POPULATION_WHERE, {}
    if opts.since is not None:
        sql, values["since"] = sql + _SINCE_SQL, opts.since
    if opts.hosts:
        sql, values["hosts"] = sql + _HOSTS_SQL, sorted(opts.hosts)
    return sql, values


def population_keys_sql(opts: Any) -> Tuple[str, Dict[str, Any]]:
    """Every host's keys and listings in ONE scan (the host expression has no index; per-host scans
    would cost the 2-vCPU primary two sequential scans a host). No product_payload is read."""
    where, values = _population_filters(opts)
    return f"SELECT product_key, canonical_url, {HOST_SQL} AS host FROM catalog_products{where}", values


def host_rows_sql(opts: Any, keys: Iterable[str]) -> Tuple[str, Dict[str, Any]]:
    """A host's rows, read fresh after its crawl, by primary key, still inside the population."""
    where, values = _population_filters(opts)
    return _COLUMNS_SQL + where + _KEYS_SQL, {**values, "pks": sorted(keys)}


_JOBS_SQL = """
SELECT id, domain, brand, options, scope_key, status, updated_at,
       lower(regexp_replace(domain, '^www[.]', '')) AS host
  FROM retailer_ingest_jobs
 WHERE status = 'done' AND lower(regexp_replace(domain, '^www[.]', '')) = ANY(:hosts)
 ORDER BY updated_at DESC, id
"""

# THE WRITE. Every predicate is part of the guard: the row is still unsuppressed, its description,
# stage and content_changed_at are still the ones this run read and judged (content_changed_at moves
# with title, image, category, tags and payload -- the inputs of the stage and taxonomy), and it has
# not been held or archived since. A row that moved updates nothing, and RETURNING says so --
# `databases` on asyncpg runs `execute` as fetchval, which reports no rowcount. The trigger of
# migration 138 bumps content_changed_at; RETURNING hands the new value to the manifest.
_UPDATE_SQL = """
UPDATE catalog_products
   SET description = :new, pdp_lifecycle_stage = :stage, price_tier = :price_tier,
       use_case_tags = CAST(:use_case_tags AS jsonb),
       lifestyle_tags = CAST(:lifestyle_tags AS jsonb), demographic = :demographic,
       updated_at = NOW()
 WHERE product_key = :pk AND suppressed_at IS NULL
   AND description = :old
   AND pdp_lifecycle_stage IS NOT DISTINCT FROM :old_stage
   AND content_changed_at = :old_content_changed_at
   AND COALESCE(pdp_lifecycle_stage, '') NOT IN ('hold', 'archived')
RETURNING product_key, content_changed_at
"""

# THE REVERT: back to the manifest's old values, only while the row still holds what this run wrote.
# Each taxonomy column goes back only while it still holds what the run wrote (a re-crawl since may
# have moved price_tier or the tags for its own reasons): NULL-safe, jsonb compared as jsonb.
_REVERT_SQL = """
UPDATE catalog_products
   SET description = :old, pdp_lifecycle_stage = :old_stage,
       price_tier = CASE WHEN price_tier IS NOT DISTINCT FROM CAST(:new_price_tier AS TEXT)
                         THEN CAST(:price_tier AS TEXT) ELSE price_tier END,
       use_case_tags = CASE WHEN use_case_tags IS NOT DISTINCT FROM CAST(:new_use_case_tags AS jsonb)
                            THEN CAST(:use_case_tags AS jsonb) ELSE use_case_tags END,
       lifestyle_tags = CASE WHEN lifestyle_tags IS NOT DISTINCT FROM CAST(:new_lifestyle_tags AS jsonb)
                             THEN CAST(:lifestyle_tags AS jsonb) ELSE lifestyle_tags END,
       demographic = CASE WHEN demographic IS NOT DISTINCT FROM CAST(:new_demographic AS TEXT)
                          THEN CAST(:demographic AS TEXT) ELSE demographic END,
       updated_at = NOW()
 WHERE product_key = :pk AND suppressed_at IS NULL
   AND description = :new
   AND pdp_lifecycle_stage IS NOT DISTINCT FROM :new_stage
RETURNING product_key
"""

# The repo's manifest store for one-off catalog writes (action is free text; see
# services/brand_relabel.py, scripts/repair_same_title_listings.py). Inserted INSIDE the batch's
# transaction: the event exists exactly when the batch's writes committed.
_EVENT_SQL = """
INSERT INTO identity_resolution_events (proposal_id, action, run_id, detail)
VALUES (NULL, :action, :run_id, CAST(:detail AS jsonb))
RETURNING id
"""
_LOAD_EVENTS_SQL = """
SELECT action, detail FROM identity_resolution_events
 WHERE run_id = :run_id AND action = ANY(:actions)
 ORDER BY id
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


def _json_or_none(value: Any) -> Any:
    """A jsonb value as read (asyncpg: text) -> Python; None stays None (SQL NULL, not JSON null)."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def taxonomy_of(source: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _json_or_none(source.get(k)) for k in TAXONOMY_COLUMNS}


def _jsonb_param(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value:
        return _as_utc(datetime.fromisoformat(str(value)))
    return datetime.min.replace(tzinfo=timezone.utc)


def lock_wait_s(now: datetime, cap: float) -> float:
    """How long a write may wait for the lock: `cap`, but never past the start of the sweep window."""
    now = _as_utc(now).astimezone(timezone.utc)
    start = now.replace(hour=QUIET_WINDOW_UTC_MIN[0] // 60, minute=QUIET_WINDOW_UTC_MIN[0] % 60,
                        second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return max(0.0, min(float(cap), (start - now).total_seconds()))


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


# --- the safety rule ------------------------------------------------------------------------------
# Footnote markers glued onto a word ("irritation2", "eyes¹") are not part of the word.
_GLUED_FOOTNOTE_RE = re.compile(r"(?<=[^\W\d_])[\d²³¹⁰-⁹]+")
# Script-local cues ON TOP of the producer's own (`curated_brand_feed._SAFETY_CUE_RE`): the producer
# decides what its repetition pass may drop with that list, so a gate that used only it would share
# its blind spots (a store-wide skin-test line is dropped as boilerplate and the gate saw no cue).
_LOCAL_SAFETY_CUE_RE = re.compile(
    r"(?i)\b(?:stop\s+(?:using|use)|cease\s+(?:using|use)|consult\w*|health\s*care|sunburn\w*"
    r"|sun\s+exposure|sunscreen\w*|sensitiv\w*|skin\s+test\w*|patch\s+test\w*|small\s+area"
    r"|before\s+(?:first\s+)?use|irritat\w*|allerg\w*|warning\w*|caution\w*|avoid\w*|discontinu\w*"
    r"|eyes?|pregnan\w*|breast[- ]?feed\w*|nursing|doctor\w*|physician\w*|dermatolog\w*|medical\w*"
    r"|flammab\w*|swallow\w*|ingest\w*|poison\w*|toxic\w*|harmful|rash\w*|redness|swelling|burns?"
    r"|broken\s+skin|external\s+use|rinse|first\s+aid|children|reach\s+of|do\s+not"
    r"|spf|sun\s+protection|spot\s+test\w*|patch|tingl\w*|adults?\s+only)\b")


def _defootnote(text: str) -> str:
    return _GLUED_FOOTNOTE_RE.sub(" ", str(text or ""))


def is_safety_sentence(sentence: str) -> bool:
    from services.curated_brand_feed import _SAFETY_CUE_RE

    text = _defootnote(sentence)
    return bool(_SAFETY_CUE_RE.search(text) or _LOCAL_SAFETY_CUE_RE.search(text))


def split_sentences(text: Optional[str]) -> List[str]:
    """The producer's own sentence splitter over one plain-text description."""
    from services.curated_brand_feed import _split_sentences

    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return _split_sentences(flat) if flat else []


def _compact(text: Optional[str]) -> str:
    """A sentence's comparison form: NFKC, casefolded, LETTERS ONLY. The current producer reformats
    without removing -- "Moisturizer : A v egan" -> "Moisturizer: A vegan", "1 week 3 ." -> "1 week3.",
    "potent 1 :" -> "potent1:" -- so spacing, punctuation and symbols must not count. Digits do not
    count either: the producer glues and unglues footnote markers ("week 3" / "week3", "irritation2"),
    and no comparison that keeps them can see both spellings as one sentence."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(ch for ch in folded if ch.isalpha())


def sentence_survival(old: Optional[str], new: Optional[str]) -> List[Tuple[str, bool]]:
    """Each old sentence, in order, and whether it survives in the new text. Fail closed: an old
    sentence survives only when its comparison form (`_compact`)
      1. EQUALS one new sentence's (each new sentence is used once: a repeated old sentence needs
         as many copies), or
      2. equals two or more CONSECUTIVE unused new sentences joined (the producer re-split it), or
      3. is one half of a new sentence that is EXACTLY it joined with its old neighbour (the
         producer merged the two).
    Anything else -- found only in the middle of a longer new sentence ("Avoid eyes." inside "do
    not avoid eyes area"), or with words changed ("Do not use" -> "Use") -- did not survive."""
    olds = [(sentence, _compact(sentence)) for sentence in split_sentences(old)]
    news = [_compact(sentence) for sentence in split_sentences(new)]
    used = [False] * len(news)
    kept = [not form for _, form in olds]  # a sentence with no letters is nothing to lose
    for i, (_, form) in enumerate(olds):  # 1. one new sentence each
        if kept[i]:
            continue
        for j, candidate in enumerate(news):
            if not used[j] and candidate == form:
                used[j] = kept[i] = True
                break
    for i in range(len(olds) - 1):  # 3. two adjacent old sentences merged into one new sentence
        if kept[i] or kept[i + 1]:
            continue
        joined = olds[i][1] + olds[i + 1][1]
        for j, candidate in enumerate(news):
            if not used[j] and candidate == joined:
                used[j] = kept[i] = kept[i + 1] = True
                break
    for i, (_, form) in enumerate(olds):  # 2. one old sentence re-split across consecutive new ones
        if kept[i]:
            continue
        for j in range(len(news)):
            run, end = "", j
            while end < len(news) and not used[end] and len(run) < len(form):
                run += news[end]
                end += 1
            if run == form and end - j >= 2:
                for k in range(j, end):
                    used[k] = True
                kept[i] = True
                break
    return [(sentence, ok) for (sentence, _), ok in zip(olds, kept)]


def lost_safety_sentences(old: Optional[str], new: Optional[str]) -> List[str]:
    """Every old sentence carrying a safety cue must survive in the new text."""
    return [s for s, ok in sentence_survival(old, new) if not ok and is_safety_sentence(s)]


def removed_sentences(old: Optional[str], new: Optional[str]) -> List[str]:
    return [s for s, ok in sentence_survival(old, new) if not ok]


# --- the stage: the drain's own rule --------------------------------------------------------------

class _KeptCopy:
    """What `_stage_from_kept_copy` reads from the catalog: the row's title and the copy it keeps."""

    def __init__(self, product_key: str, title: Any, description: Any) -> None:
        self.row = {"product_key": product_key, "title": title, "description": description}

    async def fetch_all(self, sql: str, values: Any = None) -> List[Dict[str, Any]]:
        return [dict(self.row)]


async def drain_stage(planned: Dict[str, Any], title: Any, description: Any) -> Optional[str]:
    """The stage the drain's next re-crawl writes on this row when it keeps `title` and
    `description`: `apply._stage_from_kept_copy` itself, over the planned row. A planned row whose
    category does not resolve keeps the plan's stage (ingestion's category rule: draft)."""
    from services.catalog_enrichment_agent.apply import _stage_from_kept_copy

    plan, _ = await _stage_from_kept_copy(
        {"pdps": [planned]}, _KeptCopy(str(planned.get("product_key")), title, description))
    return plan["pdps"][0].get("pdp_lifecycle_stage")


# The producer before the numeric sup/sub fix flattened <sup>1</sup> to a plain "1" glued to its word;
# the fixed one writes "\u00b9". Mapping the fixed producer's sub/superscript numerals back to ASCII
# rebuilds, byte for byte, what the glued-era producer wrote from the same body.
_SCRIPT_TO_ASCII = str.maketrans(
    "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207c\u207d\u207e"
    "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a\u208b\u208c\u208d\u208e",
    "0123456789+-=()0123456789+-=()")
# The fixed producer writes a superscript minus for both "-" and the minus sign; the glued era kept
# whichever the source had. Both readings are tried (all hyphens, or all minus signs).
_MINUS_SIGN = str.maketrans("-", "\u2212")


def glued_era_texts(new: Optional[str]) -> Tuple[str, ...]:
    """What the producer may have written for the same body before the numeric sup/sub fix.
    A literal superscript in the source ("m\u00b2" typed as such) also maps back to ASCII, so
    such a row is not matched: fail closed."""
    if new is None:
        return ()
    hyphens = new.translate(_SCRIPT_TO_ASCII)
    if hyphens == new:
        return (hyphens,)
    script_minus = {chr(0x207B), chr(0x208B)}
    minus_signs = "".join(c.translate(_SCRIPT_TO_ASCII).translate(_MINUS_SIGN) if c in script_minus
                          else c.translate(_SCRIPT_TO_ASCII) for c in new)
    return (hyphens,) if minus_signs == hyphens else (hyphens, minus_signs)


def crawl_owned(old: Optional[str], crawled_copy: Optional[str], legacy_text: Optional[str],
                glued_texts: Iterable[str] = ()) -> bool:
    """The write guard: the stored text is EXACTLY what a crawl wrote -- its recorded copy, the old
    producer's text of the current body, or (a row minted between the cleanup and the sup/sub fix,
    whose recorded copy a re-crawl has since rewritten) the glued-era text of the new one."""
    if old is None:
        return False
    return ((crawled_copy is not None and old == crawled_copy) or (legacy_text is not None and old == legacy_text)
            or old in glued_texts)


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


class Decision:
    """What one row gets, and why."""

    __slots__ = ("row", "outcome", "planned", "new", "stage", "stage_with_old", "lost_safety",
                 "removed", "decision", "written_content_changed_at")

    def __init__(self, row: Dict[str, Any], outcome: str, *, planned: Optional[Dict[str, Any]] = None,
                 new: Optional[str] = None, stage: Optional[str] = None,
                 stage_with_old: Optional[str] = None, lost_safety: Optional[List[str]] = None) -> None:
        self.row = row
        self.outcome = outcome
        self.planned = planned or {}
        self.new = new
        self.stage = stage
        self.stage_with_old = stage_with_old
        self.lost_safety = lost_safety or []
        self.removed: List[str] = []
        self.decision = outcome
        self.written_content_changed_at: Any = None

    def pk(self) -> str:
        return str(self.row["product_key"])

    def old_stage(self) -> Optional[str]:
        return self.row.get("pdp_lifecycle_stage")

    def rank_move(self) -> int:
        return STAGE_RANK.get(self.stage, 0) - STAGE_RANK.get(self.old_stage(), 0)

    def taxonomy_new(self) -> Dict[str, Any]:
        return taxonomy_of(self.planned)

    def update_params(self) -> Dict[str, Any]:
        tax = self.taxonomy_new()
        return {
            "pk": self.pk(), "new": self.new, "stage": self.stage, "price_tier": tax["price_tier"],
            "use_case_tags": _jsonb_param(tax["use_case_tags"]),
            "lifestyle_tags": _jsonb_param(tax["lifestyle_tags"]), "demographic": tax["demographic"],
            "old": self.row["description"], "old_stage": self.old_stage(),
            "old_content_changed_at": self.row.get("content_changed_at"),
        }

    def digest_line(self) -> str:
        return "|".join((self.pk(), _sha(self.row.get("description")), _sha(self.new), str(self.old_stage()),
                         str(self.stage), _sha(self.taxonomy_new())))

    def manifest_entry(self, run_id: str) -> Dict[str, Any]:
        return {
            "run_id": run_id, "pk": self.pk(), "content_key": self.row.get("content_key"),
            "host": self.row.get("host"), "decision": self.decision,
            "old": self.row.get("description"), "new": self.new,
            "old_stage": self.old_stage(), "new_stage": self.stage, "stage_with_old_copy": self.stage_with_old,
            "removed_sentences": self.removed, "lost_safety_sentences": self.lost_safety,
            "taxonomy_old": taxonomy_of(self.row), "taxonomy_new": self.taxonomy_new(),
            "content_changed_at": self.row.get("content_changed_at"),
            "written_content_changed_at": self.written_content_changed_at,
        }


def guard_outcome(row: Dict[str, Any], planned: Optional[Dict[str, Any]], legacy_text: Optional[str]) -> Optional[str]:
    """Pure: the outcome of a row that will NOT be rewritten, or None when it would be."""
    if planned is None:
        return NOT_IN_FEED
    old = row.get("description")
    new = planned.get("description") or None
    if new is not None and new == old:
        return UNCHANGED
    if not crawl_owned(old, row.get("crawled_copy"), legacy_text, glued_era_texts(new)):
        return NOT_CRAWL_OWNED
    if new is None:
        return WOULD_EMPTY
    return None


def decide(row: Dict[str, Any], planned: Dict[str, Any], *, stage: Optional[str], stage_with_old: Optional[str],
           allow_demote: bool, allow_promote: bool) -> Decision:
    """Pure: the gates over a row the guard lets through."""
    old, new = row.get("description"), planned.get("description")
    lost = lost_safety_sentences(old, new)
    d = Decision(row, WRITE, planned=planned, new=new, stage=stage, stage_with_old=stage_with_old, lost_safety=lost)
    d.removed = removed_sentences(old, new)
    if lost:
        d.outcome = SKIP_SAFETY
    elif d.rank_move() < 0 and not allow_demote:
        d.outcome = SKIP_DEMOTE
    elif d.rank_move() > 0 and not allow_promote:
        d.outcome = SKIP_PROMOTE
    d.decision = d.outcome
    return d


async def classify(row: Dict[str, Any], planned: Optional[Dict[str, Any]], legacy_text: Optional[str], *,
                   allow_demote: bool, allow_promote: bool) -> Decision:
    outcome = guard_outcome(row, planned, legacy_text)
    if outcome is not None:
        return Decision(row, outcome)
    stage = await drain_stage(planned, row.get("title"), planned.get("description"))
    stage_with_old = await drain_stage(planned, row.get("title"), row.get("description"))
    return decide(row, planned, stage=stage, stage_with_old=stage_with_old,
                  allow_demote=allow_demote, allow_promote=allow_promote)


def count_decision(counts: collections.Counter, transitions: collections.Counter, d: Decision) -> None:
    counts["rows"] += 1
    if d.outcome not in REWRITE_OUTCOMES:
        counts[d.outcome] += 1
        return
    counts["would_rewrite"] += 1
    if d.stage != d.old_stage():
        transitions[f"{d.old_stage()}->{d.stage}"] += 1
        counts["transition_due_to_description"] += int(d.stage != d.stage_with_old)
    counts["would_demote"] += int(d.rank_move() < 0)
    counts["would_promote"] += int(d.rank_move() > 0)
    counts["safety_rows"] += int(any(is_safety_sentence(s) for s in split_sentences(d.row.get("description"))))
    counts["safety_cue_lost"] += int(d.outcome == SKIP_SAFETY)
    counts["skipped_demote"] += int(d.outcome == SKIP_DEMOTE)
    counts["skipped_promote"] += int(d.outcome == SKIP_PROMOTE)


def plan_digest(decisions: Iterable[Decision]) -> str:
    """One host's reviewed decisions: every row to write, its old and new text, stages and taxonomy."""
    lines = sorted(d.digest_line() for d in decisions if d.outcome == WRITE)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


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
                await deps.db.execute(f"SET LOCAL statement_timeout = '{READ_STATEMENT_TIMEOUT}'")
                return [dict(r) for r in await deps.db.fetch_all(sql, values or {})]
            finally:
                await tx.rollback()
        except Exception as exc:  # noqa: BLE001
            if attempt == 3 or not _should_heal(exc):
                raise
            print(f"  (read attempt {attempt} failed: {str(exc)[:100]} -- reconnecting)", flush=True)
            await deps.reconnect()
    return []


async def _write_tx(deps: Any, statements: Callable[[], Any]) -> Tuple[Any, int]:
    """Run `statements` in one transaction with this script's own timeouts; a transport failure is
    healed and retried once. -> (its result, failed: 0/1)."""
    for attempt in (1, 2):
        try:
            async with deps.db.transaction():
                await deps.db.execute(f"SET LOCAL statement_timeout = '{WRITE_STATEMENT_TIMEOUT}'")
                await deps.db.execute(f"SET LOCAL lock_timeout = '{WRITE_LOCK_TIMEOUT}'")
                result = await statements()
            return result, 0
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and _should_heal(exc):
                await deps.reconnect()
                continue
            print(f"  WARN transaction not committed: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
            return None, 1
    return None, 1


async def _write_batch(batch: List[Decision], deps: Any, run_id: str) -> Tuple[List[Decision], List[Decision], int]:
    """-> (written, raced, failed). The batch's UPDATEs and its manifest event commit together or not
    at all (a row whose first attempt did commit then reads back as raced on the retry)."""
    async def statements() -> Tuple[List[Decision], List[Decision]]:
        written: List[Decision] = []
        raced: List[Decision] = []
        for d in batch:
            hit = await deps.db.fetch_one(_UPDATE_SQL, d.update_params())
            if hit is not None:
                d.written_content_changed_at = dict(hit).get("content_changed_at")
                d.decision = "written"
                written.append(d)
            else:
                d.decision = "raced"
                raced.append(d)
        if written:
            detail = {"run_id": run_id, "rows": [d.manifest_entry(run_id) for d in written]}
            stored = await deps.db.fetch_one(_EVENT_SQL, {"action": APPLIED_ACTION, "run_id": run_id,
                                                          "detail": json.dumps(detail, default=str)})
            if stored is None:
                raise RuntimeError("the manifest event was not stored")
        return written, raced

    result, failed = await _write_tx(deps, statements)
    if failed:
        for d in batch:
            d.decision, d.written_content_changed_at = "failed", None
        return [], [], len(batch)
    return result[0], result[1], 0


async def _after_commit(content_keys: Iterable[str], stage_changed: Iterable[str], deps: Any,
                        counts: collections.Counter) -> None:
    """Outside the lock, after the commit: the served view of every touched key, and serving
    eligibility of every key whose stage moved. Never undoes the write; failures are counted."""
    for ck in sorted(set(content_keys)):
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
    for ck in sorted(set(stage_changed)):
        try:
            # strict: a failure raises (the default swallows it and returns False, uncounted)
            await deps.recompute(ck, reason=REFRESH_SOURCE, db=deps.db, strict=True)
            counts["eligibility_recomputed"] += 1
        except Exception as exc:  # noqa: BLE001
            counts["eligibility_failed"] += 1
            print(f"  WARN eligibility recompute failed for {ck}: {str(exc)[:120]}", flush=True)


class StopRun(RuntimeError):
    """The write phase cannot go on: nothing more is written this run."""


async def _batches(items: List[Any], deps: Any, opts: Any, counts: collections.Counter,
                   write: Callable[[List[Any]], Any]) -> None:
    """Shared batch loop: the sweep window and --max-writes before each batch, the lock around
    each batch's transaction only, the pause between batches."""
    start = 0
    while start < len(items):
        size = opts.batch_size
        if deps.writes_left is not None:
            size = min(size, deps.writes_left)
        if size <= 0:
            counts["unwritten"] += len(items) - start
            deps.stopped = deps.stopped or "max writes reached"
            return
        if in_quiet_window(deps.now()):
            counts["unwritten"] += len(items) - start
            raise StopRun("inside the 05:15-06:15Z sweep window")
        batch = items[start:start + size]
        in_window = False
        try:
            async with deps.lock():
                # The lock wait can be long: judge the window again now that the lock is held.
                in_window = in_quiet_window(deps.now())
                if not in_window:
                    after = await write(batch)
        except Exception as exc:  # the lock was busy or unavailable: nothing of this batch ran
            if type(exc).__name__ not in ("CatalogWriteLockBusy", "CatalogWriteLockUnavailable"):
                raise
            counts["unwritten"] += len(items) - start
            raise StopRun(f"catalog write lock: {type(exc).__name__}") from exc
        if in_window:
            counts["unwritten"] += len(items) - start
            raise StopRun("inside the 05:15-06:15Z sweep window (reached while waiting for the lock)")
        if deps.writes_left is not None:
            deps.writes_left -= len(batch)
        await after()
        start += len(batch)
        if start < len(items):
            await deps.sleep(opts.batch_sleep_s)


async def write_rows(writes: List[Decision], deps: Any, counts: collections.Counter, opts: Any,
                     run_id: str) -> None:
    async def write(batch: List[Decision]) -> Callable[[], Any]:
        written, raced, failed = await _write_batch(batch, deps, run_id)
        counts["written"] += len(written)
        counts["raced"] += len(raced)
        counts["update_failed"] += failed
        for d in batch:
            deps.manifest(d.manifest_entry(run_id))

        async def after() -> None:
            await _after_commit([str(d.row["content_key"]) for d in written if d.row.get("content_key")],
                                [str(d.row["content_key"]) for d in written
                                 if d.row.get("content_key") and d.stage != d.old_stage()], deps, counts)
        return after

    try:
        await _batches(writes, deps, opts, counts, write)
    finally:
        for d in writes:
            if d.decision == WRITE:  # never reached a batch
                d.decision = "unwritten"
                deps.manifest(d.manifest_entry(run_id))


# --- the run -----------------------------------------------------------------------------------------

def _emit(tag: str, payload: Dict[str, Any]) -> None:
    print(f"{tag} {json.dumps(payload, default=str, ensure_ascii=False)}", flush=True)


async def process_host(host: str, host_keys: List[Dict[str, Any]], host_jobs: List[Dict[str, Any]],
                       deps: Any, opts: Any,
                       totals: collections.Counter, transitions: collections.Counter, run_id: str,
                       digests: Dict[str, str]) -> None:
    from services.catalog_enrichment_agent.ingestion import content_listing
    from services.curated_brand_feed import _clean_domain

    counts: collections.Counter = collections.Counter()
    host_transitions: collections.Counter = collections.Counter()
    jobs, excluded = select_jobs(host_jobs, jobs_since=opts.jobs_since)
    skip = None if jobs else "no completed storefront job"
    errors: List[str] = []
    keys = {str(r["product_key"]): r for r in host_keys}
    matched: Dict[str, Dict[str, Any]] = {}
    legacy: Dict[str, str] = {}
    plan_listings: set = set()
    for job in jobs:
        if len(matched) == len(keys):
            break
        if deps.clock() - deps.started > opts.budget_s:
            skip = skip or "time budget reached; rerun this host"
            break
        if opts.apply and in_quiet_window(deps.now()):
            skip = skip or "inside the sweep window; rerun this host"
            break
        try:
            records, products = await deps.crawl(job)
            planned = deps.plan(job, records, list(keys.values()))
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
            if pk not in keys or pk in matched:
                continue
            # The plan keeps each row on the listing it names; a key planned on another listing is
            # another page's copy, never this row's.
            if content_listing(pdp.get("canonical_url")) != content_listing(keys[pk].get("canonical_url")):
                continue
            matched[pk] = pdp
    if not matched and (skip or errors):
        counts["rows"] += len(keys)
        counts["host_skipped"] += len(keys)
        totals.update(counts)
        _emit("HOST", {"host": host, **counts, "skip": skip or "every crawl failed", "errors": errors,
                       "excluded_sources": excluded})
        return
    # The host's rows are read AFTER its crawl: what is judged is what the row holds now.
    sql, values = host_rows_sql(opts, keys)
    host_rows = await _read_only(deps, sql, values)
    decisions: List[Decision] = []
    for row in host_rows:
        pk = str(row["product_key"])
        planned = matched.get(pk)
        if planned is not None and content_listing(planned.get("canonical_url")) != content_listing(
                row.get("canonical_url")):
            planned = None  # the row moved to another listing since the crawl
        d = await classify(row, planned, legacy.get(norm_url(row.get("canonical_url"))),
                           allow_demote=opts.allow_demote, allow_promote=opts.allow_promote)
        count_decision(counts, host_transitions, d)
        if d.outcome == NOT_IN_FEED and content_listing(row.get("canonical_url")) in plan_listings:
            counts["not_in_feed_url_in_plan"] += 1
        decisions.append(d)
    removed = collections.Counter(s for d in decisions if d.outcome in REWRITE_OUTCOMES for s in d.removed)
    digest = plan_digest(decisions)
    writes = [d for d in decisions if d.outcome == WRITE]
    if writes:
        digests[host] = digest
    reviewed = (opts.expect_plan or {}).get(host)
    try:
        if not opts.apply:
            for d in decisions:
                if d.outcome in REWRITE_OUTCOMES:
                    deps.manifest(d.manifest_entry(run_id))
        else:
            for d in decisions:
                if d.outcome in (SKIP_SAFETY, SKIP_DEMOTE, SKIP_PROMOTE):
                    deps.manifest(d.manifest_entry(run_id))
            if writes and digest != reviewed:
                counts["plan_changed"] += len(writes)
                for d in writes:
                    d.decision = "plan_changed"
                    deps.manifest(d.manifest_entry(run_id))
                skip = skip or "plan changed since the reviewed dry run: nothing written"
            elif writes:
                await write_rows(writes, deps, counts, opts, run_id)
    finally:
        totals.update(counts)
        transitions.update(host_transitions)
        _emit("HOST", {"host": host, **counts, "to_write": len(writes), "plan_digest": digest if writes else None,
                       "stage_transitions": dict(sorted(host_transitions.items())),
                       # what the producer drops here, most often first: the line to scan before the go
                       "top_removed": [[text[:200], n] for text, n in removed.most_common(TOP_REMOVED)],
                       "skip": skip,
                       "errors": errors, "excluded_sources": excluded})


async def run_forward(opts: Any, deps: Any, run_id: str) -> Tuple[int, Dict[str, Any]]:
    sql, values = population_keys_sql(opts)
    keys_by_host: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in await _read_only(deps, sql, values):
        keys_by_host[r["host"]].append(r)
    counts_by_host = {h: len(v) for h, v in keys_by_host.items()}
    hosts = sorted(counts_by_host, key=lambda h: (-counts_by_host[h], h))
    jobs = await _read_only(deps, _JOBS_SQL, {"hosts": hosts}) if hosts else []
    jobs_by_host: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for j in jobs:
        jobs_by_host[j["host"]].append(j)
    _emit("START", {"run_id": run_id, "rows": sum(counts_by_host.values()), "hosts": len(hosts),
                    "apply": opts.apply, "allow_demote": opts.allow_demote, "allow_promote": opts.allow_promote,
                    "since": opts.since, "jobs_since": opts.jobs_since, "budget_s": opts.budget_s,
                    "max_writes": opts.max_writes, "manifest_out": opts.manifest_out})
    totals: collections.Counter = collections.Counter()
    transitions: collections.Counter = collections.Counter()
    digests: Dict[str, str] = {}
    stopped_failure = None
    for host in hosts:
        if deps.stopped:
            break
        try:
            await process_host(host, keys_by_host[host], jobs_by_host.get(host, []), deps, opts, totals,
                               transitions, run_id, digests)
        except StopRun as exc:
            stopped_failure = str(exc)
            break
    summary = {k: totals.get(k, 0) for k in SUMMARY_KEYS + EXTRA_KEYS}
    summary.update(run_id=run_id, apply=opts.apply, allow_demote=opts.allow_demote,
                   allow_promote=opts.allow_promote, stage_transitions=dict(sorted(transitions.items())),
                   stopped=stopped_failure or deps.stopped, manifest_out=opts.manifest_out,
                   manifest_rows=getattr(deps.manifest, "rows", None),
                   manifest_bytes=getattr(deps.manifest, "bytes", None),
                   expect_plan=",".join(f"{h}={d}" for h, d in sorted(digests.items())),
                   runtime_s=round(deps.clock() - deps.started, 1))
    _emit("SUMMARY", summary)
    failed = stopped_failure or summary["update_failed"] or summary["refresh_failed"] or summary["eligibility_failed"]
    return (3 if failed else 0), summary


# --- revert ------------------------------------------------------------------------------------------

def _entries_from_lines(lines: Iterable[str]) -> List[Dict[str, Any]]:
    out = []
    for line in lines:
        line = line.strip()
        if line.startswith("ROW "):
            line = line[4:]
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


async def load_revert_entries(source: str, deps: Any) -> List[Dict[str, Any]]:
    """The written rows of a run: from a manifest file (JSONL, or the job's `ROW` lines), else from
    the run's stored events, less every row a stored revert already restored. Last entry per pk."""
    if os.path.exists(source):
        with open(source, encoding="utf-8") as fh:
            entries = [e for e in _entries_from_lines(fh) if e.get("decision") == "written"]
    else:
        events = await _read_only(deps, _LOAD_EVENTS_SQL, {"run_id": source,
                                                           "actions": [APPLIED_ACTION, REVERTED_ACTION]})
        entries, reverted = [], set()
        for event in events:
            detail = _json_or_none(event["detail"]) or {}
            if event["action"] == APPLIED_ACTION:
                entries.extend(detail.get("rows") or [])
            else:
                reverted.update(detail.get("pks") or [])
        entries = [e for e in entries if e.get("pk") not in reverted]
    by_pk: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        by_pk[str(e["pk"])] = e
    return list(by_pk.values())


def revert_params(entry: Dict[str, Any]) -> Dict[str, Any]:
    tax = entry.get("taxonomy_old") or {}
    written = entry.get("taxonomy_new") or {}
    return {"pk": entry["pk"], "old": entry["old"], "old_stage": entry.get("old_stage"),
            "new": entry["new"], "new_stage": entry.get("new_stage"),
            "price_tier": tax.get("price_tier"), "new_price_tier": written.get("price_tier"),
            "use_case_tags": _jsonb_param(tax.get("use_case_tags")),
            "new_use_case_tags": _jsonb_param(written.get("use_case_tags")),
            "lifestyle_tags": _jsonb_param(tax.get("lifestyle_tags")),
            "new_lifestyle_tags": _jsonb_param(written.get("lifestyle_tags")),
            "demographic": tax.get("demographic"), "new_demographic": written.get("demographic")}


async def run_revert(opts: Any, deps: Any, run_id: str) -> Tuple[int, Dict[str, Any]]:
    entries = await load_revert_entries(opts.revert, deps)
    if opts.hosts:
        entries = [e for e in entries if e.get("host") in opts.hosts]
    _emit("START", {"run_id": run_id, "revert": opts.revert, "rows": len(entries), "apply": opts.apply})
    counts: collections.Counter = collections.Counter(revert_rows=len(entries))
    stopped = None
    if opts.apply:
        async def write(batch: List[Dict[str, Any]]) -> Callable[[], Any]:
            async def statements() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
                done, raced = [], []
                for e in batch:
                    hit = await deps.db.fetch_one(_REVERT_SQL, revert_params(e))
                    (done if hit is not None else raced).append(e)
                if done:
                    detail = {"run_id": run_id, "of_run": opts.revert, "pks": [e["pk"] for e in done]}
                    stored = await deps.db.fetch_one(_EVENT_SQL, {
                        "action": REVERTED_ACTION, "run_id": done[0].get("run_id") or opts.revert,
                        "detail": json.dumps(detail, default=str)})
                    if stored is None:
                        raise RuntimeError("the revert event was not stored")
                return done, raced

            result, failed = await _write_tx(deps, statements)
            done, raced = result if result else ([], [])
            counts["reverted"] += len(done)
            counts["revert_raced"] += len(raced)
            counts["revert_failed"] += len(batch) if failed else 0
            for e in done:
                _emit("REVERTED", {"pk": e["pk"], "content_key": e.get("content_key")})
            for e in raced:
                _emit("REVERT_RACED", {"pk": e["pk"], "content_key": e.get("content_key")})

            async def after() -> None:
                await _after_commit([e["content_key"] for e in done if e.get("content_key")],
                                    [e["content_key"] for e in done
                                     if e.get("content_key") and e.get("new_stage") != e.get("old_stage")],
                                    deps, counts)
            return after

        try:
            await _batches(entries, deps, opts, counts, write)
        except StopRun as exc:
            stopped = str(exc)
    else:
        for e in entries:
            _emit("WOULD_REVERT", {"pk": e["pk"], "content_key": e.get("content_key"), "host": e.get("host")})
    summary = dict(counts, run_id=run_id, apply=opts.apply, stopped=stopped or deps.stopped,
                   runtime_s=round(deps.clock() - deps.started, 1))
    _emit("SUMMARY", summary)
    failed = stopped or counts["revert_failed"] or counts["refresh_failed"] or counts["eligibility_failed"]
    return (3 if failed else 0), summary


async def run(opts: Any, deps: Any) -> Tuple[int, Dict[str, Any]]:
    """-> (exit code, summary)."""
    if opts.apply and in_quiet_window(deps.now()):
        print("REFUSED: --apply does not start inside the 05:15-06:15Z sweep window", flush=True)
        return 2, {}
    run_id = getattr(opts, "run_id", None) or f"{RUN_PREFIX}{secrets.token_hex(6)}"
    deps.started = deps.clock()
    deps.stopped = None
    deps.writes_left = opts.max_writes
    await deps.reconnect()
    if opts.revert:
        return await run_revert(opts, deps, run_id)
    return await run_forward(opts, deps, run_id)


class ManifestSink:
    """Each entry as a `ROW {json}` line (the copy a Cloud Run job keeps) and appended to a JSONL file."""

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self.rows = 0
        self.bytes = 0

    def __call__(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, default=str, ensure_ascii=False)
        print(f"ROW {line}", flush=True)
        self.rows += 1
        self.bytes += len(line.encode("utf-8")) + 1
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def production_deps(opts: Any) -> Any:
    from db.retailer_ingest import catalog_write_lock
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility

    sink = ManifestSink(opts.manifest_out)
    deps = types.SimpleNamespace(
        db=database, crawl=crawl_job, plan=plan_rows,
        lock=lambda: catalog_write_lock(wait_s=lock_wait_s(datetime.now(timezone.utc), opts.lock_wait_s),
                                        poll_s=LOCK_POLL_S),
        refresh=refresh_agent_pdp_view_for_content_key, recompute=recompute_serving_eligibility,
        reconnect=lambda: _reconnect(database), sleep=asyncio.sleep,
        now=lambda: datetime.now(timezone.utc), clock=time.monotonic, started=0.0,
        stopped=None, writes_left=None, manifest=sink,
    )
    return deps


def _date_arg(value: str) -> Optional[datetime]:
    """'YYYY-MM-DD' -> naive UTC midnight; 'all' -> None (no bound)."""
    if value.strip().lower() == "all":
        return None
    return datetime.fromisoformat(value.strip()).replace(tzinfo=None)


def _expect_plan_arg(value: str) -> Dict[str, str]:
    out = {}
    for part in value.split(","):
        if part.strip():
            host, _, digest = part.strip().partition("=")
            if not host or not re.fullmatch(r"[0-9a-f]{16}", digest):
                raise argparse.ArgumentTypeError(f"--expect-plan wants host=<16 hex>, got {part!r}")
            out[host.strip().lower().removeprefix("www.")] = digest
    return out


def parse_args(argv: Optional[List[str]] = None) -> Any:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write (default: dry run, writes nothing)")
    p.add_argument("--expect-plan", type=_expect_plan_arg, default=None,
                   help="with --apply: host=digest,... from the reviewed dry run's SUMMARY (required). It "
                        "binds one apply: after any partial apply (--max-writes, a failed batch, a window or "
                        "lock stop) dry-run again and use the fresh digest for the remainder")
    p.add_argument("--allow-demote", action="store_true",
                   help="also write rows whose new text lowers their lifecycle stage (give it to the dry run "
                        "too: its plan digest covers exactly the rows it would write)")
    p.add_argument("--allow-promote", action="store_true",
                   help="also write rows whose new text raises their lifecycle stage (dry run too)")
    p.add_argument("--max-writes", type=int, default=None,
                   help="stop after attempting this many rows (a canary); the rest needs a fresh dry run and digest")
    p.add_argument("--revert", default=None, help="restore the written rows of a run: its run id, or a manifest file")
    p.add_argument("--manifest-out", default=None,
                   help="JSONL manifest path (default: <tmp>/<run id>.jsonl; 'none' = ROW lines only)")
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
    if args.apply and not args.revert and not args.expect_plan:
        p.error("--apply needs --expect-plan from the reviewed dry run (its SUMMARY expect_plan)")
    if args.expect_plan and args.revert:
        p.error("--expect-plan is for a forward run, not --revert")
    if args.batch_size < 1:
        p.error("--batch-size must be at least 1")
    if args.max_writes is not None and args.max_writes < 1:
        p.error("--max-writes must be at least 1")
    args.since = _date_arg(args.since)
    if args.jobs_since is None:
        args.jobs_since = None if args.since is None else args.since - timedelta(days=1)
    else:
        args.jobs_since = _date_arg(args.jobs_since)
    if args.jobs_since is not None:
        args.jobs_since = args.jobs_since.replace(tzinfo=timezone.utc)
    args.hosts = {h.strip().lower().removeprefix("www.") for h in args.hosts.split(",") if h.strip()}
    if args.expect_plan:
        args.hosts = (args.hosts & set(args.expect_plan)) if args.hosts else set(args.expect_plan)
    args.run_id = f"{RUN_PREFIX}{secrets.token_hex(6)}"
    if args.manifest_out is None:
        args.manifest_out = os.path.join(tempfile.gettempdir(), f"{args.run_id}.jsonl")
    elif args.manifest_out.lower() == "none":
        args.manifest_out = None
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
