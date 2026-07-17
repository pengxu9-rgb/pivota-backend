"""Round-trip-eliminating bulk upsert helper for the ingest paths.

The commerce-index ingest is reached from a laptop over the Railway public proxy
at ~530ms/query RTT, so the dominant cost is the NUMBER of round trips, not the
work per row. This helper collapses a stage's per-row `INSERT ... ON CONFLICT`
upserts into ONE multi-row `VALUES (...),(...),...` statement per chunk — a real
single round trip per chunk (the `databases` 0.7.x `execute_many` merely loops
`connection.execute`, so it does NOT reduce round trips; a multi-row VALUES does).

CONTRACT (matches the per-row executor it replaces, plus the founder addendum):

  1. REPLAY-ONLY, NEVER DEGRADE. When a chunk's multi-row statement raises, the
     rows are replayed through the IDENTICAL single-row `single_sql` with the
     IDENTICAL row dicts. Nothing is synthesized, defaulted, trimmed, or mutated
     to make a row insertable — a row that cannot be written as-is is dropped,
     logged, and counted (fail loud beats fabricate).

  2. ERROR CLASSIFICATION. A transport-level failure (Timeout/Connection/asyncpg
     InterfaceError etc.) retries the SAME chunk as-is before any per-row replay;
     a data/constraint failure goes straight to per-row replay so only the
     genuinely-bad rows are skipped. A data error is never blanket-retried.

  3. IDEMPOTENCY PRECONDITION. `single_sql` MUST be an `ON CONFLICT` upsert keyed
     on a deterministic natural key, so a partially-committed chunk + replay can
     never duplicate a row. `split_upsert_sql` raises if VALUES is missing; the
     caller is responsible for the ON CONFLICT target.

Rows passed in MUST be "clean" — their keys are exactly the columns the SQL
binds (the same projection the single-row path already required). A row missing
any referenced key is rejected BEFORE binding (counted + returned in
skipped_rows): binding it would silently pass NULL for the missing column, which
an `ON CONFLICT ... DO UPDATE` could then write over existing data.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Sequence, Tuple

logger = logging.getLogger("catalog_enrichment_agent.bulk_writer")

_PARAM_RE = re.compile(r":(\w+)")
DEFAULT_CHUNK_SIZE = 100

# asyncpg / driver transport errors (matched by class name so we never hard-import
# asyncpg here). These mean "the wire failed", not "this row is bad" → retry as-is.
_TRANSPORT_EXC_NAMES = frozenset({
    "InterfaceError",
    "ConnectionDoesNotExistError",
    "ConnectionDoesNotExist",
    "CannotConnectNowError",
    "TooManyConnectionsError",
    "PostgresConnectionError",
    "ConnectionFailureError",
    "ConnectionRejectionError",
    "InternalClientError",
    "ClientCannotConnectError",
})


def is_transport_error(exc: BaseException) -> bool:
    """True when `exc` (or a cause/context in its chain) is a transport failure
    that warrants retrying the same rows, rather than a data/constraint error."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    seen: set = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in _TRANSPORT_EXC_NAMES:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def split_upsert_sql(sql: str) -> Tuple[str, str, str]:
    """Split a single-row upsert into (head_including_VALUES, values_tuple,
    conflict_tail). Robust for the ingest SQLs (one VALUES, one ON CONFLICT, no
    nested occurrences, no `::` casts — they use CAST(...))."""
    head, sep, rest = sql.partition("VALUES")
    if not sep:
        raise ValueError("bulk_upsert requires an INSERT ... VALUES statement")
    values_tuple, csep, conflict = rest.partition("ON CONFLICT")
    values_tuple = values_tuple.strip()
    if not (values_tuple.startswith("(") and values_tuple.endswith(")")):
        raise ValueError("bulk_upsert could not isolate the VALUES tuple")
    if "::" in values_tuple:
        # ':param' rewriting would corrupt a '::type' cast (':type' looks like a
        # param). The ingest SQLs use CAST(... AS type) instead.
        raise ValueError("bulk_upsert VALUES tuple must use CAST(...), not '::' casts")
    conflict_tail = ("ON CONFLICT" + conflict).strip() if csep else ""
    return head.strip() + " VALUES", values_tuple, conflict_tail


def _suffix_tuple(values_tuple: str, i: int) -> str:
    return _PARAM_RE.sub(lambda m: f":{m.group(1)}__{i}", values_tuple)


async def _write_chunk_multirow(
    database: Any,
    *,
    head: str,
    values_tuple: str,
    conflict: str,
    referenced: Sequence[str],
    chunk: List[Dict[str, Any]],
    transport_retries: int,
    backoff: float,
    label: str,
) -> bool:
    """Attempt one chunk as a single multi-row statement. Returns True on success,
    False when the caller should fall back to per-row replay. Retries transport
    errors as-is; returns False (→ per-row) on data errors or exhausted retries."""
    parts: List[str] = []
    params: Dict[str, Any] = {}
    for i, row in enumerate(chunk):
        parts.append(_suffix_tuple(values_tuple, i))
        for key in referenced:
            params[f"{key}__{i}"] = row[key]
    sql = f"{head} {','.join(parts)} {conflict}".strip()

    attempt = 0
    while True:
        try:
            await database.execute(sql, params)
            return True
        except Exception as exc:  # noqa: BLE001
            if is_transport_error(exc) and attempt < transport_retries:
                attempt += 1
                logger.warning(
                    "bulk_upsert[%s] transport error on chunk of %d (retry %d/%d): %s",
                    label, len(chunk), attempt, transport_retries, str(exc)[:200],
                )
                await asyncio.sleep(backoff * attempt)
                continue
            logger.info(
                "bulk_upsert[%s] multi-row chunk failed (%s) — replaying %d rows "
                "individually: %s", label, type(exc).__name__, len(chunk), str(exc)[:200],
            )
            return False


async def _replay_rows(
    database: Any,
    *,
    single_sql: str,
    chunk: List[Dict[str, Any]],
    transport_retries: int,
    backoff: float,
    label: str,
) -> Tuple[int, int, List[Dict[str, Any]]]:
    """Per-row replay: the SAME `single_sql` + the SAME row dicts. Bad rows are
    logged and skipped (never abort). Returns (applied, skipped, skipped_rows)."""
    applied = 0
    skipped = 0
    skipped_rows: List[Dict[str, Any]] = []
    for row in chunk:
        attempt = 0
        while True:
            try:
                await database.execute(single_sql, row)
                applied += 1
                break
            except Exception as exc:  # noqa: BLE001
                if is_transport_error(exc) and attempt < transport_retries:
                    attempt += 1
                    await asyncio.sleep(backoff * attempt)
                    continue
                logger.exception(
                    "bulk_upsert[%s] row skipped (%s): %s",
                    label, type(exc).__name__, str(exc)[:200],
                )
                skipped += 1
                skipped_rows.append(row)
                break
    return applied, skipped, skipped_rows


async def bulk_upsert(
    database: Any,
    single_sql: str,
    rows: Sequence[Dict[str, Any]],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    transport_retries: int = 2,
    backoff: float = 1.0,
    label: str = "rows",
) -> Tuple[int, int, List[Dict[str, Any]]]:
    """Upsert `rows` via chunked multi-row VALUES statements built from the exact
    `single_sql` (no SQL drift — one source of truth for columns + ON CONFLICT).

    Returns (applied, skipped, skipped_rows). Bad rows are skipped and returned so
    the caller can prevent orphan children (exclude dependents of skipped parents).
    """
    row_list = list(rows or [])
    if not row_list:
        return 0, 0, []
    head, values_tuple, conflict = split_upsert_sql(single_sql)
    referenced = _PARAM_RE.findall(values_tuple)

    applied = 0
    skipped = 0
    skipped_rows: List[Dict[str, Any]] = []

    # Row-key pre-validation: never bind a row missing a referenced key — row[key]
    # would otherwise have to default the value, and a silent NULL can overwrite
    # real data through the DO UPDATE arm. Rejected rows are returned in
    # skipped_rows so callers' orphan guards see them like any other skipped row.
    valid_rows: List[Dict[str, Any]] = []
    for row in row_list:
        missing = [k for k in referenced if k not in row]
        if missing:
            row_id = (row.get("product_key") or row.get("offer_id") or row.get("id")
                      or row.get("sku_key") or row.get("merchant_id"))
            logger.error(
                "bulk_upsert[%s] row rejected pre-bind (row id=%s) — missing key(s) %s",
                label, row_id, missing,
            )
            skipped += 1
            skipped_rows.append(row)
            continue
        valid_rows.append(row)
    row_list = valid_rows
    for start in range(0, len(row_list), max(1, chunk_size)):
        chunk = row_list[start:start + max(1, chunk_size)]
        ok = await _write_chunk_multirow(
            database, head=head, values_tuple=values_tuple, conflict=conflict,
            referenced=referenced, chunk=chunk,
            transport_retries=transport_retries, backoff=backoff, label=label,
        )
        if ok:
            applied += len(chunk)
            continue
        a, s, sr = await _replay_rows(
            database, single_sql=single_sql, chunk=chunk,
            transport_retries=transport_retries, backoff=backoff, label=label,
        )
        applied += a
        skipped += s
        skipped_rows.extend(sr)
    return applied, skipped, skipped_rows
