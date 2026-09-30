"""Refresh the Reap cart-link storefront proofs on a schedule: one lane per proof writer.

    python -m jobs.reap_cart_proof_refresh mirror     --on-crawl-egress --budget-seconds 10800
    python -m jobs.reap_cart_proof_refresh enrichment --on-crawl-egress --budget-seconds 3600

WHAT IT IS. The Reap cart-link lane refuses a row without a fresh storefront proof, and two writers
author those proofs, each one --domain at a time:

  * `mirror`      scripts/backfill_shopify_variant_ids.py (option 1, #2459) writes
                  `seed_data.snapshot.shopify_cart_proof` on external-seed mirror rows. Valid 7 days
                  (services.shopify_variant_identity.CART_PROOF_MAX_AGE).
  * `enrichment`  jobs/enrichment_cart_variant_proof.py (option 2, #2464) writes
                  `enrichment_cart_variant_proofs`. Valid 72 hours
                  (services.reap_enrichment_cart_proof.MAX_PROOF_AGE).

This module owns NO proof logic. It picks and orders the domains, walks each one page by page by
calling the writer's own entry point on the writer's own cursor, remembers where each domain got to,
bounds the whole pass with a wall-clock budget, and prints ONE report line. Every rule about what a
proof is, how a storefront is read and paced inside a page, and what is written, stays in the writer.

THE DOMAINS, AND THEIR ORDER.
  * mirror: every domain on config/tierb_cart_link_merchants.json. That list IS the set of stores the
    cart-link lane can buy from (it refuses a merchant without a fresh Tier B eligibility row, and
    only this list gets one), so a mirror proof anywhere else serves no purchase. STALEST FIRST:
    stores no run has ever walked to the end go first, then by the earlier of (a) the oldest
    still-valid proof among the store's active seeds (one SQL read, `SELECT_OLDEST_VALID_MIRROR_PROOF_SQL`)
    and (b) when a run last walked the store to its end. A store the budget cut is resumed from its
    stored cursor (below), so a large store's tail is reached on the next run instead of never.
    Domains are compared exactly as the backfill's `--domain` filter compares them (case-sensitive,
    exact or a `.`-subdomain), so the order only counts seeds the backfill will actually walk.
  * enrichment: `ENRICHMENT_DOMAINS` below, the five stores #2464's census found enrichment rows on.
    Deliberately a pinned constant, not a query: #2464 refuses a default population, and so does this.
    Each must also be on the Tier B list (the writer's `plan_domains` refuses it otherwise, exit 2).
    Ordered by their worst-case `.js` cost, cheapest first, so MAC (~1,870 handles) is last.
  * BOTH lanes: a store whose last run ended `aborted_on_block` is BACKED OFF (skipped, `backed_off`)
    until its stored `blocked_until` (`block_backoff(lane)`: 3 days for mirror, whose proofs live 7
    days; 1 day for enrichment, whose proofs live 72 h, so a store that blocked us once is retried
    before its proofs lapse), and walked LAST on the first run after that. One store that always
    blocks us therefore costs one threshold's worth of requests per back-off, and never the other
    stores' refresh. A store that was aborted inside an IP-level block (the pass stopped) is NOT
    backed off: that block was our address's, not the store's.

WHERE EACH DOMAIN GOT TO (db/reap_cart_proof_refresh_cursors.py, migration 250). One row per (lane,
domain): the writer cursor to resume from (NULL = the first page), how the last run ended there, and
when a run last walked it to its end, the block back-off, and a crash count. An APPLY run writes it
after every page and when the domain ends; a DRY RUN reads it (so it walks what the next apply would)
and writes nothing. A store that crashes at the SAME resume cursor on two runs in a row has its cursor
reset to NULL (logged, `crash_cursor_reset`), so the rows before the poison page are walked again
instead of never.

APPLY OR DRY RUN: THE GATE. `REAP_CART_PROOF_APPLY` (1/true/yes/on, any case) makes the run write;
anything else, unset included, is a DRY RUN. A dry run still fetches every storefront, exactly as
hard (both writers gate the write, not the crawl). This is deliberately NOT the Tier B job's "dark
means contact nobody": the dark job is also the dry-run vehicle, so `gcloud run jobs execute` on a
dark job is the dry run on the real job definition (image, subnet, secret) before anything is armed.

THE EGRESS. Both writers fetch merchant storefronts, so this runs ONLY on the crawl subnet
(`pivota-crawl`, NAT 34.82.199.35), never the default NAT whose address payment partners allowlist.
A container cannot see its own subnet, so `--on-crawl-egress` is REQUIRED (the enrichment writer's
own rule, applied to both lanes): without it this exits 2 before touching the database or the net.

PAGES AND PACING. Mirror: `backfill.run(limit=50)` per page. Enrichment: the writer's `run_domain`
with `limit=250` products per page, so its writes (made at the end of each call) commit page by page
and the budget can stop between pages of one store. Inside a page the writer paces itself. Between
pages this module waits `inter_call_gap_s()`, the writer's own slowest spacing, because a mirror call
builds a fresh pacer that never saw the previous page's last request; the enrichment lane passes ONE
shared pacer to every call as well.

THE BUDGET is checked before every writer call: once spent, no new page starts. A page already
running finishes, which is why the task timeout in the setup script is the budget PLUS one page's
worst case. A domain the budget cut is `budget_stopped` (walked partway; resumed next run) or
`not_reached`. If the task timeout's SIGTERM arrives anyway, the partial report is printed first
(`terminated`), and every page already checkpointed stays checkpointed.

BLOCKS. Two streaks of consecutive block-shaped answers (a clean answer resets both; a challenge page
or a request never sent resets neither), each at the writer's own threshold T (mirror 8, enrichment 5):
  * the STORE streak, per store across its pages. A store is aborted (`aborted_on_block`, backed off
    as above) when it reaches T, OR when it is walked to its end having had at least one block and
    NO clean answer at all (a small store we never actually read is not `done`, and gets no
    `last_completed_at`). The pass then MOVES ON to the next store;
  * the RUN streak, carried across stores. THE WHOLE PASS stops only when the run streak is at
    `PASS_BLOCK_STORES` x T AND the CURRENT store has itself just been aborted by one of the two
    rules above -- so the store that stops the pass is itself blocked, not a healthy store that
    inherited an earlier store's trailing blocks and saw a couple of transient 429s. That is what an
    IP-level block like 2026-08-21's looks like; a run of small stores that each stay under T still
    trips it (each is aborted as "no clean answer"). When the pass stops, every store aborted since
    the last clean answer is re-recorded WITHOUT a back-off.
  Mirror: the backfill keeps its counter inside one `run()`, so the client handed to it
  (`BlockStreakClient`) classifies every answer with the backfill's OWN `fetch_product_js` and
  `_is_block`; once the store streak trips it answers 429 locally, without touching the network. Enrichment:
  the writer's own `block_state` counter, one per store (`ObservedStreak`), observed into the run
  streak (the writer stops the store at T; the run streak can trip up to T - 1 requests after the
  point it was reached).

A CRASH (an exception out of the writer, or out of summing its report) is recorded against its domain
and the pass moves on: one store's unreadable data must not cost the other stores their refresh.

THE REPORT: one line, `REAP_CART_PROOF_REPORT {json}` (a text prefix, so it lands in textPayload and
`scripts/ops/run_oneoff_job.sh` shows it). A `REAP_CART_PROOF_PROGRESS` line is printed as each
domain ends.

EXIT CODES. #2464's four, plus one for the budget:
  0  every domain walked to its end (a `backed_off` store does not count against it: it is reported)
  1  the PASS aborted on a block (the run streak tripped)
  2  bad arguments, no --on-crawl-egress, a missing merchant list, or a domain list the writer
     refuses; nothing attempted
  3  a writer crashed on at least one domain (the others were still attempted), a cursor did not
     move, or the pass could not run at all (`REAP_CART_PROOF_CRASH`, no report)
  4  a store was left unwalked: the budget, a SIGTERM (always at least 4, even with nothing walked),
     or one store that blocked us (`aborted_on_block`, backed off from now on)
2 is returned before anything runs; otherwise 1 outranks 3, which outranks 4.

Provisioned by infra/gcp/setup_reap_cart_proof_jobs.sh (dark unless --enable); runbook
docs/runbooks/reap_cart_proofs.md. Not registered with services/audit_scheduler, not run by CI or
by any deploy script; tests/test_reap_cart_proof_refresh.py pins that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import reap_cart_proof_refresh_cursors as cursor_store  # noqa: E402

logger = logging.getLogger(__name__)

LANES = ("mirror", "enrichment")
GATE_ENV = "REAP_CART_PROOF_APPLY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: The stores #2464's census (2026-09-29) found enrichment rows on, cheapest worst case first.
ENRICHMENT_DOMAINS = (
    "stilacosmetics.com",
    "jsmbeauty.sg",
    "bluemercury.com",
    "tartecosmetics.com",
    "maccosmetics.com",
)

#: Mirror candidates per writer call. The writer's own default is 100; 50 halves the worst case of
#: the one page the budget cannot stop (see the setup script's task timeout).
MIRROR_PAGE_SIZE = 50
#: Enrichment products per writer call (the writer's default is 2,000). The writer writes at the end of
#: each call, so this is also the most work a SIGTERM can cost.
ENRICHMENT_PAGE_PRODUCTS = 250
#: How long a store that blocked us is skipped, per lane: shorter than the lane's proof life, so one block
#: never guarantees a lapse (mirror proofs live 7 days, enrichment proofs 72 h). The store is walked LAST
#: on the first run after its back-off.
MIRROR_BLOCK_BACKOFF = timedelta(days=3)
ENRICHMENT_BLOCK_BACKOFF = timedelta(days=1)


def block_backoff(lane: str) -> timedelta:
    return MIRROR_BLOCK_BACKOFF if lane == "mirror" else ENRICHMENT_BLOCK_BACKOFF
#: The whole pass stops after this many stores' worth of consecutive block answers.
PASS_BLOCK_STORES = 2
#: Crashes at the same resume cursor, in consecutive runs, before that cursor is reset to NULL.
CRASH_RESET_AFTER = 2

EXIT_OK = 0
EXIT_ABORTED_ON_BLOCK = 1
EXIT_BAD_ARGS = 2
EXIT_CRASHED = 3
EXIT_BUDGET = 4

REPORT_PREFIX = "REAP_CART_PROOF_REPORT "
PROGRESS_PREFIX = "REAP_CART_PROOF_PROGRESS "

DONE = "done"
ABORTED = "aborted_on_block"
CRASHED = "crashed"
BUDGET_STOPPED = "budget_stopped"
NOT_REACHED = "not_reached"
CURSOR_STUCK = "cursor_stuck"
TERMINATED = "terminated"
IN_PROGRESS = "in_progress"
BACKED_OFF = "backed_off"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def apply_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Strict: exactly 1/true/yes/on after trimming, any case. Everything else is a dry run."""
    env = os.environ if environ is None else environ
    raw = env.get(GATE_ENV)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value:
        logger.warning("%s=%r is not a recognised truthy value; this run is a DRY RUN", GATE_ENV, raw)
    return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ── the mirror lane's order: stalest first ──────────────────────────────────────────────────────

# Per seed domain, the oldest proof that is still VALID (checked within the proof's life). A lapsed proof
# is already useless and must not pin its store to the front forever. `checked_at` is compared as text:
# the backfill writes every one as `datetime.now(timezone.utc).isoformat()`, and `:cutoff` is built the
# same way, so the strings order as the instants do.
SELECT_OLDEST_VALID_MIRROR_PROOF_SQL = """
    SELECT domain,
           min(seed_data->'snapshot'->'shopify_cart_proof'->>'checked_at') AS oldest_valid_proof
      FROM external_product_seeds
     WHERE status = 'active'
       AND jsonb_typeof(seed_data->'snapshot'->'shopify_cart_proof') = 'object'
       AND seed_data->'snapshot'->'shopify_cart_proof'->>'checked_at' >= :cutoff
     GROUP BY domain
"""


async def select_oldest_valid_mirror_proofs(db: Any, *, now: datetime, max_age: timedelta) -> Dict[str, datetime]:
    cutoff = (now.astimezone(timezone.utc) - max_age).isoformat()
    out: Dict[str, datetime] = {}
    for raw in await db.fetch_all(SELECT_OLDEST_VALID_MIRROR_PROOF_SQL, {"cutoff": cutoff}) or []:
        row = dict(raw)
        try:
            stamp = _aware(datetime.fromisoformat(str(row["oldest_valid_proof"])))
        except (TypeError, ValueError):
            continue
        out[str(row["domain"])] = stamp
    return out


def _seed_domain_is(seed_domain: str, domain: str) -> bool:
    """The backfill's own `--domain` rule, EXACTLY as its SQL applies it to the `domain` column:
    `domain = :d OR domain LIKE '%.' || :d` -- case-sensitive, no trimming. A seed the backfill will
    not select must not move a store in the order either."""
    return seed_domain == domain or seed_domain.endswith("." + domain)


def order_mirror_domains(domains: Sequence[str], oldest_valid: Mapping[str, datetime],
                         cursors: Mapping[str, "cursor_store.CursorRow"]) -> List[str]:
    """Never-completed stores first; then by the earlier of the oldest valid proof and the last
    completed walk; then by name."""
    def key(domain: str):
        proofs = [ts for seed_domain, ts in oldest_valid.items() if _seed_domain_is(seed_domain, domain)]
        oldest = min(proofs) if proofs else None
        row = cursors.get(domain)
        completed = _aware(row.last_completed_at) if row is not None else None
        stamps = [t for t in (oldest, completed) if t is not None]
        return (completed is not None, min(stamps) if stamps else _EPOCH, domain)

    return sorted(dict.fromkeys(domains), key=key)


def defer_blocked(domains: Sequence[str], cursors: Mapping[str, "cursor_store.CursorRow"],
                  now: datetime) -> "tuple[List[str], List[str]]":
    """(walk order, backed off). A store still inside its block back-off is not walked this run; a
    store whose last run ended `aborted_on_block` but whose back-off has passed is walked LAST. Every
    other store keeps its place."""
    fresh: List[str] = []
    retry: List[str] = []
    skipped: List[str] = []
    for domain in domains:
        row = cursors.get(domain)
        until = _aware(row.blocked_until) if row is not None else None
        if until is not None and until > now:
            skipped.append(domain)
        elif row is not None and row.last_status == ABORTED:
            retry.append(domain)
        else:
            fresh.append(domain)
    return fresh + retry, skipped


# ── the driver: domains -> pages -> one report ──────────────────────────────────────────────────


@dataclass
class Page:
    """One writer call's result. `next_cursor` None means the domain is walked to its end."""
    report: Dict[str, Any]
    next_cursor: Optional[str]
    #: this STORE blocked us: record it, back it off, move on
    aborted: bool = False
    #: the RUN streak tripped with this store blocked too (an IP-level block): stop the whole pass
    abort_pass: bool = False
    #: at least one clean answer during this page (the run streak was broken)
    run_reset: bool = False


PageFn = Callable[[str, Optional[str]], Awaitable[Page]]
MergeFn = Callable[[Dict[str, Any], Dict[str, Any]], None]


@dataclass
class DomainResult:
    status: str = NOT_REACHED
    pages: int = 0
    elapsed_s: float = 0.0
    start_cursor: Optional[str] = None
    last_cursor: Optional[str] = None
    error: Optional[str] = None
    checkpoint_error: Optional[str] = None
    #: this store's abort is the one that stopped the whole pass
    pass_abort: bool = False
    #: aborted inside the unbroken block streak that then stopped the pass: the address's block
    ip_block: bool = False
    #: this run reset a cursor that crashed twice in a row
    crash_cursor_reset: bool = False
    writer: Dict[str, Any] = field(default_factory=dict)

    def resume_cursor(self) -> Optional[str]:
        """Where the next run starts this domain, unless it ended here."""
        return self.last_cursor if self.last_cursor is not None else self.start_cursor

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status, "pages": self.pages,
                               "elapsed_s": round(self.elapsed_s, 1), "writer": self.writer}
        for key in ("start_cursor", "last_cursor", "error", "checkpoint_error"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        for key in ("pass_abort", "ip_block", "crash_cursor_reset"):
            if getattr(self, key):
                out[key] = True
        return out


#: `checkpoint(domain, result, final)`: after every page that returned a cursor (final False) and when the
#: domain ends (final True). Never called for a domain that was not reached.
CheckpointFn = Callable[[str, DomainResult, bool], Awaitable[None]]


async def _checkpoint(checkpoint: Optional[CheckpointFn], domain: str, result: DomainResult, final: bool) -> None:
    if checkpoint is None:
        return
    try:
        await checkpoint(domain, result, final)
    except Exception as exc:  # noqa: BLE001 - a lost cursor costs a re-walk, never the pass
        logger.exception("reap cart proof refresh: checkpoint for %s failed", domain)
        result.checkpoint_error = f"{type(exc).__name__}: {str(exc)[:200]}"


async def drive(domains: Sequence[str], run_page: PageFn, merge: MergeFn, *, budget_s: float,
                gap_s: float, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                emit: Optional[Callable[[str], None]] = None,
                start_cursors: Optional[Mapping[str, Optional[str]]] = None,
                checkpoint: Optional[CheckpointFn] = None,
                results: Optional[Dict[str, DomainResult]] = None,
                on_domain_start: Optional[Callable[[str], None]] = None) -> Dict[str, DomainResult]:
    """Walk each domain page by page, in order, inside one budget. See the module docstring.

    `results`, when given, is filled in place as the pass goes, so a caller interrupted mid-pass (the
    SIGTERM path) still holds every finished domain."""
    if not budget_s > 0:
        raise ValueError("budget_s must be positive")
    started = clock()
    results = results if results is not None else {}
    for domain in domains:
        results[domain] = DomainResult(start_cursor=(start_cursors or {}).get(domain))
    first_call = True
    stop_all = False
    #: stores aborted since the last clean answer: if the pass then stops, they were the address's block
    unbroken: List[str] = []
    for domain in domains:
        if stop_all:
            break
        result = results[domain]
        domain_started = clock()
        after: Optional[str] = result.start_cursor
        if on_domain_start is not None:
            on_domain_start(domain)
        try:
            while True:
                if clock() - started >= budget_s:
                    # Every later domain meets this same check first and is recorded `not_reached`.
                    result.status = BUDGET_STOPPED if result.pages else NOT_REACHED
                    break
                if not first_call:
                    await sleep(gap_s)
                first_call = False
                try:
                    page = await run_page(domain, after)
                    result.pages += 1
                    if page.run_reset:
                        unbroken.clear()
                    merge(result.writer, page.report)
                except Exception as exc:  # noqa: BLE001 - recorded against the domain; the pass goes on
                    logger.exception("reap cart proof refresh: %s crashed", domain)
                    result.status = CRASHED
                    result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                    break
                if page.aborted or page.abort_pass:
                    # This store blocked us. Only a tripped RUN streak (the next store blocked too)
                    # stops the pass; otherwise the next store gets its turn.
                    result.status = ABORTED
                    if page.abort_pass:
                        result.pass_abort = True
                        stop_all = True
                    break
                if page.next_cursor is None:
                    result.status = DONE
                    break
                if page.next_cursor == after:
                    # The writer handed back the cursor it was given: another call would read the same
                    # page forever. Never expected; stop the domain rather than spin until the timeout.
                    result.status = CURSOR_STUCK
                    break
                after = page.next_cursor
                result.last_cursor = after
                await _checkpoint(checkpoint, domain, result, False)
        except asyncio.CancelledError:
            result.status = TERMINATED
            result.elapsed_s = clock() - domain_started
            raise
        result.elapsed_s = clock() - domain_started
        if result.pass_abort:
            # R1: every store aborted since the last clean answer was part of the same address-level
            # block. Re-record each without its back-off (its first checkpoint gave it one).
            for earlier in unbroken:
                results[earlier].ip_block = True
                await _checkpoint(checkpoint, earlier, results[earlier], True)
        elif result.status == ABORTED:
            unbroken.append(domain)
        if result.status != NOT_REACHED:
            await _checkpoint(checkpoint, domain, result, True)
            if emit is not None:
                emit(PROGRESS_PREFIX + json.dumps({"domain": domain, **result.as_dict()}, sort_keys=True,
                                                  default=str))
    return results


def exit_code(results: Mapping[str, DomainResult], *, terminated: bool = False) -> int:
    statuses = {r.status for r in results.values()}
    if any(r.pass_abort for r in results.values()):
        return EXIT_ABORTED_ON_BLOCK
    if statuses & {CRASHED, CURSOR_STUCK}:
        return EXIT_CRASHED
    if terminated or statuses & {BUDGET_STOPPED, NOT_REACHED, TERMINATED, ABORTED}:
        return EXIT_BUDGET
    return EXIT_OK


def cursor_row_for(result: DomainResult, final: bool, now: datetime,
                   prior: Optional["cursor_store.CursorRow"] = None,
                   backoff: timedelta = MIRROR_BLOCK_BACKOFF) -> Dict[str, Any]:
    """What `checkpoint` stores for one domain. `prior` is the row as this run found it."""
    row: Dict[str, Any] = {"next_cursor": result.resume_cursor(), "last_status": result.status,
                           "completed_at": None, "blocked_until": None, "crash_count": 0}
    if not final:
        row.update(next_cursor=result.last_cursor, last_status=IN_PROGRESS)
    elif result.status == DONE:
        row.update(next_cursor=None, completed_at=now)
    elif result.status == CURSOR_STUCK:
        row.update(next_cursor=None)
    elif result.status == ABORTED and not (result.pass_abort or result.ip_block):
        # This store blocked us: skip it for a while, then walk it last. A store aborted inside the
        # block that stopped the whole pass is not backed off -- that block was the address's.
        row.update(blocked_until=now + backoff)
    elif result.status == CRASHED:
        cursor = result.resume_cursor()
        again = prior is not None and prior.last_status == CRASHED and prior.next_cursor == cursor
        count = (prior.crash_count if again else 0) + 1
        if count >= CRASH_RESET_AFTER:
            logger.warning("reap cart proof refresh: crashed %d runs in a row at cursor %r; resetting "
                           "the cursor so the rows before it are walked again", count, cursor)
            result.crash_cursor_reset = True
            row.update(next_cursor=None, crash_count=0)
        else:
            row.update(crash_count=count)
    return row


# ── merging a writer's per-page report into the domain's ────────────────────────────────────────

#: The mirror writer's report keys that are plain counts or count maps, summed over pages.
_MIRROR_COUNTS = ("candidates", "rows_with_new_ids", "variant_ids_stamped", "write_conflicts")
_MIRROR_COUNT_MAPS = ("fetch_outcomes", "match_reasons", "cart_proofs", "most_blocked_domains")


def merge_mirror(total: Dict[str, Any], page: Dict[str, Any]) -> None:
    for key in _MIRROR_COUNTS:
        total[key] = total.get(key, 0) + int(page.get(key) or 0)
    for key in _MIRROR_COUNT_MAPS:
        bucket = total.setdefault(key, {})
        for name, count in (page.get(key) or {}).items():
            bucket[name] = bucket.get(name, 0) + int(count or 0)


def merge_enrichment(total: Dict[str, Any], page: Dict[str, Any]) -> None:
    """The enrichment writer's per-call report is already a full account of its page; pages are kept
    side by side rather than summed."""
    total.setdefault("pages", []).append(page)


# ── the mirror lane's block streak, carried across calls ────────────────────────────────────────


class _Replay:
    """A client that answers one GET with a response (or an exception) already received."""

    def __init__(self, result: Any) -> None:
        self._result = result

    async def get(self, url: str, **_kwargs: Any) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class BlockStreakClient:
    """The client handed to the backfill: every GET goes to the real client, and its answer is classified
    by the backfill's OWN `fetch_product_js` + `_is_block` into two streaks (see BLOCKS in the module
    docstring): a block adds one to both, `not_json` (a challenge page or a soft-404) touches neither,
    anything else resets both. `start_store()` resets the store streak.

    `store_tripped` at the backfill's `CONSECUTIVE_BLOCK_ABORT`; `run_tripped` at `PASS_BLOCK_STORES`
    times that. Once the store trips, every further GET is answered 429 HERE, with no request sent, and
    `mirror_page_fn` reports the page aborted (and the pass, when the run streak has tripped too). The
    client also counts this store's blocks and clean answers, and every clean answer ever
    (`clean_answers`), for the "no clean answer" and "streak broken" rules."""

    def __init__(self, client: Any, backfill: Any) -> None:
        self._client = client
        self._backfill = backfill
        self.state = {"store": 0, "run": 0}
        self.limit = int(backfill.CONSECUTIVE_BLOCK_ABORT)
        self.short_circuited = 0
        self.store_blocks = 0
        self.store_clean = 0
        self.clean_answers = 0

    def start_store(self, _domain: str = "") -> None:
        self.state["store"] = 0
        self.store_blocks = 0
        self.store_clean = 0

    @property
    def store_tripped(self) -> bool:
        return self.state["store"] >= self.limit

    @property
    def run_tripped(self) -> bool:
        return self.state["run"] >= PASS_BLOCK_STORES * self.limit

    async def get(self, url: str, **kwargs: Any) -> Any:
        if self.store_tripped:
            self.short_circuited += 1
            return httpx.Response(429, request=httpx.Request("GET", url))
        try:
            result: Any = await self._client.get(url, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised below, after it is counted
            result = exc
        _payload, outcome = await self._backfill.fetch_product_js(_Replay(result), url)
        if self._backfill._is_block(outcome):
            self.state["store"] += 1
            self.state["run"] += 1
            self.store_blocks += 1
        elif outcome != "not_json":
            self.state["store"] = 0
            self.state["run"] = 0
            self.store_clean += 1
            self.clean_answers += 1
        if isinstance(result, Exception):
            raise result
        return result


class ObservedStreak(dict):
    """The enrichment writer's `block_state` for ONE store, observed into the shared run streak. The
    writer adds one per block (`block_state["consecutive"] += 1`), sets 0 on a clean answer and leaves
    it alone for a neutral one; every such write is mirrored into `run["consecutive"]`."""

    def __init__(self, run: Dict[str, int]) -> None:
        super().__init__(consecutive=0)
        self.run = run
        self.blocks = 0
        self.clean = 0

    def __setitem__(self, key: str, value: int) -> None:
        if key == "consecutive":
            old = self.get("consecutive", 0)
            if value > old:
                self.run["consecutive"] += value - old
                self.blocks += value - old
            elif value == 0:
                self.run["consecutive"] = 0
                self.run["clean_answers"] = self.run.get("clean_answers", 0) + 1
                self.clean += 1
        super().__setitem__(key, value)


# ── the two lanes: one writer call per page ─────────────────────────────────────────────────────


def mirror_domains(merchants_path: Optional[str] = None) -> List[str]:
    from services.tierb_cart_link_merchants import load_merchants

    return sorted({m.domain for m in load_merchants(merchants_path)})


def mirror_page_fn(backfill: Any, client: Any, *, apply: bool, page_size: int = MIRROR_PAGE_SIZE) -> PageFn:
    async def run_page(domain: str, after: Optional[str]) -> Page:
        clean_before = getattr(client, "clean_answers", 0)
        report = await backfill.run(limit=page_size, domain=domain, apply=apply, client=client, after=after)
        # Fewer candidates than asked for: the domain is walked to its end.
        exhausted = int(report.get("candidates") or 0) < page_size
        # A store walked to its end with blocks and not ONE clean answer was never actually read.
        never_read = (exhausted and getattr(client, "store_blocks", 0) > 0
                      and getattr(client, "store_clean", 0) == 0)
        # The backfill's own abort, OR the store streak carried across this store's calls reaching the
        # same threshold (the backfill's per-call counter cannot see blocks from earlier calls).
        aborted = (bool(report.get("aborted_on_block")) or bool(getattr(client, "store_tripped", False))
                   or never_read)
        # The pass stops only when THIS store is blocked too (R2), not on inherited trailing blocks.
        abort_pass = aborted and bool(getattr(client, "run_tripped", False))
        cursor = None if (aborted or exhausted) else report.get("next_cursor")
        return Page(report=report, next_cursor=cursor, aborted=aborted, abort_pass=abort_pass,
                    run_reset=getattr(client, "clean_answers", 0) > clean_before)

    return run_page


def enrichment_page_fn(job: Any, db: Any, client: Any, plans: Mapping[str, Any], *, apply: bool, pacer: Any,
                       run_streak: Dict[str, int], page_products: int = ENRICHMENT_PAGE_PRODUCTS) -> PageFn:
    """One `run_domain` call per page. Each store gets its own `ObservedStreak` (the writer's store
    streak, carried across that store's pages); every one feeds `run_streak`."""
    per_store: Dict[str, ObservedStreak] = {}

    async def run_page(domain: str, after: Optional[str]) -> Page:
        limit = job.abort_after_blocks()
        streak = per_store.setdefault(domain, ObservedStreak(run_streak))
        clean_before = run_streak.get("clean_answers", 0)
        report = await job.run_domain(db, client, plans[domain], apply=apply, source_mode="auto",
                                      limit=page_products, after=after, pacer=pacer,
                                      block_limit=limit, block_state=streak)
        exhausted = bool(report.get("exhausted"))
        never_read = exhausted and streak.blocks > 0 and streak.clean == 0
        aborted = bool(report.get("aborted_on_block")) or never_read
        # The pass stops only when THIS store is blocked too (R2), not on inherited trailing blocks.
        abort_pass = aborted and run_streak["consecutive"] >= PASS_BLOCK_STORES * limit
        cursor = None if (aborted or exhausted) else report.get("next_cursor")
        return Page(report=dict(report), next_cursor=cursor, aborted=aborted, abort_pass=abort_pass,
                    run_reset=run_streak.get("clean_answers", 0) > clean_before)

    return run_page


def inter_call_gap_s(lane: str, *, backfill: Any = None, job: Any = None) -> float:
    """The writer's own slowest spacing, applied between two calls."""
    if lane == "mirror":
        return max(float(backfill.GLOBAL_MIN_INTERVAL_S), float(backfill.PER_DOMAIN_MIN_GAP_S))
    return float(job.request_gap_s())


# ── planning and running a lane ─────────────────────────────────────────────────────────────────


@dataclass
class LanePlan:
    """Everything a lane needs, resolved BEFORE the database or the network is touched, so a
    domain list the writer refuses is exit 2 with nothing attempted."""
    lane: str
    domains: List[str]
    writer: Any
    gap_s: float
    plans: Dict[str, Any] = field(default_factory=dict)
    #: enrichment: the proof table's `ensure_table` (the writer's `run()` calls it; `run_domain` does not).
    ensure_proof_table: Optional[Callable[[], Awaitable[bool]]] = None
    #: mirror: how long a proof is valid (the stalest-first order ignores lapsed proofs).
    proof_max_age: Optional[timedelta] = None


def plan_lane(lane: str, now: datetime) -> LanePlan:
    if lane == "mirror":
        from scripts import backfill_shopify_variant_ids as backfill
        from services.shopify_variant_identity import CART_PROOF_MAX_AGE

        return LanePlan(lane=lane, domains=mirror_domains(), writer=backfill,
                        gap_s=inter_call_gap_s(lane, backfill=backfill), proof_max_age=CART_PROOF_MAX_AGE)
    import jobs.enrichment_cart_variant_proof as job
    from db.enrichment_cart_variant_proofs import ensure_table

    plans = {p.domain: p for p in job.plan_domains(list(ENRICHMENT_DOMAINS))}
    return LanePlan(lane=lane, domains=list(plans), writer=job, gap_s=inter_call_gap_s(lane, job=job),
                    plans=plans, ensure_proof_table=ensure_table)


@dataclass
class RunState:
    """Filled as the pass goes, so the SIGTERM path can report what was done."""
    results: Dict[str, DomainResult] = field(default_factory=dict)
    info: Dict[str, Any] = field(default_factory=dict)
    terminated: Optional[str] = None
    #: prints the report; called on the SIGTERM path before the database is disconnected
    on_terminate: Optional[Callable[[], None]] = None
    reported: bool = False


async def run_lane(plan: LanePlan, *, apply: bool, budget_s: float, emit: Callable[[str], None],
                   state: Optional[RunState] = None, db: Any = None,
                   now: Callable[[], datetime] = _utcnow) -> Dict[str, Any]:
    if db is None:
        from db.database import database as db
    state = state if state is not None else RunState()
    info = state.info
    info.update(domains_order=list(plan.domains), inter_call_gap_s=plan.gap_s)
    cursors: Dict[str, cursor_store.CursorRow] = {}

    async def checkpoint(domain: str, result: DomainResult, final: bool) -> None:
        row = cursor_row_for(result, final, now(), cursors.get(domain), backoff=block_backoff(plan.lane))
        await cursor_store.save(db, lane=plan.lane, domain=domain, next_cursor=row["next_cursor"],
                                last_status=row["last_status"], completed_at=row["completed_at"], now=now(),
                                blocked_until=row["blocked_until"], crash_count=row["crash_count"])

    await db.connect()
    try:
        if apply and not await cursor_store.ensure_table(db):
            raise RuntimeError(f"could not ensure {cursor_store.TABLE}")
        cursors.update(await cursor_store.load(db, plan.lane, table_must_exist=apply))
        start = {d: row.next_cursor for d, row in cursors.items() if row.next_cursor is not None}
        info["resumed"] = {d: c for d, c in start.items() if d in plan.domains}
        kwargs = dict(budget_s=budget_s, gap_s=plan.gap_s, emit=emit, start_cursors=start,
                      checkpoint=checkpoint if apply else None, results=state.results)
        domains = list(plan.domains)
        if plan.lane == "mirror":
            oldest = await select_oldest_valid_mirror_proofs(db, now=now(), max_age=plan.proof_max_age)
            domains = order_mirror_domains(domains, oldest, cursors)
        domains, backed_off = defer_blocked(domains, cursors, now())
        info["domains_order"] = domains
        info["backed_off"] = {d: str(cursors[d].blocked_until) for d in backed_off}
        for domain in backed_off:
            state.results[domain] = DomainResult(status=BACKED_OFF)
        if plan.lane == "mirror":
            async with httpx.AsyncClient() as raw_client:
                client = BlockStreakClient(raw_client, plan.writer)
                try:
                    await drive(domains, mirror_page_fn(plan.writer, client, apply=apply), merge_mirror,
                                on_domain_start=client.start_store, **kwargs)
                finally:
                    info["block_streak_short_circuited"] = client.short_circuited
        else:
            job = plan.writer
            if apply and not await plan.ensure_proof_table():
                raise RuntimeError("could not ensure the enrichment proof table")
            pacer = job.Pacer(job.request_gap_s())
            run_streak = {"consecutive": 0, "clean_answers": 0}
            async with job.no_cookie_client() as client:
                try:
                    await drive(domains,
                                enrichment_page_fn(job, db, client, plan.plans, apply=apply, pacer=pacer,
                                                   run_streak=run_streak),
                                merge_enrichment, **kwargs)
                finally:
                    info["requests"] = pacer.requests
    finally:
        # A SIGTERM lands here first: print the partial report while the process is still ours and
        # BEFORE the disconnect, which can itself take the grace period.
        if state.terminated is not None and state.on_terminate is not None:
            state.on_terminate()
        await db.disconnect()
    return info


# ── main ────────────────────────────────────────────────────────────────────────────────────────


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lane", choices=LANES)
    parser.add_argument("--on-crawl-egress", action="store_true",
                        help="REQUIRED: this process egresses by the crawl subnet (pivota-crawl)")
    parser.add_argument("--budget-seconds", type=float, required=True,
                        help="wall-clock budget; once spent no new page starts")
    return parser.parse_args(argv)


def _emit(line: str) -> None:
    print(line, flush=True)


async def _run_with_sigterm(plan: LanePlan, *, apply: bool, budget_s: float, emit: Callable[[str], None],
                            state: RunState) -> None:
    """Run the lane; a SIGTERM (Cloud Run's task timeout, or a cancel) cancels it and marks the state, so
    main still prints the partial report."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def on_sigterm() -> None:
        state.terminated = "SIGTERM"
        task.cancel()

    installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, on_sigterm)
        installed = True
    except (NotImplementedError, RuntimeError, ValueError):  # not the main thread / no signal support
        logger.warning("could not install a SIGTERM handler; a timeout will leave no partial report")
    try:
        await run_lane(plan, apply=apply, budget_s=budget_s, emit=emit, state=state)
    except asyncio.CancelledError:
        if state.terminated is None:
            raise
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main(argv: Optional[List[str]] = None, *, environ: Optional[Mapping[str, str]] = None,
         emit: Callable[[str], None] = _emit) -> int:
    try:
        args = _parse(argv)
    except SystemExit as exc:  # argparse: a usage error is a bad argument, never a crash
        return EXIT_BAD_ARGS if exc.code else EXIT_OK
    if not args.on_crawl_egress:
        print("REAP_CART_PROOF_ERROR refusing to crawl without --on-crawl-egress: run on the "
              "pivota-crawl subnet and say so", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    if not (math.isfinite(args.budget_seconds) and args.budget_seconds > 0):
        print("REAP_CART_PROOF_ERROR --budget-seconds must be a positive number", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    apply = apply_enabled(environ)
    now = _utcnow()
    started = time.monotonic()
    try:
        from services.tierb_cart_link_merchants import MerchantListError

        plan = plan_lane(args.lane, now)
    except (MerchantListError, FileNotFoundError) as exc:
        print(f"REAP_CART_PROOF_ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    except Exception as exc:  # noqa: BLE001 - an import or planning failure is a crash, never "a block"
        logger.exception("reap cart proof refresh could not plan the %s lane", args.lane)
        print(f"REAP_CART_PROOF_CRASH {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr, flush=True)
        return EXIT_CRASHED
    state = RunState()

    def report() -> int:
        """Build and print the one report line (once), and return the exit code. A SIGTERM always
        exits non-zero, even when it arrived before any store finished."""
        code = exit_code(state.results, terminated=state.terminated is not None)
        if not state.reported:
            state.reported = True
            body = {
                "lane": args.lane, "mode": "apply" if apply else "dry_run", "gate_env": GATE_ENV,
                "budget_s": args.budget_seconds, "elapsed_s": round(time.monotonic() - started, 1),
                "started_at": now.isoformat(), "exit_code": code, "terminated": state.terminated,
                "status_counts": _status_counts(state.results),
                "domains": {d: r.as_dict() for d, r in state.results.items()},
                **state.info,
            }
            emit(REPORT_PREFIX + json.dumps(body, sort_keys=True, default=str))
        return code

    state.on_terminate = report
    try:
        asyncio.run(_run_with_sigterm(plan, apply=apply, budget_s=args.budget_seconds, emit=emit, state=state))
    except Exception as exc:  # noqa: BLE001 - the pass itself could not run (e.g. the DB is unreachable)
        logger.exception("reap cart proof refresh crashed")
        print(f"REAP_CART_PROOF_CRASH {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr, flush=True)
        if state.terminated is not None:
            return report()
        return EXIT_CRASHED
    return report()


def _status_counts(results: Mapping[str, DomainResult]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results.values():
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
