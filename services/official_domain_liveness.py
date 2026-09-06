"""B1 — is a merchant's official domain still a place a shopper can reach?

A SIBLING OF services/external_seed_destination_liveness.py, not a second
liveness stack. That module answers the question for one published PRODUCT URL;
this one answers it for a BRAND APEX, which needs a different first move (DNS)
and a different definition of dead. Everything else is deliberately borrowed
from it: the crawl User-Agent, the politeness gate, and — the part that matters
— its central rule.

    `unverifiable` is a first-class outcome and it must never buy an exclusion.

That rule is not caution for its own sake. During that module's 2026-08-25
audit, 213 of 286 brand hosts answered EVERY request, robots.txt included, with
a Cloudflare bot challenge (HTTP 429 + `cf-mitigated: challenge`). A checker
that folded "cannot verify" into "gone" would have deleted three quarters of the
brand corpus from the official-domain set on its first run — and the official
set is what decides whether the BD report says AI sent a buyer to the merchant's
own store.

So only a CONFIRMED NEGATIVE excludes:

  * the name does not resolve — no A, no AAAA, no CNAME. This is the judydoll
    case (us.judydoll.com, judydoll.shop, joocyee.co, judydoll-joygroup.com all
    have no DNS record, and AI engines named some of them "the official
    website"). It is deterministic, needs no HTTP, and cannot be produced by a
    WAF.
  * a hard 404/410 on `https://<host>/` after following redirects. An apex that
    404s is not a storefront.

403, 429, 5xx, a TLS error, a connect timeout, a bot challenge, a DNS SERVFAIL
or timeout — all `unverifiable`. The domain keeps its place and we try again
after the TTL.

WHAT THIS FETCHES: `https://<host>/`, plus whatever `crawl_politeness` fetches
to honour robots (it loads `robots.txt` for the host first, and CRAWL_ROBOTS_
ENABLED defaults on). The host must pass `brand_claim_service.
is_valid_public_hostname` first, which rejects bare IP literals, `localhost`
and single-label names.

KNOWN GAP (not yet closed; the sweep has no caller, so nothing reaches this):
that validator does NOT reject a name that RESOLVES to a private address —
`127.0.0.1.nip.io`, `169.254.169.254.nip.io` and `metadata.google.internal`
all pass it — and `follow_redirects=True` does not validate the redirect
target, so a public host can 302 to a link-local address. Rows reach here from
`merchant_onboarding.store_url`, which merchants control. Address-family
validation after resolution, plus a redirect-target check, are owed before
this sweep is registered anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional

import httpx

from db import merchant_official_domains as mod
from services import crawl_politeness
from services.external_seed_destination_liveness import USER_AGENT

logger = logging.getLogger("official_domain_liveness")

# Re-exported so a caller never has to reach into db/ for the vocabulary.
LIVE = mod.LIVENESS_LIVE
DEAD = mod.LIVENESS_DEAD
UNVERIFIABLE = mod.LIVENESS_UNVERIFIABLE
UNCHECKED = mod.LIVENESS_UNCHECKED

# One apex GET. Shorter than the seed sweep's 25s because the sweep's budget is
# per-domain and it walks a batch: a host that has not answered in 12s is a host
# we are going to record `unverifiable` for anyway.
HTTP_TIMEOUT_SECONDS = 12.0
DNS_TIMEOUT_SECONDS = 5.0

# How long a liveness verdict stands before the sweep re-asks. A week: the
# consumer is a BD report, not a checkout, and a domain that goes dark is
# caught inside the same reporting cycle. It also keeps the probe volume at one
# request per official domain per week, which no storefront will notice.
LIVENESS_TTL = timedelta(days=7)

# Rows per sweep run.
DEFAULT_SWEEP_LIMIT = 100

# THE RUN DEADLINE EVERY JOB MUST DECLARE (issue #1754: a job with no deadline
# can hang and take the whole scheduler down with it). The arithmetic, not a
# round number: DEFAULT_SWEEP_LIMIT (100) domains, each at most one DNS lookup
# (DNS_TIMEOUT_SECONDS=5) plus one HTTP GET (HTTP_TIMEOUT_SECONDS=12) plus the
# politeness gate's default minimum interval (~1s) => ~1,800s worst case. The
# sweep enforces this itself, in-process, because it is NOT registered with
# services/audit_scheduler.py yet — when it is, this value is the
# `_JOB_RUN_DEADLINES` entry to add, and the scheduler's own guard test will
# refuse the registration without one.
DEFAULT_RUN_DEADLINE_SECONDS = 1800.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- classification

@dataclass(frozen=True)
class HostLiveness:
    """One observation about one apex."""

    status: str
    http_status: Optional[int] = None
    final_url: Optional[str] = None
    note: str = ""

    @property
    def confirmed_dead(self) -> bool:
        return self.status == DEAD

    @property
    def excludes(self) -> bool:
        """Does this verdict remove the domain from the official set?"""
        return mod.is_excluded(self.status)


def classify_host_liveness(
    *,
    dns_resolved: Optional[bool],
    status_code: Optional[int] = None,
    final_url: Optional[str] = None,
    bot_challenged: bool = False,
    transport_error: Optional[str] = None,
) -> HostLiveness:
    """Pure: turn one DNS answer + one HTTP answer into a verdict.

    `dns_resolved` is THREE-VALUED and the third value is the whole point:
      True  — the name resolves.
      False — the name is CONFIRMED absent (NXDOMAIN, or present with no
              A/AAAA/CNAME). The only DNS outcome that may kill a domain.
      None  — we could not find out (timeout, SERVFAIL, no resolver library).
              Never dead; the HTTP answer decides, and if that is also
              inconclusive the verdict is `unverifiable`.
    """
    if dns_resolved is False:
        # Cheapest and most trustworthy negative there is. A WAF can fake a 404;
        # it cannot un-publish a zone record.
        return HostLiveness(DEAD, None, None, "no_dns_record")

    if transport_error:
        return HostLiveness(UNVERIFIABLE, None, None, transport_error)
    if bot_challenged:
        # HTTP 429 + `cf-mitigated: challenge`. The client is being REFUSED, not
        # throttled and not told the site is gone.
        return HostLiveness(UNVERIFIABLE, status_code, final_url, "bot_challenge")
    if status_code is None:
        return HostLiveness(UNVERIFIABLE, None, final_url, "no status")

    if status_code in (404, 410):
        return HostLiveness(DEAD, status_code, final_url, f"http_{status_code}")
    if status_code >= 400:
        # 403 / 429 / 5xx are the origin refusing or failing. Calling those dead
        # is how a checker eats a corpus.
        return HostLiveness(UNVERIFIABLE, status_code, final_url, f"http_{status_code}")
    return HostLiveness(LIVE, status_code, final_url, "")


# --------------------------------------------------------------------------- DNS

def _default_dns_resolver(host: str) -> Optional[bool]:
    """Does `host` have an A, AAAA or CNAME record? Three-valued (see above).

    dnspython is an OPTIONAL dependency here, exactly as it is for
    brand_claim_service._default_txt_resolver — it is not in requirements.txt,
    it arrives transitively. When it is absent this returns None rather than
    guessing from `socket.getaddrinfo`, whose failures do not distinguish
    NXDOMAIN from SERVFAIL. Guessing in that direction would let a broken
    resolver mark live domains dead, which is the one error this lane must not
    make. The cost of returning None is a wasted HTTP probe, not a wrong answer.
    """
    try:
        import dns.exception  # type: ignore
        import dns.resolver  # type: ignore
    except Exception:  # noqa: BLE001 — optional dependency
        return None

    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    resolver.timeout = DNS_TIMEOUT_SECONDS

    saw_confirmed_absence = False
    for rdtype in ("A", "AAAA", "CNAME"):
        try:
            answer = resolver.resolve(host, rdtype)
            if len(answer):
                return True
        except dns.resolver.NXDOMAIN:
            # The NAME does not exist. No other record type can exist either.
            return False
        except dns.resolver.NoAnswer:
            # The name exists in the zone but carries no record of THIS type.
            # Only an absence across all three is an absence of an address.
            saw_confirmed_absence = True
            continue
        except Exception:  # noqa: BLE001 — Timeout / NoNameservers / anything else
            # An answer we did not get is not an answer of "no".
            return None
    return False if saw_confirmed_absence else None


async def resolve_host_dns(
    host: str, *, resolver: Optional[Callable[[str], Optional[bool]]] = None
) -> Optional[bool]:
    """Run the (synchronous) resolver off the event loop. Never raises."""
    fn = resolver or _default_dns_resolver
    try:
        return await asyncio.to_thread(fn, host)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dns resolve failed for %s: %s", host, str(exc)[:200])
        return None


# --------------------------------------------------------------------------- probe

async def probe_host_liveness(
    host: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    resolver: Optional[Callable[[str], Optional[bool]]] = None,
    max_wait: Optional[float] = 0,
) -> HostLiveness:
    """DNS first, then ONE politeness-gated GET of `https://<host>/`.

    DNS first because it is the cheap, deterministic half: a name with no record
    needs no HTTP request at all, and no bot rule can turn that answer into a
    false positive. Never raises.
    """
    from services.brand_claim_service import is_valid_public_hostname, normalize_host

    normalized = normalize_host(host)
    if not normalized or not is_valid_public_hostname(normalized):
        # Not a host we are willing to resolve or fetch. `unverifiable`, NOT
        # `dead`: a row we refuse to probe has told us nothing about the domain,
        # and only things we actually observed may exclude one.
        return HostLiveness(UNVERIFIABLE, None, None, "not_a_public_hostname")

    dns_resolved = await resolve_host_dns(normalized, resolver=resolver)
    if dns_resolved is False:
        return classify_host_liveness(dns_resolved=False)

    url = f"https://{normalized}/"
    try:
        await crawl_politeness.before_request(url, user_agent=USER_AGENT, max_wait=max_wait)
    except crawl_politeness.RobotsDisallowed:
        # robots.txt forbidding the apex says nothing about whether it is alive.
        return classify_host_liveness(
            dns_resolved=dns_resolved, transport_error="robots_disallowed"
        )
    except Exception as exc:  # noqa: BLE001
        return classify_host_liveness(
            dns_resolved=dns_resolved, transport_error=type(exc).__name__
        )

    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True
    )
    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:  # noqa: BLE001 — TLS error, connect timeout, read timeout
        return classify_host_liveness(
            dns_resolved=dns_resolved, transport_error=type(exc).__name__
        )
    finally:
        if owns_client:
            await client.aclose()

    crawl_politeness.note_response(
        url, resp.status_code, retry_after=resp.headers.get("retry-after")
    )
    return classify_host_liveness(
        dns_resolved=dns_resolved,
        status_code=resp.status_code,
        final_url=str(resp.url),
        bot_challenged=bool(resp.headers.get("cf-mitigated")),
    )


# --------------------------------------------------------------------------- sweep

async def seed_inferred_domains(merchant_id: str, *, now: Optional[datetime] = None) -> int:
    """Give the inferred tier rows the sweep can actually check.

    Without this the sweep only ever sees domains someone asserted, and the
    OVERSTATEMENT half of the B1 defect — us.judydoll.com, inferred from the
    catalog and carrying no DNS record — would never be probed and never
    excluded. Inserting is not promotion: the row's source stays `inferred`, so
    it grants nothing that inference did not already grant. It only makes the
    domain addressable by a liveness verdict.

    Existing rows are left alone -- with ONE exception. `upsert_official_domain`
    would otherwise rewrite an asserted/verified row's source back down to
    `inferred` and blank the verdict a previous run recorded.

    The exception is a `declared` row whose host inference now produces. The
    declare guard refuses a host already inferred, but that covers only the
    order in which inference came FIRST: declare anua.us, then ingest the
    catalog that carries it, and the row is `declared` while the inferred
    branch counts the host official. The due-queues skip `declared` and the
    audit basis drops it, so the host could never be measured dead. Promoting
    it to `inferred` here makes the row what it would have been had inference
    come first; it grants nothing, because the host was already in the used
    set. Counted in the return value: it is a row the sweep can now check.
    """
    from services.brand_claim_service import _inferred_merchant_hosts

    if not merchant_id:
        return 0
    hosts = await _inferred_merchant_hosts(merchant_id)
    if not hosts:
        return 0
    known = {
        str(r.get("domain") or ""): str(r.get("source") or "")
        for r in await mod.list_official_domains(merchant_id)
    }
    seeded = 0
    for host in sorted(hosts):
        if host in known:
            if known[host] == mod.SOURCE_DECLARED and await mod.promote_declared_to_inferred(
                merchant_id=merchant_id, domain=host, now=now,
            ):
                seeded += 1
            continue
        if await mod.upsert_official_domain(
            merchant_id=merchant_id,
            domain=host,
            source=mod.SOURCE_INFERRED,
            liveness_status=UNCHECKED,
            now=now,
        ):
            seeded += 1
    return seeded


async def refresh_official_domain_liveness(
    merchant_id: Optional[str] = None,
    *,
    limit: int = DEFAULT_SWEEP_LIMIT,
    ttl: timedelta = LIVENESS_TTL,
    run_deadline_seconds: float = DEFAULT_RUN_DEADLINE_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
    resolver: Optional[Callable[[str], Optional[bool]]] = None,
    seed_inferred: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """(Re)check every official domain whose verdict is older than `ttl`.

    Not registered with services/audit_scheduler.py — this is the entry point
    only. It still enforces `run_deadline_seconds` itself: the deadline is a
    property of the WORK (a hung probe must not run past its window), not of
    whoever happens to call it, and wiring it up later must not be the moment
    the bound first appears.

    A run that hits the deadline stops between domains and reports
    `deadline_hit: True` with the counts it did finish. Partial progress is
    real progress here — every domain checked is one fewer next run, because
    the queue is ordered stalest-first.

    Returns a summary. `checked` is what we LOOKED AT, not what we changed:
    a run that found every domain unverifiable must not read the same as a run
    that had nothing to do.
    """
    started = time.monotonic()
    summary: Dict[str, Any] = {
        "due": 0,
        "checked": 0,
        "seeded": 0,
        "deadline_hit": False,
        "verdicts": {LIVE: 0, DEAD: 0, UNVERIFIABLE: 0},
    }

    # Seeding is per-merchant only. A global run (merchant_id=None) has no
    # merchant to infer for without walking every merchant in the catalog, which
    # is a different job with a different budget — it sweeps the rows that exist.
    if merchant_id and seed_inferred:
        summary["seeded"] = await seed_inferred_domains(merchant_id, now=now)

    due = await mod.list_domains_due_for_liveness(
        ttl=ttl, limit=limit, merchant_id=merchant_id, now=now
    )
    summary["due"] = len(due)
    if not due:
        return summary

    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True
    )
    try:
        for row in due:
            if time.monotonic() - started >= run_deadline_seconds:
                summary["deadline_hit"] = True
                logger.warning(
                    "official-domain-liveness: run deadline %.0fs reached after %d/%d "
                    "domains; the rest stay due",
                    run_deadline_seconds, summary["checked"], summary["due"],
                )
                break

            row_merchant = str(row.get("merchant_id") or "")
            domain = str(row.get("domain") or "")
            if not row_merchant or not domain:
                continue
            observation = await probe_host_liveness(
                domain, client=client, resolver=resolver
            )
            summary["checked"] += 1
            summary["verdicts"][observation.status] = (
                summary["verdicts"].get(observation.status, 0) + 1
            )
            await mod.record_liveness(
                merchant_id=row_merchant,
                domain=domain,
                liveness_status=observation.status,
                checked_at=now or _now(),
            )
            if observation.confirmed_dead:
                # The loud one. A domain leaving the official set changes a
                # headline number in the BD report, so it is never silent.
                logger.warning(
                    "official-domain-liveness: %s/%s is CONFIRMED DEAD (%s) — it no "
                    "longer counts as a first-party destination",
                    row_merchant, domain, observation.note,
                )
    finally:
        if owns_client:
            await client.aclose()

    logger.info("official-domain-liveness sweep complete: %s", summary)
    return summary


__all__: Iterable[str] = (
    "DEAD",
    "DEFAULT_RUN_DEADLINE_SECONDS",
    "DEFAULT_SWEEP_LIMIT",
    "HostLiveness",
    "LIVE",
    "LIVENESS_TTL",
    "UNCHECKED",
    "UNVERIFIABLE",
    "classify_host_liveness",
    "probe_host_liveness",
    "refresh_official_domain_liveness",
    "resolve_host_dns",
    "seed_inferred_domains",
)
