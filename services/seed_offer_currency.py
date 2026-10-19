"""A seed's own currency, and which offers copied off a seed are prices at all.

Peng 2026-10-19: a price's currency is the buyer market's currency, and a market or currency is
never defaulted. Live that day, US search_catalog served SKINTIFIC "Glow Cushion & Peptide Lip
Serum Set" at "USD 23.56". The price came from 20 offers the variant-identity backfill wrote on
09-10 from seed_data.variants. The seed has since been re-read in AUD (snapshot AUD 57, column
AUD 56, mirror offer AUD 56, store's live read AUD). Nothing retired the 20 USD rows.

WHICH of a seed's currencies is its currency. Measured on prod 2026-10-19 (read-only censuses
spccy-1019 / spccy2-1019, active seeds that carry a live projected offer):

  * the SNAPSHOT (seed_data.snapshot.price_currency) is the page as read. Where the column
    disagrees with it, the column holds the writers' old `or "USD"` default (column USD with
    snapshot GBP 483, QAR 367, EUR 166, INR 108, ZAR 64 seeds). A USD offer copied off such a
    column is an amount that was read in another currency.
  * the COLUMN (price_currency) stands in when the seed kept no snapshot currency.
  * seed_data.price_currency is NOT evidence. It is a copy the writers default to "USD" as well:
    16 serving SGD offers have column SGD and snapshot SGD, with amounts equal to both, while
    their seed_data says "USD".

So a seed's currency is its snapshot's, else its column's, else none. Nothing here defaults, and
nothing infers a currency from a market or the reverse (services/region_pricing).

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
    snapshot = _as_dict(_as_dict(seed.get("seed_data")).get("snapshot"))
    return _code(snapshot.get("price_currency"))


def seed_currency_of(seed: Mapping[str, Any]) -> Optional[str]:
    """The seed's own currency: its snapshot's, else its price_currency column's, else None."""
    return snapshot_currency_of(seed) or _code(seed.get("price_currency"))


def seed_currency_sql(alias: str) -> str:
    """`seed_currency_of` in SQL for an external_product_seeds alias; NULL means the seed states no
    currency. A seed_data or snapshot stored as anything but a JSON object (a JSON string) hides the
    page read, so it states none: not parsed here, where one malformed string would abort the read.
    (Prod 2026-10-19, census spccy5-1019b: active seeds' snapshots are 26,656 objects, 294 JSON null,
    0 strings.)"""
    column = f"nullif(upper(trim({alias}.price_currency)), '')"
    snapshot = f"{alias}.seed_data->'snapshot'"
    return (
        f"(CASE"
        f" WHEN {alias}.seed_data IS NOT NULL AND jsonb_typeof({alias}.seed_data) NOT IN ('object', 'null') THEN NULL"
        f" WHEN jsonb_typeof({snapshot}) = 'object'"
        f" THEN coalesce(nullif(upper(trim({alias}.seed_data#>>'{{snapshot,price_currency}}')), ''), {column})"
        f" WHEN {snapshot} IS NULL OR jsonb_typeof({snapshot}) = 'null' THEN {column}"
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
    active = (
        f"eps_own.attached_product_key = {product_key_expr}"
        f" AND lower(trim(coalesce(eps_own.status, ''))) = 'active'"
    )
    return (
        f"(coalesce({offer_alias}.source_system, '') NOT IN ({systems})"
        f" OR (EXISTS (SELECT 1 FROM external_product_seeds eps_own WHERE {active})"
        f" AND NOT EXISTS (SELECT 1 FROM external_product_seeds eps_own WHERE {active}"
        f" AND {seed_currency_sql('eps_own')} IS DISTINCT FROM upper(trim(coalesce({offer_alias}.currency, ''))))))"
    )
