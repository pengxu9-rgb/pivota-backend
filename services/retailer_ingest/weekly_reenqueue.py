"""Weekly re-enqueue of every retailer-ingest cohort, each with an EXPLICIT options.market.

WHY. Jobs reach the drain only by hand (scripts/enqueue_retailer_ingest.py), so a cohort that is `done`
is never crawled again and its prices go stale. A drain row may serve public only while its offers were
read within 7 days in a DECLARED market, and the drain declares a market (offer_payload.declared_market)
only for a job whose options name it. So every re-crawl queued here names its market, and a cohort whose
market cannot be proven is skipped (`market_unproven`), never defaulted.

THE COHORT is db.retailer_ingest.scope_key(domain, brand, options): the key the open-job unique index
(migration 234) is built on, recomputed from each row's options so every job of one lineage groups
together. options.market "US" folds into the no-market key there, so naming the market this module
derives never changes a cohort's key (checked anyway: `scope_changed`).

PER COHORT, in order (the first that applies is the reported reason):
  excluded_host     RETAILER_INGEST_WEEKLY_REENQUEUE_EXCLUDED_HOSTS names the host (www.-folded);
  leased            a job of the cohort holds a live lease (a stage is running);
  open_job          a job is queued, apply_due or held (the drain or a reviewer has it);
  no_done_job       the cohort never reached `done`;
  failed_lineage / cancelled_lineage / nothing_lineage
                    a job created after the latest done one ended failed / cancelled / nothing: a human
                    decides (a later done job reopens the lineage);
  not_due_recent    the cohort's newest job is less than 168 whole UTC hours old (at most once a week);
  not_due_slot      outside the cohort's weekly slot (below);
  market_unproven   no explicit market and the evidence does not prove one (detail says which);
  invalid_options   the copied options no longer pass pipeline.validate_options;
  scope_changed     naming the market would change the cohort's key (never expected);
  run_cap           more cohorts were due this run than RETAILER_INGEST_WEEKLY_REENQUEUE_MAX_PER_RUN;
  exists            the insert met an open job created since the read (ON CONFLICT DO NOTHING).
Everything else is enqueued: the latest done job's options + options.market, priority 0, source
"weekly_reenqueue:<that job's id>".

THE SPREAD. Each cohort has an hour of the week, sha256(cohort key) mod 168 (Monday 00Z = 0). It is due
only within SLOT_WINDOW_HOURS after its slot, and only when its newest job is >= 168 whole UTC hours old
(hours floored, so the hourly tick enqueues at the same hour every week and the slot does not drift by the
seconds a run takes). A missed tick is caught up inside the window; a cohort past its window waits a week.

THE MARKET. 1) The latest done job's explicit options.market. 2) Otherwise the evidence its last APPLIED
run recorded: the store's /meta.json currency (crawl.storefront.currency), the currency the crawl's own
/products.json pages were PRESENTED in (crawl.presented_currency, the cart_currency cookie -- ADR-026: the
cookie, not /meta.json, proves the currency of a read), the market the run checked (checks.storefront.
market) and its readback, which counted every applied product's offers on the host in that market's
currency and stamped with that market. The market is the checked one only when the presented currency is
exactly the store currency, that is the checked market's currency, and every readback row proves it.
3) Otherwise unproven: skipped and reported. There is NO default market anywhere in this module.

IDEMPOTENT. A re-run in the same hour finds the job it queued (open_job, then not_due_recent), and two
concurrent runs cannot both insert: the open-job unique index answers the second with DO NOTHING (exists).
It writes nothing but ledger.enqueue_job rows, and nothing at all with the flag off or in dry run.
"""
from __future__ import annotations

import copy
import hashlib
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

#: Greppable, one WARNING line per run.
SUMMARY_MARKER = "retailer_ingest_weekly_reenqueue: "
SOURCE_PREFIX = "weekly_reenqueue"
HOURS_PER_WEEK = 168
#: At most once per this many whole UTC hours per cohort.
INTERVAL_HOURS = HOURS_PER_WEEK
#: A cohort is due only in the first SLOT_WINDOW_HOURS hours after its weekly slot.
SLOT_WINDOW_HOURS = 24
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
NOT_DUE_SLOT = "not_due_slot"
MARKET_UNPROVEN = "market_unproven"
INVALID_OPTIONS = "invalid_options"
SCOPE_CHANGED = "scope_changed"
RUN_CAP = "run_cap"
EXISTS = "exists"
#: The skips a human should see by name (the rest are the schedule working).
NAMED_REASONS = (MARKET_UNPROVEN, INVALID_OPTIONS, SCOPE_CHANGED, FAILED_LINEAGE)
_LINEAGE_REASON = {"failed": FAILED_LINEAGE, "cancelled": CANCELLED_LINEAGE, "nothing": NOTHING_LINEAGE}

MARKET_EXPLICIT = "explicit"
MARKET_EVIDENCE = "evidence"

#: Why the evidence did not prove a market (market_unproven's detail).
EXPLICIT_MARKET_INVALID = "explicit_market_invalid"
NO_APPLIED_RUN = "no_applied_run"
NO_CHECKED_MARKET = "no_checked_market"
NO_STORE_CURRENCY = "no_store_currency"
CURRENCY_MARKET_MISMATCH = "currency_market_mismatch"
PRESENTED_CURRENCY_ABSENT = "presented_currency_absent"
PRESENTED_CURRENCY_MISMATCH = "presented_currency_mismatch"
READBACK_UNPROVEN = "readback_unproven"


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


def max_per_run_from_env() -> int:
    raw = str(os.getenv(MAX_PER_RUN_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_PER_RUN
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if 1 <= value <= MAX_PER_RUN_CEILING:
        return value
    logger.warning("%s=%r is not 1..%d; using %d", MAX_PER_RUN_ENV, raw, MAX_PER_RUN_CEILING, DEFAULT_MAX_PER_RUN)
    return DEFAULT_MAX_PER_RUN


def _options(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def cohort_key(job: Dict[str, Any]) -> str:
    return ledger.scope_key(str(job.get("domain") or ""), str(job.get("brand") or ""), _options(job.get("options")))


def week_slot(key: str) -> int:
    """The cohort's hour of the week (0 = Monday 00Z): stable across processes and deploys."""
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % HOURS_PER_WEEK


def floor_hour(at: datetime) -> datetime:
    return at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def hour_of_week(at: datetime) -> int:
    utc = at.astimezone(timezone.utc)
    return utc.weekday() * 24 + utc.hour


def due_reason(*, key: str, last_created: datetime, now: datetime) -> Optional[str]:
    """None when the cohort is due now, else not_due_recent / not_due_slot."""
    # last_created is floored to its hour and `//` floors the rest: whole UTC hours, so a job created at
    # hh:59 counts from hh:00 and the weekly slot does not drift by the seconds a run takes.
    elapsed_h = (now - floor_hour(last_created)) // timedelta(hours=1)
    if elapsed_h < INTERVAL_HOURS:
        return NOT_DUE_RECENT
    if (hour_of_week(now) - week_slot(key)) % HOURS_PER_WEEK >= SLOT_WINDOW_HOURS:
        return NOT_DUE_SLOT
    return None


def _market_currency(market: str) -> Optional[str]:
    from services.region_pricing import pricing_currency_for_region_or_none
    return pricing_currency_for_region_or_none(market)


def _ingest_markets() -> Tuple[str, ...]:
    from services.retailer_ingest.pipeline import INGEST_MARKETS
    return tuple(INGEST_MARKETS)


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def evidence_market(evidence: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """(market, None) when the done job's last applied run proves one, else (None, why)."""
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
    presented = _json(evidence.get("presented"))
    presented = presented if isinstance(presented, dict) else {}
    pages = presented.get("pages_by_currency")
    pages = {_norm(c): n for c, n in pages.items()} if isinstance(pages, dict) else {}
    seen = {c for c, n in pages.items() if isinstance(n, int) and not isinstance(n, bool) and n > 0}
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


def _created(job: Dict[str, Any]) -> datetime:
    at = job.get("created_at")
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at


def group_cohorts(jobs: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Cohort key -> its jobs, oldest first."""
    cohorts: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        cohorts.setdefault(cohort_key(job), []).append(job)
    for key in cohorts:
        cohorts[key].sort(key=lambda j: (_created(j), str(j.get("id"))))
    return cohorts


def lineage_reason(jobs: List[Dict[str, Any]], now: datetime) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(skip reason or None, the latest done job). `jobs` oldest first."""
    for job in jobs:
        lease = job.get("lease_until")
        if isinstance(lease, str):
            lease = datetime.fromisoformat(lease)
        if lease is not None and (lease if lease.tzinfo else lease.replace(tzinfo=timezone.utc)) > now:
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
                evidence: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """One cohort's decision: {"decision": "enqueue" | "skip", "reason", ...}. `evidence` maps a done
    job id to its last applied run's evidence (load_evidence); it is read only for a due cohort."""
    newest = jobs[-1]
    out: Dict[str, Any] = {"cohort": key, "host": fold_host(newest.get("domain")), "brand": newest.get("brand"),
                           "decision": "skip", "slot": week_slot(key)}
    if out["host"] in excluded:
        return {**out, "reason": EXCLUDED_HOST}
    reason, done = lineage_reason(jobs, now)
    if done is not None:
        out["prev_job_id"] = done.get("id")
    if reason:
        return {**out, "reason": reason}
    reason = due_reason(key=key, last_created=max(_created(j) for j in jobs), now=now)
    if reason:
        return {**out, "reason": reason}
    options = copy.deepcopy(_options(done.get("options")))
    market, source, why = derive_market(options, evidence.get(done.get("id")))
    if not market:
        return {**out, "reason": MARKET_UNPROVEN, "detail": why}
    out.update(market=market, market_source=source)
    options["market"] = market
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


#: Every job: a few hundred rows, the columns the plan reads. `:hosts` narrows it to www.-folded hosts
#: (the CLI's --host, and the real-Postgres test, which shares its database with other modules).
_JOBS_SQL = """
    SELECT id, domain, brand, options, status, lease_until, created_at
    FROM retailer_ingest_jobs
    WHERE CAST(:all_hosts AS boolean) OR regexp_replace(lower(domain), '^www[.]', '') = ANY(:hosts)
"""

#: Per done job, its newest APPLIED run's evidence (see evidence_market). The readback rows are reduced
#: here, so a 200-product readback is two integers on the wire.
_EVIDENCE_SQL = """
    SELECT DISTINCT ON (r.job_id)
           r.job_id, r.id AS run_id,
           r.crawl -> 'storefront' ->> 'currency' AS store_currency,
           r.crawl -> 'presented_currency' AS presented,
           r.checks -> 'storefront' ->> 'market' AS checked_market,
           (SELECT count(*) FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(r.readback -> 'rows') = 'array' THEN r.readback -> 'rows'
                     ELSE '[]'::jsonb END) e) AS readback_rows,
           (SELECT count(*) FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(r.readback -> 'rows') = 'array' THEN r.readback -> 'rows'
                     ELSE '[]'::jsonb END) e
             WHERE CASE WHEN jsonb_typeof(e -> 'offers') = 'number'
                        THEN CAST(e ->> 'offers' AS numeric) > 0 ELSE false END
               AND e -> 'offers_in_currency' = e -> 'offers'
               AND e -> 'offers_in_market' = e -> 'offers') AS readback_rows_proven
    FROM retailer_ingest_runs r
    WHERE r.job_id = ANY(:ids) AND r.stage = 'apply' AND r.outcome = 'applied'
    ORDER BY r.job_id, r.started_at DESC, r.id DESC
"""


async def load_jobs(db: Any, hosts: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    wanted = sorted({fold_host(h) for h in hosts or [] if str(h).strip()})
    rows = await db.fetch_all(_JOBS_SQL, {"all_hosts": not wanted, "hosts": wanted})
    return [dict(r) for r in rows]


async def load_evidence(db: Any, job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not job_ids:
        return {}
    rows = await db.fetch_all(_EVIDENCE_SQL, {"ids": sorted(set(job_ids))})
    return {r["job_id"]: dict(r) for r in rows}


async def run_reenqueue(*, db: Any, now: Optional[datetime] = None, dry_run: bool = False,
                        excluded: Optional[Set[str]] = None, max_per_run: int = DEFAULT_MAX_PER_RUN,
                        hosts: Optional[List[str]] = None) -> Dict[str, Any]:
    """Plan every cohort and (unless dry_run) enqueue the due ones. Returns the run summary, with every
    decision under "decisions"."""
    now = now or datetime.now(timezone.utc)
    excluded = set(excluded or ())
    cohorts = group_cohorts(await load_jobs(db, hosts))
    # Evidence is read only for the cohorts that reach the market step without an explicit market: a
    # first pass with no evidence names them (market_unproven / no_applied_run), the second decides.
    first = [plan_cohort(k, jobs, now=now, excluded=excluded, evidence={}) for k, jobs in cohorts.items()]
    evidence = await load_evidence(db, [d["prev_job_id"] for d in first
                                        if d.get("reason") == MARKET_UNPROVEN and d.get("detail") == NO_APPLIED_RUN])
    decisions: List[Dict[str, Any]] = []
    queued = 0
    # The longest-waiting slot first, so a run cap defers the cohorts with the most window left.
    order = sorted(cohorts, key=lambda k: (-((hour_of_week(now) - week_slot(k)) % HOURS_PER_WEEK), k))
    for key in order:
        decision = plan_cohort(key, cohorts[key], now=now, excluded=excluded, evidence=evidence)
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
    return summarize(decisions, dry_run=dry_run, now=now)


def summarize(decisions: List[Dict[str, Any]], *, dry_run: bool, now: datetime) -> Dict[str, Any]:
    skipped = Counter(d["reason"] for d in decisions if d["decision"] == "skip")
    unproven = Counter(d.get("detail") for d in decisions if d.get("reason") == MARKET_UNPROVEN)
    taken = [d for d in decisions if d["decision"] in ("enqueue", "would_enqueue")]
    named = {r: [{k: d.get(k) for k in ("host", "brand", "prev_job_id", "detail") if d.get(k) is not None}
                 for d in decisions if d.get("reason") == r][:NAMED_PER_REASON]
             for r in NAMED_REASONS if skipped.get(r)}
    return {
        "dry_run": bool(dry_run), "at": now.isoformat(),
        "cohorts_seen": len(decisions),
        ("would_enqueue" if dry_run else "enqueued"): len(taken),
        "skipped": dict(sorted(skipped.items())),
        "market_source": dict(sorted(Counter(d.get("market_source") for d in taken).items())),
        "market_unproven_detail": dict(sorted(unproven.items())),
        "named": named,
        "decisions": decisions,
    }


def log_summary(summary: Dict[str, Any]) -> None:
    """ONE WARNING line per run (prod keeps WARNING from module loggers), without the per-cohort list."""
    line = {k: v for k, v in summary.items() if k != "decisions"}
    line["queued"] = [{k: d.get(k) for k in ("host", "brand", "market", "market_source", "job_id")}
                      for d in summary.get("decisions") or [] if d["decision"] in ("enqueue", "would_enqueue")]
    logger.warning(SUMMARY_MARKER + json.dumps(line, default=str, sort_keys=True))
