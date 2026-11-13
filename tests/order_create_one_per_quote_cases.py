"""One quote backs at most one order per agent: the harness and the cases, for both dialects.

Collected by tests/test_order_create_one_order_per_quote.py (SQLite) and
tests/test_order_create_one_order_per_quote_postgres.py (the production dialect). No `test_`
prefix on this file, so pytest collects the cases only through those two.

WHAT IS REAL. The routes (`POST /agent/v1/orders/create`, `POST /agent/v2/orders`) over
httpx.ASGITransport with the real error envelope, `agent_create_order`, `create_new_order`,
`db.orders.create_order` with its quote claim, `find_replayable_order_for_create`, and real `orders`
and `order_quote_claims` rows in the database DATABASE_URL names.

WHAT IS DOUBLED. Everything around the order row: the agent context, governance, the quote store
(in memory, with a consume that can be made to not land), merchant, inventory, payment routing
and the payment surface, telemetry.

HOW A DATABASE FAILURE IS INJECTED. `db.orders.database` is wrapped (`FaultyDatabase`): each
statement `db.orders` sends is named by what it is (`replay_lookup`, `claim_insert`, `claim_select`,
`claim_order_exists`, `order_insert`) and a fault raises a busy error either BEFORE the statement
runs (it never reached the database) or AFTER it ran (it committed and the answer was lost). Every
statement is its own autocommit transaction, so "after the insert, before the commit" and "after
the commit" are the same point: AFTER. Gates (asyncio events) hold one request at a named
statement while another runs, so races are driven in a fixed order with no sleeps.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

MERCHANT = "merch_oopq_fixture"
AGENT_A = "agent_oopq_a"
AGENT_B = "agent_oopq_b"

# (market, shipping country, the quote's currency, the quote's total). "none" is a request that
# names no market at all: its quote is in a currency no default in this codebase would produce.
MARKETS: List[Tuple[str, str, str, str]] = [
    ("US", "US", "USD", "9.09"),
    ("SG", "SG", "SGD", "12.40"),
    ("GB", "GB", "GBP", "7.15"),
    ("DE", "DE", "EUR", "8.30"),
    ("none", "NZ", "NZD", "15.75"),
]
MARKET_IDS = [m[0] for m in MARKETS]

BUSY = "canceling statement due to statement timeout"

_REQUEST_TAG: contextvars.ContextVar[str] = contextvars.ContextVar("oopq_request_tag", default="-")
_REQUEST_AGENT: contextvars.ContextVar[str] = contextvars.ContextVar("oopq_request_agent", default=AGENT_A)


# ---------------------------------------------------------------------------
# The database seam
# ---------------------------------------------------------------------------


def classify_statement(query: Any) -> str:
    if isinstance(query, str):
        flat = " ".join(query.split()).lower()
        if flat.startswith("insert into order_quote_claims"):
            return "claim_insert"
        if flat.startswith("select order_id from order_quote_claims"):
            return "claim_select"
        if flat.startswith("select order_id from orders where order_id"):
            return "claim_order_exists"
        if "from orders" in flat and "order by created_at desc" in flat:
            return "replay_lookup"
        return "other"
    name = type(query).__name__.lower()
    table = getattr(getattr(query, "table", None), "name", "")
    if name == "insert" and table == "orders":
        return "order_insert"
    return "other"


@dataclass
class Fault:
    step: str
    when: str = "before"  # "before": never ran. "after": ran, the answer was lost.
    tag: Optional[str] = None  # only for this request; None = any
    skip: int = 0  # let this many matching statements through first
    times: int = 1  # then fail this many (a large number = every one)
    message: str = BUSY
    armed_after: Optional[str] = None  # inert until a statement of this name has been sent
    hits: int = 0

    def take(self, step: str, when: str, tag: str, seen: List[str]) -> bool:
        if step != self.step or when != self.when:
            return False
        if self.armed_after is not None and self.armed_after not in seen:
            return False
        if self.tag is not None and tag != self.tag:
            return False
        if self.skip > 0:
            self.skip -= 1
            return False
        if self.times <= 0:
            return False
        self.times -= 1
        self.hits += 1
        return True


class FaultyDatabase:
    """`db.orders.database`, with named statements, injected failures and gates."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.faults: List[Fault] = []
        self.log: List[Tuple[str, str]] = []
        self._gates: Dict[Tuple[str, str, str], List[Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def apply(self, world: "World", specs: List[Dict[str, Any]]) -> None:
        for spec in specs:
            if spec.get("step") == "after_row":
                world.after_row_busy_runs = set(spec["runs"])
            else:
                self.fail(**spec)

    def fail(self, step: str, when: str = "before", **kwargs: Any) -> Fault:
        fault = Fault(step=step, when=when, **kwargs)
        self.faults.append(fault)
        return fault

    def hold(self, tag: str, step: str, when: str = "before", *, skip: int = 0) -> Tuple[asyncio.Event, asyncio.Event]:
        """Stop request `tag` at its (skip+1)th (step, when). Returns (reached, release)."""
        pair = (asyncio.Event(), asyncio.Event())
        self._gates[(tag, step, when)] = [skip, pair]
        return pair

    def count(self, step: str, tag: Optional[str] = None) -> int:
        return sum(1 for t, s in self.log if s == step and (tag is None or t == tag))

    async def _point(self, step: str, when: str) -> None:
        tag = _REQUEST_TAG.get()
        gate = self._gates.get((tag, step, when))
        if gate is not None and gate[0] > 0:
            gate[0] -= 1
        elif gate is not None:
            del self._gates[(tag, step, when)]
            reached, release = gate[1]
            reached.set()
            await release.wait()
        seen = [s for _, s in self.log[:-1]] if when == "before" else [s for _, s in self.log]
        for fault in self.faults:
            if fault.take(step, when, tag, seen):
                raise RuntimeError(fault.message)

    async def _run(self, method: str, query: Any, values: Any) -> Any:
        step = classify_statement(query)
        self.log.append((_REQUEST_TAG.get(), step))
        await self._point(step, "before")
        call = getattr(self._real, method)
        result = await (call(query, values) if values is not None else call(query))
        await self._point(step, "after")
        return result

    async def execute(self, query: Any, values: Any = None) -> Any:
        return await self._run("execute", query, values)

    async def fetch_one(self, query: Any, values: Any = None) -> Any:
        return await self._run("fetch_one", query, values)

    async def fetch_all(self, query: Any, values: Any = None) -> Any:
        return await self._run("fetch_all", query, values)


# ---------------------------------------------------------------------------
# The quote store (in memory)
# ---------------------------------------------------------------------------


@dataclass
class QuoteStore:
    quotes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    consume_lands: bool = True
    consume_calls: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    # A case may reshape what a quote load returns: called with (quote_id, snapshot, nth load).
    reshape: Optional[Any] = None
    loads: Dict[str, int] = field(default_factory=dict)

    def add(self, *, agent: str = AGENT_A, market: str = "US") -> str:
        _, country, currency, total = next(m for m in MARKETS if m[0] == market)
        quote_id = f"q_oopq_{uuid.uuid4().hex[:20]}"
        self.quotes[quote_id] = {
            "status": "active",
            "agent_id": agent,
            "market": market,
            "country": country,
            "currency": currency,
            "total": total,
            "consumed_order_id": None,
        }
        return quote_id

    def snapshot(self, quote_id: str) -> SimpleNamespace:
        from services.quote_service import QuoteError

        q = self.quotes.get(quote_id)
        if q is None:
            raise QuoteError("QUOTE_NOT_FOUND", "Quote not found")
        if q["status"] == "consumed":
            raise QuoteError(
                "QUOTE_CONSUMED",
                "Quote already consumed",
                debug_id="debug_oopq",
                details={"order_id": q["consumed_order_id"]},
            )
        subtotal = q["total"]
        self.loads[quote_id] = self.loads.get(quote_id, 0) + 1
        snapshot = SimpleNamespace(
            quote_id=quote_id,
            merchant_id=MERCHANT,
            agent_id=q["agent_id"],
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            status="active",
            engine="shopify_storefront_cart",
            engine_ref="cart_ref_oopq",
            request_fingerprint="fp_oopq",
            quote_hash_sha256="h" * 64,
            debug_id="debug_oopq",
            created_at=None,
            request_json={
                "items": [{"product_id": "prod_oopq_1", "variant_id": "var_oopq_1", "quantity": 1}],
                "customer_email": "buyer@example.com",
                "shipping_address": shipping_address(q["country"]),
            },
            snapshot_json={
                "currency": q["currency"],
                "platform": "shopify",
                "pricing": {
                    "subtotal": subtotal,
                    "discount_total": "0.00",
                    "shipping_fee": "0.00",
                    "tax": "0.00",
                    "total": q["total"],
                },
                "line_items": [
                    {
                        "product_id": "prod_oopq_1",
                        "variant_id": "var_oopq_1",
                        "quantity": 1,
                        "title": "Fixture item",
                        "unit_price_original": subtotal,
                        "unit_price_effective": subtotal,
                    }
                ],
            },
        )
        if self.reshape is not None:
            self.reshape(quote_id, snapshot, self.loads[quote_id])
        return snapshot

    def consume(self, quote_id: str, order_id: Optional[str]) -> None:
        self.consume_calls.append((quote_id, order_id))
        if not self.consume_lands:
            raise RuntimeError(BUSY)
        q = self.quotes.get(quote_id)
        if q is not None and q["status"] == "active":
            q["status"] = "consumed"
            q["consumed_order_id"] = order_id


def shipping_address(country: str) -> Dict[str, Any]:
    return {
        "name": "Fixture Buyer",
        "address_line1": "1 Example Street",
        "address_line2": "",
        "city": "Exampleton",
        "state": "",
        "postal_code": "00001",
        "country": country,
        "phone": "",
    }


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class _Context:
    agent_name = "Fixture Agent"
    allowed_merchants = None
    checkout_token_payload = None
    forwarded_by_agent_id = None

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


@dataclass
class Answer:
    status: int
    body: Dict[str, Any]
    retry_after: Optional[str] = None

    @property
    def order_id(self) -> Optional[str]:
        if self.status != 200:
            return None
        if isinstance(self.body.get("order"), dict):
            return self.body["order"].get("order_id")
        return self.body.get("order_id")

    @property
    def error(self) -> Dict[str, Any]:
        """The refusal's own detail, wherever the envelope put it."""
        err = self.body.get("error")
        if isinstance(err, dict):
            details = err.get("details")
            if isinstance(details, dict) and details.get("error"):
                return details
            return {"error": err.get("message") or err.get("code"), **err}
        detail = self.body.get("detail")
        return detail if isinstance(detail, dict) else {"error": str(detail)}

    @property
    def code(self) -> Optional[str]:
        return self.error.get("error")


class World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import db.orders as orders_module
        from main import app

        self.monkeypatch = monkeypatch
        self.app = app
        self.orders_module = orders_module
        self.quotes = QuoteStore()
        self.db = FaultyDatabase(orders_module.database)
        self.guard_present = hasattr(orders_module, "order_quote_claims")
        monkeypatch.setattr(orders_module, "database", self.db)
        self._stub_everything_around_the_order_row()

    # -- doubles -----------------------------------------------------------

    def _stub_everything_around_the_order_row(self) -> None:
        mp = self.monkeypatch
        quotes = self.quotes

        import mvp.events as mvp_events
        import mvp.ledger_events as ledger_events
        import routes.agent_api as agent_api
        import routes.agent_auth as agent_auth
        import routes.agent_v2 as agent_v2
        import routes.order_routes as order_routes
        import services.agent_governance as governance
        import services.agent_webhook_service as agent_webhooks
        import services.quote_first_enforcement as quote_first
        from mvp.idempotency import InMemoryIdempotencyStore

        async def noop(*_: Any, **__: Any) -> None:
            return None

        mp.setattr(mvp_events, "emit_best_effort", lambda **_: None)
        mp.setattr(ledger_events, "emit_best_effort", lambda **_: None)
        mp.setattr(governance.agent_governance, "validate_request", noop)
        mp.setattr(governance.agent_governance, "record_response", noop)
        mp.setattr(agent_webhooks, "emit_agent_webhook_event", noop)
        mp.setattr(agent_auth, "log_agent_request", noop)
        mp.setattr(agent_api, "log_agent_request", noop)

        async def store(_merchant_id: str) -> Dict[str, Any]:
            return {"platform": "shopify", "store_id": "store_oopq"}

        mp.setattr(agent_api, "get_primary_store", store)
        mp.setattr(order_routes, "get_primary_store", store)

        async def quote_required(*, merchant_id: str) -> Tuple[bool, Dict[str, Any]]:
            return True, {"reason": "fixture"}

        mp.setattr(quote_first, "should_require_quote_for_order_create", quote_required)

        async def require_quote_for_caller(quote_id: str, context: Any) -> None:
            from fastapi import HTTPException

            q = quotes.quotes.get(quote_id)
            if q is None or q["agent_id"] != getattr(context, "agent_id", None):
                raise HTTPException(status_code=404, detail="Quote not found")

        mp.setattr(agent_api, "require_quote_for_caller", require_quote_for_caller)
        mp.setattr(agent_v2, "require_quote_for_caller", require_quote_for_caller)

        shared = InMemoryIdempotencyStore()
        world = self

        class _IdempotencyStore:
            async def get(self, *, scope: str, key: str) -> Any:
                if world.idempotency_cache_off:
                    return None
                return await shared.get(scope=scope, key=key)

            async def put(self, *, scope: str, key: str, value: Dict[str, Any]) -> Any:
                if world.idempotency_cache_off:
                    return None
                return await shared.put(scope=scope, key=key, value=value)

        # The response cache is a second replay layer in front of the table. It is OFF by default
        # here so every case exercises the table lookup and the database guard on their own.
        self.idempotency_cache_off = True
        mp.setattr(agent_api, "_AGENT_ORDER_IDEMPOTENCY_STORE", _IdempotencyStore())

        # A second process: the per-merchant lock is process-local, so two resends that land on
        # two instances are not serialised by it. Each request here gets its own lock.
        mp.setattr(agent_api, "_get_order_create_lock", lambda _merchant_id: asyncio.Lock())

        # The create path's own backoff waits (50 ms and up) become a bare yield. `agent_api.asyncio`
        # is the asyncio module, so this is undone by monkeypatch at the end of each case.
        real_sleep = asyncio.sleep

        async def yield_once(_seconds: float, *args: Any, **kwargs: Any) -> None:
            await real_sleep(0)

        mp.setattr(agent_api.asyncio, "sleep", yield_once)

        # -- create_new_order's surroundings --------------------------------
        async def merchant(merchant_id: str) -> Dict[str, Any]:
            return {"merchant_id": merchant_id, "status": "approved"}

        async def inventory(_merchant_id: str, _items: Any) -> Tuple[bool, Dict[str, Any]]:
            return True, {}

        async def load_active_quote_or_raise(self: Any, *, quote_id: str) -> SimpleNamespace:
            return quotes.snapshot(quote_id)

        async def validate_live(self: Any, quote: Any, **_: Any) -> Dict[str, Any]:
            return {"status": "validated", "engine": quote.engine, "engine_ref": quote.engine_ref}

        async def consume(self: Any, quote_id: str, *, order_id: Optional[str] = None) -> None:
            try:
                quotes.consume(quote_id, order_id)
            except Exception:  # noqa: BLE001 - best effort, as the real one
                pass

        async def select_psp(self: Any, **_: Any) -> Tuple[str, Dict[str, Any]]:
            return "stripe", {"route_id": "route_oopq"}

        async def resolve_psp(_merchant_id: str, _hint: Any, **_: Any) -> Tuple[str, str]:
            return "stripe", "psp_oopq"

        async def no_payment(*_: Any, **__: Any) -> Tuple[bool, None, str, str]:
            return False, None, "payment skipped", "stripe"

        async def ready() -> None:
            return None

        async def explicit_psp(**_: Any) -> None:
            return None

        async def not_test(_ids: Any) -> None:
            return None

        import services.test_traffic as test_traffic

        mp.setattr(test_traffic, "read_is_test_merchant_ids", not_test)
        mp.setattr(order_routes, "ensure_database_ready", ready)
        mp.setattr(order_routes, "get_merchant_onboarding", merchant)
        mp.setattr(order_routes, "check_inventory_availability", inventory)
        mp.setattr(order_routes.QuoteService, "load_active_quote_or_raise", load_active_quote_or_raise)
        mp.setattr(order_routes.QuoteService, "validate_quote_snapshot_live", validate_live)
        mp.setattr(order_routes.QuoteService, "consume_quote_best_effort", consume)
        mp.setattr(order_routes, "compute_request_fingerprint", lambda **_: "fp_oopq")
        mp.setattr(order_routes.PaymentRoutingService, "select_psp", select_psp)
        mp.setattr(order_routes, "_resolve_active_order_psp", resolve_psp)
        mp.setattr(order_routes, "_ensure_explicit_preferred_psp_available", explicit_psp)
        mp.setattr(order_routes, "_apply_server_granted_test_psp_stamp", noop)
        mp.setattr(order_routes, "upsert_order_attribution_edge", noop)
        mp.setattr(order_routes, "emit_merchant_webhook_event", noop)

        # "Busy after the row is committed": the order event write that follows the insert is not
        # swallowed by create_new_order, so a busy error there fails the create with the row
        # already written. `after_row_busy_runs` names which runs of the create (1-based) meet it.
        self.after_row_busy_runs: set = set()

        async def order_event(*_: Any, **__: Any) -> None:
            if len(world.create_runs) in world.after_row_busy_runs:
                raise RuntimeError(BUSY)

        mp.setattr(order_routes, "log_order_event", order_event)
        mp.setattr(order_routes, "create_payment_with_failover", no_payment)

        # Count how often the whole create ran (the real one still runs).
        import functools

        real_create_new_order = order_routes.create_new_order
        self.create_runs: List[str] = []

        @functools.wraps(real_create_new_order)
        async def counted_create_new_order(*args: Any, **kwargs: Any) -> Any:
            world.create_runs.append(_REQUEST_TAG.get())
            return await real_create_new_order(*args, **kwargs)

        mp.setattr(order_routes, "create_new_order", counted_create_new_order)

    # -- driving -------------------------------------------------------------

    async def _post(
        self,
        path: str,
        payload: Optional[Dict[str, Any]],
        *,
        agent: str,
        tag: str,
        headers: Optional[Dict[str, str]] = None,
        content: Optional[bytes] = None,
    ) -> Answer:
        from routes.agent_auth import get_agent_context

        async def context() -> _Context:
            return _Context(_REQUEST_AGENT.get())

        self.app.dependency_overrides[get_agent_context] = context
        tag_token = _REQUEST_TAG.set(tag)
        agent_token = _REQUEST_AGENT.set(agent)
        try:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                if content is not None:
                    sent = {"Content-Type": "application/json", **(headers or {})}
                    response = await client.post(path, content=content, headers=sent)
                else:
                    response = await client.post(path, json=payload, headers=headers)
        finally:
            _REQUEST_TAG.reset(tag_token)
            _REQUEST_AGENT.reset(agent_token)
        await self.settle()
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}
        return Answer(response.status_code, body, response.headers.get("retry-after"))

    async def settle(self) -> None:
        """Let fire-and-forget work (the quote consume, usage logging) finish."""
        for _ in range(20):
            pending = [
                t
                for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done() and _is_background(t)
            ]
            if not pending:
                return
            await asyncio.wait(pending, timeout=2)

    async def create_v1(
        self,
        quote_id: str,
        *,
        key: Optional[str] = None,
        agent: str = AGENT_A,
        tag: str = "r1",
        preferred_psp: Optional[str] = None,
        with_quote: bool = True,
    ) -> Answer:
        q = self.quotes.quotes[quote_id]
        payload: Dict[str, Any] = {
            "merchant_id": MERCHANT,
            "customer_email": "buyer@example.com",
            "items": [{"product_id": "prod_oopq_1", "variant_id": "var_oopq_1", "quantity": 1}],
            "shipping_address": shipping_address(q["country"]),
            "currency": q["currency"],
            "metadata": {} if q["market"] == "none" else {"market": q["market"]},
        }
        if with_quote:
            payload["quote_id"] = quote_id
        if key is not None:
            payload["idempotency_key"] = key
        if preferred_psp is not None:
            payload["preferred_psp"] = preferred_psp
        return await self._post("/agent/v1/orders/create", payload, agent=agent, tag=tag)

    async def create_v2(
        self,
        quote_id: str,
        *,
        key: Optional[str] = None,
        agent: str = AGENT_A,
        tag: str = "r1",
        with_quote: bool = True,
    ) -> Answer:
        q = self.quotes.quotes[quote_id]
        payload: Dict[str, Any] = {"buyer_context": {"customer_email": "buyer@example.com"}}
        if with_quote:
            payload["quote_id"] = quote_id
        if q["market"] != "none":
            payload["request_context"] = {"country": q["country"]}
        if key is not None:
            payload["idempotency_key"] = key
        return await self._post("/agent/v2/orders", payload, agent=agent, tag=tag)

    async def create(self, route: str, quote_id: str, **kwargs: Any) -> Answer:
        return await (self.create_v1 if route == "v1" else self.create_v2)(quote_id, **kwargs)

    # -- reading the tables directly (never through the faulty seam) ----------

    async def orders_for(self, quote_id: str, agent: Optional[str] = None) -> List[Dict[str, Any]]:
        from db.orders import orders

        rows = await self.db._real.fetch_all(orders.select().where(orders.c.merchant_id == MERCHANT))
        out = []
        for row in rows:
            record = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
            md = record.get("metadata")
            if isinstance(md, str):
                md = json.loads(md)
            record["metadata"] = md or {}
            if ((md or {}).get("pricing_quote") or {}).get("quote_id") != quote_id:
                continue
            if agent is not None and record.get("agent_id") != agent:
                continue
            if record.get("is_deleted"):
                continue
            out.append(record)
        return out

    async def claims_for(self, quote_id: str) -> List[Dict[str, Any]]:
        rows = await self.db._real.fetch_all(
            "SELECT agent_id, quote_id, order_id FROM order_quote_claims WHERE quote_id = :q",
            {"q": quote_id},
        )
        return [dict(r._mapping) if hasattr(r, "_mapping") else dict(r) for r in rows]


def _is_background(task: "asyncio.Task[Any]") -> bool:
    name = getattr(task.get_coro(), "__qualname__", "") or ""
    return any(part in name for part in ("_bg_", "_run_agent_create_order_background", "background"))


# ---------------------------------------------------------------------------
# Fixtures (imported by both collectors)
# ---------------------------------------------------------------------------


async def _delete_fixture_rows(real_db: Any, *, guard_present: bool) -> None:
    await real_db.execute("DELETE FROM orders WHERE merchant_id = :m", {"m": MERCHANT})
    if guard_present:
        try:
            await real_db.execute("DELETE FROM order_quote_claims WHERE quote_id LIKE 'q_oopq_%'")
        except Exception:  # noqa: BLE001 - a case may have dropped the table
            pass


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    import db.orders as orders_module
    from db.database import database, engine, metadata

    guard_table = getattr(orders_module, "order_quote_claims", None)
    tables = [orders_module.orders] + ([guard_table] if guard_table is not None else [])
    metadata.create_all(engine, tables=tables)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _delete_fixture_rows(database, guard_present=guard_table is not None)

    logger_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore", "asyncio")}
    w = World(monkeypatch)
    try:
        yield w
    finally:
        from routes.agent_auth import get_agent_context

        w.app.dependency_overrides.pop(get_agent_context, None)
        await w.settle()
        if guard_table is not None:
            metadata.create_all(engine, tables=[guard_table])
        await _delete_fixture_rows(database, guard_present=guard_table is not None)
        for name, level in logger_levels.items():
            logging.getLogger(name).setLevel(level)
        if not was_connected and database.is_connected:
            await database.disconnect()


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


async def assert_one_order_at_most(w: World, quote_id: str, answers: List[Answer], *, agent: str = AGENT_A) -> List[Dict[str, Any]]:
    """The three things that must hold whatever failed: at most one order for (agent, quote);
    every 200 names an order that is in the table; every 200 for the quote names the SAME order."""
    rows = await w.orders_for(quote_id, agent)
    assert len(rows) <= 1, f"{len(rows)} orders for one quote: {[r['order_id'] for r in rows]}"
    row_ids = {r["order_id"] for r in rows}
    answered = {a.order_id for a in answers if a.status == 200}
    assert answered <= row_ids, f"a 200 named an order that is not in the table: {answered - row_ids}"
    assert len(answered) <= 1, f"two different orders were answered for one quote: {answered}"
    return rows


def assert_retriable_503(answer: Answer, reason: Optional[str] = None) -> None:
    assert answer.status == 503, (answer.status, answer.body)
    assert answer.code == "TEMPORARY_UNAVAILABLE", answer.body
    assert answer.retry_after == "1", answer.retry_after
    if reason is not None:
        assert answer.error.get("reason") == reason, answer.body


# ---------------------------------------------------------------------------
# 1. The defect, as it was (these three fail on the commit before the fix: two rows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["v1", "v2"])
async def test_one_request_whose_insert_answer_was_lost_writes_one_order(world: World, route: str) -> None:
    """(a) The in-request busy retry. The row is written and its answer is lost; the lookup that
    should find it fails; the retry used to run the create again."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    w.after_row_busy_runs = {1, 2}
    w.db.fail("replay_lookup", "before", times=999, armed_after="order_insert")

    first = await w.create(route, quote, key="key-1")

    rows = await assert_one_order_at_most(w, quote, [first])
    assert len(rows) == 1
    assert_retriable_503(first, "ORDER_REPLAY_LOOKUP_UNAVAILABLE")
    assert w.create_runs == ["r1"], "the create ran again although the lookup could not tell"


@pytest.mark.parametrize("route", ["v1", "v2"])
async def test_resend_with_the_same_key_under_pressure_writes_one_order(world: World, route: str) -> None:
    """(b) The first attempt writes the row and answers 503. The resend's lookup fails too."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    w.db.fail("order_insert", "after", times=1)
    lookups_down = w.db.fail("replay_lookup", "before", times=999, armed_after="order_insert")

    first = await w.create(route, quote, key="key-1", tag="r1")
    second = await w.create(route, quote, key="key-1", tag="r2")

    rows = await assert_one_order_at_most(w, quote, [first, second])
    assert len(rows) == 1
    assert_retriable_503(first)
    assert_retriable_503(second, "ORDER_REPLAY_LOOKUP_UNAVAILABLE")

    lookups_down.times = 0  # the pressure ends
    third = await w.create(route, quote, key="key-1", tag="r3")
    assert third.status == 200 and third.order_id == rows[0]["order_id"]
    await assert_one_order_at_most(w, quote, [first, second, third])


@pytest.mark.parametrize("route", ["v1", "v2"])
async def test_resend_with_another_key_under_pressure_writes_one_order(world: World, route: str) -> None:
    """(c) As (b), and the resend carries a different idempotency key."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    w.db.fail("order_insert", "after", times=1)
    lookups_down = w.db.fail("replay_lookup", "before", times=999, armed_after="order_insert")

    first = await w.create(route, quote, key="key-1", tag="r1")
    second = await w.create(route, quote, key="key-2", tag="r2")

    rows = await assert_one_order_at_most(w, quote, [first, second])
    assert len(rows) == 1
    assert_retriable_503(first)
    assert_retriable_503(second, "ORDER_REPLAY_LOOKUP_UNAVAILABLE")

    lookups_down.times = 0
    third = await w.create(route, quote, key="key-2", tag="r3")
    assert third.status == 200 and third.order_id == rows[0]["order_id"]
    await assert_one_order_at_most(w, quote, [first, second, third])


# ---------------------------------------------------------------------------
# 2. One failure at every step of one request, then a clean resend
# ---------------------------------------------------------------------------

# (id, faults for the first request, the first request's status, orders after it)
#   200         = the request itself recovered and answered its order
#   503         = refused, retriable
# Whatever the first answer, a clean resend (same key or another) must answer 200 with THE order.
_STEP_FAULTS: List[Tuple[str, List[Dict[str, Any]], int, int]] = [
    ("lookup_before_create_fails", [dict(step="replay_lookup", when="before")], 503, 0),
    ("lookup_before_create_fails_not_busy", [dict(step="replay_lookup", when="before", message="connection reset by peer")], 503, 0),
    ("claim_insert_never_ran", [dict(step="claim_insert", when="before")], 200, 1),
    ("claim_insert_ran_answer_lost", [dict(step="claim_insert", when="after")], 200, 1),
    ("claim_select_fails", [dict(step="claim_select", when="before")], 200, 1),
    ("claim_select_answer_lost", [dict(step="claim_select", when="after")], 200, 1),
    ("order_insert_never_ran_once", [dict(step="order_insert", when="before")], 200, 1),
    ("order_insert_never_ran_twice", [dict(step="order_insert", when="before", times=2)], 200, 1),
    ("order_insert_ran_answer_lost", [dict(step="order_insert", when="after")], 200, 1),
    (
        "order_insert_ran_answer_lost_then_lookup_fails",
        [dict(step="order_insert", when="after"), dict(step="replay_lookup", when="before", times=999, armed_after="order_insert", tag="r1")],
        503,
        1,
    ),
    (
        "order_insert_never_ran_twice_then_lookup_fails",
        [dict(step="order_insert", when="before", times=2), dict(step="replay_lookup", when="before", times=999, armed_after="order_insert", tag="r1")],
        503,
        0,
    ),
    ("busy_after_the_row_is_committed", [dict(step="after_row", runs=[1])], 200, 1),
    ("busy_after_the_row_is_committed_on_every_run", [dict(step="after_row", runs=[1, 2, 3])], 200, 1),
    (
        "busy_after_the_row_is_committed_then_lookup_fails",
        [dict(step="after_row", runs=[1, 2]), dict(step="replay_lookup", when="before", times=999, armed_after="order_insert", tag="r1")],
        503,
        1,
    ),
    ("guard_down_for_the_whole_request", [dict(step="claim_insert", when="before", times=999, tag="r1")], 503, 0),
    (
        "guard_down_not_busy",
        [dict(step="claim_insert", when="before", times=999, tag="r1", message="permission denied for table order_quote_claims")],
        503,
        0,
    ),
    (
        "guard_read_down_for_the_whole_request",
        [dict(step="claim_select", when="before", times=999, tag="r1")],
        503,
        0,
    ),
]


@pytest.mark.parametrize("consume_lands", [True, False], ids=["consume_lands", "consume_fails"])
@pytest.mark.parametrize("resend_key", ["key-1", "key-2"], ids=["same_key", "another_key"])
@pytest.mark.parametrize("case", _STEP_FAULTS, ids=[c[0] for c in _STEP_FAULTS])
async def test_a_failure_at_any_step_never_makes_a_second_order_v1(
    world: World, case: Tuple[str, List[Dict[str, Any]], int, int], resend_key: str, consume_lands: bool
) -> None:
    w = world
    _, faults, first_status, rows_after_first = case
    w.quotes.consume_lands = consume_lands
    quote = w.quotes.add()
    w.db.apply(w, faults)

    first = await w.create_v1(quote, key="key-1", tag="r1")

    rows = await assert_one_order_at_most(w, quote, [first])
    assert first.status == first_status, (first.status, first.body)
    assert len(rows) == rows_after_first
    if first.status == 503:
        assert_retriable_503(first)

    w.after_row_busy_runs = set()
    second = await w.create_v1(quote, key=resend_key, tag="r2")
    third = await w.create_v1(quote, key=resend_key, tag="r3")

    rows = await assert_one_order_at_most(w, quote, [first, second, third])
    assert len(rows) == 1
    assert second.status == 200 and third.status == 200, (second.body, third.body)
    assert second.order_id == third.order_id == rows[0]["order_id"]
    assert len(await w.claims_for(quote)) == 1
    assert (await w.claims_for(quote))[0]["order_id"] == rows[0]["order_id"]


@pytest.mark.parametrize("case", _STEP_FAULTS, ids=[c[0] for c in _STEP_FAULTS])
async def test_a_failure_at_any_step_never_makes_a_second_order_v2(
    world: World, case: Tuple[str, List[Dict[str, Any]], int, int]
) -> None:
    """The v2 route reads the quote before any replay, so the consume is kept from landing here:
    with it landed, v2 answers every resend 409 QUOTE_CONSUMED (see the consumed case below)."""
    w = world
    _, faults, first_status, rows_after_first = case
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    w.db.apply(w, faults)

    first = await w.create_v2(quote, key="key-1", tag="r1")
    rows = await assert_one_order_at_most(w, quote, [first])
    assert first.status == first_status, (first.status, first.body)
    assert len(rows) == rows_after_first

    w.after_row_busy_runs = set()
    second = await w.create_v2(quote, key="key-2", tag="r2")
    rows = await assert_one_order_at_most(w, quote, [first, second])
    assert len(rows) == 1
    assert second.status == 200 and second.order_id == rows[0]["order_id"]


async def test_v2_resend_after_the_consume_landed_is_refused_and_writes_nothing(world: World) -> None:
    w = world
    quote = w.quotes.add()
    first = await w.create_v2(quote, key="key-1", tag="r1")
    second = await w.create_v2(quote, key="key-1", tag="r2")

    rows = await assert_one_order_at_most(w, quote, [first, second])
    assert first.status == 200 and len(rows) == 1
    assert second.status == 409 and second.code == "QUOTE_CONSUMED"
    assert w.create_runs == ["r1"]


async def test_the_consume_failing_is_not_what_keeps_the_quote_single_use(world: World) -> None:
    """The quote stays `active` (the consume never lands) and every lookup is made to find nothing
    it could replay (another provider choice): only the database guard is left, and it holds."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()

    first = await w.create_v1(quote, key="key-1", tag="r1")
    second = await w.create_v1(quote, key="key-2", tag="r2", preferred_psp="adyen")

    rows = await assert_one_order_at_most(w, quote, [first, second])
    assert first.status == 200 and len(rows) == 1
    assert w.quotes.quotes[quote]["status"] == "active"
    assert second.status == 409 and second.code == "QUOTE_CONSUMED", second.body
    assert w.create_runs == ["r1", "r2"]
    assert w.db.count("order_insert", "r2") == 0, "the guard must refuse before the order is written"


# ---------------------------------------------------------------------------
# 3. The lookup's three outcomes
# ---------------------------------------------------------------------------


async def test_an_unreadable_lookup_refuses_and_writes_nothing(world: World) -> None:
    w = world
    quote = w.quotes.add()
    w.db.fail("replay_lookup", "before", times=999)

    answer = await w.create_v1(quote, key="key-1")

    assert_retriable_503(answer, "ORDER_REPLAY_LOOKUP_UNAVAILABLE")
    assert w.create_runs == []
    assert w.db.count("claim_insert") == 0 and w.db.count("order_insert") == 0
    assert await w.orders_for(quote) == []
    assert await w.claims_for(quote) == []


async def test_a_cached_answer_still_replays_while_the_lookup_is_unreadable(world: World) -> None:
    w = world
    w.idempotency_cache_off = False
    quote = w.quotes.add()
    first = await w.create_v1(quote, key="key-1", tag="r1")
    w.db.fail("replay_lookup", "before", times=999)

    second = await w.create_v1(quote, key="key-1", tag="r2")

    assert first.status == 200 and second.status == 200
    assert second.body == first.body
    await assert_one_order_at_most(w, quote, [first, second])


@pytest.mark.parametrize(
    "polls, expected",
    [
        (["none", "none"], "none"),
        (["fail", "none"], "none"),
        (["none", "fail"], "unavailable"),
        (["fail", "fail"], "unavailable"),
        (["fail", "found"], "found"),
        (["found"], "found"),
    ],
    ids=["none_none", "fail_then_none", "none_then_fail", "fail_fail", "fail_then_found", "found"],
)
async def test_backoff_polling_says_no_order_only_when_its_last_poll_read_the_table(
    monkeypatch: pytest.MonkeyPatch, polls: List[str], expected: str
) -> None:
    import routes.agent_api as agent_api

    script = list(polls)
    found = {"status": "success", "order_id": "ORD_FIXTURE"}

    async def lookup(_request: Any, **_: Any) -> Optional[Dict[str, Any]]:
        outcome = script.pop(0)
        if outcome == "fail":
            raise agent_api.OrderCreateReplayUnavailable()
        return found if outcome == "found" else None

    real_sleep = asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        await real_sleep(0)

    async def no_cache(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(agent_api, "_load_replayable_agent_order_create_response", lookup)
    monkeypatch.setattr(agent_api, "_cache_agent_order_create_response_best_effort", no_cache)
    monkeypatch.setattr(agent_api.asyncio, "sleep", no_sleep)
    request = SimpleNamespace(idempotency_key="key-1")

    if expected == "unavailable":
        with pytest.raises(agent_api.OrderCreateReplayUnavailable) as raised:
            await agent_api._load_replayable_agent_order_create_response_with_backoff(
                request, agent_id=AGENT_A, intent_id=None, attempts=2
            )
        assert raised.value.status_code == 503
        assert raised.value.detail["error"] == "TEMPORARY_UNAVAILABLE"
        assert raised.value.detail["reason"] == "ORDER_REPLAY_LOOKUP_UNAVAILABLE"
        assert raised.value.headers == {"Retry-After": "1"}
    else:
        result = await agent_api._load_replayable_agent_order_create_response_with_backoff(
            request, agent_id=AGENT_A, intent_id=None, attempts=2
        )
        assert result == (found if expected == "found" else None)


# ---------------------------------------------------------------------------
# 4. The guard itself (db.orders.create_order, called directly)
# ---------------------------------------------------------------------------


def _order_data(quote_id: Optional[str], agent: Optional[str] = AGENT_A) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"idempotency_key": f"k_{uuid.uuid4().hex[:8]}"}
    if quote_id:
        metadata["pricing_quote"] = {"quote_id": quote_id, "currency": "USD"}
    return {
        "merchant_id": MERCHANT,
        "customer_email": "buyer@example.com",
        "items": [],
        "shipping_address": shipping_address("US"),
        "subtotal": 9.09,
        "discount_total": 0.0,
        "shipping_fee": 0.0,
        "tax": 0.0,
        "total": 9.09,
        "currency": "USD",
        "agent_id": agent,
        "agent_session_id": None,
        "metadata": metadata,
        "psp_used": "stripe",
    }


def _quote_id() -> str:
    return f"q_oopq_{uuid.uuid4().hex[:20]}"


async def test_the_claim_is_taken_before_the_order_is_written_and_a_second_create_writes_nothing(world: World) -> None:
    w = world
    orders_module = w.orders_module
    quote = _quote_id()

    first = await orders_module.create_order(_order_data(quote))
    steps = [s for _, s in w.db.log]
    assert steps.index("claim_insert") < steps.index("claim_select") < steps.index("order_insert")

    before = len(w.db.log)
    with pytest.raises(orders_module.OrderQuoteAlreadyClaimed) as refused:
        await orders_module.create_order(_order_data(quote))
    assert refused.value.order_id == first
    assert [s for _, s in w.db.log[before:]] == ["claim_insert", "claim_select", "claim_order_exists"]
    assert [r["order_id"] for r in await w.orders_for(quote)] == [first]


async def test_an_order_without_a_quote_takes_no_claim(world: World) -> None:
    w = world
    a = await w.orders_module.create_order(_order_data(None))
    b = await w.orders_module.create_order(_order_data(None))
    assert a != b
    assert [s for _, s in w.db.log if s.startswith("claim")] == []


async def test_the_claim_is_per_agent_and_a_quote_without_an_agent_is_claimed_too(world: World) -> None:
    w = world
    orders_module = w.orders_module
    quote = _quote_id()

    a = await orders_module.create_order(_order_data(quote, AGENT_A))
    b = await orders_module.create_order(_order_data(quote, AGENT_B))
    nobody = await orders_module.create_order(_order_data(quote, None))
    assert len({a, b, nobody}) == 3
    claims = {c["agent_id"]: c["order_id"] for c in await w.claims_for(quote)}
    assert claims == {AGENT_A: a, AGENT_B: b, "": nobody}

    for agent, holder in ((AGENT_A, a), (AGENT_B, b), (None, nobody), ("", nobody), ("  ", nobody)):
        with pytest.raises(orders_module.OrderQuoteAlreadyClaimed) as refused:
            await orders_module.create_order(_order_data(quote, agent))
        assert refused.value.order_id == holder


async def test_a_claim_whose_order_was_never_written_is_finished_under_its_own_id(world: World) -> None:
    """The process died between the claim and the order. The quote is not stuck: the next create
    writes the order under the id the claim names."""
    w = world
    quote = _quote_id()
    await w.db._real.execute(
        "INSERT INTO order_quote_claims (agent_id, quote_id, order_id) VALUES (:a, :q, :o)",
        {"a": AGENT_A, "q": quote, "o": "ORD_OOPQ_STRANDED"},
    )
    try:
        order_id = await w.orders_module.create_order(_order_data(quote))
        assert order_id == "ORD_OOPQ_STRANDED"
        assert [r["order_id"] for r in await w.orders_for(quote)] == ["ORD_OOPQ_STRANDED"]
        with pytest.raises(w.orders_module.OrderQuoteAlreadyClaimed):
            await w.orders_module.create_order(_order_data(quote))
    finally:
        await w.db._real.execute("DELETE FROM orders WHERE order_id = 'ORD_OOPQ_STRANDED'")


async def test_a_soft_deleted_order_still_holds_its_quote(world: World) -> None:
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    first = await w.create_v1(quote, key="key-1", tag="r1")
    assert first.status == 200
    await w.db._real.execute("UPDATE orders SET is_deleted = TRUE WHERE order_id = :o", {"o": first.order_id})

    second = await w.create_v1(quote, key="key-1", tag="r2")

    assert second.status == 409 and second.code == "QUOTE_CONSUMED", second.body
    assert w.db.count("order_insert", "r2") == 0, "refused by the claim's own read, not by a failed write"
    assert await w.orders_for(quote) == []
    total = await w.db._real.fetch_all("SELECT order_id FROM orders WHERE merchant_id = :m", {"m": MERCHANT})
    assert len(total) == 1


@pytest.mark.parametrize("step", ["claim_insert", "claim_select", "claim_order_exists"])
@pytest.mark.parametrize("message", [BUSY, "permission denied for table order_quote_claims"], ids=["busy", "not_busy"])
async def test_a_guard_that_cannot_tell_refuses_and_writes_no_order(world: World, step: str, message: str) -> None:
    w = world
    orders_module = w.orders_module
    quote = _quote_id()
    if step == "claim_order_exists":
        await w.db._real.execute(
            "INSERT INTO order_quote_claims (agent_id, quote_id, order_id) VALUES (:a, :q, :o)",
            {"a": AGENT_A, "q": quote, "o": "ORD_OOPQ_ELSEWHERE"},
        )
    w.db.fail(step, "before", message=message)

    with pytest.raises(orders_module.OrderQuoteClaimUnavailable):
        await orders_module.create_order(_order_data(quote))

    assert w.db.count("order_insert") == 0
    assert await w.orders_for(quote) == []


async def test_without_the_claims_table_the_order_is_written_and_a_warning_says_so(world: World, caplog: pytest.LogCaptureFixture) -> None:
    """Deployed before the table exists: order create must keep working, and must not be silent."""
    w = world
    w.quotes.consume_lands = False
    await w.db._real.execute("DROP TABLE order_quote_claims")
    quote = w.quotes.add()

    with caplog.at_level(logging.WARNING, logger="db.orders"):
        first = await w.create_v1(quote, key="key-1", tag="r1")
        second = await w.create_v1(quote, key="key-2", tag="r2")

    assert first.status == 200 and second.status == 200
    assert second.order_id == first.order_id  # the lookup still replays
    rows = await w.orders_for(quote)
    assert len(rows) == 1
    warnings = [r for r in caplog.records if r.name == "db.orders" and r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    text = warnings[0].getMessage()
    assert "one-order-per-quote guard is OFF" in text and "order_quote_claims" in text and first.order_id in text


def test_only_the_missing_claims_table_turns_the_guard_off() -> None:
    import db.orders as orders_module

    missing = orders_module._is_missing_quote_claims_table
    assert missing(RuntimeError("no such table: order_quote_claims"))
    assert missing(RuntimeError('relation "order_quote_claims" does not exist'))
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = RuntimeError('relation "order_quote_claims" does not exist')
    assert missing(wrapped)
    assert not missing(RuntimeError("no such table: orders"))
    assert not missing(RuntimeError('relation "orders" does not exist'))
    assert not missing(RuntimeError(BUSY))
    assert not missing(RuntimeError("permission denied for table order_quote_claims"))
    assert not missing(RuntimeError('duplicate key value violates unique constraint "order_quote_claims_pkey"'))


# ---------------------------------------------------------------------------
# 5. Two requests at once (a second instance: no shared in-process lock)
# ---------------------------------------------------------------------------

_HOLD_POINTS = [
    ("claim_insert", "before"),
    ("claim_insert", "after"),
    ("claim_select", "after"),
    ("order_insert", "before"),
    ("order_insert", "after"),
]


@pytest.mark.parametrize("second_key", ["key-1", "key-2"], ids=["same_key", "another_key"])
@pytest.mark.parametrize("hold", _HOLD_POINTS, ids=[f"{s}_{w}" for s, w in _HOLD_POINTS])
@pytest.mark.parametrize("route", ["v1", "v2"])
async def test_two_creates_at_once_make_one_order_and_both_answer_it(
    world: World, route: str, hold: Tuple[str, str], second_key: str
) -> None:
    """The first request is stopped at one statement of its create; the second runs start to
    finish; then the first goes on. One order, and both answers name it."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    reached, release = w.db.hold("r1", *hold)

    first_task = asyncio.create_task(w.create(route, quote, key="key-1", tag="r1"))
    await asyncio.wait_for(reached.wait(), timeout=10)
    second = await asyncio.wait_for(w.create(route, quote, key=second_key, tag="r2"), timeout=10)
    release.set()
    first = await asyncio.wait_for(first_task, timeout=10)

    rows = await assert_one_order_at_most(w, quote, [first, second])
    assert len(rows) == 1
    assert first.status == 200 and second.status == 200, (first.body, second.body)
    assert first.order_id == second.order_id == rows[0]["order_id"]
    assert len(await w.claims_for(quote)) == 1
    if hold == ("order_insert", "before"):
        # The held insert is refused by the primary key: that is the answer, at once. It is not
        # sent again and it is not handed to the missing-column repair that follows other errors.
        assert w.db.count("order_insert", "r1") == 1


@pytest.mark.parametrize("hold", _HOLD_POINTS, ids=[f"{s}_{w}" for s, w in _HOLD_POINTS])
async def test_two_creates_at_once_while_lookups_fail_make_one_order(world: World, hold: Tuple[str, str]) -> None:
    """As above, with every replay lookup failing once the first order statement has been sent.
    Answers may be refusals; the table still holds at most one order, and a 200 names it."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    reached, release = w.db.hold("r1", *hold)
    lookups_down = w.db.fail("replay_lookup", "before", times=9999, armed_after="claim_insert")

    first_task = asyncio.create_task(w.create_v1(quote, key="key-1", tag="r1"))
    await asyncio.wait_for(reached.wait(), timeout=10)
    second = await asyncio.wait_for(w.create_v1(quote, key="key-2", tag="r2"), timeout=10)
    release.set()
    first = await asyncio.wait_for(first_task, timeout=10)

    rows = await assert_one_order_at_most(w, quote, [first, second])
    for answer in (first, second):
        assert answer.status in (200, 503), answer.body
        if answer.status == 503:
            assert_retriable_503(answer)

    lookups_down.times = 0
    third = await w.create_v1(quote, key="key-2", tag="r3")
    rows = await assert_one_order_at_most(w, quote, [first, second, third])
    assert third.status == 200 and len(rows) == 1 and third.order_id == rows[0]["order_id"]


async def test_the_in_request_retry_meets_an_order_another_request_wrote_and_answers_it(world: World) -> None:
    """The first run of the create fails before anything is written and the lookup reads "no
    order", so the request may run the create again. Before it does, another request writes the
    quote's order. The second run is refused by the guard and the request answers that order."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    w.db.fail("claim_insert", "before", tag="r1", times=1)
    reached, release = w.db.hold("r1", "claim_insert", "before", skip=1)

    first_task = asyncio.create_task(w.create_v1(quote, key="key-1", tag="r1"))
    await asyncio.wait_for(reached.wait(), timeout=10)
    second = await asyncio.wait_for(w.create_v1(quote, key="key-2", tag="r2"), timeout=10)
    release.set()
    first = await asyncio.wait_for(first_task, timeout=10)

    rows = await assert_one_order_at_most(w, quote, [first, second])
    assert len(rows) == 1
    assert first.status == 200 and second.status == 200, (first.body, second.body)
    assert first.order_id == second.order_id
    assert w.create_runs == ["r1", "r1", "r2"]
    assert w.db.count("order_insert", "r1") == 0


async def test_many_creates_racing_on_their_own_connections_write_one_order(world: World) -> None:
    """No gates and no shared connection: each racer runs `create_order` in its own context, so on
    Postgres each holds its own pooled connection and the primary keys decide."""
    w = world
    orders_module = w.orders_module

    for _ in range(12):
        quote = _quote_id()

        async def racer() -> Tuple[str, str]:
            try:
                return "created", await orders_module.create_order(_order_data(quote))
            except orders_module.OrderQuoteAlreadyClaimed as claimed:
                return "claimed", claimed.order_id

        loop = asyncio.get_running_loop()
        tasks = [loop.create_task(racer(), context=contextvars.Context()) for _ in range(8)]
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)

        rows = await w.orders_for(quote)
        assert len(rows) == 1, [r["order_id"] for r in rows]
        assert [kind for kind, _ in results].count("created") == 1, results
        assert {order_id for _, order_id in results} == {rows[0]["order_id"]}


# ---------------------------------------------------------------------------
# 6. Whose order answers
# ---------------------------------------------------------------------------


async def test_another_agent_on_the_same_quote_id_gets_its_own_order_never_the_first_agents(world: World) -> None:
    """A quote id both agents may use (one with no owner recorded). Each agent's claim is its own:
    the second agent is neither refused by the first agent's claim nor handed its order."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    w.quotes.quotes[quote]["agent_id"] = None

    async def anyone(_quote_id: str, _context: Any) -> None:
        return None

    import routes.agent_api as agent_api

    w.monkeypatch.setattr(agent_api, "require_quote_for_caller", anyone)

    a1 = await w.create_v1(quote, key="key-1", agent=AGENT_A, tag="a1")
    b1 = await w.create_v1(quote, key="key-1", agent=AGENT_B, tag="b1")
    b2 = await w.create_v1(quote, key="key-2", agent=AGENT_B, tag="b2")
    a2 = await w.create_v1(quote, key="key-2", agent=AGENT_A, tag="a2")

    assert [x.status for x in (a1, b1, b2, a2)] == [200, 200, 200, 200]
    assert a1.order_id == a2.order_id and b1.order_id == b2.order_id
    assert a1.order_id != b1.order_id
    await assert_one_order_at_most(w, quote, [a1, a2], agent=AGENT_A)
    await assert_one_order_at_most(w, quote, [b1, b2], agent=AGENT_B)
    assert a1.order_id not in json.dumps(b1.body) and a1.order_id not in json.dumps(b2.body)


async def test_a_guard_refusal_is_answered_only_with_the_quotes_own_order(world: World) -> None:
    """The request is past its first lookup when two rows appear: the quote's order (another key,
    another provider choice, so nothing this request may replay) and an order of the same agent
    for ANOTHER quote under this request's idempotency key. The guard refuses the create; the
    lookup then finds the other quote's order by key. That is not this quote's order: 409."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    reached, release = w.db.hold("r1", "claim_insert", "before")

    task = asyncio.create_task(w.create_v1(quote, key="key-1", tag="r1"))
    await asyncio.wait_for(reached.wait(), timeout=10)
    holder = _order_data(quote)
    holder["metadata"]["idempotency_key"] = "key-other"
    holder["psp_used"] = "adyen"
    holder_id = await w.orders_module.create_order(holder)
    other = _order_data(_quote_id())
    other["metadata"]["idempotency_key"] = "key-1"
    other_id = await w.orders_module.create_order(other)
    release.set()
    answer = await asyncio.wait_for(task, timeout=10)

    assert answer.status == 409 and answer.code == "QUOTE_CONSUMED", answer.body
    assert other_id not in json.dumps(answer.body)
    assert [r["order_id"] for r in await w.orders_for(quote)] == [holder_id]


async def test_a_guard_refusal_of_the_in_request_retry_that_cannot_be_replayed_is_the_409(world: World) -> None:
    """The same refusal met by the request's second run of the create (its first run failed busy
    before writing anything): the answer is the guard's 409, not a retriable 503 that would have
    the caller resend a create that can never succeed."""
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add()
    w.db.fail("claim_insert", "before", tag="r1", times=1)
    reached, release = w.db.hold("r1", "claim_insert", "before", skip=1)

    task = asyncio.create_task(w.create_v1(quote, key="key-1", tag="r1", preferred_psp="stripe"))
    await asyncio.wait_for(reached.wait(), timeout=10)
    holder = _order_data(quote)
    holder["metadata"]["idempotency_key"] = "key-other"
    holder["psp_used"] = "adyen"
    holder_id = await w.orders_module.create_order(holder)
    release.set()
    answer = await asyncio.wait_for(task, timeout=10)

    assert answer.status == 409 and answer.code == "QUOTE_CONSUMED", answer.body
    assert [r["order_id"] for r in await w.orders_for(quote)] == [holder_id]


async def test_a_guard_that_cannot_tell_is_named_as_the_guard_in_the_refusal(world: World) -> None:
    w = world
    quote = w.quotes.add()
    w.db.fail("claim_insert", "before", times=999, message="permission denied for table order_quote_claims")

    answer = await w.create_v1(quote, key="key-1")

    assert_retriable_503(answer, "ORDER_QUOTE_GUARD_UNAVAILABLE")
    assert await w.orders_for(quote) == []


async def test_a_quote_the_caller_does_not_own_is_refused_before_anything_is_read(world: World) -> None:
    w = world
    quote = w.quotes.add(agent=AGENT_A)
    first = await w.create_v1(quote, key="key-1", agent=AGENT_A, tag="a1")

    other = await w.create_v1(quote, key="key-1", agent=AGENT_B, tag="b1")

    assert first.status == 200 and other.status == 404
    assert first.order_id not in json.dumps(other.body)
    assert w.db.count("replay_lookup", "b1") == 0 and w.db.count("claim_insert", "b1") == 0


# ---------------------------------------------------------------------------
# 7. Nothing else moved: the order is still priced from the quote, in the quote's currency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("market", MARKET_IDS)
@pytest.mark.parametrize("route", ["v1", "v2"])
async def test_the_order_and_its_replays_carry_the_quotes_currency_and_total(world: World, route: str, market: str) -> None:
    w = world
    w.quotes.consume_lands = False
    quote = w.quotes.add(market=market)
    expected = w.quotes.quotes[quote]

    created = await w.create(route, quote, key="key-1", tag="r1")
    by_key = await w.create(route, quote, key="key-1", tag="r2")
    by_quote = await w.create(route, quote, key="key-2", tag="r3")

    rows = await assert_one_order_at_most(w, quote, [created, by_key, by_quote])
    assert [a.status for a in (created, by_key, by_quote)] == [200, 200, 200]
    assert len(rows) == 1
    row = rows[0]
    assert row["currency"] == expected["currency"]
    assert f"{float(row['total']):.2f}" == expected["total"]
    assert row["metadata"]["pricing_quote"]["currency"] == expected["currency"]
    assert row["metadata"]["amounts_source"] == "quote_snapshot"
    for answer in (created, by_key, by_quote):
        order = answer.body["order"] if route == "v2" else answer.body
        assert json.dumps(order).count(expected["currency"]) >= 1
        if route == "v1":
            assert order["currency"] == expected["currency"]
            assert f"{float(order['total_amount']):.2f}" == expected["total"]
    if route == "v2":
        assert by_key.body["order"] == by_quote.body["order"] == created.body["order"]
    else:
        assert by_key.body == by_quote.body


# ---------------------------------------------------------------------------
# 8. A busy database error that arrives wrapped in an HTTP error
# ---------------------------------------------------------------------------


def _wrapped(status: int, detail: Any, cause_message: str) -> Any:
    from fastapi import HTTPException

    try:
        raise RuntimeError(cause_message)
    except RuntimeError as cause:
        wrapped = HTTPException(status_code=status, detail=detail)
        wrapped.__cause__ = cause
        return wrapped


@pytest.mark.parametrize(
    "status, detail, cause, expected_status, expected_code",
    [
        # A helper turned a busy error into its own 5xx: answered as the retriable 503.
        (500, "internal error: " + BUSY, BUSY, 503, "TEMPORARY_UNAVAILABLE"),
        (502, {"error": "UPSTREAM_FAILED", "message": "fixture"}, BUSY, 503, "TEMPORARY_UNAVAILABLE"),
        # Not busy: unchanged.
        (500, "internal error: fixture", "something else", 500, "internal error: fixture"),
        # A refusal that was decided stays that refusal, whatever is behind it.
        (409, {"error": "QUOTE_CONSUMED", "message": "Quote already consumed"}, BUSY, 409, "QUOTE_CONSUMED"),
        (400, {"error": "FIXTURE_REFUSAL", "message": "fixture"}, BUSY, 400, "FIXTURE_REFUSAL"),
    ],
    ids=["busy_500", "busy_502", "plain_500", "busy_behind_a_409", "busy_behind_a_400"],
)
async def test_a_busy_error_wrapped_in_a_5xx_leaves_as_the_retriable_503_and_nothing_else_changes(
    world: World, status: int, detail: Any, cause: str, expected_status: int, expected_code: str
) -> None:
    import routes.agent_api as agent_api

    w = world
    quote = w.quotes.add()

    async def helper_fails(_quote_id: str, _context: Any) -> None:
        raise _wrapped(status, detail, cause)

    w.monkeypatch.setattr(agent_api, "require_quote_for_caller", helper_fails)

    answer = await w.create_v1(quote, key="key-1")

    assert answer.status == expected_status, (answer.status, answer.body)
    assert answer.code == expected_code, answer.body
    assert (answer.retry_after == "1") is (expected_status == 503)
    assert w.create_runs == [] and w.db.count("replay_lookup") == 0
    assert await w.orders_for(quote) == []


async def test_the_named_503_keeps_its_reason_on_the_way_out(world: World) -> None:
    """The could-not-tell refusal has a busy error behind it and is already a 503: it is not
    rewritten into the plain busy 503, which would lose the reason."""
    w = world
    quote = w.quotes.add()
    w.after_row_busy_runs = {1}
    w.db.fail("replay_lookup", "before", times=999, armed_after="order_insert")

    answer = await w.create_v1(quote, key="key-1")

    assert_retriable_503(answer, "ORDER_REPLAY_LOOKUP_UNAVAILABLE")
    assert w.create_runs == ["r1"] and len(await w.orders_for(quote)) == 1
