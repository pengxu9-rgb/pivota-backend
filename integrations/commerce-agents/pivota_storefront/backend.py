"""``PivotaStorefrontBackend``: the blueprint's storefront interface over Pivota's
MCP tools.

What maps, and to what:

| Backend method | Pivota tool | Notes |
|---|---|---|
| ``search_products`` | ``search_catalog`` | rows are plain products; ids carry the merchant |
| ``get_product_details`` | ``get_product`` | a record with option-bearing variants is a family |
| ``get_disclosure`` | ``get_product`` + ``include: ["decision"]`` | Pivota Insights as the facts box |
| cart methods | none | held here per session; Pivota has no cart |
| ``checkout_handoff`` | ``create_checkout_session`` then ``create_payment_link`` | one hosted page per merchant |
| ``get_order`` | ``get_order`` | |
| ``get_orders`` | none | Pivota lists no orders for a buyer; returns ``[]`` |
| ``search_policies`` | none | returns ``[]``; turn ``enable_policies`` off |
| ``get_fulfillment_options`` | none | ``NotOffered``; turn ``enable_fulfillment`` off |

The blueprint's marketplace rule (``docs/backends.md``) is followed exactly: a
record has one price, the one Pivota's index would sell, and checkout hands off
one link per seller.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from shopping_agent.backend import NotOffered, StorefrontBackend, Unavailable
from shopping_agent.types import (
    Cart,
    CartItem,
    CheckoutHandoff,
    Disclosure,
    FulfillmentOption,
    Order,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    UserPreferences,
)

from .ids import ProductRef, decode_product_id, encode_product_id
from .mapping import (
    checkout_session_id_of,
    details_from_record,
    disclosure_from_record,
    hosted_checkout_url_of,
    order_from_record,
    summary_from_record,
    variant_details,
)
from .transport import McpTransport, ToolCallError

logger = logging.getLogger(__name__)

_UNKNOWN_PRODUCT_CODES = {"UNKNOWN_PRODUCT_ID", "PRODUCT_NOT_FOUND", "NOT_FOUND"}


class PivotaShoppingSession(ShoppingSessionContext):
    """The session context plus what Pivota's checkout needs: the buyer's email for
    the hosted page and, for a signed-in buyer, the bearer the door accepts. Both
    live here, beside the identity, and never in a tool argument."""

    customer_email: str | None = None
    bearer_token: str | None = None


class PivotaStorefrontBackend(StorefrontBackend):
    def __init__(
        self,
        transport: McpTransport,
        *,
        currency: str = "USD",
        merchant_id: str | None = None,
        max_page_size: int = 50,
    ) -> None:
        """``merchant_id`` scopes search to one store; leave it None for the whole
        index. Carts and the records that back them are kept per session in this
        process; a multi-process host supplies its own session affinity."""
        self._transport = transport
        self._currency = currency
        self._merchant_id = merchant_id
        self._max_page_size = max_page_size
        self._carts: dict[str, Cart] = {}
        self._resolved: dict[str, dict[str, ProductDetails]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- helpers -----------------------------------------------------------------

    async def _call(self, session: ShoppingSessionContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        bearer = getattr(session, "bearer_token", None)
        return await self._transport.call_tool(name, arguments, bearer=bearer)

    def _lock(self, session: ShoppingSessionContext) -> asyncio.Lock:
        lock = self._locks.get(session.session_id)
        if lock is None:
            lock = self._locks[session.session_id] = asyncio.Lock()
        return lock

    def _remember(self, session: ShoppingSessionContext, family: ProductDetails) -> None:
        self._resolved.setdefault(session.session_id, {})[family.product_id] = family

    async def _family(self, session: ShoppingSessionContext, ref: ProductRef) -> ProductDetails | None:
        family_id = encode_product_id(ref.family)
        cached = self._resolved.get(session.session_id, {}).get(family_id)
        if cached is not None:
            return cached
        try:
            record = await self._call(
                session, "get_product", {"merchant_id": ref.merchant_id, "product_id": ref.product_id}
            )
        except ToolCallError as exc:
            if exc.code in _UNKNOWN_PRODUCT_CODES or exc.retriable is False:
                return None
            raise
        body = record.get("product") if isinstance(record.get("product"), dict) else record
        if not body or not (body.get("title") or body.get("product_id") or body.get("id")):
            return None
        family = details_from_record(body, ref.family, currency=self._currency)
        self._remember(session, family)
        return family

    def _cart(self, session: ShoppingSessionContext) -> Cart:
        cart = self._carts.get(session.session_id)
        if cart is None:
            cart = self._carts[session.session_id] = Cart(currency=self._currency)
        return cart

    # -- catalog -----------------------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        arguments: dict[str, Any] = {
            "query": query,
            "page_size": max(1, min(int(limit), self._max_page_size)),
            "currency": self._currency,
        }
        if self._merchant_id:
            arguments["merchant_id"] = self._merchant_id
        if filters is not None:
            if filters.category:
                arguments["category"] = filters.category
            if filters.min_price is not None:
                arguments["price_min"] = float(filters.min_price)
            if filters.max_price is not None:
                arguments["price_max"] = float(filters.max_price)
            if filters.attributes.get("in_stock_only", "").lower() in {"1", "true", "yes"}:
                arguments["in_stock_only"] = True
        result = await self._call(session, "search_catalog", arguments)
        rows = result.get("products")
        products: list[Product] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            merchant_id = row.get("merchant_id") or self._merchant_id
            product_id = row.get("product_id") or row.get("id")
            if not merchant_id or not product_id:
                continue
            ref = ProductRef(str(merchant_id), str(product_id))
            products.append(summary_from_record(row, ref, currency=self._currency))
            if len(products) >= limit:
                break
        return products

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        ref = decode_product_id(product_id)
        if ref is None:
            return None
        family = await self._family(session, ref)
        if family is None:
            return None
        if ref.is_variant:
            return variant_details(family, product_id)
        return family

    async def get_disclosure(
        self, session: ShoppingSessionContext, product_id: str
    ) -> Disclosure | None:
        ref = decode_product_id(product_id)
        if ref is None:
            return None
        try:
            record = await self._call(
                session,
                "get_product",
                {"merchant_id": ref.merchant_id, "product_id": ref.product_id, "include": ["decision"]},
            )
        except ToolCallError:
            return None
        body = record.get("product") if isinstance(record.get("product"), dict) else record
        return disclosure_from_record(body, product_id)

    # -- cart --------------------------------------------------------------------

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        return self._cart(session).model_copy(deep=True)

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        ref = decode_product_id(product_id)
        if ref is None:
            raise Unavailable(f"{product_id} is not a product this store knows")
        family = await self._family(session, ref)
        if family is None:
            raise Unavailable(f"{product_id} is not a product this store knows")
        if ref.is_variant:
            line = next((v for v in family.variants if v.product_id == product_id), None)
            if line is None:
                raise Unavailable(f"{product_id} is not a variant of {family.product_id}")
        else:
            if family.has_options:
                # The executor holds a family before this point; a backend used
                # from another path gets the same answer, ids only.
                in_stock = [v.product_id for v in family.variants if v.in_stock]
                listed = ", ".join(in_stock[:6]) if in_stock else "no variant in stock"
                raise Unavailable(
                    f"{product_id} is sold as variants; add one of them: {listed}"
                )
            line = family
        if not line.in_stock:
            if ref.is_variant:
                in_stock = [v.product_id for v in family.variants if v.in_stock]
                listed = ", ".join(in_stock[:6]) if in_stock else "no other variant"
                raise Unavailable(
                    f"{product_id} is out of stock; in-stock variants of {family.product_id}: {listed}"
                )
            raise Unavailable(f"{product_id} is out of stock")
        async with self._lock(session):
            cart = self._cart(session)
            existing = next((i for i in cart.items if i.product_id == product_id), None)
            if existing is not None:
                existing.quantity += max(1, quantity)
            else:
                cart.items.append(
                    CartItem(
                        product_id=product_id,
                        title=line.title,
                        price=line.price,
                        quantity=max(1, quantity),
                        image_url=line.image_url,
                        option_values=line.option_values,
                        variant_of=line.variant_of,
                    )
                )
            return cart.model_copy(deep=True)

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        async with self._lock(session):
            cart = self._cart(session)
            for item in cart.items:
                if item.product_id == product_id:
                    item.quantity = max(1, quantity)
            return cart.model_copy(deep=True)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        async with self._lock(session):
            cart = self._cart(session)
            cart.items = [i for i in cart.items if i.product_id != product_id]
            return cart.model_copy(deep=True)

    # -- customer context --------------------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        return UserPreferences(user_id=session.user_id)

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        """One hosted payment page per merchant in the cart. The page needs the
        buyer's email, held on the session; without it the host's own checkout
        route stays the destination, and this logs why."""
        email = getattr(session, "customer_email", None)
        if not email:
            logger.warning("checkout_handoff: session %s carries no customer_email; host route applies", session.session_id)
            return []
        by_merchant: dict[str, list[CartItem]] = {}
        for item in cart.items:
            ref = decode_product_id(item.product_id)
            if ref is None:
                continue
            by_merchant.setdefault(ref.merchant_id, []).append(item)
        handoffs: list[CheckoutHandoff] = []
        for merchant_id, items in by_merchant.items():
            lines = []
            for item in items:
                ref = decode_product_id(item.product_id)
                assert ref is not None
                line: dict[str, Any] = {"product_id": ref.product_id, "quantity": item.quantity}
                if ref.variant_id:
                    line["variant_id"] = ref.variant_id
                lines.append(line)
            key = _idempotency_key(session.session_id, merchant_id, lines)
            quote: dict[str, Any] = {"merchant_id": merchant_id, "items": lines, "customer_email": email}
            created = await self._call(
                session, "create_checkout_session", {"idempotency_key": key, "quote": quote}
            )
            session_id = checkout_session_id_of(created)
            if not session_id:
                raise ToolCallError("create_checkout_session returned no session id")
            link = await self._call(
                session,
                "create_payment_link",
                {"idempotency_key": key + "-link", "session_id": session_id, "customer_email": email},
            )
            url = hosted_checkout_url_of(link)
            if not url:
                raise ToolCallError("create_payment_link returned no hosted checkout URL")
            # The seller label is the merchant id: a merchant name is catalog text
            # and this string leaves the fence.
            handoffs.append(CheckoutHandoff(url=url, seller=merchant_id, label="Pay on the merchant's page"))
        return handoffs

    # -- orders and policies -----------------------------------------------------

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        # Pivota exposes no order listing for a buyer; an order is read by its id.
        return []

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        try:
            record = await self._call(session, "get_order", {"order_id": order_id})
        except ToolCallError as exc:
            if exc.retriable is False or (exc.code or "").upper().endswith("NOT_FOUND"):
                return None
            raise
        return order_from_record(record, currency=self._currency)

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        return []

    # -- fulfillment --------------------------------------------------------------

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        raise NotOffered("delivery options are quoted at checkout, not before it")


def _idempotency_key(session_id: str, merchant_id: str, lines: list[dict[str, Any]]) -> str:
    """Stable for the same session, merchant, and cart lines, so a replayed handoff
    returns the same session and page instead of a second one."""
    digest = hashlib.sha256()
    digest.update(session_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(merchant_id.encode("utf-8"))
    for line in sorted(lines, key=lambda l: (l["product_id"], l.get("variant_id") or "")):
        digest.update(f"|{line['product_id']}#{line.get('variant_id') or ''}x{line['quantity']}".encode("utf-8"))
    return "ca-" + digest.hexdigest()[:32]
