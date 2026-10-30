"""Reap rail STAGING pre-flight: count, and (with --apply) scrub, live production rows in STAGING.

Staging's database is a RESTORED COPY of production. Every Reap purchase that was live in
production when the copy was taken is live in staging too, with the buyer's email, address and
Reap approval link, and an armed staging poller would claim it and talk to Reap about a real
buyer. docs/runbooks/reap_agentic_purchase.md ("Staging pre-flight") requires this BEFORE
REAP_AGENTIC_ENABLED=1 on staging, and again after EVERY staging restore.

    census          read-only: non-terminal purchases and pending/active enrollments.
                    exit 0 = CLEAR, 3 = STOP (live rows present).
    scrub           dry run: the census, plus what --apply would change. Writes nothing.
    scrub --apply   terminalise and scrub, BOTH UPDATEs in ONE transaction, then re-count.
                    Prints row counts only.

THE TARGET CHECK RUNS FIRST AND ABORTS (exit 2) BEFORE ANY QUERY THAT READS OR WRITES REAP ROWS
unless ALL of these hold — any doubt is an abort:
    * PIVOTA_ENV is exactly `staging` (no whitespace, no case-folding);
    * the DATABASE_URL names ONE host, exactly staging's private IP (EXPECTED_HOST) — prod's is
      10.25.0.2 — and carries no host / hostaddr / service query parameter; the connection is
      then made with host=EXPECTED_HOST (and the URL's port) explicitly, so the URL cannot
      redirect it;
    * the server answers current_database() with exactly staging's database name (EXPECTED_DB).
The server's own inet_server_addr() is PRINTED, not compared: what Cloud SQL reports there for a
private-IP connection is not verified, and a check that might never pass is a check operators
learn to edit out. An unreadable server identity still aborts.
scripts/ops/run_oneoff_job.sh DEFAULTS TO PRODUCTION (PROJECT=pivota-prod, prod's DATABASE_URL,
PIVOTA_ENV=production). An operator who drops the PROJECT/ENV_VARS overrides lands here with
prod's URL and prod's env, and this check is what stops a scrub from refusing every live
production purchase — including ones mid-payment.

Self-contained on purpose (stdlib + asyncpg, no repo imports, and no at-sign anywhere in this file): it is
run as an inline program through run_oneoff_job.sh, before it is in any deployed image:

    PROJECT=pivota-staging \\
    ENV_VARS=PIVOTA_ENV=staging,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600 \\
    SECRETS=DATABASE_URL=<staging worker's DATABASE_URL secret>:latest \\
      scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/reap_staging_preflight.py)" census

Never prints the URL, a row id, an email or an address — host, database name and counts only.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlparse

#: Staging's Cloud SQL private IP and database name (infra/gcp/README.md). NOT overridable from
#: the command line or the environment: a knob here is a way to point the scrub at production.
EXPECTED_HOST = "10.122.0.3"
EXPECTED_DB = "pivota"
EXPECTED_ENV = "staging"

TERMINAL = ["completed", "failed", "refused", "expired"]

EXIT_OK = 0
EXIT_ABORT = 2
EXIT_STOP = 3

_CENSUS_PURCHASES = (
    "SELECT state, count(*) AS n, count(buyer_email) AS with_email, "
    "count(hosted_url) AS with_link "
    "FROM reap_agentic_purchases WHERE state <> ALL($1::text[]) GROUP BY state ORDER BY state"
)
_CENSUS_ENROLLMENTS = (
    "SELECT status, count(*) AS n FROM reap_agentic_enrollments "
    "WHERE status IN ('pending', 'active') GROUP BY status ORDER BY status"
)

# The terminal transition's scrub (db/reap_agentic_ledger.py `_TRANSITION_SQL`): PII, offer code
# and claim nulled, terminal_at stamped — plus the hosted approval link, which for a restored row
# is a LIVE Reap approval URL for a real production checkout.
_SCRUB_PURCHASES = (
    "UPDATE reap_agentic_purchases "
    "SET state = 'refused', last_error_code = 'staging_preflight_scrub', "
    "shipping_address = NULL, buyer_email = NULL, offer_code = NULL, "
    "hosted_url = NULL, hosted_url_expires_at = NULL, "
    "claimed_by = NULL, claimed_at = NULL, next_poll_at = NULL, "
    "terminal_at = clock_timestamp(), state_entered_at = clock_timestamp(), "
    "updated_at = clock_timestamp() "
    "WHERE state <> ALL($1::text[])"
)
_SCRUB_ENROLLMENTS = (
    "UPDATE reap_agentic_enrollments "
    "SET status = 'dead', hosted_url = NULL, hosted_url_expires_at = NULL, updated_at = now() "
    "WHERE status IN ('pending', 'active')"
)


def _rowcount(status: str) -> int:
    """asyncpg's execute() returns the command tag, e.g. 'UPDATE 3'."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return -1


#: libpq/asyncpg query parameters that can point a connection somewhere other than the URL's
#: host. Any of them present is an abort, whatever its value.
_HOST_OVERRIDE_QUERY_KEYS = frozenset({"host", "hostaddr", "service"})


def static_problems(
    url: str, env: Mapping[str, str], *, expected_host: str = EXPECTED_HOST,
) -> List[str]:
    """Reasons to abort BEFORE connecting at all. Empty list = connect (to `expected_host`)."""
    problems: List[str] = []
    # EXACT: no strip, no case-folding. ' staging', 'staging ', 'STAGING', 'staging2' and
    # 'staging-old' are all refused. run_oneoff_job.sh passes ENV_VARS verbatim, so the only way
    # to a near-miss is someone typing one, and a near-miss is not a statement that this is staging.
    pivota_env = env.get("PIVOTA_ENV") or ""
    if pivota_env != EXPECTED_ENV:
        problems.append(f"PIVOTA_ENV is {pivota_env!r}, not {EXPECTED_ENV!r}")
    parsed = urlparse(url)
    # The host part only (after any userinfo), so a comma in a password is not misread.
    hostinfo = parsed.netloc.rpartition(chr(64))[2]
    if "," in hostinfo:
        problems.append("DATABASE_URL names more than one host")
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    overrides = sorted(query_keys & _HOST_OVERRIDE_QUERY_KEYS)
    if overrides:
        problems.append(f"DATABASE_URL query sets {', '.join(overrides)}")
    url_host = (parsed.hostname or "").lower()
    if url_host != expected_host:
        problems.append(f"DATABASE_URL host is {url_host!r}, not {expected_host!r}")
    try:
        parsed.port
    except ValueError:
        problems.append("DATABASE_URL port is not a number")
    return problems


async def server_problems(conn: Any, *, expected_db: str = EXPECTED_DB) -> List[str]:
    """Reasons the database we reached is not staging's. Empty list = proceed."""
    problems: List[str] = []
    try:
        database = await conn.fetchval("SELECT current_database()")
    except Exception as exc:  # noqa: BLE001 — cannot tell where we are: that is an abort
        problems.append(f"could not read current_database() ({type(exc).__name__})")
        return problems
    if database != expected_db:
        problems.append(f"current_database() is {database!r}, not {expected_db!r}")
    return problems


async def census(conn: Any) -> Dict[str, Any]:
    purchases = await conn.fetch(_CENSUS_PURCHASES, TERMINAL)
    enrollments = await conn.fetch(_CENSUS_ENROLLMENTS)
    return {
        "purchases": [(r["state"], r["n"], r["with_email"], r["with_link"]) for r in purchases],
        "enrollments": [(r["status"], r["n"]) for r in enrollments],
    }


def _print_census(result: Mapping[str, Any], out: Callable[[str], None]) -> bool:
    for state, n, with_email, with_link in result["purchases"]:
        out(f"non-terminal purchase {state} {n} with_email {with_email} with_link {with_link}")
    for status, n in result["enrollments"]:
        out(f"live enrollment {status} {n}")
    live = bool(result["purchases"] or result["enrollments"])
    out("STOP: live rows present" if live else
        "CLEAR: no non-terminal purchase, no pending/active enrollment")
    return live


async def scrub(conn: Any) -> Dict[str, int]:
    """Both UPDATEs in ONE transaction: either every live row is scrubbed or none is."""
    async with conn.transaction():
        purchases = _rowcount(await conn.execute(_SCRUB_PURCHASES, TERMINAL))
        enrollments = _rowcount(await conn.execute(_SCRUB_ENROLLMENTS))
    return {"purchases_scrubbed": purchases, "enrollments_scrubbed": enrollments}


async def main(
    argv: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
    *,
    connect: Optional[Callable[..., Awaitable[Any]]] = None,
    expected_host: str = EXPECTED_HOST,
    expected_db: str = EXPECTED_DB,
    out: Callable[[str], None] = print,
) -> int:
    """`expected_host` / `expected_db` exist for tests only; nothing on the command line or in
    the environment reaches them."""
    env = os.environ if env is None else env
    args = list(argv)
    if args[:1] not in (["census"], ["scrub"]) or len(args) > 2 or (
        len(args) == 2 and args != ["scrub", "--apply"]
    ):
        out("usage: census | scrub [--apply]")
        return EXIT_ABORT
    apply = args == ["scrub", "--apply"]

    url = (env.get("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        out("ABORT: DATABASE_URL is not set")
        return EXIT_ABORT
    parsed = urlparse(url)
    out(f"db host {parsed.hostname} db name {parsed.path.lstrip('/')}")

    problems = static_problems(url, env, expected_host=expected_host)
    if problems:
        for problem in problems:
            out(f"ABORT: {problem}")
        out("ABORT: this is not staging's database; nothing was read or written")
        return EXIT_ABORT

    if connect is None:
        import asyncpg

        connect = asyncpg.connect
    # host= EXPLICITLY, not only via the URL: asyncpg lets keyword arguments override the DSN, so
    # nothing in the URL can steer this connection anywhere but the host checked above. port= goes
    # with it, and that is not decoration: measured on asyncpg 0.31, a `host=` keyword alone
    # DROPS the DSN's port and falls back to 5432 (or PGPORT), i.e. possibly a different server.
    conn = await connect(url, host=expected_host, port=parsed.port or 5432)
    try:
        problems = await server_problems(conn, expected_db=expected_db)
        if problems:
            for problem in problems:
                out(f"ABORT: {problem}")
            out("ABORT: this is not staging's database; nothing was read or written")
            return EXIT_ABORT

        try:
            server_addr = await conn.fetchval("SELECT host(inet_server_addr())")
        except Exception:  # noqa: BLE001 — informational only
            server_addr = None
        out(f"server address {server_addr} current_database {expected_db}")

        if not apply:
            # Read-only for the whole session: census and dry run cannot write by construction.
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        before = await census(conn)
        live = _print_census(before, out)
        if not apply:
            if args[0] == "scrub" and live:
                out("dry run: re-run with `scrub --apply` to scrub the rows above")
            return EXIT_STOP if live else EXIT_OK
        if not live:
            out("nothing to scrub")
            return EXIT_OK
        counts = await scrub(conn)
        out(f"scrubbed purchases {counts['purchases_scrubbed']} "
            f"enrollments {counts['enrollments_scrubbed']}")
        return EXIT_STOP if _print_census(await census(conn), out) else EXIT_OK
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
