"""One atomic read-modify-write for a store's credential blob, for any platform.

`merchant_stores.api_key` is a single cell that, once a platform's telemetry is
provisioned, holds several unrelated secrets and pieces of derived state at
once — the merchant's API token, a site/website binding, a webhook secret Pivota
may hold the ONLY copy of, the ids of webhooks it installed, and a
reconciliation cursor. Every writer therefore has to preserve what it was not
asked to change.

Preserving is not enough on its own, which is the whole reason this module
exists rather than each platform hand-rolling `read -> merge -> write`:

* **read-modify-write without a transaction is a LOST UPDATE.** A sweep
  persisting its cursor between a provisioning call's read and its write erases
  the webhook secret that call just minted — and a secret shown once, or a URL
  secret already installed at the platform, cannot be recovered; after that
  every delivery 401s. The reverse interleaving reverts a reconnect to the
  credential the merchant just replaced. So the whole cycle runs inside
  `database.transaction()` and, on Postgres, behind `SELECT ... FOR UPDATE` on
  the store row. The row lock is what makes a concurrent merge WAIT instead of
  reading a value that is about to be stale.
* **there must be exactly ONE critical section per cell.** Two hand-copied
  read-modify-writes over one cell are two chances to interleave, and a race
  proof written against one of them says nothing about the other. So connect,
  provisioning and the sweep all call THIS function; the `mutate` callback is
  how a caller expresses "and drop these keys" inside the same lock rather than
  opening a second one.

On SQLite there is no `FOR UPDATE` and the select is plain. That is tolerable
only because SQLite here is tests and local development, and it is why the
serialization claim is pinned in the Postgres gates
(`tests/test_squarespace_ledger_postgres.py`,
`tests/test_webflow_ledger_postgres.py`) rather than in the SQLite suite, which
cannot observe it.

The re-read at the end is not belt-and-braces: `databases` + asyncpg reports no
rowcount from an UPDATE, so reading the row back is the only proof the write
landed — and, under a race, the only way a caller learns whether ITS write won.

This module was generalized out of `services/squarespace_connection.py`; the
Squarespace helper is now a thin delegate with identical behaviour.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional


def parse_store_credentials(raw: Any, *, bare_key: str = "api_key") -> Dict[str, Any]:
    """The credential JSON out of `merchant_stores.api_key`, as a dict.

    A bare string is read as ``{<bare_key>: <string>}`` so a row written by some
    other path — every platform's column held a plain key before its telemetry
    existed — is not silently treated as credential-less. Unparseable JSON is
    read the same way rather than raising: a merge that blew up on a malformed
    cell could never repair it.

    ``bare_key`` is the name of that platform's OWN credential field, because a
    string read into the wrong key is a credential the read path cannot see —
    indistinguishable, from every caller's point of view, from no credential at
    all. Squarespace's is ``api_key``; Webflow's is ``api_token``.
    """
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value:
        return {}
    if not value.startswith("{"):
        return {bare_key: value}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {bare_key: value}
    return dict(parsed) if isinstance(parsed, dict) else {bare_key: value}


def serialize_store_credentials(credentials: Dict[str, Any]) -> str:
    return json.dumps(credentials, separators=(",", ":"))


async def merge_store_credentials(
    *,
    store_id: str,
    updates: Optional[Dict[str, Any]] = None,
    mutate: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    mark_connected: bool = False,
    db: Any = None,
    parse: Optional[Callable[[Any], Dict[str, Any]]] = None,
    serialize: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> Dict[str, Any]:
    """Read-modify-write one store's credential blob ATOMICALLY, then re-read it.

    Never an overwrite.

    ``mutate`` receives the LOCKED blob and returns the one to persist; it runs
    inside the critical section, which is how a connect expresses "drop the keys
    that belonged to the old site" without opening a second one. ``updates`` is
    then merged over the result, so an explicit update always wins over the
    mutate's view. ``mark_connected`` folds the row's connect bookkeeping into
    the SAME statement, so a reconnect is one write rather than a merge racing an
    UPDATE.

    ``parse``/``serialize`` default to this module's JSON blob codec; a platform
    passes its own only if it has a different on-disk shape.
    """
    from db.database import IS_POSTGRES
    from db.database import database as default_database

    decode = parse or parse_store_credentials
    encode = serialize or serialize_store_credentials
    handle = db if db is not None else default_database
    select_sql = "SELECT api_key FROM merchant_stores WHERE store_id = :store_id"
    # The row lock is the whole point of the transaction: without it two merges
    # both read the pre-write blob and the second silently discards the first.
    locking_sql = f"{select_sql} FOR UPDATE" if IS_POSTGRES else select_sql
    assignments = "api_key = :api_key"
    if mark_connected:
        assignments += (
            ", status = 'active', last_sync = CURRENT_TIMESTAMP,"
            " connected_at = CURRENT_TIMESTAMP"
        )

    async with handle.transaction():
        row = await handle.fetch_one(locking_sql, {"store_id": store_id})
        credentials = decode(dict(row).get("api_key") if row else None)
        if mutate is not None:
            credentials = dict(mutate(credentials))
        if updates:
            credentials.update(updates)
        await handle.execute(
            f"UPDATE merchant_stores SET {assignments} WHERE store_id = :store_id",
            {"store_id": store_id, "api_key": encode(credentials)},
        )
        persisted = await handle.fetch_one(select_sql, {"store_id": store_id})
    return decode(dict(persisted).get("api_key") if persisted else None)
