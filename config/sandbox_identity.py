"""The partner sandbox's OWN identity: its project and its database. The single source (infra Q13: a
project rename or a new DB range is one edit here).

Read by every sandbox job that WRITES the sandbox database (schema apply, catalog import, the
service-agent registration). The startup guard (config/sandbox_guard.py) refuses prod and staging
names; this module answers the positive question: is this database the sandbox's?

WHERE asyncpg REALLY CONNECTS is decided by asyncpg's own DSN parser, not by reading the URL here.
A hand-rolled read got it wrong: `postgresql://u:p@203.0.113.7/db?host=/cloudsql/pivota-sandbox:...`
connects to 203.0.113.7 (the netloc wins; `?host=` is used only when the netloc has none), and
`?service=` or PGHOST can supply a host the URL never shows. Every address the parser returns must be
a sandbox address. If asyncpg's private parser changes shape, the answer is a refusal, never a pass.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import List, Mapping, Optional, Tuple, Union

#: The sandbox's GCP project.
SANDBOX_PROJECT = "pivota-sandbox"
#: The one-way drop between prod and the sandbox (catalog copy and schema dump). Pinned: the jobs refuse
#: any other bucket. scripts/sandbox/dump_prod_schema.sh carries the same name (a test holds them equal).
SANDBOX_DROP_BUCKET = "pivota-sandbox-catalog-drop"
#: The sandbox Cloud SQL private range (sandbox-infra pins it), as (prefix length, sha256 of the masked
#: network string str(ipaddress.ip_network("<addr>/<prefix>", strict=False))). The repo is public, so
#: the range is not written here; an address is in it when its own masked network hashes the same.
SANDBOX_DB_NETWORKS_SHA256 = ((20, "447f3aded69332bf20f941cf9f6eae4edcac48e2988f89b1da7a23d806a6404a"),)
#: The sandbox Cloud SQL Auth Proxy socket directory prefix.
SANDBOX_SQL_SOCKET_PREFIX = "/cloudsql/%s:" % SANDBOX_PROJECT
#: The WHOLE socket path asyncpg will open: /cloudsql/<project>:<region>:<instance>/.s.PGSQL.<port>.
#: Exact shape, not a prefix: `/cloudsql/pivota-sandbox:x/../../<anything>` passes a prefix test and
#: opens any socket on the host (found while checking the controller's traversal question).
_SANDBOX_SOCKET_RE = re.compile(r"^/cloudsql/%s:[a-z0-9-]+:[a-z0-9-]+/\.s\.PGSQL\.[0-9]{1,5}\Z" % re.escape(SANDBOX_PROJECT))  # \Z: `$` would allow a trailing newline

Address = Union[str, Tuple[str, int]]


def database_addresses(dsn: str) -> List[Address]:
    """The addresses asyncpg would connect to for ``dsn``: (host, port) tuples or unix socket paths.

    Raises ValueError when the DSN cannot be parsed or asyncpg's parser is not the shape this
    module was written against (asyncpg is pinned in requirements.txt)."""
    from asyncpg import connect_utils

    try:
        addrs, _params = connect_utils._parse_connect_dsn_and_args(
            dsn=dsn, host=None, port=None, user=None, password=None, passfile=None, database=None,
            ssl=None, service=None, servicefile=None, direct_tls=None, server_settings=None,
            target_session_attrs=None, krbsrvname=None, gsslib=None,
        )
    except TypeError as exc:  # the private signature moved: refuse, do not guess
        raise ValueError("asyncpg's DSN parser changed shape") from exc
    except Exception as exc:  # noqa: BLE001 - an unparseable DSN is a refusal
        raise ValueError("DATABASE_URL could not be parsed") from exc
    return list(addrs)


def _address_ok(addr: Address) -> bool:
    if isinstance(addr, str):  # a unix socket path, e.g. /cloudsql/pivota-sandbox:us-west1:pg/.s.PGSQL.5432
        return bool(_SANDBOX_SOCKET_RE.match(addr))
    host = addr[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a hostname resolves to anything; only an address in the range is the sandbox's
    return any(_masked_sha256(ip, prefix) == digest for prefix, digest in SANDBOX_DB_NETWORKS_SHA256)


def _masked_sha256(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address], prefix: int) -> str:
    try:
        network = ipaddress.ip_network("%s/%d" % (ip, prefix), strict=False)
    except ValueError:  # a prefix longer than this address family allows: not the range
        return ""
    return hashlib.sha256(str(network).encode("ascii")).hexdigest()


def sandbox_database_refusal(env: Mapping[str, str]) -> Optional[str]:
    """Why DATABASE_URL / GOOGLE_CLOUD_PROJECT do not name the sandbox's own database, or None.
    Never quotes the DSN (it carries a password)."""
    project = (env.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    if project and project != SANDBOX_PROJECT:
        return "GOOGLE_CLOUD_PROJECT is not %s" % SANDBOX_PROJECT
    dsn = (env.get("DATABASE_URL") or "").strip().replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        return "DATABASE_URL names no host"
    try:
        addrs = database_addresses(dsn)
    except ValueError as exc:
        return str(exc)
    if not addrs:
        return "DATABASE_URL names no host"
    for addr in addrs:
        if not _address_ok(addr):
            if isinstance(addr, str):
                return "DATABASE_URL's socket is not the sandbox's Cloud SQL instance"
            return "DATABASE_URL's host is not an address in the sandbox's private range"
    return None
