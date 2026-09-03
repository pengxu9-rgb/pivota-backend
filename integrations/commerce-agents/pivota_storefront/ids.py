"""One id namespace for the agent, self-describing on the wire.

The blueprint gives a product one id and resolves it with ``get_product_details``
alone. Pivota addresses a product by ``(merchant_id, product_id)`` and a variant
by a third key, and two merchants can reuse the same platform id. So every id
the agent sees carries all of it:

    merch_x/9854988910809              a family or a plain product
    merch_x/9854988910809#4739         one of its variants

The delimiters never appear in Pivota's own ids (``merch_…``, ``sig_…``, ``ext_…``,
numeric, ``prod::a::b::c``). An id the agent invents that does not parse is an
unknown product, which is the answer the blueprint expects for it.
"""

from __future__ import annotations

from dataclasses import dataclass

MERCHANT_SEP = "/"
VARIANT_SEP = "#"


@dataclass(frozen=True)
class ProductRef:
    merchant_id: str
    product_id: str
    variant_id: str | None = None

    @property
    def family(self) -> ProductRef:
        return ProductRef(self.merchant_id, self.product_id)

    @property
    def is_variant(self) -> bool:
        return self.variant_id is not None


def encode_product_id(ref: ProductRef) -> str:
    text = f"{ref.merchant_id}{MERCHANT_SEP}{ref.product_id}"
    if ref.variant_id is not None:
        text = f"{text}{VARIANT_SEP}{ref.variant_id}"
    return text


def decode_product_id(text: str) -> ProductRef | None:
    """The ref an agent-facing id names, or None when the id is not one of ours."""
    if not isinstance(text, str):
        return None
    merchant_id, sep, rest = text.partition(MERCHANT_SEP)
    if not sep or not merchant_id or not rest:
        return None
    product_id, vsep, variant_id = rest.partition(VARIANT_SEP)
    if not product_id or (vsep and not variant_id):
        return None
    return ProductRef(merchant_id, product_id, variant_id or None)
