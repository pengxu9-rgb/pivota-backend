"""The stock claim an external seed product is served with — one rule for both builders.

`routes/agent_api.py` and `routes/agent_sdk_fixed.py` each hold a routed copy of
`_build_external_seed_product`. Until 2026-09-25 both hard-coded product-level
`in_stock: True, inventory_quantity: 999` whenever the referral gate accepted the seed's
catalog facts, so a seed stored `out_of_stock` was advertised in stock. agent_v2 publishes
that boolean as `offers[].availability.in_stock`, and the gateway lets a boolean `in_stock`
outrank every other stock signal.

Every value is read through `utils.availability_vocabulary.normalize_availability`, the
shared choke point, NOT the builders' old `_availability_to_in_stock`, which read an
absent value as in stock and missed "out of stock" with a space. Unknown stays unknown.

Measured 2026-09-25 across 12,020 active seeds: the `availability` column is always
populated (10,636 in_stock / 1,384 out_of_stock), and 272 seeds have a column that an
explicit variant contradicts (156 column-out/variant-in, 116 the reverse).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from utils.availability_vocabulary import IN_STOCK, OUT_OF_STOCK, normalize_availability


def _state(value: Any) -> Optional[bool]:
    verdict = normalize_availability(value)
    if verdict == IN_STOCK:
        return True
    if verdict == OUT_OF_STOCK:
        return False
    return None


def _stored_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every stored variant, top-level first, else the snapshot's.

    Read HERE rather than taken from the caller: the two builders' own variant readers
    differ (agent_sdk_fixed's has no snapshot fallback), and handing each its own list
    gave the same seed a different claim depending on which lane served it.
    """
    for container in (seed_data, seed_data.get("snapshot")):
        if isinstance(container, dict) and isinstance(container.get("variants"), list):
            return [v for v in container["variants"] if isinstance(v, dict)]
    return []


def seed_stock_state(seed_row: Dict[str, Any], seed_data: Dict[str, Any]) -> Optional[bool]:
    """The seed's OWN product-level stock claim, or None when it makes none.

    Precedence: the `availability` column, which every writer stamps as the seed's
    summary. An explicit variant that CONTRADICTS the column makes the claim unknown
    rather than picking a side. With no usable column, explicit variants decide (any in
    -> in, every variant out -> out), then seed_data / snapshot `availability`.
    """
    seed_variants = _stored_variants(seed_data)
    variant_states = [
        state
        for state in (_state(v.get("availability")) for v in seed_variants)
        if state is not None
    ]
    if True in variant_states:
        variant_state: Optional[bool] = True
    elif variant_states and len(variant_states) == len(seed_variants):
        variant_state = False
    else:
        variant_state = None

    column_state = _state(seed_row.get("availability"))
    if column_state is not None:
        if variant_state is not None and variant_state != column_state:
            return None
        return column_state
    if variant_state is not None:
        return variant_state
    snapshot = seed_data.get("snapshot")
    for value in (
        seed_data.get("availability"),
        snapshot.get("availability") if isinstance(snapshot, dict) else None,
    ):
        state = _state(value)
        if state is not None:
            return state
    return None


def seed_variant_in_stock(availability: Any, product_state: Optional[bool]) -> bool:
    """A served variant's stock: its own explicit signal, else the product's claim.

    A variant with no signal of its own INHERITS the product claim, the same fallback
    `services/external_seed_audit` applies. Without it, an out-of-stock product served
    in-stock variants, and the gateway's offer card reads the variant's `in_stock` first.
    A variant of an unknown product with no signal keeps the builders' pre-existing
    in-stock default; that default is out of this module's scope.
    """
    own = _state(availability)
    if own is not None:
        return own
    if product_state is not None:
        return product_state
    return True


def seed_stock_fields(stock_state: Optional[bool]) -> Dict[str, Any]:
    """Product-level stock fields for a seed whose catalog facts are trusted."""
    if stock_state is True:
        return {"in_stock": True, "inventory_quantity": 999}
    if stock_state is False:
        return {"in_stock": False, "inventory_quantity": 0, "availability": "out_of_stock"}
    # No boolean at all, never `in_stock: None`: agent_v2 reads None as False (a sold-out
    # claim) while a missing key gets the default it already gives live-verification rows,
    # and the gateway falls through to the variants.
    return {"availability": "unknown"}
