"""sha256 pins for the infrastructure identity the reviewed ops jobs must find.

The reviewed refresh and expansion jobs refuse to run anywhere but the database, Shopify seller and
runtime service account that were reviewed. Each job reads the real value from what it already
has (its DATABASE_URL, the connected server, the storefront response, the runtime metadata
server), hashes it and compares the digest with a constant below. The raw values are not kept in
the repository and are never printed; a mismatch reports the observed digest only.

The constants are sha256 hex digests of the UTF-8 value exactly as the job reads it: a database
host as `urlsplit(DATABASE_URL).hostname` and `host(inet_server_addr())`, a database name as the
DSN path without its leading `/` and `current_database()`, a shop id as `str(shop["id"])`, and a
service account as the runtime metadata email.
"""

from hashlib import sha256
import hmac


PROD_DB_HOST_SHA256 = "4bffbd8da0d2a6414d0b853c39a53edc9b4afab5f48bc34402ec123de51b9549"
PROD_DB_NAME_SHA256 = "6825e22219175a4e481c7aab725a7c6ecf2c63c9e5d58be7ac59dc06d6d9db20"
STAGING_DB_HOST_SHA256 = "988a54ac13e5d710586eaa86a5af573f643438607cb950bf8ac58209b7ba1eda"
STAGING_DB_NAME_SHA256 = "41d9da66c504337cabf6674cc1fa7219bfe16da0ca75374991a445329261bca9"
PROD_SERVICE_ACCOUNT_SHA256 = "d9aec4d7e173ad89d1ea5b9bb19efb23768470d0a0e8406b6afad0dcd55572a6"
STAGING_SERVICE_ACCOUNT_SHA256 = "fdca635913f1b663fe3746a5be854bc713d403d2f61be51094c882a209bce33c"
COHORT_B_SHOPIFY_SHOP_ID_SHA256 = "6d675f57cb039a8c6eb6c52d6b499a0d34cd964c7db5dfa85a16c7dbd0a98118"


class PinMismatch(ValueError):
    """An observed identity does not hash to its reviewed pin. The job holds; nothing is written."""


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def pinned(value, expected_sha256: str) -> bool:
    return isinstance(value, str) and bool(value) and hmac.compare_digest(digest(value), expected_sha256)


def require_pins(**checks: tuple) -> None:
    """Each keyword is `name=(observed value, expected sha256)`. Raise listing digests only."""
    mismatched = []
    for name, (value, expected) in checks.items():
        if not pinned(value, expected):
            observed = digest(value) if isinstance(value, str) and value else "missing"
            mismatched.append(f"{name} sha256={observed}")
    if mismatched:
        raise PinMismatch("reviewed identity pin mismatch: " + ", ".join(mismatched))


def dsn_database_name(path: str) -> str | None:
    """The database name in a DSN path: exactly one leading `/`, nothing else stripped."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    return path[1:] or None


def require_dsn_pins(dsn, *, host_sha256: str, name_sha256: str):
    """Pin the host and database a job's own DATABASE_URL names. Returns (host, name) once pinned."""
    host, name = dsn.hostname, dsn_database_name(dsn.path)
    require_pins(db_host=(host, host_sha256), db_name=(name, name_sha256))
    return host, name


_SERVER_IDENTITY_SQL = """
    SELECT host(inet_server_addr()) AS host, current_database() AS name, pg_is_in_recovery() AS replica
"""


async def require_server_pins(db, *, host_sha256: str, name_sha256: str, reason: str) -> None:
    """The connected server itself must be the pinned primary, independent of the DSN."""
    server = await db.fetch_one(_SERVER_IDENTITY_SQL)
    if not server or server["replica"] is not False:
        raise ValueError(reason)
    require_pins(server_db_host=(server["host"], host_sha256), server_db_name=(server["name"], name_sha256))
