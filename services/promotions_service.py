import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

from pydantic import BaseModel, Field, validator
from sqlalchemy import and_, desc, func, select, update

from db.database import database, promotions


def _normalize_dt(dt: datetime) -> datetime:
    """
    Ensure datetimes stored in the DB are offset-naive UTC.
    Asyncpg cannot adapt offset-aware datetimes (it subtracts a naive epoch),
    so we normalize everything to UTC and drop tzinfo.
    """
    if dt is None:
        return dt
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class PromotionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    UPCOMING = "UPCOMING"
    ENDED = "ENDED"


class PromotionBase(BaseModel):
    merchantId: str
    name: str
    type: str  # 'FLASH_SALE' | 'MULTI_BUY_DISCOUNT' | 'FREE_SHIPPING'
    description: Optional[str] = ""
    startAt: datetime
    endAt: datetime
    channels: List[str] = Field(default_factory=list)
    scope: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    exposeToCreators: bool = True
    allowedCreatorIds: Optional[List[str]] = None

    @validator("type")
    def validate_type(cls, v: str) -> str:
        if v not in ("FLASH_SALE", "MULTI_BUY_DISCOUNT", "FREE_SHIPPING"):
            raise ValueError("type must be FLASH_SALE, MULTI_BUY_DISCOUNT, or FREE_SHIPPING")
        return v

    @validator("channels")
    def validate_channels(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("channels must be a non-empty array")
        return v
    
    @validator("startAt", pre=False, always=True)
    def normalize_start_at(cls, start_at: datetime) -> datetime:
        if isinstance(start_at, datetime):
            return _normalize_dt(start_at)
        return start_at

    @validator("endAt", pre=False, always=True)
    def normalize_and_validate_end_at(
        cls, end_at: datetime, values: Dict[str, Any]
    ) -> datetime:
        if isinstance(end_at, datetime):
            end_at = _normalize_dt(end_at)
        start_at = values.get("startAt")
        if isinstance(start_at, datetime):
            start_at = _normalize_dt(start_at)
        if start_at and end_at and end_at <= start_at:
            raise ValueError("endAt must be after startAt")
        return end_at


class PromotionCreate(PromotionBase):
    id: Optional[str] = None


class PromotionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    startAt: Optional[datetime] = None
    endAt: Optional[datetime] = None
    channels: Optional[List[str]] = None
    scope: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    exposeToCreators: Optional[bool] = None
    allowedCreatorIds: Optional[List[str]] = None


class PromotionOut(PromotionBase):
    id: str
    humanReadableRule: str = ""
    status: PromotionStatus
    createdAt: datetime
    updatedAt: datetime
    deletedAt: Optional[datetime] = None


def _compute_status(row: Dict[str, Any], now: Optional[datetime] = None) -> PromotionStatus:
    now = now or datetime.utcnow()
    if row.get("deleted_at"):
        return PromotionStatus.ENDED
    start_at = row.get("start_at")
    end_at = row.get("end_at")
    if start_at is None or end_at is None:
        return PromotionStatus.ACTIVE
    if now < start_at:
        return PromotionStatus.UPCOMING
    if now >= end_at:
        return PromotionStatus.ENDED
    return PromotionStatus.ACTIVE


def _compute_human_readable_rule(promo: Dict[str, Any]) -> str:
    if promo.get("human_readable_rule"):
        return promo["human_readable_rule"]

    cfg = promo.get("config") or {}
    ptype = promo.get("type")
    if ptype == "MULTI_BUY_DISCOUNT" or cfg.get("kind") == "MULTI_BUY_DISCOUNT":
        t = cfg.get("thresholdQuantity") or cfg.get("threshold_quantity")
        d = cfg.get("discountPercent") or cfg.get("discount_percent")
        if t and d:
            return f"Buy {int(t)}, get {int(d)}% off"
        return "Bundle & save"
    if ptype == "FLASH_SALE" or cfg.get("kind") == "FLASH_SALE":
        if cfg.get("flashPrice") or cfg.get("flash_price"):
            return "Flash deal"
        return "Flash deal"
    if ptype == "FREE_SHIPPING" or cfg.get("kind") == "FREE_SHIPPING":
        min_subtotal = cfg.get("minSubtotal") or cfg.get("min_subtotal")
        if min_subtotal:
            return "Free shipping over minimum order"
        return "Free shipping"
    return promo.get("name") or "Deal"


def _row_to_promotion_out(row: Dict[str, Any]) -> PromotionOut:
    now = datetime.utcnow()
    status = _compute_status(row, now)
    scope = row.get("scope") or {}
    config = row.get("config") or {}
    channels = row.get("channels") or []

    promo_dict: Dict[str, Any] = {
        "id": row["id"],
        "merchantId": row["merchant_id"],
        "name": row["name"],
        "type": row["type"],
        "description": row.get("description") or "",
        "startAt": row["start_at"],
        "endAt": row["end_at"],
        "channels": channels,
        "scope": scope,
        "config": config,
        "exposeToCreators": bool(row.get("expose_to_creators", True)),
        "allowedCreatorIds": row.get("allowed_creator_ids"),
        "humanReadableRule": _compute_human_readable_rule(
            {
                "type": row["type"],
                "config": config,
                "name": row["name"],
                "human_readable_rule": row.get("human_readable_rule"),
            }
        ),
        "status": status,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "deletedAt": row.get("deleted_at"),
    }
    return PromotionOut(**promo_dict)


async def ensure_promotions_table() -> None:
    """
    Safety hook for older databases – ensures the promotions table exists.
    In practice, metadata.create_all(engine) in main.py will create it,
    but this hook allows us to run additional ALTER/INDEX statements later.
    """
    # For now we rely on SQLAlchemy metadata in db.database; nothing else to do.
    return None


async def list_promotions(
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    channel: Optional[str] = None,
    creator_id: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[PromotionOut], int]:
    now = datetime.utcnow()
    conditions = [promotions.c.deleted_at.is_(None)]

    if merchant_id:
        conditions.append(promotions.c.merchant_id == merchant_id)

    if status in (PromotionStatus.ACTIVE, PromotionStatus.UPCOMING, PromotionStatus.ENDED):
        # We filter by time window at DB level to reduce rows.
        if status == PromotionStatus.ACTIVE:
            conditions.append(promotions.c.start_at <= now)
            conditions.append(promotions.c.end_at > now)
        elif status == PromotionStatus.UPCOMING:
            conditions.append(promotions.c.start_at > now)
        elif status == PromotionStatus.ENDED:
            conditions.append(promotions.c.end_at <= now)

    # channel / creatorId / search can be refined later; for now we do simple filtering in Python

    base_query = promotions.select().where(and_(*conditions)).order_by(
        desc(promotions.c.start_at), desc(promotions.c.created_at)
    )

    count_query = select(func.count()).select_from(base_query.alias("subq"))
    total = await database.fetch_val(count_query) or 0

    rows = await database.fetch_all(base_query.limit(limit).offset(offset))
    promos = [_row_to_promotion_out(dict(row)) for row in rows]

    # In-Python filtering for channels / creatorId / search
    def _match(p: PromotionOut) -> bool:
        if channel and channel not in p.channels:
            return False
        if creator_id:
            if not p.exposeToCreators:
                return False
            if p.allowedCreatorIds and creator_id not in p.allowedCreatorIds:
                return False
        if search:
            s = search.lower()
            if s not in p.name.lower() and (p.description or "").lower().find(s) == -1:
                return False
        return True

    filtered = [p for p in promos if _match(p)]
    return filtered, len(filtered)


async def get_promotion(promo_id: str) -> Optional[PromotionOut]:
    row = await database.fetch_one(
        promotions.select().where(promotions.c.id == promo_id)
    )
    if not row or row["deleted_at"] is not None:
        return None
    return _row_to_promotion_out(dict(row))


async def create_promotion(data: PromotionCreate) -> PromotionOut:
    payload = data.dict()

    # Basic config validation according to type
    cfg = payload.get("config") or {}
    ptype = payload["type"]
    # Normalize config.kind so downstream consumers can depend on it.
    if cfg.get("kind") not in (ptype,):
        cfg["kind"] = ptype
    if ptype == "MULTI_BUY_DISCOUNT":
        if not cfg.get("thresholdQuantity"):
            raise ValueError("thresholdQuantity is required for MULTI_BUY_DISCOUNT")
        if not cfg.get("discountPercent"):
            raise ValueError("discountPercent is required for MULTI_BUY_DISCOUNT")
    elif ptype == "FLASH_SALE":
        if cfg.get("flashPrice") is None:
            raise ValueError("flashPrice is required for FLASH_SALE")
    elif ptype == "FREE_SHIPPING":
        # For FREE_SHIPPING we keep validation minimal: at least mark freeShipping flag
        # so downstream code can recognize this deal type.
        if cfg.get("freeShipping") is not True:
            cfg["freeShipping"] = True

    promo_id = payload.get("id") or os.urandom(16).hex()
    now = datetime.utcnow()

    insert_values = {
        "id": promo_id,
        "merchant_id": payload["merchantId"],
        "name": payload["name"],
        "type": payload["type"],
        "description": payload.get("description") or "",
        "start_at": payload["startAt"],
        "end_at": payload["endAt"],
        "channels": payload["channels"],
        "scope": payload.get("scope") or {},
        "config": cfg,
        "expose_to_creators": payload.get("exposeToCreators", True),
        "allowed_creator_ids": payload.get("allowedCreatorIds"),
        "human_readable_rule": "",
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }
    insert_values["human_readable_rule"] = _compute_human_readable_rule(
        {"type": insert_values["type"], "config": insert_values["config"], "name": insert_values["name"]}
    )

    query = promotions.insert().values(**insert_values)
    await database.execute(query)

    row = await database.fetch_one(promotions.select().where(promotions.c.id == promo_id))
    return _row_to_promotion_out(dict(row))


async def update_promotion(promo_id: str, data: PromotionUpdate) -> Optional[PromotionOut]:
    existing = await database.fetch_one(promotions.select().where(promotions.c.id == promo_id))
    if not existing or existing["deleted_at"] is not None:
        return None

    row = dict(existing)

    update_fields: Dict[str, Any] = {}

    if data.name is not None:
        update_fields["name"] = data.name
    if data.description is not None:
        update_fields["description"] = data.description
    if data.startAt is not None:
        update_fields["start_at"] = data.startAt
    if data.endAt is not None:
        update_fields["end_at"] = data.endAt
    if data.channels is not None:
        update_fields["channels"] = data.channels
    if data.scope is not None:
        update_fields["scope"] = data.scope
    if data.config is not None:
        update_fields["config"] = data.config
    if data.exposeToCreators is not None:
        update_fields["expose_to_creators"] = data.exposeToCreators
    if data.allowedCreatorIds is not None:
        update_fields["allowed_creator_ids"] = data.allowedCreatorIds

    if not update_fields:
        return _row_to_promotion_out(row)

    if "start_at" in update_fields or "end_at" in update_fields:
        start_at = update_fields.get("start_at", row["start_at"])
        end_at = update_fields.get("end_at", row["end_at"])
        if end_at <= start_at:
            raise ValueError("endAt must be after startAt")

    cfg = update_fields.get("config", row.get("config") or {})
    update_fields["human_readable_rule"] = _compute_human_readable_rule(
        {"type": row["type"], "config": cfg, "name": update_fields.get("name", row["name"])}
    )

    update_fields["updated_at"] = datetime.utcnow()

    query = (
        update(promotions)
        .where(promotions.c.id == promo_id)
        .values(**update_fields)
    )
    await database.execute(query)

    new_row = await database.fetch_one(promotions.select().where(promotions.c.id == promo_id))
    return _row_to_promotion_out(dict(new_row))


async def soft_delete_promotion(promo_id: str) -> bool:
    now = datetime.utcnow()
    query = (
        update(promotions)
        .where(promotions.c.id == promo_id, promotions.c.deleted_at.is_(None))
        .values(deleted_at=now, updated_at=now)
    )
    result = await database.execute(query)
    # databases.execute returns primary key for INSERT; for UPDATE we can't rely on it
    # so we re-fetch to see if the promotion is now marked deleted.
    row = await database.fetch_one(promotions.select().where(promotions.c.id == promo_id))
    return bool(row and row["deleted_at"] is not None)
