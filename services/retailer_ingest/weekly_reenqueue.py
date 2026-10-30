"""Re-enqueue every done retailer-ingest cohort before its last price read ages out, each job naming
its options.market explicitly. (The "weekly" in the module, env and job names is the freshness window
it serves; the cadence itself is INTERVAL_HOURS, below.)

WHY. Jobs reach the drain only by hand (scripts/enqueue_retailer_ingest.py), so a `done` cohort was never
crawled again and its prices went stale. A drain row may serve public only while its offers were read
within FRESHNESS_HOURS (7 days) in a DECLARED market, and the drain declares a market
(offer_payload.declared_market) only for a job whose options name it. So every job queued here names its
market, and a cohort whose market cannot be shown is skipped (`market_unproven`), never defaulted.

THE COHORT is db.retailer_ingest.scope_key(domain, brand, options): the key the open-job unique index
(migration 234) is built on, recomputed from each row's options so every job of one lineage groups
together. options.market "US" folds into the no-market key and options.require_ships_to_market is not
scope, so what this module adds never changes a cohort's key (checked anyway: `scope_changed`).

THE CADENCE. A cohort is due when its last read is at least INTERVAL_HOURS old (default 120, env
RETAILER_INGEST_WEEKLY_REENQUEUE_INTERVAL_HOURS, 1..167). Its last read is the newest finished APPLIED
apply run of any of its jobs (a hand-queued run counts), or, for a done job with none, the newest job's
creation. The run is hourly, so a cohort is queued within an hour of becoming due unless the run cap
defers it (oldest read first). Between two applied reads there are then at most INTERVAL_HOURS + 1 hour
of scheduling + the drain's own latency (queue, claim window, held review, dry run, apply), so the drain
latency budget that keeps a read inside FRESHNESS_HOURS is FRESHNESS_HOURS - INTERVAL_HOURS - 1 (47 h at
the default). There is no fixed weekly slot: due times spread by when each cohort's reads finished, and
the run cap spreads the first catch-up.

PER COHORT, in order (the first that applies is the reported reason):
  excluded_host     RETAILER_INGEST_WEEKLY_REENQUEUE_EXCLUDED_HOSTS names the host (www.-folded);
  leased            a job of the cohort holds a live lease (a stage is running);
  open_job          a job is queued, apply_due or held (the drain or a reviewer has it);
  no_done_job       the cohort never reached `done`;
  failed_lineage / cancelled_lineage / nothing_lineage
                    a job created after the latest done one ended failed / cancelled / nothing: a human
                    decides (a later done job reopens the lineage);
  not_due_recent    its last read is younger than INTERVAL_HOURS;
  retired_host      every product the latest done job's last applied run read back is now RETIRED by the
                    catalog's own rule (catalog_enrichment_agent.apply.legacy_chain_retired: suppressed with
                    a reason no lane lifts, no active seed, no live offer) -- a hand-retired store is not
                    re-crawled. A product missing from the catalog, or suppressed with no reason or a
                    self-reviving one, is not proof, so the cohort is re-crawled;
  ships_to_market_false
                    that run's own check says the store does NOT ship to the market it was checked for
                    (checks.storefront.ships_to_market is false) -- explicit market or not;
  market_unproven   no explicit market and the evidence does not show one (detail says which check failed);
  invalid_options   the copied options no longer pass pipeline.validate_options;
  scope_changed     the new options would change the cohort's key (a guard: not expected);
  run_cap           more cohorts were due this run than RETAILER_INGEST_WEEKLY_REENQUEUE_MAX_PER_RUN;
  exists            the insert met an open job created since the read (ON CONFLICT DO NOTHING).
Everything else is enqueued: the latest done job's options (INCLUDING its accepted_flags, exclude_handles,
refile_to_sets and retire_stale_brand, so the reviewer's earlier approvals apply to every re-crawl) +
options.market, priority 0, source "weekly_reenqueue:<that job's id>".

THE SHIPS-TO GATE, by what the latest done job's newest applied run checked (checks.storefront.ships_to_market):
  true      a storefront job also carries options.require_ships_to_market = true: the drain refuses the
            crawl (status `nothing`) if the store stops shipping to the market. Not scope: the key holds.
  false     skipped (ships_to_market_false), explicit market or not.
  missing   (null, absent, or no applied run) an EXPLICIT-market cohort is queued WITHOUT the option -- a
            human named its market, and with the option the drain would fail the crawl as
            ships_to_unverifiable, raise the failed alert and end the lineage -- and is counted in the run
            summary as `ships_unverified`. An evidence-derived market already required true.
validate_options allows the option on a storefront crawl only, so a shopify_markets capture (which checks
shipping itself before any session) or an affiliate_feed cohort (no storefront to ask) never carries it
and is not counted. Options a done job already carried are copied as they were.

THE MARKET, exactly:
  1. The latest done job's explicit options.market, when it is an ingest market.
  2. Otherwise the EVIDENCE of that job's newest applied run, and only all of it:
       - the market the run checked (checks.storefront.market) -- for a job with no options.market that is
         ALWAYS pipeline.DEFAULT_MARKET ("US"), because the drain checks job_market(), which falls back to
         it. So the evidence path can only ever derive US;
       - the store's /meta.json currency (crawl.storefront.currency) is that market's currency (USD);
       - the store SHIPS to that market (checks.storefront.ships_to_market is true): a USD store that does
         not ship to the US is not a US seller;
       - the crawl's own /products.json pages were presented in exactly that currency (the cart_currency
         cookie, crawl.presented_currency -- ADR-026: the cookie, not /meta.json, proves a read's currency);
       - every product the run read back carried offers on the host, all in that currency and stamped
         that market (readback.rows).
     Whether this
     evidence amounts to DECLARING the US market is a product decision this module does not make: it is
     the crawl, presented in the store's currency, which is the default market's currency, at a store
     that ships to that market.
  3. Otherwise market_unproven, with the first failed check as its detail. There is NO default market.

IDEMPOTENT. A re-run in the same hour finds the job it queued (open_job), and two concurrent runs cannot
both insert: the open-job unique index answers the second with DO NOTHING (exists). It writes nothing
but ledger.enqueue_job rows, and nothing at all with the flag off or in dry run.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from db import retailer_ingest as ledger

logger = logging.getLogger(__name__)

ENABLED_ENV = "RETAILER_INGEST_WEEKLY_REENQUEUE_ENABLED"
DRY_RUN_ENV = "RETAILER_INGEST_WEEKLY_REENQUEUE_DRY_RUN"
EXCLUDED_HOSTS_ENV = "RETAILER_INGEST_WEEKLY_REENQUEUE_EXCLUDED_HOSTS"
MAX_PER_RUN_ENV = "RETAILER_INGEST_WEEKLY_REENQUEUE_MAX_PER_RUN"
INTERVAL_HOURS_ENV = "RETAILER_INGEST_WEEKLY_REENQUEUE_INTERVAL_HOURS"

#: Greppable, one WARNING line per run.
SUMMARY_MARKER = "retailer_ingest_weekly_reenqueue: "
SOURCE_PREFIX = "weekly_reenqueue"
#: The read age a drain row may serve public within (the CRAWL_FRESH trust rule's 7 days).
FRESHNESS_HOURS = 168
DEFAULT_INTERVAL_HOURS = 120
#: Always below FRESHNESS_HOURS: a cadence of the freshness window itself decays every row out of it.
MAX_INTERVAL_HOURS = FRESHNESS_HOURS - 1
DEFAULT_MAX_PER_RUN = 10
MAX_PER_RUN_CEILING = 100
#: Re-crawls never jump a hand-queued wave (those use priority > 0) and share the default priority.
REENQUEUE_PRIORITY = 0
#: How many skipped cohorts the run summary names per reason that a human should look at.
NAMED_PER_REASON = 20

EXCLUDED_HOST = "excluded_host"
LEASED = "leased"
OPEN_JOB = "open_job"
NO_DONE_JOB = "no_done_job"
FAILED_LINEAGE = "failed_lineage"
CANCELLED_LINEAGE = "cancelled_lineage"
NOTHING_LINEAGE = "nothing_lineage"
NOT_DUE_RECENT = "not_due_recent"
RETIRED_HOST = "retired_host"
MARKET_UNPROVEN = "market_unproven"
INVALID_OPTIONS = "invalid_options"
SCOPE_CHANGED = "scope_changed"
RUN_CAP = "run_cap"
EXISTS = "exists"
_LINEAGE_REASON = {"failed": FAILED_LINEAGE, "cancelled": CANCELLED_LINEAGE, "nothing": NOTHING_LINEAGE}

MARKET_EXPLICIT = "explicit"
MARKET_EVIDENCE = "evidence"

#: Why the evidence did not show a market (market_unproven's detail), in the order they are checked.
EXPLICIT_MARKET_INVALID = "explicit_market_invalid"
NO_APPLIED_RUN = "no_applied_run"
NO_CHECKED_MARKET = "no_checked_market"
NO_STORE_CURRENCY = "no_store_currency"
CURRENCY_MARKET_MISMATCH = "currency_market_mismatch"
SHIPS_TO_MARKET_UNPROVEN = "ships_to_market_unproven"
SHIPS_TO_MARKET_FALSE = "ships_to_market_false"
#: Queued (not a skip): an explicit-market storefront cohort whose last run never checked shipping.
SHIPS_UNVERIFIED = "ships_unverified"
#: The skips a human should see by name (the rest are the schedule working).
NAMED_REASONS = (MARKET_UNPROVEN, INVALID_OPTIONS, SCOPE_CHANGED, FAILED_LINEAGE, RETIRED_HOST,
                 SHIPS_TO_MARKET_FALSE)
PRESENTED_CURRENCY_ABSENT = "presented_currency_absent"
PRESENTED_CURRENCY_MISMATCH = "presented_currency_mismatch"
READBACK_UNPROVEN = "readback_unproven"

#: The decision of a cohort whose facts (evidence, retirement) are not loaded yet: run_reenqueue loads them.
PENDING = "pending"


def _truthy(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    """RETAILER_INGEST_WEEKLY_REENQUEUE_ENABLED, default OFF."""
    return _truthy(os.getenv(ENABLED_ENV))


def dry_run_from_env() -> bool:
    return _truthy(os.getenv(DRY_RUN_ENV))


def fold_host(domain: Any) -> str:
    """Lowercased, "www." dropped: how the claim compares hosts (db.retailer_ingest._CLAIM_SQL)."""
    host = str(domain or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


def excluded_hosts_from_env() -> Set[str]:
    return {fold_host(h) for h in str(os.getenv(EXCLUDED_HOSTS_ENV) or "").split(",") if h.strip()}


def _int_env(name: str, default: int, low: int, high: int, *, clamp_high: bool) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if clamp_high and value > high:
        logger.warning("%s=%r is above %d; using %d", name, raw, high, high)
        return high
    if low <= value <= high:
        return value
    logger.warning("%s=%r is not %d..%d; using %d", name, raw, low, high, default)
    return default


def max_per_run_from_env() -> int:
    return _int_env(MAX_PER_RUN_ENV, DEFAULT_MAX_PER_RUN, 1, MAX_PER_RUN_CEILING, clamp_high=False)


def interval_hours_from_env() -> int:
    """RETAILER_INGEST_WEEKLY_REENQUEUE_INTERVAL_HOURS: default 120; above 167 is clamped to 167 (never the
    freshness window itself); anything else that is not a positive integer is the default."""
    return _int_env(INTERVAL_HOURS_ENV, DEFAULT_INTERVAL_HOURS, 1, MAX_INTERVAL_HOURS, clamp_high=True)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _options(value: Any) -> Dict[str, Any]:
    value = _json(value)
    return dict(value) if isinstance(value, dict) else {}


def _dict(value: Any) -> Dict[str, Any]:
    value = _json(value)
    return value if isinstance(value, dict) else {}


def cohort_key(job: Dict[str, Any]) -> str:
    return ledger.scope_key(str(job.get("domain") or ""), str(job.get("brand") or ""), _options(job.get("options")))


def _market_currency(market: str) -> Optional[str]:
    from services.region_pricing import pricing_currency_for_region_or_none
    return pricing_currency_for_region_or_none(market)


def _ingest_markets() -> Tuple[str, ...]:
    from services.retailer_ingest.pipeline import INGEST_MARKETS
    return tuple(INGEST_MARKETS)


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def evidence_from_run(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The facts the market rule reads, from an applied run's crawl / checks / readback exactly as
    ledger.finish_run stored them (dicts or JSON text). None for no run."""
    if not run:
        return None
    crawl, checks, readback = _dict(run.get("crawl")), _dict(run.get("checks")), _dict(run.get("readback"))
    storefront = _dict(checks.get("storefront"))
    rows = readback.get("rows") if isinstance(readback.get("rows"), list) else []
    proven = 0
    for row in rows:
        row = row if isinstance(row, dict) else {}
        offers = _count(row.get("offers"))
        if (offers is not None and offers > 0 and _count(row.get("offers_in_currency")) == offers
                and _count(row.get("offers_in_market")) == offers):
            proven += 1
    return {"store_currency": _dict(crawl.get("storefront")).get("currency"),
            "presented": crawl.get("presented_currency"),
            "checked_market": storefront.get("market"),
            "ships_to_market": storefront.get("ships_to_market"),
            "readback_rows": len(rows), "readback_rows_proven": proven}


def evidence_market(evidence: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """(market, None) when the run's evidence shows one (see the module docstring), else (None, why)."""
    if not evidence:
        return None, NO_APPLIED_RUN
    checked = _norm(evidence.get("checked_market"))
    if checked not in _ingest_markets():
        return None, NO_CHECKED_MARKET
    store = _norm(evidence.get("store_currency"))
    if not store:
        return None, NO_STORE_CURRENCY
    if _market_currency(checked) != store:
        return None, CURRENCY_MARKET_MISMATCH
    if evidence.get("ships_to_market") is not True:
        return None, SHIPS_TO_MARKET_UNPROVEN
    presented = _dict(evidence.get("presented"))
    pages = presented.get("pages_by_currency")
    pages = {_norm(c): n for c, n in pages.items()} if isinstance(pages, dict) else {}
    seen = {c for c, n in pages.items() if (_count(n) or 0) > 0}
    if not seen:
        return None, PRESENTED_CURRENCY_ABSENT
    if seen != {store} or presented.get("unproven_values") or presented.get("problems"):
        return None, PRESENTED_CURRENCY_MISMATCH
    rows, proven = int(evidence.get("readback_rows") or 0), int(evidence.get("readback_rows_proven") or 0)
    if rows < 1 or proven != rows:
        return None, READBACK_UNPROVEN
    return checked, None


def derive_market(options: Dict[str, Any], evidence: Optional[Dict[str, Any]]) -> Tuple[Optional[str], str, Optional[str]]:
    """(market, source, unproven_detail): the explicit options.market, else the run's evidence, else
    (None, "", why). Never a default."""
    explicit = options.get("market")
    if explicit not in (None, ""):
        market = _norm(explicit)
        if market in _ingest_markets():
            return market, MARKET_EXPLICIT, None
        return None, "", EXPLICIT_MARKET_INVALID
    market, why = evidence_market(evidence)
    if market:
        return market, MARKET_EVIDENCE, None
    return None, "", why


def _at(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def group_cohorts(jobs: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Cohort key -> its jobs, oldest first."""
    cohorts: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        cohorts.setdefault(cohort_key(job), []).append(job)
    for key in cohorts:
        cohorts[key].sort(key=lambda j: (_at(j["created_at"]), str(j.get("id"))))
    return cohorts


def last_read(jobs: List[Dict[str, Any]]) -> datetime:
    """The cohort's newest finished applied read, or its newest job's creation when that is later (a done
    job with no applied run must not make the cohort due every hour)."""
    times = [_at(j["created_at"]) for j in jobs] + [t for t in (_at(j.get("last_applied_at")) for j in jobs) if t]
    return max(times)


def due_reason(*, read_at: datetime, now: datetime, interval_hours: int) -> Optional[str]:
    """None when the cohort's last read is at least `interval_hours` old."""
    return None if now - read_at >= timedelta(hours=interval_hours) else NOT_DUE_RECENT


def lineage_reason(jobs: List[Dict[str, Any]], now: datetime) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(skip reason or None, the latest done job). `jobs` oldest first."""
    for job in jobs:
        lease = _at(job.get("lease_until"))
        if lease is not None and lease > now:
            return LEASED, None
    if any(j.get("status") in ledger.OPEN_STATUSES for j in jobs):
        return OPEN_JOB, None
    done = [i for i, j in enumerate(jobs) if j.get("status") == "done"]
    if not done:
        return NO_DONE_JOB, None
    latest = done[-1]
    for job in reversed(jobs[latest + 1:]):
        reason = _LINEAGE_REASON.get(job.get("status"))
        if reason:
            return reason, jobs[latest]
    return None, jobs[latest]


def plan_cohort(key: str, jobs: List[Dict[str, Any]], *, now: datetime, excluded: Set[str],
                interval_hours: int = DEFAULT_INTERVAL_HOURS,
                facts: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One cohort's decision: {"decision": "enqueue" | "skip" | "pending", "reason", ...}. `facts` maps a
    done job id to {"evidence": evidence_from_run(...) or None, "retired": bool} (load_facts); None means
    not loaded yet, and a cohort that needs them is `pending`."""
    newest = jobs[-1]
    out: Dict[str, Any] = {"cohort": key, "host": fold_host(newest.get("domain")), "brand": newest.get("brand"),
                           "decision": "skip"}
    if out["host"] in excluded:
        return {**out, "reason": EXCLUDED_HOST}
    reason, done = lineage_reason(jobs, now)
    if done is not None:
        out["prev_job_id"] = done.get("id")
    if reason:
        return {**out, "reason": reason}
    read_at = last_read(jobs)
    out["last_read_at"] = read_at.isoformat()
    reason = due_reason(read_at=read_at, now=now, interval_hours=interval_hours)
    if reason:
        return {**out, "reason": reason}
    if facts is None:
        return {**out, "decision": PENDING}
    fact = facts.get(done.get("id")) or {}
    if fact.get("retired"):
        return {**out, "reason": RETIRED_HOST}
    ships = (fact.get("evidence") or {}).get("ships_to_market")
    if ships is False:
        return {**out, "reason": SHIPS_TO_MARKET_FALSE}
    options = copy.deepcopy(_options(done.get("options")))
    market, source, why = derive_market(options, fact.get("evidence"))
    if not market:
        return {**out, "reason": MARKET_UNPROVEN, "detail": why}
    out.update(market=market, market_source=source)
    options["market"] = market
    if (options.get("source") or "storefront") == "storefront":
        if ships is True:
            # From now on the drain itself refuses the crawl if the store does not ship to the market.
            options["require_ships_to_market"] = True
        elif not options.get("require_ships_to_market"):
            # Explicit market, shipping never checked: queued as the human named it (see the docstring).
            out["ships_unverified"] = True
    from services.retailer_ingest.pipeline import validate_options
    try:
        options = validate_options(options)
    except ValueError as exc:
        return {**out, "reason": INVALID_OPTIONS, "detail": str(exc)[:300]}
    domain, brand = str(done.get("domain") or ""), str(done.get("brand") or "")
    if ledger.scope_key(domain, brand, options) != key:
        return {**out, "reason": SCOPE_CHANGED}
    return {**out, "decision": "enqueue", "domain": domain, "brand": brand, "options": options,
            "source": f"{SOURCE_PREFIX}:{done.get('id')}"}


#: Every job (a few hundred rows) with the newest finished applied read of each. `:hosts` narrows it to
#: www.-folded hosts (the CLI's --host, and the real-Postgres test, which shares its database).
_JOBS_SQL = """
    SELECT j.id, j.domain, j.brand, j.options, j.status, j.lease_until, j.created_at,
           (SELECT max(r.finished_at) FROM retailer_ingest_runs r
             WHERE r.job_id = j.id AND r.stage = 'apply' AND r.outcome = 'applied') AS last_applied_at
    FROM retailer_ingest_jobs j
    WHERE CAST(:all_hosts AS boolean) OR regexp_replace(lower(j.domain), '^www[.]', '') = ANY(:hosts)
"""

#: Per done job, its newest APPLIED run: only the parts evidence_from_run reads, in finish_run's shape.
_FACTS_SQL = """
    SELECT DISTINCT ON (r.job_id)
           r.job_id,
           jsonb_build_object('storefront', r.crawl -> 'storefront',
                              'presented_currency', r.crawl -> 'presented_currency') AS crawl,
           jsonb_build_object('storefront', r.checks -> 'storefront') AS checks,
           jsonb_build_object('rows', r.readback -> 'rows') AS readback
    FROM retailer_ingest_runs r
    WHERE r.job_id = ANY(:ids) AND r.stage = 'apply' AND r.outcome = 'applied'
    ORDER BY r.job_id, r.started_at DESC, r.id DESC
"""

#: The products of `:keys` with the facts catalog_enrichment_agent.apply.legacy_chain_retired reads (the same
#: seed and offer predicates as its _LEGACY_LISTING_OWNERS_SQL).
_RETIRED_SQL = """
    SELECT cp.product_key, cp.suppressed_at, cp.suppression_reason,
           EXISTS (SELECT 1 FROM external_product_seeds s
                    WHERE (s.attached_product_key = cp.product_key OR s.id = cp.source_ref)
                      AND coalesce(lower(trim(s.status)), '') IN ('', 'active')) AS has_active_seed,
           EXISTS (SELECT 1 FROM catalog_offers o WHERE o.product_key = cp.product_key
                     AND o.suppressed_at IS NULL) AS has_live_offer
    FROM catalog_products cp
    WHERE cp.product_key = ANY(:keys)
"""


async def load_jobs(db: Any, hosts: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    wanted = sorted({fold_host(h) for h in hosts or [] if str(h).strip()})
    rows = await db.fetch_all(_JOBS_SQL, {"all_hosts": not wanted, "hosts": wanted})
    return [dict(r) for r in rows]


def _readback_keys(run: Dict[str, Any]) -> List[str]:
    rows = _dict(run.get("readback")).get("rows")
    return sorted({str(r["product_key"]) for r in rows if isinstance(r, dict) and r.get("product_key")}) \
        if isinstance(rows, list) else []


def host_retired(keys: List[str], products: Dict[str, Dict[str, Any]]) -> bool:
    """Every product the run read back is retired by the catalog's own rule; at least one, and a missing
    product is not proof."""
    from services.catalog_enrichment_agent.apply import legacy_chain_retired
    return bool(keys) and all(k in products and legacy_chain_retired(products[k]) for k in keys)


async def load_facts(db: Any, job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Done job id -> {"evidence": ..., "retired": bool} from its newest applied run (two reads)."""
    if not job_ids:
        return {}
    runs = {r["job_id"]: dict(r) for r in await db.fetch_all(_FACTS_SQL, {"ids": sorted(set(job_ids))})}
    keys = {jid: _readback_keys(run) for jid, run in runs.items()}
    wanted = sorted({k for ks in keys.values() for k in ks})
    products = {r["product_key"]: dict(r) for r in await db.fetch_all(_RETIRED_SQL, {"keys": wanted})} if wanted else {}
    return {jid: {"evidence": evidence_from_run(runs.get(jid)), "retired": host_retired(keys.get(jid, []), products)}
            for jid in job_ids}


async def run_reenqueue(*, db: Any, now: Optional[datetime] = None, dry_run: bool = False,
                        excluded: Optional[Set[str]] = None, max_per_run: int = DEFAULT_MAX_PER_RUN,
                        interval_hours: int = DEFAULT_INTERVAL_HOURS,
                        hosts: Optional[List[str]] = None) -> Dict[str, Any]:
    """Plan every cohort and (unless dry_run) enqueue the due ones. Returns the run summary, with every
    decision under "decisions"."""
    now = now or datetime.now(timezone.utc)
    excluded = set(excluded or ())
    cohorts = group_cohorts(await load_jobs(db, hosts))
    first = {k: plan_cohort(k, jobs, now=now, excluded=excluded, interval_hours=interval_hours)
             for k, jobs in cohorts.items()}
    facts = await load_facts(db, [d["prev_job_id"] for d in first.values() if d["decision"] == PENDING])
    decisions: List[Dict[str, Any]] = []
    queued = 0
    # The stalest read first, so a run cap defers the cohorts with the most freshness left.
    for key in sorted(cohorts, key=lambda k: (last_read(cohorts[k]), k)):
        decision = first[key]
        if decision["decision"] == PENDING:
            decision = plan_cohort(key, cohorts[key], now=now, excluded=excluded, interval_hours=interval_hours,
                                   facts=facts)
        if decision["decision"] == "enqueue":
            if queued >= max_per_run:
                decision = {**decision, "decision": "skip", "reason": RUN_CAP}
            elif dry_run:
                queued += 1
                decision = {**decision, "decision": "would_enqueue"}
            else:
                job_id = await ledger.enqueue_job(domain=decision["domain"], brand=decision["brand"],
                                                  options=decision["options"], priority=REENQUEUE_PRIORITY,
                                                  source=decision["source"], db=db)
                if job_id:
                    queued += 1
                    decision = {**decision, "job_id": job_id}
                else:
                    decision = {**decision, "decision": "skip", "reason": EXISTS}
        decisions.append(decision)
    return summarize(decisions, dry_run=dry_run, now=now, interval_hours=interval_hours)


def summarize(decisions: List[Dict[str, Any]], *, dry_run: bool, now: datetime,
              interval_hours: int = DEFAULT_INTERVAL_HOURS) -> Dict[str, Any]:
    skipped = Counter(d["reason"] for d in decisions if d["decision"] == "skip")
    unproven = Counter(d.get("detail") for d in decisions if d.get("reason") == MARKET_UNPROVEN)
    taken = [d for d in decisions if d["decision"] in ("enqueue", "would_enqueue")]
    named = {r: [{k: d.get(k) for k in ("host", "brand", "prev_job_id", "detail") if d.get(k) is not None}
                 for d in decisions if d.get("reason") == r][:NAMED_PER_REASON]
             for r in NAMED_REASONS if skipped.get(r)}
    return {
        "dry_run": bool(dry_run), "at": now.isoformat(), "interval_hours": interval_hours,
        "cohorts_seen": len(decisions),
        ("would_enqueue" if dry_run else "enqueued"): len(taken),
        "skipped": dict(sorted(skipped.items())),
        "market_source": dict(sorted(Counter(d.get("market_source") for d in taken).items())),
        "market_unproven_detail": dict(sorted(unproven.items())),
        SHIPS_UNVERIFIED: sum(1 for d in taken if d.get(SHIPS_UNVERIFIED)),
        "named": named,
        "decisions": decisions,
    }


def log_summary(summary: Dict[str, Any]) -> None:
    """ONE WARNING line per run (prod keeps WARNING from module loggers), without the per-cohort list."""
    line = {k: v for k, v in summary.items() if k != "decisions"}
    line["queued"] = [{k: d.get(k) for k in ("host", "brand", "market", "market_source", "job_id", SHIPS_UNVERIFIED)
                       if k != SHIPS_UNVERIFIED or d.get(k)}
                      for d in summary.get("decisions") or [] if d["decision"] in ("enqueue", "would_enqueue")]
    logger.warning(SUMMARY_MARKER + json.dumps(line, default=str, sort_keys=True))
