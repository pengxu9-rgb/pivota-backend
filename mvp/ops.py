from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Protocol, Tuple

from mvp.schemas import RecommendationPack
from mvp.playbooks import ops_config_for_geo


PackStatus = Literal["draft", "validated", "published"]


AUTHORIZED_SIGNAL_TYPES: Tuple[str, ...] = (
    "creator_pick",
    "merchant_provided_testimonial",
    "verified_review_pointer",
    "ugc_permission_granted",
    "affiliate_code",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_pack_id() -> str:
    return f"ops_{uuid.uuid4().hex}"


def validate_pack(
    pack: RecommendationPack,
    *,
    target_status: PackStatus,
) -> Dict[str, Any]:
    """
    Validate a RecommendationPack for a target lifecycle status.

    - `draft`: never blocks; returns warnings/errors.
    - `validated`: blocks if narrative/evidence are missing.
    - `published`: same as validated for v0.1, plus requires at least one recommendation.
    """
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    if not pack.context.session_id:
        errors.append({"code": "missing_session_id", "path": "context.session_id"})

    if not pack.recommendations:
        msg = {"code": "missing_recommendations", "path": "recommendations"}
        (errors if target_status in ("validated", "published") else warnings).append(msg)

    allowlist = AUTHORIZED_SIGNAL_TYPES
    try:
        cfg = ops_config_for_geo(country=getattr(pack.context.geo, "country", None))
        raw = cfg.get("authorized_signal_allowlist")
        if isinstance(raw, list) and raw:
            allowlist = tuple(str(x) for x in raw if x)
    except Exception:
        allowlist = AUTHORIZED_SIGNAL_TYPES

    for idx, rec in enumerate(pack.recommendations or []):
        base = f"recommendations[{idx}]"
        if not rec.offer_id:
            errors.append({"code": "missing_offer_id", "path": f"{base}.offer_id"})
        if not rec.title:
            errors.append({"code": "missing_title", "path": f"{base}.title"})

        if not (rec.narrative or "").strip():
            msg = {"code": "missing_narrative", "path": f"{base}.narrative"}
            (errors if target_status in ("validated", "published") else warnings).append(msg)

        if not rec.claims:
            msg = {"code": "missing_claims", "path": f"{base}.claims"}
            (errors if target_status in ("validated", "published") else warnings).append(msg)
        else:
            for cidx, claim in enumerate(rec.claims):
                cbase = f"{base}.claims[{cidx}]"
                if not (claim.claim or "").strip():
                    errors.append({"code": "empty_claim", "path": f"{cbase}.claim"})
                if not claim.evidence:
                    msg = {"code": "missing_evidence", "path": f"{cbase}.evidence"}
                    (errors if target_status in ("validated", "published") else warnings).append(msg)

        # Social signals: always optional in v0.1, but flagged when absent.
        if not rec.authorized_signals:
            warnings.append({"code": "missing_authorized_signals", "path": f"{base}.authorized_signals"})
        else:
            for sidx, sig in enumerate(rec.authorized_signals):
                sbase = f"{base}.authorized_signals[{sidx}]"
                if sig.signal_type not in allowlist:
                    errors.append({"code": "unauthorized_signal_type", "path": f"{sbase}.signal_type"})

    ok = len(errors) == 0 if target_status in ("validated", "published") else True
    return {"ok": ok, "errors": errors, "warnings": warnings, "target_status": target_status}


@dataclass(frozen=True)
class OpsPackRecord:
    pack_id: str
    merchant_id: str
    status: PackStatus
    created_at: datetime
    updated_at: datetime
    pack: RecommendationPack
    validation: Dict[str, Any]


class OpsPackStore(Protocol):
    async def get(self, *, pack_id: str) -> Optional[OpsPackRecord]: ...
    async def create_draft(self, *, merchant_id: str, pack: RecommendationPack) -> OpsPackRecord: ...
    async def transition(self, *, pack_id: str, target_status: PackStatus) -> OpsPackRecord: ...


class InMemoryOpsPackStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_id: Dict[str, OpsPackRecord] = {}

    async def get(self, *, pack_id: str) -> Optional[OpsPackRecord]:
        with self._lock:
            return self._by_id.get(pack_id)

    async def create_draft(self, *, merchant_id: str, pack: RecommendationPack) -> OpsPackRecord:
        now = _utc_now()
        pid = pack.pack_id or _new_pack_id()
        pack = RecommendationPack(**{**pack.model_dump(), "pack_id": pid})
        v = validate_pack(pack, target_status="draft")
        rec = OpsPackRecord(
            pack_id=pid,
            merchant_id=merchant_id,
            status="draft",
            created_at=now,
            updated_at=now,
            pack=pack,
            validation=v,
        )
        with self._lock:
            self._by_id[pid] = rec
        return rec

    async def transition(self, *, pack_id: str, target_status: PackStatus) -> OpsPackRecord:
        with self._lock:
            cur = self._by_id.get(pack_id)
        if not cur:
            raise KeyError("pack not found")
        v = validate_pack(cur.pack, target_status=target_status)
        if target_status in ("validated", "published") and not v.get("ok"):
            raise ValueError("validation_failed")
        now = _utc_now()
        nxt = OpsPackRecord(
            pack_id=cur.pack_id,
            merchant_id=cur.merchant_id,
            status=target_status,
            created_at=cur.created_at,
            updated_at=now,
            pack=cur.pack,
            validation=v,
        )
        with self._lock:
            self._by_id[pack_id] = nxt
        return nxt


class FileOpsPackStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    async def get(self, *, pack_id: str) -> Optional[OpsPackRecord]:
        if not os.path.exists(self.path):
            return None
        last = None
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("pack_id") == pack_id:
                        last = obj
        if not last:
            return None
        return _record_from_json(last)

    async def create_draft(self, *, merchant_id: str, pack: RecommendationPack) -> OpsPackRecord:
        now = _utc_now()
        pid = pack.pack_id or _new_pack_id()
        pack = RecommendationPack(**{**pack.model_dump(), "pack_id": pid})
        v = validate_pack(pack, target_status="draft")
        rec = OpsPackRecord(
            pack_id=pid,
            merchant_id=merchant_id,
            status="draft",
            created_at=now,
            updated_at=now,
            pack=pack,
            validation=v,
        )
        self._append(rec)
        return rec

    async def transition(self, *, pack_id: str, target_status: PackStatus) -> OpsPackRecord:
        cur = await self.get(pack_id=pack_id)
        if not cur:
            raise KeyError("pack not found")
        v = validate_pack(cur.pack, target_status=target_status)
        if target_status in ("validated", "published") and not v.get("ok"):
            raise ValueError("validation_failed")
        now = _utc_now()
        nxt = OpsPackRecord(
            pack_id=cur.pack_id,
            merchant_id=cur.merchant_id,
            status=target_status,
            created_at=cur.created_at,
            updated_at=now,
            pack=cur.pack,
            validation=v,
        )
        self._append(nxt)
        return nxt

    def _append(self, rec: OpsPackRecord) -> None:
        line = json.dumps(_record_to_json(rec), ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


class PostgresOpsPackStore:
    def __init__(self):
        self._ready = False

    def _try_get_db(self):
        try:
            from db.database import database

            return database
        except Exception:
            return None

    async def _ensure_table(self, db) -> None:
        if self._ready:
            return
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_recommendation_packs (
              pack_id TEXT PRIMARY KEY,
              merchant_id VARCHAR(64) NOT NULL,
              status TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL,
              pack_json JSONB NOT NULL,
              validation_json JSONB NOT NULL
            );
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_mvp_ops_packs_merchant_time ON mvp_recommendation_packs(merchant_id, updated_at DESC);"
        )
        self._ready = True

    async def get(self, *, pack_id: str) -> Optional[OpsPackRecord]:
        db = self._try_get_db()
        if db is None:
            return None
        await self._ensure_table(db)
        row = await db.fetch_one(
            """
            SELECT pack_id, merchant_id, status, created_at, updated_at, pack_json, validation_json
            FROM mvp_recommendation_packs
            WHERE pack_id = :pack_id
            """,
            {"pack_id": pack_id},
        )
        if not row:
            return None
        return OpsPackRecord(
            pack_id=row["pack_id"],
            merchant_id=row["merchant_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            pack=RecommendationPack(**dict(row["pack_json"])),
            validation=dict(row["validation_json"]) if row["validation_json"] is not None else {},
        )

    async def create_draft(self, *, merchant_id: str, pack: RecommendationPack) -> OpsPackRecord:
        db = self._try_get_db()
        if db is None:
            raise RuntimeError("DATABASE_URL not configured; PostgresOpsPackStore unavailable")
        await self._ensure_table(db)
        now = _utc_now()
        pid = pack.pack_id or _new_pack_id()
        pack = RecommendationPack(**{**pack.model_dump(), "pack_id": pid})
        v = validate_pack(pack, target_status="draft")
        await db.execute(
            """
            INSERT INTO mvp_recommendation_packs
              (pack_id, merchant_id, status, created_at, updated_at, pack_json, validation_json)
            VALUES
              (:pack_id, :merchant_id, :status, :created_at, :updated_at, CAST(:pack_json AS jsonb), CAST(:validation_json AS jsonb))
            ON CONFLICT (pack_id) DO NOTHING
            """,
            {
                "pack_id": pid,
                "merchant_id": merchant_id,
                "status": "draft",
                "created_at": now,
                "updated_at": now,
                "pack_json": json.dumps(pack.model_dump(mode="json"), ensure_ascii=False),
                "validation_json": json.dumps(v, ensure_ascii=False),
            },
        )
        rec = await self.get(pack_id=pid)
        if rec:
            return rec
        return OpsPackRecord(
            pack_id=pid,
            merchant_id=merchant_id,
            status="draft",
            created_at=now,
            updated_at=now,
            pack=pack,
            validation=v,
        )

    async def transition(self, *, pack_id: str, target_status: PackStatus) -> OpsPackRecord:
        db = self._try_get_db()
        if db is None:
            raise RuntimeError("DATABASE_URL not configured; PostgresOpsPackStore unavailable")
        await self._ensure_table(db)
        cur = await self.get(pack_id=pack_id)
        if not cur:
            raise KeyError("pack not found")
        v = validate_pack(cur.pack, target_status=target_status)
        if target_status in ("validated", "published") and not v.get("ok"):
            raise ValueError("validation_failed")
        now = _utc_now()
        await db.execute(
            """
            UPDATE mvp_recommendation_packs
            SET status = :status, updated_at = :updated_at, validation_json = CAST(:validation_json AS jsonb)
            WHERE pack_id = :pack_id
            """,
            {"pack_id": pack_id, "status": target_status, "updated_at": now, "validation_json": json.dumps(v)},
        )
        rec = await self.get(pack_id=pack_id)
        if rec:
            return rec
        return OpsPackRecord(
            pack_id=cur.pack_id,
            merchant_id=cur.merchant_id,
            status=target_status,
            created_at=cur.created_at,
            updated_at=now,
            pack=cur.pack,
            validation=v,
        )


def _default_file_path() -> str:
    return os.getenv("MVP_OPS_PACKS_FILE", "mvp_ops_packs.jsonl")


_fallback_inmem_store = InMemoryOpsPackStore()


def get_default_ops_store() -> OpsPackStore:
    prefer_db = os.getenv("MVP_OPS_PACKS_STORE", "db").lower() != "file"
    if prefer_db:
        try:
            return PostgresOpsPackStore()
        except Exception:
            pass
    # File sink is durable for local dev; if it cannot be created, fall back to memory.
    try:
        return FileOpsPackStore(_default_file_path())
    except Exception:
        return _fallback_inmem_store


def _record_to_json(rec: OpsPackRecord) -> Dict[str, Any]:
    return {
        "pack_id": rec.pack_id,
        "merchant_id": rec.merchant_id,
        "status": rec.status,
        "created_at": rec.created_at.isoformat(),
        "updated_at": rec.updated_at.isoformat(),
        "pack": rec.pack.model_dump(mode="json"),
        "validation": rec.validation,
    }


def _record_from_json(obj: Dict[str, Any]) -> OpsPackRecord:
    return OpsPackRecord(
        pack_id=str(obj.get("pack_id") or ""),
        merchant_id=str(obj.get("merchant_id") or ""),
        status=obj.get("status") or "draft",
        created_at=datetime.fromisoformat(obj.get("created_at")),
        updated_at=datetime.fromisoformat(obj.get("updated_at")),
        pack=RecommendationPack(**(obj.get("pack") or {})),
        validation=obj.get("validation") or {},
    )
