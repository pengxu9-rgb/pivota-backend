"""Operator attestations that a domain is a brand's official store (migration 259).

An attestation is NOT proof of control: merchant_official_domains carries that (verified / asserted
come from DNS TXT or mailbox proof the brand itself completed). This table records a weaker,
explicitly named kind of evidence -- an operator reviewed independent sources and states that
`domain` is the official store of the brand whose catalog seller is `merchant_id` -- together with
who approved it, where that approval lives, and the sources reviewed.

Read ONLY by the enrichment lane's seller-type labelling (scripts/relabel_offer_seller_type.py) as the
`official_domain` input of services.offer_seller_identity.derive_offer_seller_identity. It never
widens merchant_official_domains, OFFICIAL_SOURCES or any citation tier. Rows are never deleted: a
withdrawal sets `revoked_at`, and a revoked row is never read.

DDL backstop -- the db/merchant_official_domains.py pattern: production does not auto-run
db/migrations/*.sql, so the table is created on first use; the statements below are migration 259's.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional

from db._ddl_guard import apply_ddl_statements
from db.database import database
from services.brand_claim_service import normalize_host

logger = logging.getLogger(__name__)

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS merchant_domain_attestations (
        merchant_id  TEXT NOT NULL,
        domain       TEXT NOT NULL,
        attested_by  TEXT NOT NULL,
        review_ref   TEXT NOT NULL,
        evidence     JSONB NOT NULL DEFAULT '{}'::jsonb,
        attested_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        revoked_at   TIMESTAMPTZ NULL,
        PRIMARY KEY (merchant_id, domain),
        CONSTRAINT ck_merchant_domain_attestations_domain
          CHECK (
            domain = lower(domain)
            AND domain <> ''
            AND domain LIKE '%.%'
            AND domain NOT LIKE '% %'
            AND domain NOT LIKE '%/%'
            AND domain NOT LIKE '%:%'
            AND domain NOT LIKE '%.'
            AND domain NOT LIKE 'www.%'
          ),
        CONSTRAINT ck_merchant_domain_attestations_who
          CHECK (trim(attested_by) <> '' AND trim(review_ref) <> '')
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_merchant_domain_attestations_domain "
    "ON merchant_domain_attestations (domain);",
]

_DDL_LABEL = "ensure_merchant_domain_attestations_table"
_DDL_READY = False

ACTIVE_SQL = """
SELECT merchant_id, domain FROM merchant_domain_attestations
WHERE merchant_id = ANY(:merchant_ids) AND revoked_at IS NULL
"""

# Idempotent: re-attesting an active row changes nothing; a revoked row is NOT silently revived (that
# would hide the revocation) -- the plan reports it and the caller must decide.
UPSERT_SQL = """
INSERT INTO merchant_domain_attestations (merchant_id, domain, attested_by, review_ref, evidence)
VALUES (:merchant_id, :domain, :attested_by, :review_ref, CAST(:evidence AS JSONB))
ON CONFLICT (merchant_id, domain) DO NOTHING
RETURNING merchant_id
"""

EXISTING_SQL = """
SELECT merchant_id, domain, revoked_at FROM merchant_domain_attestations
WHERE merchant_id = ANY(:merchant_ids)
"""


async def ensure_merchant_domain_attestations_table(db: Any = None) -> bool:
    global _DDL_READY
    if _DDL_READY:
        return True
    execute = (db or database).execute
    try:
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS, label=_DDL_LABEL, logger=logger, execute=execute,
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort; readers then see no attestations
        logger.warning("%s failed: %s", _DDL_LABEL, str(exc)[:200])
    return _DDL_READY


def reset_ddl_ready_for_tests() -> None:
    global _DDL_READY
    from db._ddl_guard import reset_ddl_state

    _DDL_READY = False
    reset_ddl_state(_DDL_LABEL)


def attestation_host(value: Optional[str]) -> str:
    """The stored form: lower case, no scheme/port/path, no leading www. (the table's CHECK)."""
    host = normalize_host(value)
    while host.startswith("www."):
        host = host[4:]
    return host


async def active_attested_domains(merchant_ids: Iterable[str], db: Any = None) -> Dict[str, set]:
    """merchant_id -> set of attested hosts (revoked rows excluded). Empty when the table is absent."""
    ids = sorted({str(m) for m in merchant_ids if m})
    if not ids:
        return {}
    db = db or database
    if not await ensure_merchant_domain_attestations_table(db):
        return {}
    out: Dict[str, set] = {}
    for row in await db.fetch_all(ACTIVE_SQL, {"merchant_ids": ids}):
        out.setdefault(row["merchant_id"], set()).add(row["domain"])
    return out


def validate_entries(entries: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise and check an approval list before anything is written. Raises on any bad entry."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, entry in enumerate(entries):
        merchant_id = str(entry.get("merchant_id") or "").strip()
        domain = attestation_host(entry.get("domain"))
        attested_by = str(entry.get("attested_by") or "").strip()
        review_ref = str(entry.get("review_ref") or "").strip()
        evidence = entry.get("evidence") or {}
        if not merchant_id or not domain or "." not in domain:
            raise ValueError(f"entry {i}: merchant_id and a hostname domain are required")
        if not attested_by or not review_ref:
            raise ValueError(f"entry {i}: attested_by and review_ref are required (who approved, and where)")
        if not isinstance(evidence, dict) or not evidence.get("sources"):
            raise ValueError(f"entry {i}: evidence.sources (the independent sources reviewed) is required")
        key = (merchant_id, domain)
        if key in seen:
            raise ValueError(f"entry {i}: duplicate {merchant_id} / {domain}")
        seen.add(key)
        out.append({"merchant_id": merchant_id, "domain": domain, "attested_by": attested_by,
                    "review_ref": review_ref, "evidence": evidence})
    return out


async def plan_attestations(entries: List[Dict[str, Any]], db: Any = None) -> Dict[str, Any]:
    db = db or database
    await ensure_merchant_domain_attestations_table(db)
    existing = {(r["merchant_id"], r["domain"]): r["revoked_at"] for r in await db.fetch_all(
        EXISTING_SQL, {"merchant_ids": sorted({e["merchant_id"] for e in entries})})}
    new, active, revoked = [], [], []
    for e in entries:
        key = (e["merchant_id"], e["domain"])
        if key not in existing:
            new.append(e)
        elif existing[key] is None:
            active.append(key)
        else:
            revoked.append(key)
    return {"new": new, "already_active": active, "revoked": revoked}


async def write_attestations(new: List[Dict[str, Any]], db: Any = None) -> int:
    db = db or database
    if not await ensure_merchant_domain_attestations_table(db):
        raise RuntimeError("merchant_domain_attestations is unavailable; nothing written")
    written = 0
    async with db.transaction():
        for e in new:
            row = await db.fetch_one(UPSERT_SQL, {**{k: e[k] for k in ("merchant_id", "domain", "attested_by",
                                                                         "review_ref")},
                                                  "evidence": json.dumps(e["evidence"], default=str)})
            written += int(bool(row))
    return written
