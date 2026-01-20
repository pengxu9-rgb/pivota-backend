import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy import and_, select, update

from db.database import database
from db.external_offers import external_offer_snapshots


MAX_BODY_BYTES = int(os.getenv("EXTERNAL_OFFER_MAX_BODY_BYTES") or "1200000")  # ~1.2MB
MAX_AGE_DAYS = int(os.getenv("EXTERNAL_OFFER_MAX_AGE_DAYS") or "7")
DEFAULT_UA = os.getenv("EXTERNAL_OFFER_USER_AGENT") or "Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)"


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: Dict[Tuple[str, str], str] = {}
        self.title: Optional[str] = None
        self._in_title = False
        self.links: Dict[str, str] = {}
        self.jsonld: list[str] = []
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "meta":
            key = ""
            if a.get("property"):
                key = f"property:{a.get('property')}"
            elif a.get("name"):
                key = f"name:{a.get('name')}"
            if key and a.get("content"):
                self.meta[(tag, key.lower())] = a.get("content") or ""
        elif tag.lower() == "title":
            self._in_title = True
        elif tag.lower() == "link":
            rel = (a.get("rel") or "").strip().lower()
            href = (a.get("href") or "").strip()
            if rel and href:
                self.links[rel] = href
        elif tag.lower() == "script":
            t = (a.get("type") or "").strip().lower()
            if t == "application/ld+json":
                self._in_jsonld = True
                self._jsonld_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        elif tag.lower() == "script" and self._in_jsonld:
            self._in_jsonld = False
            raw = "".join(self._jsonld_buf).strip()
            if raw:
                self.jsonld.append(raw)
            self._jsonld_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title or "") + data
        elif self._in_jsonld:
            self._jsonld_buf.append(data)


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("INVALID_URL")
    if not parsed.hostname:
        raise ValueError("INVALID_URL")

    # Drop fragment and common tracking params to improve cache hit-rate.
    qs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    qs = [(k, v) for k, v in qs if k.lower() not in {"fbclid", "gclid", "yclid", "msclkid"}]
    query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(fragment="", query=query))


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _domain(url: str) -> str:
    return urlparse(url).hostname or ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_db() -> datetime:
    """
    Return a DB-safe datetime for columns that may be `timestamp` (without tz) in some environments.

    We have seen production tables drift from `timestamptz` to `timestamp` historically; asyncpg
    will error if an offset-aware datetime is bound to a `timestamp` column.
    """
    return _now().replace(tzinfo=None)


def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc)
    except Exception:
        return dt

def _parse_price(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    # Keep digits and dot.
    cleaned = re.sub(r"[^0-9.]", "", value)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except Exception:
        return None


def _extract_jsonld_offer(json_texts: list[str]) -> Dict[str, Any]:
    def iter_nodes(obj: Any):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from iter_nodes(v)
        elif isinstance(obj, list):
            for it in obj:
                yield from iter_nodes(it)

    parsed_objs: list[Any] = []
    for raw in json_texts:
        try:
            parsed_objs.append(json.loads(raw))
        except Exception:
            continue

    best: Dict[str, Any] = {}
    for root in parsed_objs:
        for node in iter_nodes(root):
            t = node.get("@type") if isinstance(node, dict) else None
            if isinstance(t, list):
                tset = {str(x).lower() for x in t}
            else:
                tset = {str(t).lower()} if t else set()
            if "product" not in tset and "offer" not in tset:
                continue

            name = node.get("name") if isinstance(node, dict) else None
            brand = None
            b = node.get("brand") if isinstance(node, dict) else None
            if isinstance(b, dict):
                brand = b.get("name")
            elif isinstance(b, str):
                brand = b

            image = node.get("image") if isinstance(node, dict) else None
            image_url = image[0] if isinstance(image, list) and image else image if isinstance(image, str) else None

            offers = node.get("offers") if isinstance(node, dict) else None
            if isinstance(offers, list) and offers:
                offers = offers[0]
            price = None
            currency = None
            availability = None
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
                currency = offers.get("priceCurrency")
                availability = offers.get("availability")

            # Prefer nodes that include price/currency.
            score = 0
            if name:
                score += 1
            if image_url:
                score += 1
            if price and currency:
                score += 2
            if availability:
                score += 1

            if score and score > best.get("_score", 0):
                best = {
                    "_score": score,
                    "title": str(name).strip() if name else None,
                    "brand": str(brand).strip() if brand else None,
                    "image_url": str(image_url).strip() if image_url else None,
                    "price_raw": str(price).strip() if price is not None else None,
                    "currency": str(currency).strip().upper() if currency else None,
                    "availability_raw": str(availability).strip() if availability else None,
                }

    best.pop("_score", None)
    return best


def _availability_from_raw(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    v = raw.lower()
    if "instock" in v or "in_stock" in v or "in stock" in v:
        return "in_stock"
    if "outofstock" in v or "out_of_stock" in v or "out of stock" in v:
        return "out_of_stock"
    return "unknown"


async def _fetch_html(url: str) -> Tuple[str, str]:
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers={"User-Agent": DEFAULT_UA}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        # Read up to MAX_BODY_BYTES to keep latency predictable.
        body = resp.content[:MAX_BODY_BYTES]
        text = body.decode(resp.encoding or "utf-8", errors="replace")
        return text, content_type


def _extract_from_html(base_url: str, html: str) -> Dict[str, Any]:
    p = _MetaParser()
    p.feed(html)

    def meta(*keys: str) -> Optional[str]:
        for k in keys:
            v = p.meta.get(("meta", k.lower()))
            if v and v.strip():
                return v.strip()
        return None

    canonical = p.links.get("canonical") or meta("property:og:url") or base_url
    title = meta("property:og:title", "name:twitter:title") or (p.title.strip() if p.title else None)
    image = meta("property:og:image", "name:twitter:image")

    price = meta("property:product:price:amount", "property:og:price:amount", "property:product:price") or None
    currency = meta("property:product:price:currency", "property:og:price:currency") or None

    jsonld = _extract_jsonld_offer(p.jsonld)

    # JSON-LD is preferred when it provides structured offers.
    out = {
        "canonical_url": canonical,
        "title": jsonld.get("title") or title,
        "brand": jsonld.get("brand") or None,
        "image_url": jsonld.get("image_url") or image,
        "price_amount": _parse_price(jsonld.get("price_raw")) or _parse_price(price),
        "price_currency": (jsonld.get("currency") or currency or "").strip().upper() or None,
        "availability": _availability_from_raw(jsonld.get("availability_raw")),
        "evidence_provider": "jsonld" if jsonld.get("price_raw") or jsonld.get("title") else ("og" if title or image else "manual"),
    }
    return out


@dataclass(frozen=True)
class ExternalOfferSnapshot:
    snapshot_id: str
    market: str
    canonical_url: str
    domain: str
    title: Optional[str]
    brand: Optional[str]
    image_url: Optional[str]
    price_amount: Optional[float]
    price_currency: Optional[str]
    availability: str
    last_checked_at: Optional[datetime]
    evidence: Optional[Dict[str, Any]]

    # Manual override fields (phase 2)
    override_title: Optional[str] = None
    override_brand: Optional[str] = None
    override_image_url: Optional[str] = None
    override_price_amount: Optional[float] = None
    override_price_currency: Optional[str] = None

    def to_public(self) -> Dict[str, Any]:
        title = self.override_title or self.title
        brand = self.override_brand or self.brand
        image_url = self.override_image_url or self.image_url
        price_amount = self.override_price_amount if self.override_price_amount is not None else self.price_amount
        price_currency = self.override_price_currency or self.price_currency
        return {
            "snapshotId": self.snapshot_id,
            "market": self.market,
            "canonicalUrl": self.canonical_url,
            "domain": self.domain,
            "title": title,
            "brand": brand,
            "imageUrl": image_url,
            "price": {"amount": float(price_amount), "currency": price_currency} if price_amount is not None and price_currency else None,
            "availability": self.availability,
            "lastCheckedAt": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "evidence": self.evidence,
        }


async def _get_snapshot_row(market: str, url_hash: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        select(external_offer_snapshots).where(
            and_(
                external_offer_snapshots.c.market == market,
                external_offer_snapshots.c.url_hash == url_hash,
            )
        )
    )
    return dict(row) if row else None


def _row_to_snapshot(row: Dict[str, Any]) -> ExternalOfferSnapshot:
    return ExternalOfferSnapshot(
        snapshot_id=row["id"],
        market=row["market"],
        canonical_url=row["canonical_url"],
        domain=row["domain"],
        title=row.get("title"),
        brand=row.get("brand"),
        image_url=row.get("image_url"),
        price_amount=float(row["price_amount"]) if row.get("price_amount") is not None else None,
        price_currency=row.get("price_currency"),
        availability=row.get("availability") or "unknown",
        last_checked_at=_as_aware_utc(row.get("last_checked_at")),
        evidence=row.get("evidence"),
        override_title=row.get("override_title"),
        override_brand=row.get("override_brand"),
        override_image_url=row.get("override_image_url"),
        override_price_amount=float(row["override_price_amount"]) if row.get("override_price_amount") is not None else None,
        override_price_currency=row.get("override_price_currency"),
    )


def _is_stale(last_checked_at: Optional[datetime]) -> bool:
    last_checked_at = _as_aware_utc(last_checked_at)
    if not last_checked_at:
        return True
    return last_checked_at < (_now() - timedelta(days=MAX_AGE_DAYS))


async def resolve_external_offer(*, market: str, url: str, force_refresh: bool = False) -> ExternalOfferSnapshot:
    market_norm = str(market or "US").upper()
    url_norm = _normalize_url(url)
    url_hash = _url_hash(url_norm)

    existing = await _get_snapshot_row(market_norm, url_hash)
    if existing and not force_refresh and not _is_stale(existing.get("last_checked_at")):
        return _row_to_snapshot(existing)

    # Try to refresh (best-effort). If refresh fails, return existing cached value.
    try:
        html, _ct = await _fetch_html(url_norm)
        extracted = _extract_from_html(url_norm, html)

        canonical_url = _normalize_url(extracted.get("canonical_url") or url_norm)
        domain = _domain(canonical_url) or _domain(url_norm)

        currency = (extracted.get("price_currency") or "").strip().upper() or None
        amount = extracted.get("price_amount")
        if currency is None:
            currency = "JPY" if market_norm == "JP" else "USD"

        rid = f"eo_{url_hash[:24]}"
        now = _now()
        now_db = _now_db()
        evidence = {"provider": extracted.get("evidence_provider") or "manual", "fetchedAt": now.isoformat(), "snapshotId": (existing or {}).get("id") or rid}
        if existing:
            stmt = (
                update(external_offer_snapshots)
                .where(and_(external_offer_snapshots.c.market == market_norm, external_offer_snapshots.c.url_hash == url_hash))
                .values(
                    canonical_url=canonical_url,
                    domain=domain,
                    title=extracted.get("title"),
                    brand=extracted.get("brand"),
                    image_url=extracted.get("image_url"),
                    price_amount=amount,
                    price_currency=currency,
                    availability=extracted.get("availability") or "unknown",
                    evidence=evidence,
                    last_checked_at=now_db,
                    updated_at=now_db,
                )
            )
            await database.execute(stmt)
        else:
            await database.execute(
                external_offer_snapshots.insert(),
                {
                    "id": rid,
                    "market": market_norm,
                    "canonical_url": canonical_url,
                    "url_hash": url_hash,
                    "domain": domain,
                    "title": extracted.get("title"),
                    "brand": extracted.get("brand"),
                    "image_url": extracted.get("image_url"),
                    "price_amount": amount,
                    "price_currency": currency,
                    "availability": extracted.get("availability") or "unknown",
                    "evidence": evidence,
                    "last_checked_at": now_db,
                },
            )

        refreshed_row = await _get_snapshot_row(market_norm, url_hash)
        if refreshed_row:
            return _row_to_snapshot(refreshed_row)
    except Exception:
        if existing:
            return _row_to_snapshot(existing)
        raise

    # Fallback: should never happen, but keep function total.
    if existing:
        return _row_to_snapshot(existing)
    raise ValueError("FETCH_FAILED")
