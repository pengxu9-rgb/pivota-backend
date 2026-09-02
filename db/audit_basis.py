"""A3 — the run-level audit basis: what a run was measured WITH (db layer).

services/prompt_basis.py already pins the PROMPT SET, which was the loudest
source of non-comparability. It does not pin the model behind each provider, the
tier mix of the selected questions, the official-domain set that decides
`first_party` on every cited host, or the primary-destination selection rule.
Any of those can move a headline number between two runs with no change in the
world, so a before/after diff that does not check them can report a config
change as merchant movement.

This module records all of it, once per run, and answers the one question a diff
must ask first: :func:`bases_are_comparable`.

See db/migrations/208_audit_basis.sql for the measurement rationale per column.

TWO INVARIANTS LIVE HERE RATHER THAN IN THE CALLER
--------------------------------------------------

* **INSERT-ONLY.** :func:`record_basis` for a run that already has a basis is a
  NO-OP that returns the STORED row — never an update. A basis that can be
  rewritten after the fact is not a basis: it would let a later deploy
  retroactively make two runs look comparable. `uq_audit_basis_run` is what
  makes this hold under a concurrent retry, not merely the read-then-write
  below.

* **COMPARABILITY IS CONSERVATIVE.** :func:`bases_are_comparable` answers True
  only on a positive match of every component it checks. A missing basis, a
  missing component, or anything it cannot compare answers False — because the
  cost of a wrong True (a rule change reported to a merchant as their own
  movement) is much higher than the cost of a wrong False (a real move labelled
  "basis changed, not comparable").

Best-effort accessors (DB failures log + return None/empty) + an inline DDL
backstop — the db/merchant_official_domains.py + db/audit_evidence.py pattern.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

from sqlalchemy import Column, DateTime, Index, Integer, Table, Text, UniqueConstraint, text as sa_text

from db._ddl_guard import apply_ddl_statements
from db.database import database, metadata

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- vocabulary

# The methodology version. Bump on ANY component change — a new selection rule,
# a changed prompt generator, a different provider default, a reworked tier
# budget. Deliberately ONE coarse value: a diff needs a single question ("same
# methodology?"), and a per-component version set invites a caller to check
# three of four.
#
# v1: first cut. Components at this version — prompt_basis.PROMPT_BASIS_VERSION
# = 3, primary_destination.PRIMARY_DESTINATION_VERSION = 1, official-domain set
# from migration 207.
METHODOLOGY_VERSION = "1"

# The fields whose equality defines comparability. Named as data, not as a chain
# of `and`s, so the set is greppable and a reviewer can COUNT the conjuncts
# rather than reason about them.
COMPARABILITY_FIELDS: Sequence[str] = (
    "methodology_version",
    "providers_and_models",
    "primary_destination_version",
    "prompt_set_id",
    "selected_set_id",
    # Review: these four were RECORDED and then never consulted, which left the
    # check blind to the two config edits most likely to move the headline.
    # official_domains decides first_party on every cited host, so adding one
    # moves "AI sends buyers to your own store" with no merchant behaviour
    # change at all; migration 208's own comment says two runs with the same
    # prompt_set_id but a different tier mix are not measuring the same thing;
    # and market/language change which SERP was probed.
    "official_domains",
    "tier_mix",
    "market",
    "language",
)


# --------------------------------------------------------------------------- model

audit_basis = Table(
    "audit_basis",
    metadata,
    Column("basis_id", Text, primary_key=True, nullable=False),
    Column("audit_run_id", Text, nullable=False),
    Column("merchant_id", Text, nullable=False),
    Column("methodology_version", Text, nullable=False),
    # TEXT, not JSON/JSONB: written once, read back whole, never queried
    # inside. That keeps the DDL byte-identical on both engines (so the
    # hermetic tests exercise the REAL table) and keeps this table out of the
    # json/jsonb model-vs-migration drift class entirely. See migration 208.
    Column("providers_and_models", Text, nullable=False, server_default=sa_text("'{}'")),
    Column("prompt_set_id", Text, nullable=True),
    Column("selected_set_id", Text, nullable=True),
    Column("tier_mix", Text, nullable=False, server_default=sa_text("'{}'")),
    Column("official_domains", Text, nullable=False, server_default=sa_text("'[]'")),
    Column("primary_destination_version", Integer, nullable=True),
    Column("market", Text, nullable=True),
    Column("language", Text, nullable=True),
    Column("currency", Text, nullable=True),
    # server_default mirrors migration 208's DEFAULT CURRENT_TIMESTAMP so the
    # create_all path and the migration path produce the SAME shape. record_basis
    # sets it explicitly anyway — the default is the backstop.
    Column(
        "created_at", DateTime(timezone=True), nullable=False,
        server_default=sa_text("CURRENT_TIMESTAMP"),
    ),
    # Mirrors migration 208 verbatim. If you change one, change both.
    UniqueConstraint("audit_run_id", name="uq_audit_basis_run"),
    Index("idx_audit_basis_merchant", "merchant_id", "created_at"),
    extend_existing=True,
)


# --------------------------------------------------------------------------- DDL backstop

# Byte-for-byte the shape migration 208 creates (minus its COMMENT ON lines,
# which are Postgres-only metadata and change no shape). Deliberately portable
# to SQLite too — CURRENT_TIMESTAMP rather than NOW(), TEXT rather than JSONB —
# so the hermetic test suite exercises the REAL table instead of a hand-written
# fixture that is free to be laxer than production.
_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS audit_basis (
        basis_id                   TEXT NOT NULL,
        audit_run_id               TEXT NOT NULL,
        merchant_id                TEXT NOT NULL,
        methodology_version        TEXT NOT NULL,
        providers_and_models       TEXT NOT NULL DEFAULT '{}',
        prompt_set_id              TEXT NULL,
        selected_set_id            TEXT NULL,
        tier_mix                   TEXT NOT NULL DEFAULT '{}',
        official_domains           TEXT NOT NULL DEFAULT '[]',
        primary_destination_version INTEGER NULL,
        market                     TEXT NULL,
        language                   TEXT NULL,
        currency                   TEXT NULL,
        created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (basis_id),
        CONSTRAINT uq_audit_basis_run UNIQUE (audit_run_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_basis_merchant "
    "ON audit_basis (merchant_id, created_at);",
]

_DDL_LABEL = "ensure_audit_basis_table"
_DDL_READY = False


async def ensure_audit_basis_table() -> None:
    """Idempotent schema backstop for envs where migration 208 has not run.

    Memoization goes through db/_ddl_guard so a pass that left a statement
    outstanding genuinely retries instead of marking the module ready with the
    object still missing.
    """
    global _DDL_READY
    if _DDL_READY:
        return
    try:
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label=_DDL_LABEL,
            logger=logger,
            execute=database.execute,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("%s failed: %s", _DDL_LABEL, str(exc)[:200])


def reset_ddl_ready_for_tests() -> None:
    """Test hook: forget the memoized pass so a fresh DB gets its DDL again."""
    global _DDL_READY
    from db._ddl_guard import reset_ddl_state

    _DDL_READY = False
    reset_ddl_state(_DDL_LABEL)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- SQL

# INSERT-ONLY. `ON CONFLICT (audit_run_id) DO NOTHING` is the immutability
# guarantee at the database rather than in Python: two concurrent completions of
# the same run race here, and the loser writes nothing instead of overwriting a
# recorded basis. The clause is valid on BOTH engines (Postgres and SQLite share
# this syntax), and as a module-level constant it is collected by
# tests/test_repo_sql_prepare_postgres.py, so it is PREPARE-checked there.
#
# There is deliberately no DO UPDATE arm anywhere in this module.
INSERT_BASIS_SQL = """
INSERT INTO audit_basis (
    basis_id, audit_run_id, merchant_id, methodology_version,
    providers_and_models, prompt_set_id, selected_set_id, tier_mix,
    official_domains, primary_destination_version,
    market, language, currency, created_at
) VALUES (
    :basis_id, :audit_run_id, :merchant_id, :methodology_version,
    :providers_and_models, :prompt_set_id, :selected_set_id, :tier_mix,
    :official_domains, :primary_destination_version,
    :market, :language, :currency, :created_at
)
ON CONFLICT (audit_run_id) DO NOTHING
"""

SELECT_BASIS_FOR_RUN_SQL = """
SELECT basis_id, audit_run_id, merchant_id, methodology_version,
       providers_and_models, prompt_set_id, selected_set_id, tier_mix,
       official_domains, primary_destination_version,
       market, language, currency, created_at
  FROM audit_basis
 WHERE audit_run_id = :audit_run_id
"""


# --------------------------------------------------------------------------- codecs

def _dump_json(value: Any, *, fallback: str) -> str:
    """Serialize a JSON document for storage. A value that will not serialize
    is stored as the empty document rather than crashing the write: the basis is
    a best-effort telemetry row, and a half-recorded basis is still worth more
    than none — but note that an empty component makes the run NON-comparable,
    which is the safe direction."""
    if value is None:
        return fallback
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.warning("audit_basis: undumpable JSON component: %s", str(exc)[:200])
        return fallback


def _load_json(value: Any, *, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return loaded


def _decode_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["providers_and_models"] = _load_json(out.get("providers_and_models"), fallback={})
    out["tier_mix"] = _load_json(out.get("tier_mix"), fallback={})
    out["official_domains"] = _load_json(out.get("official_domains"), fallback=[])
    return out


# --------------------------------------------------------------------------- accessors

async def get_basis_for_run(audit_run_id: str) -> Optional[Dict[str, Any]]:
    """The recorded basis for one run, JSON components decoded, or None.

    Best-effort: returns None on any DB error, never raises. A caller that gets
    None must treat the run as NON-comparable, not as "basis unchanged".
    """
    if not audit_run_id:
        return None
    await ensure_audit_basis_table()
    try:
        row = await database.fetch_one(
            SELECT_BASIS_FOR_RUN_SQL, {"audit_run_id": str(audit_run_id)}
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "get_basis_for_run failed for %s: %s", audit_run_id, str(exc)[:200]
        )
        return None
    return _decode_row(row) if row is not None else None


async def record_basis(
    *,
    audit_run_id: str,
    merchant_id: str,
    methodology_version: str = METHODOLOGY_VERSION,
    providers_and_models: Optional[Mapping[str, Any]] = None,
    prompt_set_id: Optional[str] = None,
    selected_set_id: Optional[str] = None,
    tier_mix: Optional[Mapping[str, Any]] = None,
    official_domains: Optional[Sequence[str]] = None,
    primary_destination_version: Optional[int] = None,
    market: Optional[str] = None,
    language: Optional[str] = None,
    currency: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Record what this run was measured with. INSERT-ONLY.

    Returns the basis row for the run — the one just written, or the one that
    was ALREADY there. A second call for the same `audit_run_id` is a NO-OP: it
    does not update, and it does not fail; it hands back the stored row. That is
    what makes the basis immutable, and it is why the audit worker can call this
    on every completion path (including a reclaim after a crash) without
    special-casing.

    Best-effort: returns None on any DB error, never raises, so it cannot take
    down the audit lifecycle.
    """
    if not audit_run_id or not merchant_id:
        return None
    await ensure_audit_basis_table()

    # Read first so a repeat call costs one SELECT instead of a doomed INSERT,
    # and so the returned row is the STORED one. The ON CONFLICT below is what
    # actually enforces immutability — this check is the fast path, not the
    # guarantee, because two callers can both read "absent" here.
    existing = await get_basis_for_run(audit_run_id)
    if existing is not None:
        return existing

    params = {
        "basis_id": uuid.uuid4().hex,
        "audit_run_id": str(audit_run_id),
        "merchant_id": str(merchant_id),
        "methodology_version": str(methodology_version),
        "providers_and_models": _dump_json(
            dict(providers_and_models or {}), fallback="{}"
        ),
        "prompt_set_id": str(prompt_set_id) if prompt_set_id else None,
        "selected_set_id": str(selected_set_id) if selected_set_id else None,
        "tier_mix": _dump_json(dict(tier_mix or {}), fallback="{}"),
        "official_domains": _dump_json(
            sorted({str(d).strip().lower() for d in (official_domains or ()) if d}),
            fallback="[]",
        ),
        "primary_destination_version": (
            None if primary_destination_version is None
            else int(primary_destination_version)
        ),
        "market": str(market).strip() or None if market else None,
        "language": str(language).strip() or None if language else None,
        "currency": str(currency).strip().upper() or None if currency else None,
        "created_at": now or _now_utc(),
    }
    try:
        await database.execute(INSERT_BASIS_SQL, params)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "record_basis failed for run=%s: %s", audit_run_id, str(exc)[:200]
        )
        return None
    # Re-read rather than echoing `params`: on the concurrent-loser path the
    # INSERT wrote nothing, and returning our own payload would report a basis
    # that is not in the database.
    return await get_basis_for_run(audit_run_id)


# --------------------------------------------------------------------------- comparability

def _normalized_component(field: str, basis: Mapping[str, Any]) -> Any:
    """One comparability component, in a form two rows can be compared in.

    `providers_and_models` is decoded (a row read straight from the DB carries
    the JSON string; a row from :func:`record_basis` carries the dict) and its
    per-provider payload is reduced to the two fields that define the
    measurement — model_id and temperature. Anything else a caller stapled on
    (a display name, a default_model) must NOT make two runs non-comparable.
    """
    value = basis.get(field)
    if field != "providers_and_models":
        return value
    decoded = _load_json(value, fallback={})
    if not isinstance(decoded, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for provider, payload in decoded.items():
        key = str(provider or "").strip().lower()
        if not key:
            continue
        if isinstance(payload, Mapping):
            out[key] = (
                str(payload.get("model_id") or "") or None,
                payload.get("temperature"),
            )
        else:
            # A caller that stored a bare model string still compares.
            out[key] = (str(payload or "") or None, None)
    return out


# Components whose ABSENCE cannot support a comparability claim. market/
# language/currency are excluded: a legitimately unset locale is a real,
# equal state on both sides, not missing evidence.
_EVIDENCE_REQUIRED_FIELDS = frozenset({
    "methodology_version", "providers_and_models", "primary_destination_version",
})


def bases_are_comparable(
    a: Optional[Mapping[str, Any]],
    b: Optional[Mapping[str, Any]],
) -> bool:
    """Can a before/after diff claim MOVEMENT between these two runs?

    True only when every field in :data:`COMPARABILITY_FIELDS` matches:
    `methodology_version`, `providers_and_models`, `primary_destination_version`,
    the pinned prompt/selected set ids, `official_domains`, `tier_mix`, and
    `market`/`language`. Anything else — a missing basis, a None on one side
    only, an undecodable component, or a missing component on BOTH sides for a
    field in :data:`_EVIDENCE_REQUIRED_FIELDS` — answers False.

    Conservative by design. A wrong True tells a merchant that a model swap or a
    selection-rule change was their own movement; a wrong False only says "we
    changed how we measure, so this isn't a like-for-like comparison", which is
    both true and useful.

    NOTE FOR THE CALLER: services/audit_delta.py is where this belongs. Its
    `_measurement_basis` currently decides comparability from the prompt set
    ALONE (`_prompt_set_id`) and hands the verdict to `build_reaudit_delta`,
    which uses it to pick between MATERIAL_SCORE_DELTA (15) and
    MATERIAL_SCORE_DELTA_SAME_BASIS (5) — so today a model swap can tighten the
    noise mask to 5 points and then report a 6-point swing as movement. The
    fix is to AND this function into `_measurement_basis`'s `same`, which
    requires audit_delta to receive the two runs' basis rows; that plumbing is
    deliberately NOT part of this change.
    """
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return False
    for field in COMPARABILITY_FIELDS:
        left = _normalized_component(field, a)
        right = _normalized_component(field, b)
        if left != right:
            return False
        # Review: two bases that are BOTH missing a component used to compare
        # equal and answer True, so a report that lost its provider block got a
        # green comparability light on zero evidence — the docstring already
        # promised False. An empty/undecodable component is not evidence that
        # two runs were measured the same way; it is the absence of evidence.
        if field in _EVIDENCE_REQUIRED_FIELDS and not left:
            return False
    return True


__all__: Sequence[str] = (
    "COMPARABILITY_FIELDS",
    "METHODOLOGY_VERSION",
    "audit_basis",
    "bases_are_comparable",
    "ensure_audit_basis_table",
    "get_basis_for_run",
    "record_basis",
    "reset_ddl_ready_for_tests",
)
