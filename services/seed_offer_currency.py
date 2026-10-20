"""A seed's own currency, and which offers copied off a seed are prices at all.

Peng 2026-10-19: a price's currency is the buyer market's currency, and a market or currency is
never defaulted. Live that day, US search_catalog served SKINTIFIC "Glow Cushion & Peptide Lip
Serum Set" at "USD 23.56". The price came from 20 offers the variant-identity backfill wrote on
09-10 from seed_data.variants. The seed has since been re-read in AUD (snapshot AUD 57, column
AUD 56, mirror offer AUD 56, store's live read AUD). Nothing retired the 20 USD rows.

WHICH of a seed's currencies is its currency. Measured on prod 2026-10-19 (read-only censuses
spccy-1019 .. spccy8-1019, active seeds that carry a live projected offer):

  * the COLUMN (price_currency) and the SNAPSHOT (seed_data.snapshot.price_currency, the page as read)
    must AGREE. The refresh never redenominates: a read in another currency is
    `skipped_currency_mismatch` and the column is kept (routes/employee_products). It writes the
    snapshot from every read. So where the two disagree, either can be the wrong one:
      - a geo-served misread over a right column (us.oiolab.co: column USD, snapshot EUR, same amount);
      - the writers' old `or "USD"` default over a right page read (column USD with snapshot GBP 483,
        QAR 367, EUR 166, INR 108, ZAR 64 seeds).
    Such a seed states NO currency: no price in either direction, never relabelled.
  * Either one stands in alone when the other is absent.
  * seed_data.price_currency is NOT evidence. It is a copy the writers default to "USD" as well:
    16 serving SGD offers have column SGD and snapshot SGD, with amounts equal to both, while their
    seed_data says "USD".
  * In SQL, a snapshot or seed_data that is not a JSON object hides the page read, so a seed with a
    column and such a snapshot states none (it is not parsed there).

The PIVOTA-Agent gateway's canonical search applies the same rule to the offers it picks
(src/services/seedSearchOfferScope.js, seedProjectedOfferInSeedCurrencySql). Both sides must
agree, which is why the SQL here is spelled the same way.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

# The offer writers that COPY a seed's money onto catalog_offers: their row projects the seed and
# is not a read of its own. A Shopify-Markets capture (shopify_markets_us_localization,
# us_market_capture) reads the store's own US price and is deliberately not on this list.
SEED_PROJECTED_OFFER_SOURCE_SYSTEMS = (
    "external_product_seeds_mirror_v1",
    "variant_identity_backfill_v1",
    "variant_offer_projection_v1",
)


def _code(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    return text or None


def _as_dict(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (str, bytes)) and value:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def snapshot_currency_of(seed: Mapping[str, Any]) -> Optional[str]:
    """The page-read currency. Taken from a projected `snapshot_price_currency` column when the
    caller selected one, otherwise read out of seed_data.snapshot."""
    if "snapshot_price_currency" in seed:
        return _code(seed.get("snapshot_price_currency"))
    # seed_data itself may arrive as the driver's jsonb text, but a snapshot INSIDE it that is a
    # string is a JSON string value: the SQL reads no page read through it, and neither does this.
    snapshot = _as_dict(seed.get("seed_data")).get("snapshot")
    return _code(snapshot.get("price_currency")) if isinstance(snapshot, Mapping) else None


def _hides_the_page_read(seed: Mapping[str, Any]) -> bool:
    """seed_data, or its snapshot, held as something other than a JSON object (or null): the SQL
    cannot read a page read through it and states no currency, so the twin does the same. (A
    JSON-encoded object string counts as an object, as the asyncpg/databases drivers hand jsonb
    columns back as text.)"""
    raw = seed.get("seed_data")
    if raw is None:
        return False
    data = raw
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return True
    if data is None:
        return False
    if not isinstance(data, Mapping):
        return True
    snapshot = data.get("snapshot")
    return snapshot is not None and not isinstance(snapshot, Mapping)


def seed_currency_of(seed: Mapping[str, Any]) -> Optional[str]:
    """The seed's own currency: its column and its page read when they agree, whichever is stated
    when only one is, and None when they disagree, neither is stated, or the payload hides the page
    read (the same answer as seed_currency_sql)."""
    column = _code(seed.get("price_currency"))
    page = snapshot_currency_of(seed)
    if column and page and column != page:
        return None
    if column and not page and "snapshot_price_currency" not in seed and _hides_the_page_read(seed):
        return None
    return column or page


class SeedOfferCurrencyRefused(ValueError):
    """A seed-offer write refused because the seed states no currency (or no market) for its amount.
    A ValueError, so every caller that already skips a refused row keeps doing so; its own type, so a
    caller can report it apart from a real failure (`refused_currency`)."""


def seed_currency_sql(alias: str) -> str:
    """`seed_currency_of` in SQL for an external_product_seeds alias; NULL means the seed states no
    currency. The same text as the gateway's seedCurrencySql (PIVOTA-Agent#2467).

    ONE detoast of seed_data on the common path: seed_data is ~25 KB and TOASTed, and every reference
    re-detoasts it (a four-reference form cost +30-35% on scoped recall, spccy6/7-1019). A simple CASE
    evaluates its test once, `#>>` yields NULL through a non-object, and only a seed with no snapshot
    currency pays the type checks."""
    column = f"nullif(upper(trim({alias}.price_currency)), '')"
    page = f"nullif(upper(trim({alias}.seed_data#>>'{{snapshot,price_currency}}')), '')"
    snapshot = f"{alias}.seed_data->'snapshot'"
    return (
        f"(CASE WHEN {column} IS NULL THEN {page}"
        f" ELSE (CASE coalesce({page}, '~')"
        f" WHEN {column} THEN {column}"
        f" WHEN '~' THEN (CASE WHEN {alias}.seed_data IS NULL"
        f" OR (jsonb_typeof({alias}.seed_data) IN ('object', 'null')"
        f" AND ({snapshot} IS NULL OR jsonb_typeof({snapshot}) IN ('object', 'null')))"
        f" THEN {column} END)"
        f" END)"
        f" END)"
    )


def seed_projected_offer_in_seed_currency_sql(offer_alias: str = "o", product_key_expr: str = "o.product_key") -> str:
    """TRUE unless `offer_alias` was copied off a seed (SEED_PROJECTED_OFFER_SOURCE_SYSTEMS) and
    either no ACTIVE seed (status compared case-insensitively) is attached to `product_key_expr`
    (SQL, not a value), or one that is states another currency or none. Such an offer is not a
    price. Two reads of one listing that disagree on the unit cannot be quoted to anyone, and an
    orphan (its seed retired, detached, or superseded) has no read behind it at all.

    The same rule as the gateway's seedProjectedOfferInSeedCurrencySql (PIVOTA-Agent#2467), so a
    PDP and a search card agree on whether a row is a price. Probes idx on
    external_product_seeds(attached_product_key, attached_variant_id)."""
    systems = ", ".join(f"'{value}'" for value in SEED_PROJECTED_OFFER_SOURCE_SYSTEMS)
    # ONE index probe per offer (PR controller, spccy6-1019: two correlated EXISTS cost +30-35%). bool_and
    # over the active attached seeds is NULL when there are none, so IS TRUE fails an orphan closed; it is
    # false as soon as one seed states another currency or none.
    return (
        f"(coalesce({offer_alias}.source_system, '') NOT IN ({systems})"
        f" OR (SELECT bool_and({seed_currency_sql('eps_own')} IS NOT DISTINCT FROM upper(trim(coalesce({offer_alias}.currency, ''))))"
        f" FROM external_product_seeds eps_own"
        f" WHERE eps_own.attached_product_key = {product_key_expr}"
        f" AND lower(trim(coalesce(eps_own.status, ''))) = 'active') IS TRUE)"
    )


#: KILL SWITCH, default ON, the same name as the gateway's (PIVOTA-Agent#2467): any of 0/false/off/no/
#: disabled turns the reader guard off, read per call, and the statement is then the pre-guard one.
SEED_PROJECTED_OFFER_CURRENCY_GUARD_FLAG = "CANONICAL_SEED_PROJECTED_OFFER_CURRENCY_GUARD"
_GUARD_OFF = {"0", "false", "off", "no", "disabled"}


def seed_currency_guard_enabled() -> bool:
    import os

    return str(os.getenv(SEED_PROJECTED_OFFER_CURRENCY_GUARD_FLAG) or "").strip().lower() not in _GUARD_OFF


def seed_projected_offer_guard_and_sql(
    offer_alias: str = "o", product_key_expr: str = "o.product_key", *, prefix: str = "\n          "
) -> str:
    """The guard as `<prefix>AND (...)` for a reader's WHERE, or "" with the kill switch off. A
    reader interpolates it at the END of its previous line, so "off" adds not even a blank line
    and the statement is byte-identical to the pre-guard one."""
    if not seed_currency_guard_enabled():
        return ""
    return f"{prefix}AND {seed_projected_offer_in_seed_currency_sql(offer_alias, product_key_expr)}"

