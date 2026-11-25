"""The image of the exact thing a purchase bought, for the AgenPas buyer page (internal read only).

THE RULE (the owner, 2026-11-25: "Variant image only", then "Only a distinct SKU image"). The page shows an image
only when it is the image of what was bought. A wrong shade on the approval screen claims more than we know,
and the Shopify sync copies the product's hero image onto EVERY sku, so a sku's image_url alone does not say
it is that variant's own:

  * the purchase's sku(s) are found from the rail row:
      - Reap variant lane: catalog_skus.sku_key = variant_key;
      - Reap cart-link lane (`variant_key` "shopify:<n>") and A2A (the cart line of `checkout_url`): every sku
        of the product whose source_variant_id names Shopify variant <n> (routes/agent_commerce_reap's
        `_cart_numeric_variant`, the cart lane's one sku rule);
    the `<product_key>::canonical` placeholder names no variant and is never one of them;
  * the bought skus' images must agree (one distinct cleaned URL), or nothing;
  * a product with ONE variant (counted over EVERY sku row, withdrawn ones too: a shade withdrawn from the
    catalog is still a shade): the sku's own image when it differs from the product's; else the product's image,
    but only when the purchase names no variant of its own (no variant title, or Shopify's "Default Title"): a
    product the catalog knows one named variant of may have more on the store;
  * a product with several variants: the sku image only when it DIFFERS from the product's image (a copied
    hero is not the variant's own), never the product image.
Anything else (no row, no catalog row, a suppressed row, no bought sku, too many skus, a URL that is not a
clean public https URL): None, and the page shows no image.

Writes nothing. Raises on a read fault; the route decides what a fault means. The URL goes only to the AgenPas
gateway, which fetches it server-side (the buyer's browser never loads a store's host).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

from db.database import database

#: Longest image URL the page will name.
PRODUCT_IMAGE_URL_MAX_CHARS = 2048
#: A product with more live skus than this is not one whose bought variant can be named (the cart lane's cap).
_MAX_SKUS = 50

_REAP_ROW_SQL = """
    SELECT product_key, variant_key, variant_title
      FROM reap_agentic_purchases
     WHERE id = :id AND agent_id = :agent_id AND agent_user_ref_hash = :owner
"""
_A2A_ROW_SQL = """
    SELECT product_key, checkout_url, variant_title
      FROM a2a_rail_purchases
     WHERE id = :id AND agent_id = :agent_id AND agent_user_ref_hash = :owner
"""
_PRODUCT_SQL = """
    SELECT image_url
      FROM catalog_products
     WHERE product_key = :product_key
       AND suppression_reason IS NULL AND suppressed_at IS NULL
"""
# EVERY sku row, withdrawn ones too: they count as variants (THE RULE); only live rows can be the one bought.
_SKUS_SQL = """
    SELECT sku_key, source_variant_id, image_url, suppression_reason, suppressed_at
      FROM catalog_skus
     WHERE product_key = :product_key
     ORDER BY sku_key LIMIT 51
"""
# A host as a name: dot-separated labels of letters, digits and hyphens (an optional trailing dot), optionally :443.
# No user info, no other port, no "%", "\\", ";" or anything else a parser could read two ways.
_AUTHORITY_RE = re.compile(r"^((?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+)\.?(?::443)?\Z")
# A label a resolver could read as a number (decimal, octal or hex): a host made only of these is an IP address.
_NUMERIC_LABEL_RE = re.compile(r"^(?:[0-9]+|0[xX][0-9A-Fa-f]*)\Z")
#: Shopify's title for a product's only variant.
_DEFAULT_VARIANT_TITLE = "default title"



def clean_product_image_url(raw: Any) -> Optional[str]:
    """The stored URL when it is a clean public https URL, else None. Never repairs one.

    Surrounding whitespace dropped; at most 2048 characters; printable ASCII only (no space, control or
    non-ASCII character inside); scheme https; no fragment; the authority exactly a host NAME (two or more
    labels of letters, digits and hyphens, not all of them numeric or hex, so never an IP address in any
    spelling) with no port or exactly ":443": no user info, no "%", "\\" or ";"."""
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url or len(url) > PRODUCT_IMAGE_URL_MAX_CHARS:
        return None
    if any(not ("!" <= ch <= "~") for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or parts.fragment:
        return None
    authority = _AUTHORITY_RE.match(parts.netloc)
    if authority is None or all(_NUMERIC_LABEL_RE.match(label) for label in authority.group(1).split(".")):
        return None
    return url


async def _rail_row(rail: str, rail_purchase_id: str, agent_id: str, owner: str) -> Optional[Dict[str, Any]]:
    sql = {"reap": _REAP_ROW_SQL, "a2a": _A2A_ROW_SQL}.get(rail)
    if sql is None:
        return None
    row = await database.fetch_one(sql, {"id": rail_purchase_id, "agent_id": agent_id, "owner": owner})
    return dict(row) if row is not None else None


def _bought(rail: str, row: Mapping[str, Any], product_key: str, real: List[Dict[str, Any]],
            numeric_variant) -> List[Dict[str, Any]]:
    """The real skus that are the thing bought, or [] when the row does not name one."""
    from services.reap_cart_link import cart_link_line

    if rail == "reap":
        variant_key = str(row.get("variant_key") or "").strip()
        if not variant_key:
            return []
        if variant_key.startswith("shopify:"):
            wanted = variant_key[len("shopify:"):]
        else:
            return [sku for sku in real if str(sku.get("sku_key") or "") == variant_key]
    else:
        line = cart_link_line(row.get("checkout_url"))
        if line is None:
            return []
        wanted = str(line[0])
    if not wanted.isdigit():
        return []
    return [sku for sku in real if numeric_variant(sku.get("source_variant_id"), product_key) == wanted]


async def product_image_for_owner(rail: str, rail_purchase_id: str, agent_id: str, owner: str) -> Optional[str]:
    """The image URL of what this owner's purchase bought (THE RULE above), or None."""
    import routes.agent_commerce_reap as reap_route

    row = await _rail_row(rail, rail_purchase_id, agent_id, owner)
    if row is None:
        return None
    product_key = str(row.get("product_key") or "").strip()
    if not product_key:
        return None
    product = await database.fetch_one(_PRODUCT_SQL, {"product_key": product_key})
    if product is None:
        return None
    skus = [dict(s) for s in await database.fetch_all(_SKUS_SQL, {"product_key": product_key})]
    if len(skus) > _MAX_SKUS:
        return None
    real = [sku for sku in skus if not reap_route._is_placeholder_sku(sku, product_key)]
    live = [sku for sku in real if sku.get("suppression_reason") is None and sku.get("suppressed_at") is None]
    bought = _bought(rail, row, product_key, live, reap_route._cart_numeric_variant)
    if not bought:
        return None
    images = {clean_product_image_url(sku.get("image_url")) for sku in bought} - {None}
    if len(images) > 1:
        return None  # the skus for this variant disagree: no image rather than a guess
    sku_image = next(iter(images), None)
    product_image = clean_product_image_url(dict(product).get("image_url"))
    # The sku's own image, when it differs from the product's, is the variant's own on any product.
    if sku_image is not None and sku_image != product_image:
        return sku_image
    # Otherwise only a ONE-variant product shows the product's image, and only for a purchase that names no
    # variant of its own. Variant identity = the Shopify variant a sku names, else its key; withdrawn rows count.
    variants = {reap_route._cart_numeric_variant(sku.get("source_variant_id"), product_key) or str(sku.get("sku_key"))
                for sku in real}
    title = str(row.get("variant_title") or "").strip().lower()
    if len(variants) == 1 and title in ("", _DEFAULT_VARIANT_TITLE):
        return product_image
    return None
