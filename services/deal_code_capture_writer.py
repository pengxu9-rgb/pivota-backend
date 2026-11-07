"""Record the codes the shadow capture found on a store page (services/deal_code_capture.py) in
`observed_store_offers`, as UNVERIFIED `merchant_published` rows (ADR-027 D2; coupon discovery design §3.2 A2).

STORAGE, NOT TESTING. A row here is evidence that the store printed the code on its own page, nothing more: it is
never verified, emitted or applied from this module. Verification (a cart on the store's own checkout) is a
separate step with its own flags and approvals; serving reads only verified rows.

WHAT IS WRITTEN, per captured code:
  - seller_id   the page's host, lower-cased, "www." stripped (the same host the gateway's verify route takes);
  - market      the refresh partition's market ONLY when the page's own currency is that market's currency
                (US/USD, SG/SGD). Otherwise none: a row with no market evidence is never verified or served
                (D6 "Market" — never defaulted);
  - code        as the store printed it (Shopify matches codes case-insensitively; the deal_key upper-cases it);
  - kind/value  from the terms the bar stated next to the code: percent, amount (with the page's currency when
                the symbol agrees with it), free_shipping. A code with no stated kind is NOT written (the table
                requires one; guessing would be a false fact) and is counted `skipped_no_kind`;
  - min_subtotal, new_customer_only / once_per_customer / segment_limited from the bar's hints;
                eligibility_known stays FALSE (hints are a reading of prose, not a store's rule);
  - source_url  the page; terms_text the bar snippet (the store's own published text).
One row per (seller, market, code): the identity key is `observed_store_offer_key`. A later sighting refreshes the
observation fields and `observed_at`; it never touches verifications, outcomes or `created_at`.

A seller in `deal_lane_optout` is never written (`skipped_optout`). The code itself is never logged. Every failure is
swallowed and counted (`errors`): recording can never cost a page refresh.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional
from urllib.parse import urlparse

from sqlalchemy import and_, select

from db.database import database
from db.deal_lane import deal_lane_optout, observed_store_offer_key, observed_store_offers
from services import deal_code_capture

logger = logging.getLogger(__name__)

# The served markets and their currencies (PIVOTA_SERVING_PRICING_REGIONS = US,SG). A page whose own currency is not
# its partition's is not evidence for that market.
_MARKET_CURRENCY = {"US": "USD", "SG": "SGD"}
# A stated amount's symbol must agree with the page's currency, or the amount has no currency.
_SYMBOL_CURRENCIES = {"$": {"USD", "SGD", "AUD", "CAD", "NZD", "HKD"}, "£": {"GBP"}, "€": {"EUR"}}
_KINDS = {"percent", "amount", "free_shipping"}


def seller_id_for(url: str) -> Optional[str]:
    host = (urlparse(url or "").hostname or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def market_evidence(market: Optional[str], page_currency: Optional[str]) -> Optional[str]:
    m = str(market or "").strip().upper()
    expected = _MARKET_CURRENCY.get(m)
    return m if expected and str(page_currency or "").strip().upper() == expected else None


def _decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def row_values(captured: "deal_code_capture.CapturedCode", *, url: str, market: Optional[str],
               page_currency: Optional[str]) -> Optional[dict]:
    """The observation columns for one captured code, or None when it cannot be written (no stated kind, no seller,
    or a value that is not a number). Pure."""
    seller = seller_id_for(url)
    if not seller or not captured.code or captured.kind not in _KINDS:
        return None
    currency = str(page_currency or "").strip().upper() or None
    value = None
    row_currency = None
    if captured.kind in ("percent", "amount"):
        value = _decimal(captured.value)
        if value is None:
            return None
        if captured.kind == "percent" and not (0 < value <= 100):
            return None
        if captured.kind == "amount" and currency and currency in _SYMBOL_CURRENCIES.get(captured.currency_symbol or "", set()):
            row_currency = currency
    hints = set(captured.hints or [])
    min_subtotal = _decimal(captured.min_subtotal)
    market_ev = market_evidence(market, page_currency)
    return {
        "seller_id": seller,
        "market": market_ev,
        "code": captured.code,
        "kind": captured.kind,
        "value": value,
        "currency": row_currency,
        "min_subtotal": min_subtotal,
        "new_customer_only": "new_customer_only" in hints,
        "once_per_customer": "once_per_customer" in hints,
        "segment_limited": "segment_limited" in hints,
        "usage_limited": False,
        "eligibility_known": False,
        "source": "merchant_published",
        "source_url": url,
        "terms_text": (captured.snippet or None),
    }


async def record(codes: Iterable["deal_code_capture.CapturedCode"], *, url: str, market: Optional[str],
                 page_currency: Optional[str]) -> None:
    """Write this page's captured codes. Never raises."""
    try:
        codes = list(codes or [])
        if not codes:
            return
        seller = seller_id_for(url)
        if not seller:
            deal_code_capture.bump_write("skipped_no_seller", len(codes))
            return
        opted_out = await database.fetch_one(
            select(deal_lane_optout.c.seller_id).where(deal_lane_optout.c.seller_id == seller)
        )
        if opted_out:
            deal_code_capture.bump_write("skipped_optout", len(codes))
            return
        now = datetime.now(timezone.utc)
        for captured in codes:
            try:
                values = row_values(captured, url=url, market=market, page_currency=page_currency)
                if values is None:
                    deal_code_capture.bump_write("skipped_no_kind" if captured.kind not in _KINDS else "skipped_unreadable")
                    continue
                if values["market"] is None:
                    deal_code_capture.bump_write("no_market_evidence")
                key = observed_store_offer_key(
                    seller_id=values["seller_id"], market=values["market"], code=values["code"],
                    platform_rule_ref=None, program_id=None,
                )
                existing = await database.fetch_one(
                    select(observed_store_offers.c.deal_id).where(observed_store_offers.c.deal_key == key)
                )
                observation = {k: values[k] for k in ("kind", "value", "currency", "min_subtotal", "new_customer_only",
                                                      "once_per_customer", "segment_limited", "source_url", "terms_text")}
                if existing:
                    await database.execute(
                        observed_store_offers.update()
                        .where(and_(observed_store_offers.c.deal_key == key,
                                    observed_store_offers.c.source == "merchant_published"))
                        .values(**observation, observed_at=now, updated_at=now)
                    )
                    deal_code_capture.bump_write("updated")
                else:
                    await database.execute(
                        observed_store_offers.insert().values(
                            deal_id="dl_" + uuid.uuid4().hex, deal_key=key, observed_at=now,
                            created_at=now, updated_at=now, **values,
                        )
                    )
                    deal_code_capture.bump_write("inserted")
            except Exception as exc:  # noqa: BLE001 - one code's failure never costs the others or the page
                deal_code_capture.bump_write("errors")
                logger.warning("deal code capture write failed: %s", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - recording never costs a page refresh
        deal_code_capture.bump_write("errors")
        logger.warning("deal code capture write failed: %s", type(exc).__name__)
