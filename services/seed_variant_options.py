"""One reader for the option axis a seed variant carries.

TWO WRITERS, TWO SHAPES. `external_product_seeds.seed_data.variants[].options` is
written by two lanes that never agreed on a shape:

  * `catalog_enrichment_agent.ingestion` writes a LIST of `{"name", "value"}`
    pairs — the shape the PDP renderer reads.
  * `routes/employee_products.py` (the employee CSV lane) writes a MAPPING,
    `{option_name: option_value}`, and has done since long before the list form
    existed.

Three builders read that column back. A reader that understands one shape does
not merely miss the other, it silently discards an axis that lane already had —
which is how the CSV lane stayed unlabelled on the page while carrying the
answer in the row all along. So those three decide the shape here and differ
only in the form they hand on: the PDP-facing pair emit the list the renderer
reads, while `agent_shop_gateway` keeps the mapping it has always emitted.

Not the only reader in the repo: `beauty_external_ranking._normalize_seed_variant_options`
also takes both shapes and additionally accepts `key`/`label` aliases that no
writer emits. Consolidating the two is worth doing and is not this change.

Malformed entries are dropped rather than forwarded: a half-formed pair reaches
the page as a selector entry naming no choice.
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["normalize_seed_variant_options", "seed_variant_options_as_mapping"]


def normalize_seed_variant_options(raw: Any) -> List[Dict[str, str]]:
    """Either stored shape -> the list of `{"name", "value"}` pairs the PDP reads."""
    pairs: List[Dict[str, str]] = []
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = [
            (o.get("name"), o.get("value"))
            for o in raw
            if isinstance(o, dict)
        ]
    else:
        return pairs

    seen: set = set()
    for name, value in items:
        # `str()` of a dict or a list is NON-EMPTY, so coercing first and testing
        # for emptiness after lets `{"Shade": {"hex": "#f00"}}` through as the
        # literal text "{'hex': '#f00'}" — a selector entry naming no choice,
        # which is the shape this module exists to refuse. Only a scalar can be
        # a label. bool is excluded on purpose: True/False name nothing.
        if isinstance(name, bool) or isinstance(value, bool):
            continue
        if not isinstance(name, (str, int, float)) or not isinstance(value, (str, int, float)):
            continue
        name = str(name).strip()
        value = str(value).strip()
        if not name or not value:
            continue
        if (name, value) in seen:
            continue
        seen.add((name, value))
        pairs.append({"name": name, "value": value})
    return pairs


def seed_variant_options_as_mapping(raw: Any) -> Dict[str, str]:
    """Either stored shape -> the `{name: value}` mapping this repo's
    `StandardProductVariant.options` is typed for. First pair wins on a repeated
    axis name, because a mapping cannot hold both and dropping the earlier one
    would silently reorder what the page shows."""
    mapping: Dict[str, str] = {}
    for pair in normalize_seed_variant_options(raw):
        mapping.setdefault(pair["name"], pair["value"])
    return mapping
