"""One-off, READ-ONLY: what does each merchant domain publish for agentic checkout?

For every domain this job asks AT MOST THREE paths, one request at a time,

    GET https://{domain}/robots.txt             asked first; decides whether the next two are sent
    GET https://{domain}/.well-known/ucp        the merchant's UCP profile
    GET https://{domain}/.well-known/acp.json   the merchant's ACP discovery document

and prints one line of facts per domain. It writes nothing anywhere: no database row, no file. It
never calls an MCP endpoint, lists tools, builds a cart or a checkout, follows a tracking link or
sends a credential. The only URL builder in this module (`well_known_url`) takes a validated
hostname and one of the three path constants, so no other URL can be requested.

DARK BY DEFAULT. `MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED` is read per run. Unset or off, the job
prints one line saying so and exits 0 before it parses arguments, reads a file, connects to the
database, resolves a name or opens a socket.

    MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED=on python -m jobs.merchant_protocol_profile_census \
        --domains shop-a.example,shop-b.example --domains-file domains.txt
    MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED=on python -m jobs.merchant_protocol_profile_census \
        --from-directory

POPULATION. `--domains` and/or `--domains-file` (one bare hostname per line), OR `--from-directory`:
the distinct domains of the two tables the merchant directory is built from
(`tierb_cart_link_eligibility.shop_domain`, `merchant_purchasability.merchant_domain`), read with
two SELECTs and nothing else, normalized, de-duplicated and sorted. The two sources cannot be
combined. More than 300 domains is refused, whichever source they came from.

REQUEST RULES. https only; no cookies; `Accept: application/json` (`text/plain` for robots.txt);
the crawl lanes' declared User-Agent; no retry; each request is bounded in three parts (resolving
the name at most 5 s, waiting for the pace at most 1 s, the GET itself at most 5 s, so about 11 s
at worst); ONE request at a
time and one per second across the whole run; at most 256 KB of body is read; a well-known body
is parsed only when it is JSON. Before EVERY request the host goes through
`validate_merchant_domain` and `resolves_only_public` (imported from
services/agent_card_issuance, the same guard the merchant-quote lane uses). A redirect is followed
at most once, and only the apex <-> www move of the same registrable domain, over https, on the
same path (`redirect_sibling`, the rule the merchant-door lane applies in
services/merchant_ucp_checkout); the next URL is rebuilt from the host and the path constant, and
the new host goes through both guards. Anything else is recorded as `redirect_refused`.

ROBOTS. The fetched robots.txt is read by this module's own small RFC 9309 reader (plain text in,
no fetching, linear time): the group that names this crawler's token is used, else the `*` group;
among the rules of that group that match a path the LONGEST pattern wins, and Allow wins a tie.
`*` and a trailing `$` are understood. A Disallow pattern too large to evaluate (over 512
characters, or more than 16 `*` once runs of `*` are collapsed) disallows; such an Allow is
ignored. `robots` in the row is:

    absent       404, 410 or an empty file: nothing to obey, both paths are asked
    ok           read, and it disallows neither path
    disallowed   read, and it disallows at least one path: that path is NOT requested and its
                 column says `robots_disallowed`
    unavailable  anything else (no answer, 5xx, any other status, a refused redirect, an
                 oversize or undecodable file, more rules than this reads): FAIL CLOSED, neither
                 path is requested, both columns say `robots_unavailable` and `err` says why
                 (`robots_<code>`)

When robots.txt was answered by the www sibling (or the apex) after the one redirect, the two
well-known paths are asked on that host directly, so such a shop costs three paced requests, not
six. That host goes through both address guards again before each of those requests.

RUN BOUNDS. One request per second, never two at once. `UCPPROF_PLAN` is printed before the first
request: the number of domains and the most requests they can cost (6 each: three paths, one
redirect hop each). `--deadline-seconds` (default 1500) is measured from the start of the crawl
and checked before every request: once it has passed nothing more is sent, the domain in hand
keeps what was already read and gets `err="deadline"`, every later domain gets a `deadline` row,
and the totals and the done line are still printed (exit 0).

OUTPUT (stdout only; a Cloud Run job's stdout reaches Cloud Logging whatever the log level):

    UCPPROF_PLAN {"domains":n,"max_requests":n,"pace_seconds":1,"deadline_seconds":n}
    UCPPROF_ROW {"d":..,"robots":..,"ucp":..,"ver":..,"supported":[..],
                 "svc":[{"name":..,"transport":..}],"cap":[..],"ph":{handler id: [types]},"keys":n,
                 "acp":..,"acp_ver":..,"acp_cap":[..],"err":..[,"truncated":true]}
    UCPPROF_SUM {"domains":n,"ucp_ok":n,"with_checkout":n,"with_order":n,"with_discount":n,
                 "handlers_accepting_network_token":n,"acp_ok":n,"errors":n,"robots_ok":n,
                 "robots_absent":n,"robots_disallowed":n,"robots_unavailable":n,"deadline":n}
    UCPPROF_DONE

Every list in a row is cut at 32 entries (`"truncated":true` then says so; the totals are counted
before the cut), so one row stays far below the size at which a log line is truncated.

`ucp` / `acp` are the HTTP status, or a short code when no status was read. `err` is null or the
first problem as `robots_<code>` / `ucp_<code>` / `acp_<code>`; a plain non-200 status on a
well-known path (a merchant that publishes nothing answers 404) is not an error. A response body,
an endpoint, a header or a Location is never printed: every string in a row is the domain, a
constant of this module, or a short token that matched a strict pattern (a version date, a
reverse-DNS name, a transport word).

A host whose name does not resolve is `unresolvable`; one that resolves to a non-public address is
`not_public`. Neither is retried on another host.

The two parsers never raise, whatever they are given; a per-domain failure never stops the run.
Profile shapes read: UCP 2026-04-08 and 2026-08-25 (the fields and fallbacks follow the gateway's
`parseMerchantProfile`), ACP discovery 2026-04-17.

KNOWN LIMITS, deliberately left (review of 2026-11-10):
  * `--from-directory` is the raw content of the two tables, wider than the listed directory (no
    rig, quarantine or operator exclusion is applied); rows carry `d`, so the reader filters.
  * a name lookup that hangs is abandoned after 5 s but its thread is not; many at once would slow
    later lookups into `timeout` rows.
  * `Crawl-delay` is not read (the run's own pace is one request per second, a handful per host),
    and requests are not signed.
  * the address that was checked is not pinned for the connection; https with certificate
    verification leaves a rebound name a connect-time probe, not a read.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import socket
import sys
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import httpx

from services.agent_card_issuance import resolves_only_public, validate_merchant_domain
from services.crawl_identity import DECLARED_USER_AGENT

FLAG = "MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED"

UCP_PATH = "/.well-known/ucp"
ACP_PATH = "/.well-known/acp.json"
ROBOTS_PATH = "/robots.txt"
_PATHS = frozenset({ROBOTS_PATH, UCP_PATH, ACP_PATH})

MAX_DOMAINS = 300
MAX_BODY_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
MIN_SECONDS_BETWEEN_REQUESTS = 1.0
MAX_DOMAINS_FILE_BYTES = 1024 * 1024
DEFAULT_DEADLINE_SECONDS = 1500.0
MAX_REQUESTS_PER_DOMAIN = 6  # three paths, and at most one redirect hop on each
MAX_ROW_LIST = 32
MAX_ROBOTS_PATTERN_CHARS = 512
MAX_ROBOTS_PATTERN_STARS = 16
MAX_ROBOTS_RULES = 5000

USER_AGENT = DECLARED_USER_AGENT
# The product token robots.txt groups are matched on: the name inside the declared User-Agent. The
# whole User-Agent starts with "Mozilla", which is not a name any robots.txt addresses this crawler by.
_ROBOTS_TOKEN_MATCH = re.search(r"compatible;\s*([A-Za-z][A-Za-z0-9_-]*)/", USER_AGENT)
ROBOTS_TOKEN = _ROBOTS_TOKEN_MATCH.group(1) if _ROBOTS_TOKEN_MATCH else USER_AGENT
# `Accept-Encoding: identity`: the body cap is then a cap on the bytes that arrive, and a small
# compressed body cannot expand past it. A server that compresses anyway is recorded, not decoded.
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "identity"}
_ROBOTS_HEADERS = {**_HEADERS, "Accept": "text/plain"}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_READ_CHUNK_BYTES = 16 * 1024

ROW_PREFIX = "UCPPROF_ROW "
PLAN_PREFIX = "UCPPROF_PLAN "
SUM_PREFIX = "UCPPROF_SUM "
DONE_LINE = "UCPPROF_DONE"
DISABLED_LINE = f"UCPPROF_DISABLED {FLAG} is off; nothing was read"
REFUSED_PREFIX = "UCPPROF_REFUSED "

ROBOTS_OK = "ok"
ROBOTS_ABSENT = "absent"
ROBOTS_DISALLOWED = "disallowed"
ROBOTS_UNAVAILABLE = "unavailable"
CODE_ROBOTS_DISALLOWED = "robots_disallowed"
CODE_ROBOTS_UNAVAILABLE = "robots_unavailable"
CODE_DEADLINE = "deadline"
CODE_UNRESOLVABLE = "unresolvable"
CODE_NOT_PUBLIC = "not_public"

# The directory's population: the same two tables services/agent_merchant_directory reads. Module
# constants handed straight to `database.fetch_all`, so tests/test_repo_sql_prepare_postgres.py
# plans them. SELECT only.
_DIRECTORY_TIERB_DOMAINS_SQL = """
SELECT DISTINCT shop_domain FROM tierb_cart_link_eligibility
"""

_DIRECTORY_PURCHASABILITY_DOMAINS_SQL = """
SELECT DISTINCT merchant_domain FROM merchant_purchasability
"""

DIRECTORY_READ_TIMEOUT_SECONDS = 30.0

EXIT_OK = 0
EXIT_REFUSED = 2

CAP_CHECKOUT = "dev.ucp.shopping.checkout"
CAP_ORDER = "dev.ucp.shopping.order"
CAP_DISCOUNT = "dev.ucp.shopping.discount"
NETWORK_TOKEN = "network_token"

_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRANSPORT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_TLD_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_MAX_ENTRIES = 256


def is_enabled() -> bool:
    return str(os.getenv(FLAG) or "").strip().lower() in ("1", "true", "on", "yes")


# ── input ────────────────────────────────────────────────────────────────────────────────────


def normalize_domain(raw: Any) -> Optional[str]:
    """A bare public hostname, lower-cased and without a leading `www.`, or None.

    Anything carrying a scheme, a path, a port, userinfo, a query or an IP literal is refused, not
    repaired: a census of the wrong host is worse than no row."""
    if not isinstance(raw, str):
        return None
    d = raw.strip().lower()
    if d.startswith("www."):
        d = d[4:]
    # Equality, not truthiness: the guard's hostname pattern admits no scheme, slash, colon, `@`,
    # query or space, and anything it would have tidied (a trailing dot) is refused here.
    if validate_merchant_domain(d) != d:
        return None
    # `validate_merchant_domain` refuses a dotted-quad; a short numeric form (127.1) is refused
    # here, by its last label, instead of being left for the resolver.
    if not _TLD_RE.fullmatch(d.rsplit(".", 1)[-1]):
        return None
    return d


def split_entries(domains_arg: Optional[str], file_text: Optional[str]) -> List[str]:
    entries: List[str] = []
    for part in (domains_arg or "").split(","):
        if part.strip():
            entries.append(part.strip())
    for line in (file_text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def plan_domains(entries: Sequence[str]) -> List[Optional[str]]:
    """Each entry normalized, in order, duplicates dropped. None stands for an entry that was
    refused (it gets a row of its own, without echoing what was typed)."""
    planned: List[Optional[str]] = []
    seen = set()
    for entry in entries:
        d = normalize_domain(entry)
        if d is None:
            planned.append(None)
        elif d not in seen:
            seen.add(d)
            planned.append(d)
    return planned


def well_known_url(domain: str, path: str) -> str:
    """THE ONLY URL BUILDER. A validated bare hostname and one of the three path constants."""
    if path not in _PATHS:
        raise ValueError("path is not one of the three constants")
    if not isinstance(domain, str) or validate_merchant_domain(domain) != domain:
        raise ValueError("not a bare public hostname")
    return f"https://{domain}{path}"


def redirect_sibling(host: str, location: str, path: str) -> Optional[str]:
    """The ONE redirect that is followed: apex <-> www of the host just asked, over https, on the
    default port, to the same well-known path. Returns that sibling host, or None to refuse.

    The apex/www pair is always one registrable domain, and needs no public-suffix list to say so
    (a suffix list that misses a registry would call two unrelated shops one domain). This decides
    shape only: the caller rebuilds the URL from the returned host and the path constant, so a
    Location's query, fragment, userinfo and port never reach a request, and it runs both address
    guards on the host before asking it."""
    if not location or not isinstance(location, str):
        return None
    try:
        parsed = urlsplit(location)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.path != path or port not in (None, 443):
        return None
    target = (parsed.hostname or "").lower().rstrip(".")
    sibling = host[4:] if host.startswith("www.") else f"www.{host}"
    return sibling if target == sibling else None


# ── parsing (never raises) ───────────────────────────────────────────────────────────────────


def _bounded_keys(value: Any) -> List[str]:
    if not isinstance(value, dict):
        return []
    return [k for k in itertools.islice(value, _MAX_ENTRIES) if isinstance(k, str)]


def _bounded_list(value: Any) -> List[Any]:
    return value[:_MAX_ENTRIES] if isinstance(value, list) else []


def _entries_of(value: Any) -> List[Any]:
    """A registry value: the spec's array of entries, or one entry on its own."""
    if isinstance(value, list):
        return _bounded_list(value)
    return [value] if isinstance(value, dict) else []


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else None


def _name(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _NAME_RE.fullmatch(value) else None


def _version(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _VERSION_RE.fullmatch(value) else None


def _load_json(doc: Any) -> Tuple[Any, Optional[str]]:
    if isinstance(doc, (bytes, bytearray, str)):
        if len(doc) > MAX_BODY_BYTES:
            return None, "too_large"
        try:
            return json.loads(doc), None
        except (ValueError, RecursionError):  # ValueError covers JSONDecodeError and bad bytes
            return None, "not_json"
    return doc, None


def _credential_types(instrument: Any) -> List[str]:
    """Credential families an available instrument names for the request: the `type` constants
    under `ucp.request_constraints.properties.credential`, directly or in its anyOf/oneOf."""
    constraints = _get(_get(instrument, "ucp"), "request_constraints")
    found: List[str] = []
    for constraint in _entries_of(constraints):
        credential = _get(_get(constraint, "properties"), "credential")
        branches = [credential] + _bounded_list(_get(credential, "anyOf")) + _bounded_list(_get(credential, "oneOf"))
        for branch in branches:
            type_rule = _get(_get(branch, "properties"), "type")
            for value in [_get(type_rule, "const")] + _bounded_list(_get(type_rule, "enum")):
                if _name(value) and value not in found:
                    found.append(value)
    return found


def _read_ucp_profile(doc: Any) -> Dict[str, Any]:
    doc, problem = _load_json(doc)
    if problem:
        return {"ok": False, "reason": problem}
    if not isinstance(doc, dict):
        return {"ok": False, "reason": "not_an_object"}
    # Both published lines nest the protocol metadata under `ucp`; a flat document is read from
    # the top level.
    ucp = doc["ucp"] if isinstance(doc.get("ucp"), dict) else doc
    raw_version = ucp.get("version")
    if not (isinstance(raw_version, str) and raw_version.strip()):
        raw_version = doc.get("ucp_version")
    if not (isinstance(raw_version, str) and raw_version.strip()):
        return {"ok": False, "reason": "version_missing"}
    version = _version(raw_version.strip())
    if not version:
        return {"ok": False, "reason": "version_invalid"}

    # 2026-08-25 maps each older version to its profile URI; a plain list is read too.
    raw_supported = ucp.get("supported_versions")
    candidates = _bounded_keys(raw_supported) if isinstance(raw_supported, dict) else _bounded_list(raw_supported)
    supported = sorted({v for v in candidates if _version(v) and v != version})

    services: List[Dict[str, Any]] = []

    def push_binding(name: Any, binding: Any) -> None:
        transport = _get(binding, "transport")
        if not isinstance(transport, str) or len(services) >= _MAX_ENTRIES:
            return
        transport = transport.strip().lower()
        if _TRANSPORT_RE.fullmatch(transport):
            services.append({"name": _name(name), "transport": transport})

    raw_services = ucp.get("services")
    if isinstance(raw_services, list):
        for binding in _bounded_list(raw_services):
            push_binding(_get(binding, "name") or _get(binding, "id"), binding)
    else:
        for name in _bounded_keys(raw_services):
            for binding in _entries_of(raw_services[name]):
                push_binding(name, binding)

    capabilities: List[str] = []

    def push_capability(name: Any) -> None:
        if _name(name) and name not in capabilities:
            capabilities.append(name)

    raw_capabilities = ucp.get("capabilities")
    if isinstance(raw_capabilities, list):
        for entry in _bounded_list(raw_capabilities):
            push_capability(entry if isinstance(entry, str) else (_get(entry, "name") or _get(entry, "id")))
    else:
        # A key whose value is null, false or an empty list (no version) is not advertised.
        for name in _bounded_keys(raw_capabilities):
            value = raw_capabilities[name]
            if value is not None and value is not False and value != []:
                push_capability(name)

    # Handlers: id and declared instrument / credential types ONLY. A handler's `config` (merchant
    # account ids, tokenisation endpoints) is never read.
    handlers: Dict[str, List[str]] = {}

    def push_handler(registry_name: Any, entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        key = _name(entry.get("id")) or _name(registry_name)
        if not key or (key not in handlers and len(handlers) >= _MAX_ENTRIES):
            return
        types = handlers.setdefault(key, [])
        for instrument in _bounded_list(entry.get("available_instruments")):
            for value in [_get(instrument, "type")] + _credential_types(instrument):
                if _name(value) and value not in types:
                    types.append(value)

    raw_handlers = ucp.get("payment_handlers")
    if isinstance(raw_handlers, list):
        for entry in _bounded_list(raw_handlers):
            push_handler(_get(entry, "name"), entry)
    else:
        for name in _bounded_keys(raw_handlers):
            for entry in _entries_of(raw_handlers[name]):
                push_handler(name, entry)

    # Signing keys: top-level `keys[]` (2026-08-25), else `signing_keys[]` beside `ucp`
    # (2026-04-08), else inside it. Counted, never printed.
    key_list = next(
        (c for c in (doc.get("keys"), doc.get("signing_keys"), ucp.get("signing_keys")) if isinstance(c, list)), []
    )
    keys = sum(
        1
        for k in _bounded_list(key_list)
        if isinstance(_get(k, "kid"), str) and _get(k, "kid") and isinstance(_get(k, "kty"), str) and _get(k, "kty")
    )

    return {
        "ok": True,
        "ver": version,
        "supported": supported,
        "svc": services,
        "cap": capabilities,
        "ph": handlers,
        "keys": keys,
    }


def parse_ucp_profile(doc: Any) -> Dict[str, Any]:
    """Facts from a `/.well-known/ucp` document (parsed, or its JSON text/bytes).

    NEVER RAISES: anything unreadable is `{"ok": False, "reason": <code>}`."""
    try:
        return _read_ucp_profile(doc)
    except Exception:  # noqa: BLE001 - the contract is that no input can stop the run
        return {"ok": False, "reason": "unreadable"}


def _read_acp_discovery(doc: Any) -> Dict[str, Any]:
    doc, problem = _load_json(doc)
    if problem:
        return {"ok": False, "reason": problem}
    if not isinstance(doc, dict):
        return {"ok": False, "reason": "not_an_object"}
    protocol = doc.get("protocol")
    raw_version = _get(protocol, "version")
    if not (isinstance(raw_version, str) and raw_version.strip()):
        return {"ok": False, "reason": "version_missing"}
    version = _version(raw_version.strip())
    if not version:
        return {"ok": False, "reason": "version_invalid"}
    capabilities = doc.get("capabilities")
    names: List[str] = []
    for value in _bounded_list(_get(capabilities, "services")):
        if _name(value) and value not in names:
            names.append(value)
    for extension in _bounded_list(_get(capabilities, "extensions")):
        value = extension if isinstance(extension, str) else _get(extension, "name")
        if _name(value) and value not in names:
            names.append(value)
    return {"ok": True, "ver": version, "cap": names}


def parse_acp_discovery(doc: Any) -> Dict[str, Any]:
    """Facts from a `/.well-known/acp.json` document. NEVER RAISES."""
    try:
        return _read_acp_discovery(doc)
    except Exception:  # noqa: BLE001 - as above
        return {"ok": False, "reason": "unreadable"}


# ── fetching ─────────────────────────────────────────────────────────────────────────────────


class Pacer:
    """At most one request per `interval` seconds across the run. Clock and sleep are injected."""

    def __init__(
        self,
        interval: float = MIN_SECONDS_BETWEEN_REQUESTS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: Optional[float] = None

    async def wait(self) -> None:
        if self._last is not None:
            remaining = self._last + self._interval - self._clock()
            if remaining > 0:
                await self._sleep(remaining)
        self._last = self._clock()


def _looks_like_json(content_type: str, body: bytes) -> bool:
    return "json" in content_type.lower() or body.lstrip()[:1] == b"{"


async def _get_capped(
    client: httpx.AsyncClient, url: str, *, text: bool = False
) -> Tuple[Any, Optional[str], Optional[bytes]]:
    """One GET. Returns (status or error code, Location for a redirect, body for a 200). A 200
    body must look like JSON unless `text` (robots.txt) is asked for."""
    client.cookies.clear()  # no cookie is ever sent, a redirect hop included
    async with client.stream("GET", url, headers=_ROBOTS_HEADERS if text else _HEADERS) as response:
        status = response.status_code
        if status in _REDIRECT_STATUSES:
            return status, response.headers.get("location") or "", None
        if status != 200:
            return status, None, None
        encoding = (response.headers.get("content-encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            return "encoded_body", None, None
        declared = response.headers.get("content-length") or ""
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return "too_large", None, None
        body = bytearray()
        async for chunk in response.aiter_raw(_READ_CHUNK_BYTES):
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return "too_large", None, None  # leaving the block closes the stream unread
        if not text and not _looks_like_json(response.headers.get("content-type") or "", bytes(body)):
            return "not_json", None, None
        return 200, None, bytes(body)


class Fetched(NamedTuple):
    """What one path answered: the HTTP status or a short code, the body (a 200 within the cap
    only), and the host that gave the answer (the sibling, after the one redirect)."""

    code: Any
    body: Optional[bytes]
    host: Optional[str]


class Deadline:
    """The run's own time limit, on the injected clock."""

    def __init__(self, seconds: float, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._at = clock() + seconds

    def passed(self) -> bool:
        return self._clock() >= self._at


def _default_resolver(host: str) -> Any:
    """True when `resolves_only_public` admits the host. Otherwise WHY it did not: the guard answers
    False both for a name that does not resolve and for one that resolves inside, so a refused
    name is looked up once more, only to tell the two apart. The guard alone decides."""
    if resolves_only_public(host) is True:
        return True
    try:
        answers = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return CODE_UNRESOLVABLE
    return False if answers else CODE_UNRESOLVABLE


async def fetch_well_known(
    client: httpx.AsyncClient,
    domain: str,
    path: str,
    *,
    resolver: Callable[[str], Any],
    pacer: Pacer,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    deadline: Optional[Deadline] = None,
) -> Fetched:
    """Ask `path` on `domain`, following the one permitted redirect. The body is present only for a
    200 within the size cap (and, for the two well-known paths, only when it is JSON). Never
    raises. `resolver` answers True for a public host, "unresolvable", or anything else for a
    refusal."""
    host = domain
    for _attempt in ("first", "one-hop"):
        try:
            # THE DEADLINE, before the name is resolved or the pace is waited for.
            if deadline is not None and deadline.passed():
                return Fetched(CODE_DEADLINE, None, None)
            # BOTH guards, before every request, the redirect target included.
            if validate_merchant_domain(host) != host:
                return Fetched("invalid_domain", None, None)
            answer = await asyncio.wait_for(asyncio.to_thread(resolver, host), timeout)
            if answer is not True:
                return Fetched(CODE_UNRESOLVABLE if answer == CODE_UNRESOLVABLE else CODE_NOT_PUBLIC, None, None)
            url = well_known_url(host, path)
            await pacer.wait()
            # ...and again right before the request: the wait above may have crossed it.
            if deadline is not None and deadline.passed():
                return Fetched(CODE_DEADLINE, None, None)
            status, location, body = await asyncio.wait_for(
                _get_capped(client, url, text=path == ROBOTS_PATH), timeout
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return Fetched("timeout", None, None)
        except httpx.HTTPError:
            return Fetched("http_error", None, None)
        except Exception:  # noqa: BLE001 - one domain's failure never stops the run
            return Fetched("fetch_failed", None, None)
        if status not in _REDIRECT_STATUSES:
            return Fetched(status, body, host)
        sibling = redirect_sibling(host, location or "", path)
        if not sibling:
            return Fetched("redirect_refused", None, None)
        host = sibling
    return Fetched("redirect_refused", None, None)  # the one hop was spent on another redirect


# ── robots.txt (RFC 9309, the part two fixed paths need) ─────────────────────────────────────


class _TooManyRules(Exception):
    pass


def glob_matches(pattern: str, path: str) -> bool:
    """Does a robots path pattern match `path`? `*` is any run of characters, a trailing `$` ties
    the pattern to the end of the path, anything else is literal (percent-encoding is compared as
    written), and without `$` the pattern is a prefix.

    Two pointers and one remembered `*`: at most len(pattern) x len(path) steps, never the
    exponential search a regular expression built from a run of `*` falls into."""
    if pattern.endswith("$"):
        pattern = pattern[:-1]
    elif not pattern.endswith("*"):
        pattern += "*"
    p = t = 0
    star, mark = -1, 0
    while t < len(path):
        if p < len(pattern) and pattern[p] == "*":
            star, mark = p, t
            p += 1
        elif p < len(pattern) and pattern[p] == path[t]:
            p += 1
            t += 1
        elif star >= 0:
            mark += 1
            p, t = star + 1, mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _robots_rules(text: str) -> List[Tuple[bool, str]]:
    """The (allow, pattern) rules that apply to this crawler: those of every group naming its
    token, else those of every `*` group. One pass over the lines."""
    token = ROBOTS_TOKEN.lower()
    ours: List[Tuple[bool, str]] = []
    everyone: List[Tuple[bool, str]] = []
    named = False
    agents: set = set()
    listing_agents = False
    for line in text.splitlines():
        field, colon, value = line.split("#", 1)[0].partition(":")
        if not colon:
            continue
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not listing_agents:
                agents, listing_agents = set(), True
            agents.add(value.split("/", 1)[0].strip().lower())
            named = named or token in agents
        elif field in ("allow", "disallow"):
            listing_agents = False
            if not value:
                continue  # an empty pattern matches nothing
            for wanted, rules in ((token, ours), ("*", everyone)):
                if wanted in agents:
                    rules.append((field == "allow", value))
            if len(ours) + len(everyone) > MAX_ROBOTS_RULES:
                raise _TooManyRules()
    return ours if named else everyone


def robots_allows(rules: Sequence[Tuple[bool, str]], path: str) -> bool:
    """Longest matching pattern wins; Allow wins a tie; no matching rule allows."""
    best: Optional[Tuple[int, bool]] = None
    for allow, raw in rules:
        if len(raw) > MAX_ROBOTS_PATTERN_CHARS and "*" not in raw:
            too_large = True
        else:
            pattern = re.sub(r"\*+", "*", raw)  # a run of `*` means what one does
            too_large = len(pattern) > MAX_ROBOTS_PATTERN_CHARS or pattern.count("*") > MAX_ROBOTS_PATTERN_STARS
        if too_large:
            # Not evaluated. FAIL CLOSED: as a Disallow it disallows, as an Allow it is not there.
            if allow:
                continue
            return False
        if glob_matches(pattern, path):
            candidate = (len(pattern), allow)
            if best is None or candidate > best:
                best = candidate
    return best is None or best[1]


def read_robots(domain: str, fetched: Sequence[Any]) -> Tuple[str, Dict[str, bool], Optional[str]]:
    """What a fetched robots.txt says about the two well-known paths.

    Returns (state, {path: allowed}, why). `why` is the short code behind an `unavailable`.
    FAIL CLOSED: only a 404/410, an empty file or a file that was read can allow a path. Never
    raises, and nothing here fetches: the text is all it is given."""
    nothing = {UCP_PATH: False, ACP_PATH: False}
    everything = {UCP_PATH: True, ACP_PATH: True}
    try:
        status, body = fetched[0], fetched[1]
        if status in (404, 410):
            return ROBOTS_ABSENT, everything, None
        if status != 200 or not isinstance(body, (bytes, bytearray)):
            return ROBOTS_UNAVAILABLE, nothing, str(status)
        try:
            text = bytes(body).decode("utf-8-sig")
        except UnicodeDecodeError:
            return ROBOTS_UNAVAILABLE, nothing, "undecodable"
        if not text.strip():
            return ROBOTS_ABSENT, everything, None
        try:
            rules = _robots_rules(text)
        except _TooManyRules:
            return ROBOTS_UNAVAILABLE, nothing, "too_many_rules"
        allowed = {path: robots_allows(rules, path) for path in nothing}
        return (ROBOTS_OK if all(allowed.values()) else ROBOTS_DISALLOWED), allowed, None
    except Exception:  # noqa: BLE001 - a robots file this cannot read allows nothing
        return ROBOTS_UNAVAILABLE, nothing, "unparseable"


# ── rows ─────────────────────────────────────────────────────────────────────────────────────


def empty_row(domain: Optional[str]) -> Dict[str, Any]:
    return {
        "d": domain, "robots": None, "ucp": None, "ver": None, "supported": [], "svc": [], "cap": [], "ph": {}, "keys": 0,
        "acp": None, "acp_ver": None, "acp_cap": [], "err": None,
    }  # fmt: skip


def build_row(
    domain: str,
    ucp: Sequence[Any],
    acp: Sequence[Any],
    robots: Optional[str] = ROBOTS_ABSENT,
    robots_why: Optional[str] = None,
) -> Dict[str, Any]:
    """One domain's row. A path the deadline kept from being asked stays null, what was read
    before it is kept (it is true), and `err` is then `deadline` whatever else went wrong."""
    row = empty_row(domain)
    row["robots"] = robots
    errors: List[str] = [f"robots_{robots_why}"] if robots == ROBOTS_UNAVAILABLE else []
    out_of_time = CODE_DEADLINE in (ucp[0], acp[0])
    row["ucp"], ucp_body = (None, None) if ucp[0] == CODE_DEADLINE else (ucp[0], ucp[1])
    if isinstance(row["ucp"], str):
        errors.append(f"ucp_{row['ucp']}")
    elif row["ucp"] == 200:
        facts = parse_ucp_profile(ucp_body)
        if facts["ok"]:
            row.update({k: facts[k] for k in ("ver", "supported", "svc", "cap", "ph", "keys")})
        else:
            errors.append(f"ucp_{facts['reason']}")
    row["acp"], acp_body = (None, None) if acp[0] == CODE_DEADLINE else (acp[0], acp[1])
    if isinstance(row["acp"], str):
        errors.append(f"acp_{row['acp']}")
    elif row["acp"] == 200:
        facts = parse_acp_discovery(acp_body)
        if facts["ok"]:
            row["acp_ver"], row["acp_cap"] = facts["ver"], facts["cap"]
        else:
            errors.append(f"acp_{facts['reason']}")
    row["err"] = CODE_DEADLINE if out_of_time else (errors[0] if errors else None)
    return row


def printable_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """The row as printed: every list cut at MAX_ROW_LIST entries, with `truncated` set when a cut
    was made. The totals are counted on the uncut row."""
    cut = False
    printed = dict(row)
    for key in ("supported", "svc", "cap", "acp_cap"):
        if len(row[key]) > MAX_ROW_LIST:
            printed[key], cut = row[key][:MAX_ROW_LIST], True
    handlers = dict(itertools.islice(row["ph"].items(), MAX_ROW_LIST))
    cut = cut or len(row["ph"]) > MAX_ROW_LIST or any(len(types) > MAX_ROW_LIST for types in handlers.values())
    printed["ph"] = {name: types[:MAX_ROW_LIST] for name, types in handlers.items()}
    if cut:
        printed["truncated"] = True
    return printed


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    totals = {
        "domains": 0, "ucp_ok": 0, "with_checkout": 0, "with_order": 0, "with_discount": 0,
        "handlers_accepting_network_token": 0, "acp_ok": 0, "errors": 0,
        "robots_ok": 0, "robots_absent": 0, "robots_disallowed": 0, "robots_unavailable": 0, "deadline": 0,
    }  # fmt: skip
    for row in rows:
        totals["domains"] += 1
        totals["ucp_ok"] += 1 if row["ucp"] == 200 and row["ver"] else 0
        totals["with_checkout"] += 1 if CAP_CHECKOUT in row["cap"] else 0
        totals["with_order"] += 1 if CAP_ORDER in row["cap"] else 0
        totals["with_discount"] += 1 if CAP_DISCOUNT in row["cap"] else 0
        totals["handlers_accepting_network_token"] += sum(1 for types in row["ph"].values() if NETWORK_TOKEN in types)
        totals["acp_ok"] += 1 if row["acp"] == 200 and row["acp_ver"] else 0
        totals["errors"] += 1 if row["err"] else 0
        totals["deadline"] += 1 if row["err"] == CODE_DEADLINE else 0
        if f"robots_{row['robots']}" in totals:
            totals[f"robots_{row['robots']}"] += 1
    return totals


def _line(prefix: str, payload: Dict[str, Any]) -> str:
    return prefix + json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


# ── the run ──────────────────────────────────────────────────────────────────────────────────


def _plain(number: float) -> Any:
    return int(number) if float(number).is_integer() else number


async def run(
    planned: Sequence[Optional[str]],
    *,
    out: Callable[[str], None],
    transport: Optional[httpx.AsyncBaseTransport] = None,
    resolver: Optional[Callable[[str], Any]] = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> Dict[str, int]:
    """Census the planned domains (see `plan_domains`) and print the plan, the rows, the totals and
    the done line through `out`. `transport`, `resolver`, `clock` and `sleep` are test seams; left
    alone, the resolver is `resolves_only_public` (through `_default_resolver`) and the transport
    is httpx's own."""
    resolve = resolver or _default_resolver
    pacer = Pacer(clock=clock, sleep=sleep)
    rows: List[Dict[str, Any]] = []
    out(_line(PLAN_PREFIX, {
        "domains": len(planned),
        "max_requests": MAX_REQUESTS_PER_DOMAIN * sum(1 for d in planned if d is not None),
        "pace_seconds": _plain(MIN_SECONDS_BETWEEN_REQUESTS),
        "deadline_seconds": _plain(deadline_seconds),
    }))
    deadline = Deadline(deadline_seconds, clock)
    # trust_env=False: no proxy and no .netrc credential from the environment reaches a request.
    async with httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        for domain in planned:
            try:
                if domain is None:
                    row = empty_row(None)
                    row["err"] = "invalid_domain"
                else:
                    ask = {"resolver": resolve, "pacer": pacer, "timeout": timeout, "deadline": deadline}
                    # ROBOTS FIRST. A path it does not allow is never requested.
                    robots = await fetch_well_known(client, domain, ROBOTS_PATH, **ask)
                    if robots.code == CODE_DEADLINE:
                        row = build_row(domain, robots, robots, None)
                    else:
                        state, allowed, why = read_robots(domain, robots)
                        refusal = CODE_ROBOTS_UNAVAILABLE if state == ROBOTS_UNAVAILABLE else CODE_ROBOTS_DISALLOWED
                        # The host that answered robots.txt (the sibling, after the one redirect) is
                        # where the two paths are asked. It is guarded again inside each fetch.
                        host = robots.host or domain
                        fetched: Dict[str, Sequence[Any]] = {}
                        for path in (UCP_PATH, ACP_PATH):
                            if allowed[path] is True:
                                fetched[path] = await fetch_well_known(client, host, path, **ask)
                            else:
                                fetched[path] = (refusal, None)
                        row = build_row(domain, fetched[UCP_PATH], fetched[ACP_PATH], state, why)
            except Exception:  # noqa: BLE001 - a per-domain failure never stops the run
                row = empty_row(domain)
                row["err"] = "row_failed"
            rows.append(row)
            out(_line(ROW_PREFIX, printable_row(row)))
    totals = summarize(rows)
    out(_line(SUM_PREFIX, totals))
    out(DONE_LINE)
    return totals


def _print(line: str) -> None:
    print(line, flush=True)


async def read_directory_domains() -> List[str]:
    """The directory's distinct domains: two SELECTs on the process database, nothing written.

    Connects only if nothing has (a test's own connection is left alone) and disconnects what it
    connected. Each read is bounded here; the pool's own `statement_timeout`
    (DB_STATEMENT_TIMEOUT_SECONDS) applies as well when the job sets it."""
    from db.database import database  # noqa: PLC0415 - only this population needs the database
    from utils.database_readiness import connect_database_with_timeout  # noqa: PLC0415

    connected_here = not database.is_connected
    if connected_here:
        await connect_database_with_timeout(DIRECTORY_READ_TIMEOUT_SECONDS, db=database)
    try:
        tierb = await asyncio.wait_for(
            database.fetch_all(_DIRECTORY_TIERB_DOMAINS_SQL), DIRECTORY_READ_TIMEOUT_SECONDS
        )
        purchasability = await asyncio.wait_for(
            database.fetch_all(_DIRECTORY_PURCHASABILITY_DOMAINS_SQL), DIRECTORY_READ_TIMEOUT_SECONDS
        )
    finally:
        if connected_here:
            await database.disconnect()
    values = [dict(row).get("shop_domain") for row in tierb]
    values += [dict(row).get("merchant_domain") for row in purchasability]
    return [v for v in values if isinstance(v, str) and v.strip()]


def plan_directory(values: Sequence[str]) -> List[Optional[str]]:
    """Directory values normalized, de-duplicated and sorted. A stored value that is not a bare
    public hostname keeps a row of its own (None), after the sorted domains."""
    planned = plan_domains(values)
    domains = sorted(d for d in planned if d is not None)
    return domains + [None] * (len(planned) - len(domains))


async def amain(args: Any, *, out: Callable[[str], None], **seams: Any) -> int:
    if args.from_directory:
        if args.domains.strip() or args.domains_file:
            out(f"{REFUSED_PREFIX}conflicting_sources --from-directory cannot be combined with --domains or --domains-file")
            return EXIT_REFUSED
        try:
            planned = plan_directory(await read_directory_domains())
        except Exception as exc:  # noqa: BLE001 - mapped to a line and an exit code, never a traceback
            out(f"{REFUSED_PREFIX}directory_unreadable error_type={type(exc).__name__}")
            return EXIT_REFUSED
    else:
        file_text: Optional[str] = None
        if args.domains_file:
            try:
                with open(args.domains_file, "rb") as handle:
                    raw = handle.read(MAX_DOMAINS_FILE_BYTES + 1)
                if len(raw) > MAX_DOMAINS_FILE_BYTES:
                    raise ValueError("domains file too large")
                file_text = raw.decode("utf-8")
            except (OSError, ValueError):
                out(f"{REFUSED_PREFIX}domains_file_unreadable")
                return EXIT_REFUSED
        planned = plan_domains(split_entries(args.domains, file_text))
    if not planned:
        out(f"{REFUSED_PREFIX}no_domains")
        return EXIT_REFUSED
    if len(planned) > MAX_DOMAINS:
        out(f"{REFUSED_PREFIX}too_many_domains n={len(planned)} max={MAX_DOMAINS}; nothing was read")
        return EXIT_REFUSED
    if not 0 < args.deadline_seconds <= 86400:
        out(f"{REFUSED_PREFIX}bad_deadline --deadline-seconds must be above 0 and at most 86400")
        return EXIT_REFUSED
    await run(planned, out=out, deadline_seconds=args.deadline_seconds, **seams)
    return EXIT_OK


def main(argv: Optional[List[str]] = None, *, out: Callable[[str], None] = _print, **seams: Any) -> int:
    # THE DIAL COMES FIRST: off means no argument parsing, no file, no database, no name lookup,
    # no socket.
    if not is_enabled():
        out(DISABLED_LINE)
        return EXIT_OK

    import argparse  # noqa: PLC0415 - only the CLI needs it

    parser = argparse.ArgumentParser(
        prog="python -m jobs.merchant_protocol_profile_census",
        description="Read each domain's robots.txt, /.well-known/ucp and /.well-known/acp.json once and print "
        f"what it publishes. Read-only. At most {MAX_DOMAINS} domains per run.",
    )
    parser.add_argument("--domains", default="", help="comma-separated bare hostnames")
    parser.add_argument("--domains-file", default="", help="a file with one bare hostname per line")
    parser.add_argument("--from-directory", action="store_true",
                        help="the merchant directory's domains, read from the database (not with --domains*)")
    parser.add_argument("--deadline-seconds", type=float, default=DEFAULT_DEADLINE_SECONDS,
                        help="stop sending requests this long after the crawl starts (default %(default)s)")
    args = parser.parse_args(argv)

    # httpx logs every request URL at INFO; this job's own lines are the whole output. The levels are
    # put back afterwards: a caller in the same process (a test run, another job) keeps its own.
    quieted = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    for name in quieted:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        return asyncio.run(amain(args, out=out, **seams))
    finally:
        for name, level in quieted.items():
            logging.getLogger(name).setLevel(level)


if __name__ == "__main__":
    sys.exit(main())
