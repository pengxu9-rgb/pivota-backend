"""The cart-link lane's cases, written ONCE and collected under BOTH dialects.

NOT A TEST MODULE ITSELF (the name does not match `test_*.py`, so pytest never collects it
directly). Two thin modules collect it:

    tests/test_reap_agentic_cart_link.py            SQLite — skips itself under Postgres
    tests/test_reap_agentic_cart_link_postgres.py   Postgres — skips itself under SQLite, adds the
                                                    catalog-level checks only Postgres can make,
                                                    and is what .github/workflows/
                                                    postgres-dialect-gate.yml picks up by glob

WHY SHARED, WHEN THE REST OF THIS RAIL DUPLICATES ITS FIXTURES PER DIALECT. The rail's
convention exists because importing a SQLite test module would execute its `pytestmark` and bind
`IS_POSTGRES` at the wrong moment. This module has neither: no `pytestmark`, and every
dialect-dependent decision reads `IS_POSTGRES` at call time inside the fixture. The collectors
own the skip. What sharing buys is that "the same case under both engines" is literally the same
function, so the two arms cannot drift — which, for a lane whose whole safety argument is a
validator and a CHECK, is the property worth having.

FIXTURE NAMES HAVE NO LEADING UNDERSCORE ON PURPOSE: the collectors `import *`, and `*` skips
underscored names.

WHAT IS REAL: the ledger, the schema (self-heal on SQLite — the thing production runs; the
migrations on Postgres, with the self-heal compared against them in the Postgres collector), the
validator, the client's pure builders. WHAT IS FAKE: every client call that would reach the
network, plus the attribution hook. `httpx.AsyncClient` raises if anything reaches for it.
`rc.CART_LINK_QUOTE_FIELD` is set to a deliberately FAKE name — Reap has not published the real
one, and a test must not look like it knows it.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
from pathlib import Path

import pytest

import db.reap_agentic_ledger as ledger
import services.reap_agentic_client as rc
import services.reap_agentic_purchase as svc
import services.reap_cart_link as cl
from db.database import IS_POSTGRES, database
from db.schema_guard import ensure_required_schema_light

from test_reap_cart_link import ACCEPT, PREFILL_LINK, REFUSE  # the validator's own table

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "db/migrations"
MIGRATIONS = (
    MIGRATIONS_DIR / "224_reap_agentic_ledger.sql",
    MIGRATIONS_DIR / "225_reap_agentic_purchase_hints.sql",
    MIGRATIONS_DIR / "226_reap_agentic_purchase_item_source.sql",
    # 228: the per-click attribution claim + the purchases click_id index.
    MIGRATIONS_DIR / "228_conversion_click_claims.sql",
)
SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

#: A name Reap has NOT published — chosen so it can never be mistaken for the real one.
FAKE_FIELD = "pivotaTestOnlyCartUrl"

SHOP = "judydoll.com"
CLICK = "clk_9b1c3dcd4a854e26a5c8a295"
VARIANT = "49922977038613"
CART_URL = f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=US"
RETURN_URL = "https://agent.pivota.cc/reap/return?click=abc123"
EMAIL = "ada@example.test"
ADDRESS = {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "phone": "+15550100",
    "addressLine1": "900 Brannan St",
    "city": "San Francisco",
    "country": "US",
    "postalCode": "94103",
}
#: Written out independently of ADDRESS so the PII test cannot be satisfied by a field that was
#: dropped from both.
PII_STRINGS = ("ada@example.test", "900 Brannan St", "Lovelace", "+15550100", "adrian")

ENROLLMENT_UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
ENROLLMENT_CREATED = {
    "id": ENROLLMENT_UUID,
    "status": "REQUIRES_ACTION",
    "nextAction": {
        "type": "REDIRECT",
        "url": "https://pay.prava.space/enroll/3fa85f64",
        "expiresAt": "2026-09-17T21:00:00Z",
    },
}
ENROLLMENT_ACTIVE = {
    "id": ENROLLMENT_UUID,
    "status": "ACTIVE",
    "paymentMethod": {"type": "CARD", "network": "VISA", "last4": "4242"},
    "nextAction": None,
}


def cart_quote(**over):
    """A cart-link quote at the row's price, IN THE LIVE SHAPE: 28.20 + 5.00 + 0 = 33.20 USD.

    Key names and value types are exactly what a live sandbox `POST /agentic/quotes` returned on
    2026-09-18: NO `items` (Reap does not echo line items — an earlier version of this fixture
    invented one), `tax` nested one level deeper with an INT amount, empty `discounts` /
    `additionalCharges`, FLOAT amounts, and four shipping options each
    `{id, name, selected, price}`. Tests that exercise a PRESENT `items` pass one explicitly.
    """
    body = {
        "id": "q_cart_1",
        "expiresAt": "2099-01-01T00:00:00Z",
        "amountBreakdown": {
            "itemsSubtotal": {"amount": 28.2, "currency": "USD"},
            "shipping": {"amount": 5.0, "currency": "USD"},
            "tax": {"amount": {"amount": 0, "currency": "USD"}},
            "discounts": [],
            "additionalCharges": [],
            "finalAmount": {"amount": 33.2, "currency": "USD"},
        },
        "shippingOptions": [
            {"id": "ship_std", "name": "Standard", "selected": True,
             "price": {"amount": 5.0, "currency": "USD"}},
            {"id": "ship_exp", "name": "Express", "selected": False,
             "price": {"amount": 12.99, "currency": "USD"}},
            {"id": "ship_ovn", "name": "Overnight", "selected": False,
             "price": {"amount": 29.5, "currency": "USD"}},
            {"id": "ship_free", "name": "Free over 50", "selected": False,
             "price": {"amount": 0.0, "currency": "USD"}},
        ],
    }
    body.update(over)
    return body


#: A PRESENT, correct echo — the shape the lane accepts IF Reap ever sends one.
ONE_LINE = [{"variantId": "var_opaque_1", "quantity": 1}]


CHECKOUT_CREATED = {
    "id": "chk_cart1",
    "status": "REQUIRES_ACTION",
    "quoteId": "q_cart_1",
    "nextAction": {
        "type": "REDIRECT",
        "url": "https://pay.prava.space/checkout/chk_cart1",
        "expiresAt": "2026-09-17T21:00:00Z",
    },
}
CHECKOUT_COMPLETED = {
    "id": "chk_cart1",
    "status": "COMPLETED",
    "orderId": "ord_cart_1",
    "finalAmount": {"amount": 33.20, "currency": "USD"},
    "nextAction": None,
}


def ok(data) -> rc.ReapResponse:
    return rc.ReapResponse(ok=True, status=200, data=json.loads(json.dumps(data)))


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


def _assert_throwaway_database() -> None:
    url = (os.getenv("DATABASE_URL") or "").strip()
    dbname = url.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in url for m in SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop the agentic-ledger tables in {dbname!r}; throwaway only")


async def apply_migrations(paths=MIGRATIONS):
    from db.sql_migrations import split_statements

    for path in paths:
        for statement in split_statements(path.read_text(encoding="utf-8")):
            await database.execute(statement)


async def drop_tables():
    await database.execute("DROP TABLE IF EXISTS conversion_click_claims")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")


@pytest.fixture(autouse=True)
async def cartlink_db():
    """SQLite: the self-heal, which is the schema production gets. Postgres: the migrations,
    224 → 225 → 226 — the Postgres collector then proves the self-heal builds the same catalog."""
    if IS_POSTGRES:
        _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await drop_tables()
    if IS_POSTGRES:
        await apply_migrations()
    else:
        await ensure_required_schema_light()
    yield
    if IS_POSTGRES and not was_connected and database.is_connected:
        await database.disconnect()


@pytest.fixture(autouse=True)
def cartlink_env(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)
    monkeypatch.delenv("REAP_API_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", FAKE_FIELD)


@pytest.fixture(autouse=True)
def cartlink_no_network(monkeypatch):
    import httpx

    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a test reached the network; the Reap client must be faked")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden)


def _must_not_be_called(name):
    def _fail(**kwargs):
        raise AssertionError(f"{name} must not be called on the cart-link lane")

    return _fail


class FakeReap:
    """Scripted stand-ins for every client call that would reach the network, plus a call log.

    `resolve_our_row` and `request_quote` FAIL if reached: the cart-link lane has no resolver and
    does not send `items`. A test that wants the variant lane replaces them.
    """

    NAMES = (
        "resolve_our_row",
        "request_quote",
        "request_cart_link_quote",
        "create_enrollment",
        "get_enrollment",
        "create_checkout",
        "get_checkout",
    )

    def __init__(self):
        self.calls = []
        self.resolve_our_row = _must_not_be_called("resolve_our_row")
        self.request_quote = _must_not_be_called("request_quote")
        self.request_cart_link_quote = ok(cart_quote())
        self.create_enrollment = ok(ENROLLMENT_CREATED)
        self.get_enrollment = ok(ENROLLMENT_ACTIVE)
        self.create_checkout = ok(CHECKOUT_CREATED)
        self.get_checkout = ok(CHECKOUT_COMPLETED)

    def named(self, name):
        return [kwargs for called, kwargs in self.calls if called == name]

    def sequence(self):
        return [called for called, _ in self.calls]


@pytest.fixture
def reap(monkeypatch):
    fake = FakeReap()

    def _install(name):
        async def _call(*args, **kwargs):
            if args:
                kwargs = dict(kwargs, id=args[0])
            fake.calls.append((name, kwargs))
            scripted = getattr(fake, name)
            if isinstance(scripted, list):
                scripted = scripted.pop(0) if len(scripted) > 1 else scripted[0]
            answer = scripted(**kwargs) if callable(scripted) else scripted
            if inspect.isawaitable(answer):
                answer = await answer
            return answer

        monkeypatch.setattr(rc, name, _call)

    for name in FakeReap.NAMES:
        _install(name)
    return fake


class Attribution:
    """Stands in for `close_external_order_conversion` on EVERY channel (Reap, webhook, poller),
    and keeps its one real dedupe: an edge per (merchant_id, external_order_id), ON CONFLICT DO
    NOTHING. That is exactly the guard that CANNOT see a Reap close and a merchant close of one
    sale as the same thing, which is what the mig-228 claim is for."""

    def __init__(self):
        self.calls = []
        self.edges = {}
        self.raises = None

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        key = (kwargs["merchant_id"], kwargs["external_order_id"])
        if key in self.edges:
            return {"replayed": True}
        self.edges[key] = kwargs
        return {"edge_id": f"edge_{len(self.edges)}", "replayed": False}


@pytest.fixture(autouse=True)
def attribution(monkeypatch):
    import services.commerce_attribution_service as cas

    recorder = Attribution()
    monkeypatch.setattr(cas, "close_external_order_conversion", recorder)
    return recorder


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def item(**over) -> svc.CartLinkItem:
    kwargs = dict(
        cart_url=CART_URL,
        shop_domain=SHOP,
        our_price_minor=2820,
        currency="USD",
        market_country="US",
        product_name="Judydoll Lip Gloss",
    )
    kwargs.update(over)
    return svc.CartLinkItem(**kwargs)


async def start(**over) -> str:
    kwargs = dict(
        agent_id="agent_one",
        agent_user_ref_hash="hash_alice",
        buyer_ref="bref_alice",
        cart_link=item(),
        buyer=svc.BuyerContact(email=EMAIL, shipping_address=dict(ADDRESS)),
        quantity=1,
        click_id=CLICK,
        return_url=RETURN_URL,
    )
    kwargs.update(over)
    return await svc.start_purchase(**kwargs)


async def count() -> int:
    row = await database.fetch_one("SELECT COUNT(*) AS n FROM reap_agentic_purchases")
    return int(row["n"])


async def get(purchase_id):
    return await ledger.get_purchase_internal(purchase_id)


async def step(purchase_id, worker_id="w1") -> svc.AdvanceResult:
    await database.execute(
        "UPDATE reap_agentic_purchases SET claimed_by = :w, claimed_at = CURRENT_TIMESTAMP "
        "WHERE id = :i",
        {"w": worker_id, "i": purchase_id},
    )
    return await svc.advance(purchase_id, worker_id)


async def active_enrollment(buyer_ref="bref_alice", reap_id=ENROLLMENT_UUID):
    created = await ledger.upsert_pending_enrollment(buyer_ref=buyer_ref, reap_enrollment_id=reap_id)
    await ledger.mark_enrollment_active(created["id"], reap_enrollment_id=reap_id)
    return created


async def to_quoting(reap) -> str:
    """Start a cart-link purchase for an enrolled buyer and take it to 'quoting'."""
    await active_enrollment()
    purchase_id = await start()
    moved = await step(purchase_id)
    assert moved.state == "quoting", moved
    return purchase_id


async def mk_cart_row(**over):
    """A cart-link row straight through the LEDGER (no service), for ledger-level cases."""
    kwargs = dict(
        buyer_ref="bref_alice",
        agent_id="agent_one",
        agent_user_ref_hash="hash_alice",
        merchant_domain=SHOP,
        quantity=1,
        currency="USD",
        our_price_minor=2820,
        click_id=CLICK,
        market_country="US",
        buyer_email=EMAIL,
        shipping_address=dict(ADDRESS),
        item_source="cart_link",
        cart_url=CART_URL,
    )
    kwargs.update(over)
    return await ledger.create_purchase(**kwargs)


async def purchase_columns() -> set:
    if IS_POSTGRES:
        rows = await database.fetch_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'reap_agentic_purchases'"
        )
        return {r["column_name"] for r in rows}
    rows = await database.fetch_all("PRAGMA table_info(reap_agentic_purchases);")
    return {r["name"] for r in rows}


async def to_pre_226_shape():
    """The table exactly as every environment that deployed 225 has it: drop the two mig-226
    columns (cart_url first — its CHECK names item_source). Both engines accept this form."""
    await database.execute("ALTER TABLE reap_agentic_purchases DROP COLUMN cart_url")
    await database.execute("ALTER TABLE reap_agentic_purchases DROP COLUMN item_source")


def _norm(text: str) -> str:
    return " ".join(text.split())


# ══ 1. SCHEMA: migration 226 and its self-heal ═══════════════════════════════════════════════


async def test_the_226_columns_exist_with_their_default():
    columns = await purchase_columns()
    assert {"item_source", "cart_url"} <= columns


async def test_a_default_create_is_a_reap_variant_row_with_no_url():
    """Every caller before mig 226: the row it gets is a reap_variant row, no URL."""
    row = await ledger.create_purchase(
        buyer_ref="bref_alice", agent_id="agent_one", agent_user_ref_hash="hash_alice",
        merchant_domain="brand.example", currency="USD", our_price_minor=4250,
    )
    assert row["item_source"] == "reap_variant"
    assert row["cart_url"] is None


async def test_the_self_heal_adds_both_columns_to_a_225_shaped_table():
    """PATH TWO — the one production takes: the table exists, only the ALTER can heal it."""
    await to_pre_226_shape()
    assert not ({"item_source", "cart_url"} & await purchase_columns()), "precondition"
    await ensure_required_schema_light()
    assert {"item_source", "cart_url"} <= await purchase_columns()
    row = await mk_cart_row()
    assert row["item_source"] == "cart_link" and row["cart_url"] == CART_URL


async def test_existing_rows_take_the_default_when_the_column_is_healed_on():
    await to_pre_226_shape()
    await database.execute(
        "INSERT INTO reap_agentic_purchases (id, buyer_ref, state) "
        "VALUES ('rp_before226', 'bref_x', 'resolving')"
    )
    await ensure_required_schema_light()
    row = await get("rp_before226")
    assert row["item_source"] == "reap_variant" and row["cart_url"] is None


async def test_the_self_heal_is_idempotent_and_the_checks_still_hold():
    await ensure_required_schema_light()
    await ensure_required_schema_light()
    assert {"item_source", "cart_url"} <= await purchase_columns()
    with pytest.raises(Exception) as caught:
        await database.execute(
            "INSERT INTO reap_agentic_purchases (id, buyer_ref, state, item_source) "
            "VALUES ('rp_nourl', 'b', 'resolving', 'cart_link')"
        )
    assert "ck_reap_agentic_purchases_cart_url_pairing" in str(caught.value)


async def test_a_failing_sibling_statement_does_not_starve_the_226_columns():
    """THE OWN-try MUTANT. Two active enrollments for one buyer make the mig-224 block's unique
    index RAISE; the mig-226 columns must land anyway. Folding the 226 ALTER into that try is
    the defect this kills."""
    await to_pre_226_shape()
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await database.execute(
        """
        CREATE TABLE reap_agentic_enrollments (
            id VARCHAR(64) PRIMARY KEY,
            buyer_ref VARCHAR(128) NOT NULL,
            agent_id VARCHAR(128),
            reap_enrollment_id VARCHAR(128),
            status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'active', 'dead')),
            reap_status VARCHAR(64),
            card_network VARCHAR(32),
            card_last4 VARCHAR(4) CHECK (card_last4 IS NULL OR length(card_last4) = 4),
            hosted_url TEXT,
            hosted_url_expires_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for row_id in ("re_dup_a", "re_dup_b"):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
            "VALUES (:i, 'bref_dup', 'active')",
            {"i": row_id},
        )
    # CONTROL: the sibling really does fail on this database.
    with pytest.raises(Exception) as caught:
        await database.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reap_agentic_enrollments_one_active "
            "ON reap_agentic_enrollments (buyer_ref) WHERE status = 'active'"
        )
    assert "unique" in str(caught.value).lower() or "duplicate" in str(caught.value).lower()

    await ensure_required_schema_light()
    assert {"item_source", "cart_url"} <= await purchase_columns()


@pytest.mark.parametrize(
    "item_source,cart_url,constraint",
    [
        ("cart_link", None, "ck_reap_agentic_purchases_cart_url_pairing"),
        ("reap_variant", CART_URL, "ck_reap_agentic_purchases_cart_url_pairing"),
        ("bogus", None, "ck_reap_agentic_purchases_"),
    ],
)
async def test_the_database_refuses_an_unpaired_row_from_any_writer(
    item_source, cart_url, constraint
):
    """The CHECK, behaviourally, with the ledger bypassed — "any other writer"."""
    with pytest.raises(Exception) as caught:
        await database.execute(
            "INSERT INTO reap_agentic_purchases (id, buyer_ref, state, item_source, cart_url) "
            "VALUES ('rp_raw', 'b', 'resolving', :s, :u)",
            {"s": item_source, "u": cart_url},
        )
    assert constraint in str(caught.value), repr(caught.value)
    assert await count() == 0


async def test_a_paired_raw_row_is_accepted_by_the_database():
    """CONTROL for the case above: a CHECK that refused everything would pass it."""
    await database.execute(
        "INSERT INTO reap_agentic_purchases (id, buyer_ref, state, item_source, cart_url) "
        "VALUES ('rp_raw_ok', 'b', 'resolving', 'cart_link', :u)",
        {"u": CART_URL},
    )
    assert await count() == 1


def test_the_migration_and_both_self_heal_twins_declare_the_same_columns():
    """The self-heal is what production runs, so its DDL must be the migration's DDL.

    Postgres twin: the whole ALTER, whitespace-normalised, must EQUAL the migration's.
    SQLite twin: each column declaration must equal the migration's clause for that column —
    SQLite cannot run the multi-clause statement, but it must declare the same CHECKs."""
    migration = (MIGRATIONS_DIR / "226_reap_agentic_purchase_item_source.sql").read_text()
    guard = (ROOT / "db/schema_guard.py").read_text()

    def _alter(text: str) -> str:
        marker = "ALTER TABLE IF EXISTS reap_agentic_purchases\n"
        starts = [m.start() for m in re.finditer(re.escape(marker), text)]
        for start in starts:
            stmt = text[start: text.index(";", start) + 1]
            if "item_source" in stmt:
                return _norm(stmt)
        raise AssertionError("no mig-226 ALTER found")

    assert _alter(migration) == _alter(guard)

    sqlite_branch = guard.rsplit("if IS_SQLITE:", 1)[1]
    block = sqlite_branch[sqlite_branch.index("mig 226"):]
    block = block[: block.index("except Exception:  # noqa: BLE001\n                pass")]
    decls = re.findall(r'\(\s*"(item_source|cart_url)",\s*((?:"[^"]*"\s*)+),?\s*\)', block)
    assert [d[0] for d in decls] == ["item_source", "cart_url"], "order matters: pairing names item_source"
    def _tight(text: str) -> str:
        # Whitespace-normalised AND with no space just inside a parenthesis — the migration
        # breaks the pairing CHECK across lines, the SQLite twin writes it on one.
        return re.sub(r"\s+\)", ")", re.sub(r"\(\s+", "(", _norm(text)))

    body = _tight(_alter(migration))
    for column, pieces in decls:
        declared = _tight("".join(re.findall(r'"([^"]*)"', pieces)))
        assert f"ADD COLUMN IF NOT EXISTS {column} {declared}" in body, (column, declared)


def test_the_item_source_vocabulary_is_the_checks_own():
    migration = (MIGRATIONS_DIR / "226_reap_agentic_purchase_item_source.sql").read_text()
    check = re.search(r"CHECK \(item_source IN \(([^)]*)\)\)", migration).group(1)
    assert set(re.findall(r"'([a-z_]+)'", check)) == set(ledger.ITEM_SOURCES)


def test_the_down_migration_drops_both_and_refuses_while_cart_rows_are_in_flight():
    down = (MIGRATIONS_DIR / "down/226_reap_agentic_purchase_item_source_down.sql").read_text()
    assert "DROP COLUMN IF EXISTS cart_url" in down
    assert "DROP COLUMN IF EXISTS item_source" in down
    assert down.index("DROP COLUMN IF EXISTS cart_url") < down.index(
        "DROP COLUMN IF EXISTS item_source"
    )
    assert "RAISE EXCEPTION" in down and "item_source = 'cart_link'" in down


def test_the_coverage_gate_sees_both_new_columns():
    import test_schema_guard_migration_coverage as gate

    migration = (MIGRATIONS_DIR / "226_reap_agentic_purchase_item_source.sql").read_text()
    assert gate._extract_added_columns(migration) == {
        ("reap_agentic_purchases", "item_source"),
        ("reap_agentic_purchases", "cart_url"),
    }
    covered = gate._schema_guard_covered()
    assert ("reap_agentic_purchases", "item_source") in covered
    assert ("reap_agentic_purchases", "cart_url") in covered


# ══ 2. LEDGER: the pairing and the validator at create ═══════════════════════════════════════


async def test_a_cart_link_row_stores_the_canonical_url():
    encoded = (
        f"https://{SHOP}/cart/{VARIANT}:1?country=us&attributes%5Bpivota_click_id%5D={CLICK}"
    )
    row = await mk_cart_row(cart_url=encoded)
    assert row["cart_url"] == CART_URL
    assert (await get(row["id"]))["cart_url"] == CART_URL


async def test_a_cart_link_row_without_a_url_is_refused_and_writes_nothing():
    with pytest.raises(ValueError, match="requires a cart_url"):
        await mk_cart_row(cart_url=None)
    assert await count() == 0


async def test_a_reap_variant_row_with_a_url_is_refused_and_writes_nothing():
    with pytest.raises(ValueError, match="must not carry a cart_url"):
        await mk_cart_row(item_source="reap_variant")
    assert await count() == 0


@pytest.mark.parametrize("bad", ["", "CART_LINK", "variant", None, 1])
async def test_an_unknown_item_source_is_refused(bad):
    with pytest.raises(ValueError, match="item_source must be one of"):
        await mk_cart_row(item_source=bad)
    assert await count() == 0


@pytest.mark.parametrize("label,url,code", REFUSE, ids=[r[0] for r in REFUSE])
async def test_every_refused_link_is_refused_by_the_ledger_on_this_engine(label, url, code):
    """The validator's whole refuse table, through `create_purchase`, on THIS dialect. The
    message names the code and never the URL; no row is written."""
    with pytest.raises(ValueError) as caught:
        await mk_cart_row(cart_url=url)
    message = str(caught.value)
    assert f"cart_url refused: {code}" == message, (label, message)
    assert await count() == 0


@pytest.mark.parametrize("label,url,canonical", ACCEPT, ids=[a[0] for a in ACCEPT])
async def test_every_accepted_link_is_stored_canonically_on_this_engine(label, url, canonical):
    row = await mk_cart_row(cart_url=url)
    assert (await get(row["id"]))["cart_url"] == canonical, label


async def test_a_checkout_prefill_url_can_never_be_persisted():
    """THE PII GUARANTEE, asserted at the table: after the attempt, no row anywhere carries the
    buyer's email out of the URL, and the exception does not either."""
    with pytest.raises(ValueError) as caught:
        await mk_cart_row(cart_url=PREFILL_LINK)
    assert "adrian" not in str(caught.value).lower()
    assert "checkout[" not in str(caught.value)
    rows = await database.fetch_all("SELECT cart_url FROM reap_agentic_purchases")
    assert rows == []


async def test_the_ledger_compares_the_url_against_the_rows_own_click_id_and_market():
    with pytest.raises(ValueError, match="click_id_mismatch"):
        await mk_cart_row(click_id="clk_someone_else")
    with pytest.raises(ValueError, match="expected_click_id_invalid"):
        await mk_cart_row(click_id=None)
    with pytest.raises(ValueError, match="country_mismatch"):
        await mk_cart_row(market_country="SG")
    with pytest.raises(ValueError, match="expected_market_invalid"):
        await mk_cart_row(market_country=None)
    with pytest.raises(ValueError, match="host_mismatch"):
        await mk_cart_row(merchant_domain="other.example")
    assert await count() == 0


async def test_neither_new_column_is_a_transition_field():
    """Create-only. A poller step that could rewrite the URL could change what is bought."""
    assert "cart_url" not in ledger._TRANSITION_FIELDS
    assert "item_source" not in ledger._TRANSITION_FIELDS
    row = await mk_cart_row()
    with pytest.raises(TypeError):
        await ledger.transition(
            row["id"], from_states=["resolving"], to_state="quoting",
            cart_url=CART_URL.replace(":1?", ":2?"),
        )


async def test_the_public_view_carries_neither_column():
    row = await mk_cart_row()
    view = await ledger.get_purchase_for_owner(row["id"], "agent_one", "hash_alice")
    assert "cart_url" not in view and "item_source" not in view
    assert "cart_url" not in ledger.PUBLIC_PURCHASE_COLUMNS
    assert "item_source" not in ledger.PUBLIC_PURCHASE_COLUMNS


async def test_a_terminal_row_keeps_its_url_and_sheds_its_pii():
    row = await mk_cart_row()
    done = await ledger.transition(row["id"], from_states=["resolving"], to_state="refused")
    assert done["cart_url"] == CART_URL
    assert done["buyer_email"] is None and done["shipping_address"] is None


def test_the_reap_variant_insert_statement_is_untouched():
    """Byte-identical behaviour for existing callers, at its strongest: their statement does not
    name either new column, so it cannot fail on a database where the mig-226 heal did not land."""
    for sql in (ledger._INSERT_PURCHASE_SQL, ledger._INSERT_PURCHASE_SQL_SQLITE):
        assert "item_source" not in sql and "cart_url" not in sql
    for sql in (ledger._INSERT_CART_LINK_PURCHASE_SQL, ledger._INSERT_CART_LINK_PURCHASE_SQL_SQLITE):
        assert ":item_source" in sql and ":cart_url" in sql
    assert "JSONB" not in ledger._INSERT_CART_LINK_PURCHASE_SQL_SQLITE


async def test_a_reap_variant_create_still_works_on_a_database_without_the_226_columns():
    await to_pre_226_shape()
    row = await ledger.create_purchase(
        buyer_ref="bref_alice", agent_id="agent_one", agent_user_ref_hash="hash_alice",
        merchant_domain="brand.example", currency="USD", our_price_minor=4250,
    )
    assert row["state"] == "resolving"


# ══ 3. CLIENT: the builder and the transport, without the wire name ══════════════════════════


def test_the_shipped_field_name_is_unset():
    """The spec has not published it; the module must not guess. Read from the SOURCE, because
    the autouse fixture patches the attribute for every other test."""
    source = Path(rc.__file__).read_text()
    assert re.search(r"^CART_LINK_QUOTE_FIELD: Optional\[str\] = None$", source, re.M)


def test_supports_is_false_while_the_field_is_unset(monkeypatch):
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", None)
    assert rc.supports_cart_link_quote() is False
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", FAKE_FIELD)
    assert rc.supports_cart_link_quote() is True


@pytest.mark.parametrize("bad", ["", "email", "items", "shippingAddress", "cart url", "1x", 7])
def test_an_unusable_field_name_is_unsupported(monkeypatch, bad):
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", bad)
    assert rc.supports_cart_link_quote() is False
    with pytest.raises(rc.CartLinkQuoteUnsupported):
        rc.build_cart_link_quote_request(cart_url=CART_URL, email=EMAIL, shipping_address=ADDRESS)


def test_the_builder_raises_the_request_error_with_its_code_while_unset(monkeypatch):
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", None)
    with pytest.raises(rc.ReapRequestError) as caught:
        rc.build_cart_link_quote_request(cart_url=CART_URL, email=EMAIL, shipping_address=ADDRESS)
    assert isinstance(caught.value, rc.CartLinkQuoteUnsupported)
    assert caught.value.code == "cart_link_quote_unsupported"
    assert str(caught.value).startswith("cart_link_quote_unsupported")


def test_the_builder_puts_the_url_under_the_field_and_keeps_the_published_buyer_shape():
    body = rc.build_cart_link_quote_request(
        cart_url=CART_URL,
        email=EMAIL,
        shipping_address={**ADDRESS, "dateOfBirth": "1990-01-01"},
    )
    assert set(body) == {FAKE_FIELD, "email", "shippingAddress"}
    assert body[FAKE_FIELD] == CART_URL
    assert body["email"] == EMAIL
    # The SAME whitelist the variant quote uses — the extra key is dropped, not forwarded.
    assert body["shippingAddress"] == rc.build_shipping_address(ADDRESS)
    assert "items" not in body


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        (dict(cart_url=CART_URL, email="not-an-email", shipping_address=ADDRESS), "email"),
        (dict(cart_url=CART_URL, email=EMAIL, shipping_address=None), "shipping address"),
        (dict(cart_url=CART_URL, email=EMAIL, shipping_address={"city": "X"}), "firstName"),
        (dict(cart_url=PREFILL_LINK, email=EMAIL, shipping_address=ADDRESS), "cart link"),
        (dict(cart_url=f"https://{SHOP}/cart/c/abc", email=EMAIL, shipping_address=ADDRESS), "cart link"),
    ],
)
def test_the_builder_refuses_before_egress_and_never_names_the_url(kwargs, needle):
    with pytest.raises(rc.ReapRequestError) as caught:
        rc.build_cart_link_quote_request(**kwargs)
    message = str(caught.value)
    assert needle in message
    assert "adrian" not in message.lower() and SHOP not in message


class _Wire:
    """A recording stand-in for httpx.AsyncClient that lets `_post` run for real."""

    calls = []
    status = 200
    payload = {}

    def __init__(self, *a, **kw):
        self.timeout = kw.get("timeout")
        self.follow_redirects = kw.get("follow_redirects", "_MISSING")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None, params=None, headers=None):
        _Wire.calls.append({"method": method, "url": url, "body": json, "headers": headers,
                            "timeout": self.timeout, "follow_redirects": self.follow_redirects})
        return _WireStream()


class _WireResponse:
    def __init__(self):
        self.status_code = _Wire.status
        self.headers = {}

    async def aiter_bytes(self, chunk_size=None):
        yield json.dumps(_Wire.payload).encode("utf-8")


class _WireStream:
    async def __aenter__(self):
        return _WireResponse()

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def wire(monkeypatch):
    import httpx

    _Wire.calls = []
    _Wire.status = 200
    _Wire.payload = cart_quote()
    monkeypatch.setattr(httpx, "AsyncClient", _Wire)
    return _Wire


async def test_the_cart_link_quote_rides_request_quotes_own_transport(wire, monkeypatch):
    """REUSE, NOT A FORK: same path, same timeout, same idempotency header, same headers, no
    redirects. Compared against a real `request_quote` call on the same recorder, so a fork that
    drifted in any of these fails here."""
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    got = await rc.request_cart_link_quote(
        cart_url=CART_URL, email=EMAIL, shipping_address=ADDRESS
    )
    await rc.request_quote(
        items=[{"variantId": "var_x", "quantity": 1}], email=EMAIL, shipping_address=ADDRESS
    )
    cart, variant = wire.calls
    assert got.ok and got.data["id"] == "q_cart_1"
    assert cart["method"] == variant["method"] == "POST"
    assert cart["url"] == variant["url"] == "https://sandbox.api.reap.global/agentic/quotes"
    assert cart["timeout"] == variant["timeout"]
    assert cart["follow_redirects"] is False
    assert set(cart["headers"]) == set(variant["headers"])
    assert cart["headers"]["Idempotency-Key"].startswith("pivota-quotes-")
    assert cart["headers"]["Reap-Version"] == variant["headers"]["Reap-Version"]
    assert cart["body"][FAKE_FIELD] == CART_URL


async def test_the_cart_link_idempotency_key_is_stable_and_body_derived(wire, monkeypatch):
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    for url in (CART_URL, CART_URL, CART_URL.replace(":1?", ":2?")):
        await rc.request_cart_link_quote(cart_url=url, email=EMAIL, shipping_address=ADDRESS)
    keys = [c["headers"]["Idempotency-Key"] for c in wire.calls]
    assert keys[0] == keys[1] != keys[2]


async def test_a_503_on_a_cart_link_quote_is_marked_not_completable(wire):
    wire.status = 503
    got = await rc.request_cart_link_quote(cart_url=CART_URL, email=EMAIL, shipping_address=ADDRESS)
    assert not got.ok and got.merchant_probably_not_completable


async def test_nothing_reaches_the_wire_while_the_field_is_unset(wire, monkeypatch):
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", None)
    with pytest.raises(rc.CartLinkQuoteUnsupported):
        await rc.request_cart_link_quote(cart_url=CART_URL, email=EMAIL, shipping_address=ADDRESS)
    assert wire.calls == []


# ══ 4. SERVICE: start_purchase ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "rail,lane,reason",
    [
        (None, None, "rail_disabled"),
        (None, "1", "rail_disabled"),
        ("1", None, "cart_link_disabled"),
        ("ture", "1", "rail_disabled"),
        ("1", "ture", "cart_link_disabled"),
        ("1", "0", "cart_link_disabled"),
        ("1", "", "cart_link_disabled"),
        ("1", "enabled", "cart_link_disabled"),
    ],
)
async def test_the_dials_refuse_before_any_write(monkeypatch, rail, lane, reason):
    for env, value in (("REAP_AGENTIC_ENABLED", rail), ("REAP_AGENTIC_CART_LINK_ENABLED", lane)):
        if value is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, value)
    with pytest.raises(svc.PurchaseRefused) as caught:
        await start()
    assert caught.value.reason == reason
    assert await count() == 0


@pytest.mark.parametrize("value", ["1", "true", "on", "yes", " TRUE "])
def test_the_lane_dial_accepts_exactly_the_rails_spellings(monkeypatch, value):
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", value)
    assert svc.is_cart_link_enabled() is True


def test_the_lane_dial_defaults_off(monkeypatch):
    monkeypatch.delenv("REAP_AGENTIC_CART_LINK_ENABLED", raising=False)
    assert svc.is_cart_link_enabled() is False


async def test_the_lane_dial_does_not_gate_the_variant_lane(monkeypatch):
    """CONTROL: turning the cart-link dial off must not touch a reap_variant purchase."""
    monkeypatch.delenv("REAP_AGENTIC_CART_LINK_ENABLED", raising=False)
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", None)
    purchase_id = await start(
        cart_link=None,
        row=svc.PurchaseRow(
            merchant_domain="brand.example", product_key="pk_1", variant_key="vk_1",
            product_name="Standard Eau de Parfum", variant_title="Standard", brand="Brand",
            category="fragrance", our_price_minor=4250, currency="USD", market_country="US",
        ),
        click_id="click_abc",
    )
    row = await get(purchase_id)
    assert row["item_source"] == "reap_variant" and row["cart_url"] is None


async def test_an_unsupported_client_refuses_before_any_write(monkeypatch):
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", None)
    with pytest.raises(svc.PurchaseRefused) as caught:
        await start()
    assert caught.value.reason == "cart_link_quote_unsupported"
    assert await count() == 0


async def test_an_unconfigured_client_still_refuses_first_as_unconfigured(monkeypatch):
    monkeypatch.delenv("REAP_API_KEY", raising=False)
    with pytest.raises(svc.PurchaseRefused) as caught:
        await start()
    assert caught.value.reason == "rail_unconfigured"


@pytest.mark.parametrize(
    "over,reason,detail",
    [
        (dict(cart_link=item(cart_url=PREFILL_LINK)), "cart_link_refused", "checkout_prefill"),
        (dict(cart_link=item(cart_url=CART_URL.replace("country=US", "country=JP"))),
         "cart_link_refused", "country_mismatch"),
        (dict(click_id="clk_other"), "cart_link_refused", "click_id_mismatch"),
        (dict(cart_link=item(shop_domain="other.example")), "cart_link_refused", "host_mismatch"),
        (dict(quantity=2), "cart_link_refused", "quantity_mismatch"),
        (dict(cart_link=item(market_country=None)), "invalid_request", "market_country"),
        (dict(cart_link=item(market_country="USA")), "invalid_request", "market_country"),
        (dict(cart_link=item(our_price_minor=0)), "invalid_request", "our_price_minor"),
        (dict(cart_link=item(currency="KWD")), "currency_unsupported", "KWD"),
        (dict(quantity=11), "invalid_request", "quantity"),
        (dict(quantity=True), "invalid_request", "quantity"),
        (dict(click_id=None), "invalid_request", "click_id"),
        (dict(row=object()), "invalid_request", "not both"),
        (dict(cart_link="https://judydoll.com/cart/1:1"), "invalid_request", "CartLinkItem"),
        (dict(buyer=svc.BuyerContact(email="nope", shipping_address=dict(ADDRESS))),
         "invalid_request", "email"),
        (dict(return_url="https://evil.example/r"), "invalid_return_url", "returnUrl"),
    ],
)
async def test_every_cart_link_refusal_happens_before_any_write(over, reason, detail):
    with pytest.raises(svc.PurchaseRefused) as caught:
        await start(**over)
    assert caught.value.reason == reason
    assert detail in str(caught.value)
    assert await count() == 0
    for pii in PII_STRINGS:
        assert pii.lower() not in str(caught.value).lower()


async def test_a_started_cart_link_purchase_is_resolving_with_the_canonical_url():
    purchase_id = await start(
        cart_link=item(
            cart_url=f"https://{SHOP}/cart/{VARIANT}:1?country=us&attributes%5Bpivota_click_id%5D={CLICK}"
        )
    )
    row = await get(purchase_id)
    assert row["state"] == "resolving"
    assert row["item_source"] == "cart_link"
    assert row["cart_url"] == CART_URL
    assert row["merchant_domain"] == SHOP
    assert row["click_id"] == CLICK and row["market_country"] == "US"
    assert row["our_price_minor"] == 2820 and row["quantity"] == 1
    assert row["buyer_email"] == EMAIL


# ══ 5. SERVICE: advance, end to end, with a FAKE client ═════════════════════════════════════


async def test_the_happy_path_reaches_completed_and_closes_attribution_once(reap, attribution):
    purchase_id = await to_quoting(reap)
    assert (await step(purchase_id)).state == "awaiting_approval"
    done = await step(purchase_id)
    assert done.state == "completed", done

    row = await get(purchase_id)
    assert row["reap_order_id"] == "ord_cart_1"
    assert row["quoted_total_minor"] == 3320 and row["final_total_minor"] == 3320
    assert row["shipping_minor"] == 500 and row["tax_minor"] == 0
    assert row["last_error_code"] is None
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["cart_url"] == CART_URL

    assert len(attribution.calls) == 1
    call = attribution.calls[0]
    assert call["click_id"] == CLICK
    assert call["merchant_id"] == SHOP and call["converting_shop_domain"] == SHOP
    assert call["external_order_id"] == "ord_cart_1"
    assert call["gross_amount_cents"] == 3320 and call["currency"] == "USD"
    assert call["note_attrs_or_payload"]["partner_reported"] is True
    assert call["is_self_report"] is False

    # No resolver, no items quote: exactly one cart-link quote, carrying the stored URL and the
    # buyer's details, then the ordinary checkout legs.
    assert "resolve_our_row" not in reap.sequence() and "request_quote" not in reap.sequence()
    (quote_call,) = reap.named("request_cart_link_quote")
    assert quote_call == {"cart_url": CART_URL, "email": EMAIL,
                          "shipping_address": rc.build_shipping_address(ADDRESS)}
    assert reap.sequence() == ["request_cart_link_quote", "create_checkout", "get_checkout"]


async def test_an_unenrolled_buyer_goes_through_the_shared_enrollment_path(reap, attribution):
    purchase_id = await start()
    assert (await step(purchase_id)).state == "needs_enrollment"
    assert (await step(purchase_id)).state == "quoting"
    assert (await step(purchase_id)).state == "awaiting_approval"
    assert (await step(purchase_id)).state == "completed"
    assert len(attribution.calls) == 1
    assert "resolve_our_row" not in reap.sequence()


@pytest.mark.parametrize(
    "options",
    [[], None, "ship_std", [None], ["ship_std"], {"id": "ship_std"}],
    ids=["empty", "null", "string", "list-of-null", "list-of-string", "object"],
)
async def test_no_shipping_option_is_a_terminal_refusal(reap, attribution, options):
    """THE SHIPPING PROOF. A merchant that cannot ship to the address, or holds the cart below a
    basket minimum, answers with no option — terminal, PII gone, no checkout created."""
    purchase_id = await to_quoting(reap)
    reap.request_cart_link_quote = ok(cart_quote(shippingOptions=options))
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "no_shipping_option"
    row = await get(purchase_id)
    assert row["last_error_code"] == "quote_no_shipping_option"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert "create_checkout" not in reap.sequence()
    assert attribution.calls == []


async def test_an_absent_shipping_options_key_is_a_terminal_refusal(reap, attribution):
    purchase_id = await to_quoting(reap)
    quote = cart_quote()
    del quote["shippingOptions"]
    reap.request_cart_link_quote = ok(quote)
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "no_shipping_option"


async def test_a_subtotal_that_is_not_our_price_times_quantity_is_price_changed(reap, attribution):
    purchase_id = await to_quoting(reap)
    quote = cart_quote()
    quote["amountBreakdown"]["itemsSubtotal"] = {"amount": 28.21, "currency": "USD"}
    quote["amountBreakdown"]["finalAmount"] = {"amount": 33.21, "currency": "USD"}
    reap.request_cart_link_quote = ok(quote)
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "price_changed"
    assert (await get(purchase_id))["last_error_code"] == "quote_items_subtotal_mismatch"
    assert "create_checkout" not in reap.sequence()


async def test_a_quote_in_another_currency_refuses(reap, attribution):
    purchase_id = await to_quoting(reap)
    quote = cart_quote()
    for key in ("itemsSubtotal", "shipping", "finalAmount"):
        quote["amountBreakdown"][key]["currency"] = "SGD"
    quote["amountBreakdown"]["tax"]["amount"]["currency"] = "SGD"
    reap.request_cart_link_quote = ok(quote)
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "price_changed"
    row = await get(purchase_id)
    assert row["last_error_code"] == "quote_currency_mismatch"
    assert row["quoted_total_minor"] is None
    assert "create_checkout" not in reap.sequence()


@pytest.mark.parametrize(
    "items",
    [
        [{"variantId": "var_a", "quantity": 1}, {"variantId": "var_b", "quantity": 1}],
        [{"variantId": "var_a", "quantity": 2}],
        [{"variantId": "var_a", "quantity": True}],
        [{"variantId": "var_a"}],
        [],
        None,
        ["var_a"],
        "var_a",
        {"variantId": "var_a", "quantity": 1},
    ],
    ids=["two-lines", "wrong-qty", "bool-qty", "no-qty", "empty", "null", "not-an-object",
         "string", "dict"],
)
async def test_a_present_items_that_is_not_one_line_at_our_quantity_refuses(
    reap, attribution, items
):
    """OPTIONAL-BUT-STRICT. A PRESENT `items` — including `null` and `[]` — must be exactly one
    line at our quantity. Only a MISSING key skips the echo (what Reap actually sends)."""
    purchase_id = await to_quoting(reap)
    quote = cart_quote()
    quote["items"] = items
    reap.request_cart_link_quote = ok(quote)
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "price_unverifiable"
    assert (await get(purchase_id))["last_error_code"] == "quote_items_mismatch"
    assert "create_checkout" not in reap.sequence()


async def test_an_absent_items_key_skips_only_the_echo(reap, attribution):
    """What Reap sends. The echo is skipped; the subtotal, currency and shipping rules are not."""
    purchase_id = await to_quoting(reap)
    quote = cart_quote()
    assert "items" not in quote
    quote["amountBreakdown"]["itemsSubtotal"] = {"amount": 28.19, "currency": "USD"}
    quote["amountBreakdown"]["finalAmount"] = {"amount": 33.19, "currency": "USD"}
    reap.request_cart_link_quote = ok(quote)
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "price_changed"
    assert (await get(purchase_id))["last_error_code"] == "quote_items_subtotal_mismatch"


async def test_a_present_correct_echo_is_still_accepted(reap, attribution):
    purchase_id = await to_quoting(reap)
    reap.request_cart_link_quote = ok(cart_quote(items=ONE_LINE))
    assert (await step(purchase_id)).state == "awaiting_approval"


async def test_quantity_two_is_priced_as_two(reap, attribution):
    await active_enrollment()
    purchase_id = await start(cart_link=item(cart_url=CART_URL.replace(":1?", ":2?")), quantity=2)
    assert (await step(purchase_id)).state == "quoting"
    quote = cart_quote(items=[{"variantId": "var_opaque_1", "quantity": 2}])
    quote["amountBreakdown"]["itemsSubtotal"] = {"amount": 56.40, "currency": "USD"}
    quote["amountBreakdown"]["finalAmount"] = {"amount": 61.40, "currency": "USD"}
    reap.request_cart_link_quote = ok(quote)
    assert (await step(purchase_id)).state == "awaiting_approval"


async def test_a_transport_failure_on_the_quote_releases_and_retries(reap, attribution):
    purchase_id = await to_quoting(reap)
    reap.request_cart_link_quote = rc.ReapResponse(ok=False, error="transport_error:ReadTimeout")
    moved = await step(purchase_id)
    assert moved.outcome == "released" and moved.state == "quoting"
    assert moved.last_error_code == "transport_error:readtimeout"


@pytest.mark.parametrize(
    "exc,reason",
    [
        (rc.CartLinkQuoteUnsupported("cart_link_quote_unsupported: gone"),
         "cart_link_quote_unsupported"),
        (rc.ReapRequestError("a cart-link quote needs a shipping address"),
         "cart_link_quote_unbuildable"),
    ],
)
async def test_a_builder_refusal_is_a_refusal_not_an_exception_out_of_advance(
    reap, attribution, exc, reason
):
    """The builder raises BEFORE egress. That must end the purchase with a name, not escape
    `advance` and be retried by the poller forever."""
    purchase_id = await to_quoting(reap)

    async def _raise(**kwargs):
        raise exc

    reap.request_cart_link_quote = _raise
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == reason
    assert (await get(purchase_id))["buyer_email"] is None


async def test_a_503_on_the_quote_refuses_as_not_completable(reap, attribution):
    purchase_id = await to_quoting(reap)
    reap.request_cart_link_quote = rc.ReapResponse(
        ok=False, status=503, error="reap_status_503", merchant_probably_not_completable=True
    )
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "merchant_not_completable"


# ── the in-flight kill switch and the re-checks ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "env,value", [("REAP_AGENTIC_CART_LINK_ENABLED", "0"), ("REAP_AGENTIC_ENABLED", "ture")]
)
async def test_a_dial_turned_off_refuses_a_resolving_row(reap, attribution, monkeypatch, env, value):
    purchase_id = await start()
    monkeypatch.setenv(env, value)
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "cart_link_disabled"
    assert (await get(purchase_id))["buyer_email"] is None
    assert reap.calls == []


async def test_a_dial_turned_off_refuses_a_quoting_row_before_any_quote(
    reap, attribution, monkeypatch
):
    purchase_id = await to_quoting(reap)
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "off")
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "cart_link_disabled"
    assert reap.named("request_cart_link_quote") == []


async def test_a_field_name_lost_mid_flight_refuses_a_quoting_row(reap, attribution, monkeypatch):
    purchase_id = await to_quoting(reap)
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", None)
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "cart_link_quote_unsupported"
    assert reap.named("request_cart_link_quote") == []


async def test_a_dial_turned_off_after_the_checkout_exists_never_abandons_it(
    reap, attribution, monkeypatch
):
    """The buyer may have approved. Polling continues and the purchase completes."""
    purchase_id = await to_quoting(reap)
    assert (await step(purchase_id)).state == "awaiting_approval"
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "0")
    monkeypatch.setattr(rc, "CART_LINK_QUOTE_FIELD", None)
    assert (await step(purchase_id)).state == "completed"
    assert len(attribution.calls) == 1


@pytest.mark.parametrize(
    "column,value,code",
    [
        ("click_id", "clk_tampered", "cart_link_click_id_mismatch"),
        ("merchant_domain", "other.example", "cart_link_host_mismatch"),
        ("market_country", "SG", "cart_link_country_mismatch"),
    ],
)
async def test_a_row_that_disagrees_with_its_url_is_refused_before_the_quote(
    reap, attribution, column, value, code
):
    """The stored URL is re-validated against the STORED click id, shop and market before it
    leaves for Reap — not only at create."""
    purchase_id = await to_quoting(reap)
    await database.execute(
        f"UPDATE reap_agentic_purchases SET {column} = :v WHERE id = :i",
        {"v": value, "i": purchase_id},
    )
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "cart_link_invalid"
    assert (await get(purchase_id))["last_error_code"] == code
    assert reap.named("request_cart_link_quote") == []


async def test_a_row_whose_quantity_disagrees_with_its_url_is_refused(reap, attribution):
    purchase_id = await to_quoting(reap)
    await database.execute(
        "UPDATE reap_agentic_purchases SET quantity = 2 WHERE id = :i", {"i": purchase_id}
    )
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "cart_link_invalid"
    assert (await get(purchase_id))["last_error_code"] == "cart_link_quantity_mismatch"


async def test_a_tampered_row_in_resolving_is_refused_too(reap, attribution):
    purchase_id = await start()
    await database.execute(
        "UPDATE reap_agentic_purchases SET click_id = 'clk_tampered' WHERE id = :i",
        {"i": purchase_id},
    )
    moved = await step(purchase_id)
    assert moved.state == "refused" and moved.refusal_reason == "cart_link_invalid"
    assert reap.calls == []


async def test_the_click_id_is_checked_again_at_the_attribution_sink(reap, attribution):
    """After the checkout exists, a row whose click id no longer matches its URL still COMPLETES
    (the buyer paid) but writes NO edge — an edge is permanent and would credit a click the
    merchant's order does not carry."""
    purchase_id = await to_quoting(reap)
    assert (await step(purchase_id)).state == "awaiting_approval"
    await database.execute(
        "UPDATE reap_agentic_purchases SET click_id = 'clk_tampered' WHERE id = :i",
        {"i": purchase_id},
    )
    done = await step(purchase_id)
    assert done.state == "completed"
    assert (await get(purchase_id))["last_error_code"] == "cart_link_click_id_mismatch"
    assert attribution.calls == []


# ── verify_cart_link_quote, rule by rule ─────────────────────────────────────────────────────

_ROW = {"currency": "USD", "our_price_minor": 2820, "quantity": 1}


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda q: q, (True, None, None)),
        (lambda q: q.update(items=ONE_LINE + ONE_LINE),
         (False, "price_unverifiable", "quote_items_mismatch")),
        (lambda q: q.update(items=[{"quantity": 3}]),
         (False, "price_unverifiable", "quote_items_mismatch")),
        (lambda q: q.update(shippingOptions=[]),
         (False, "no_shipping_option", "quote_no_shipping_option")),
        (lambda q: q["amountBreakdown"]["itemsSubtotal"].update(amount=28.19),
         (False, "price_changed", "quote_items_subtotal_mismatch")),
        (lambda q: q["amountBreakdown"]["shipping"].update(currency="EUR"),
         (False, "price_changed", "quote_currency_mismatch")),
        (lambda q: q["amountBreakdown"]["finalAmount"].update(amount=40.00),
         (False, "price_changed", "quote_total_not_reconciled")),
    ],
    ids=["ok-no-items", "two-lines", "qty", "no-shipping", "subtotal", "currency", "reconcile"],
)
def test_verify_cart_link_quote_names_which_rule_refused(mutate, expected):
    quote = cart_quote()
    mutate(quote)
    check = svc.verify_cart_link_quote(quote, _ROW)
    assert (check.ok, check.refusal_reason, check.last_error_code) == expected


def test_the_items_rule_runs_before_the_shipping_rule():
    """Order is part of the contract: a quote for the wrong thing is reported as that, even when
    it also has no shipping."""
    quote = cart_quote(items=[], shippingOptions=[])
    assert svc.verify_cart_link_quote(quote, _ROW).last_error_code == "quote_items_mismatch"


@pytest.mark.parametrize("with_echo", [False, True], ids=["no-items", "items-present"])
@pytest.mark.parametrize("quantity", [0, 11, None, True, "1"])
def test_an_uncheckable_row_quantity_is_unverifiable(quantity, with_echo):
    """The ROW is checked before anything in the quote is compared to it. Without an echo that is
    also true of `verify_quote`'s own row check; WITH a present echo it is this function's own
    ceiling that decides, because the echo compares against the row's quantity."""
    quote = cart_quote(items=ONE_LINE) if with_echo else cart_quote()
    check = svc.verify_cart_link_quote(quote, {**_ROW, "quantity": quantity})
    assert (check.ok, check.last_error_code) == (False, "quote_row_unverifiable")


def test_a_successful_check_returns_the_amounts():
    check = svc.verify_cart_link_quote(cart_quote(), _ROW)
    assert (check.total_minor, check.subtotal_minor, check.shipping_minor, check.tax_minor) == (
        3320, 2820, 500, 0,
    )


# ── PII ──────────────────────────────────────────────────────────────────────────────────────


async def test_no_log_record_result_or_exception_carries_the_buyers_details(
    reap, attribution, caplog
):
    caplog.set_level(logging.DEBUG)
    seen = []

    purchase_id = await to_quoting(reap)
    seen.append(await step(purchase_id))
    seen.append(await step(purchase_id))

    refused_id = await start(buyer_ref="bref_bob")
    reap.request_cart_link_quote = ok(cart_quote(shippingOptions=[]))
    await active_enrollment(
        buyer_ref="bref_bob", reap_id="4fa85f64-5717-4562-b3fc-2c963f66afa7"
    )
    seen.append(await step(refused_id))
    seen.append(await step(refused_id))

    for over in (dict(cart_link=item(cart_url=PREFILL_LINK)), dict(quantity=2)):
        with pytest.raises(svc.PurchaseRefused) as caught:
            await start(**over)
        seen.append(str(caught.value))

    # SCOPED TO THE RAIL'S OWN LOGGERS, as in tests/test_reap_agentic_purchase.py and for the
    # reason given there: at DEBUG the driver channels echo every statement with its binds, which
    # is a deployment decision and not a property of this code.
    ours = [
        r for r in caplog.records
        if r.name.startswith(
            ("services.reap_agentic", "db.reap_agentic", "reap_agentic_client",
             "services.reap_cart_link")
        )
    ]
    haystack = "\n".join(
        [r.getMessage() for r in ours] + [repr(r.args) for r in ours] + [repr(s) for s in seen]
    ).lower()
    for pii in PII_STRINGS:
        assert pii.lower() not in haystack, f"{pii!r} leaked"
    assert "checkout[" not in haystack
    # CONTROL: the haystack is not empty — the lane did log, and the refusals were captured.
    assert any("item_source=cart_link" in r.getMessage() for r in ours)
    assert "checkout_prefill" in haystack and "quantity_mismatch" in haystack


# ══ 6. ONE EDGE PER CART-LINK SALE: the mig-228 click claim ═════════════════════════════════
#
# The P1 from the #2214 review. One cart-link sale can be closed by Reap under
# (merchant_domain, Reap orderId) AND by the merchant's own Shopify order (which carries our click
# id) under (tenant merchant, Shopify order id). `Attribution.edges` keeps the real edge table's
# only dedupe, so without the claim every ordering below produces TWO edges.

import services.commerce_attribution_service as _cas  # noqa: E402
import services.conversion_click_claims as ccc  # noqa: E402

#: Captured at import, BEFORE the autouse `attribution` fixture replaces it on the module, so the
#: real-close cases can put the genuine primitive back.
REAL_CLOSE = _cas.close_external_order_conversion

MERCHANT_TENANT = "merch_judydoll"
SHOPIFY_ORDER_ID = "5550001"
REAP_ORDER_ID = "ord_cart_1"


async def claims_count() -> int:
    row = await database.fetch_one("SELECT COUNT(*) AS n FROM conversion_click_claims")
    return int(row["n"])


async def completed_via_reap(reap) -> str:
    purchase_id = await to_quoting(reap)
    assert (await step(purchase_id)).state == "awaiting_approval"
    assert (await step(purchase_id)).state == "completed"
    return purchase_id


async def purchase_id_for(click_id=CLICK) -> str:
    row = await database.fetch_one(
        "SELECT id FROM reap_agentic_purchases WHERE click_id = :c", {"c": click_id}
    )
    return row["id"]


class _FakeWebhookRequest:
    def __init__(self, body: bytes):
        self._body = body

        class _H:
            def get(self, key, default=None):
                return default

        self.headers = _H()

    async def body(self):
        return self._body


class _FakeOrdersDB:
    async def fetch_one(self, query, values=None):
        return None

    async def execute(self, query, values=None):
        return 0


@pytest.fixture
def merchant_paths(monkeypatch, attribution):
    """The REAL `orders/paid` webhook handler and the REAL poller `_process_order`, each with its
    own module-level `close_external_order_conversion` pointed at the shared edge recorder. Only
    their non-attribution collaborators are stubbed, as in
    tests/test_t2_2_webhook_orders_paid_closure.py."""
    import db.database as db_database
    import routes.webhook_routes as wr
    import services.external_conversion_poller as poller

    async def _onboarding(merchant_id):
        return {"merchant_id": merchant_id, "mcp_shop_domain": "judydoll.myshopify.com"}

    async def _stores(merchant_id):
        return []

    async def _ingest(**kwargs):
        return (False, None)

    async def _log(**kwargs):
        return None

    monkeypatch.setattr(wr, "get_merchant_onboarding", _onboarding)
    monkeypatch.setattr(wr, "get_merchant_active_stores", _stores)
    monkeypatch.setattr(wr, "ingest_shopify_webhook", _ingest)
    monkeypatch.setattr(wr, "record_shopify_webhook", lambda *a, **k: None)
    monkeypatch.setattr(wr, "log_order_event", _log)
    monkeypatch.setattr(wr, "close_external_order_conversion", attribution)
    monkeypatch.setattr(poller, "close_external_order_conversion", attribution)
    # The webhook reads `db.database.database` at CALL time for its Pivota-orders lookup; the
    # claim module and the ledger hold the real one, which is the point.
    monkeypatch.setattr(db_database, "database", _FakeOrdersDB())

    class _Paths:
        async def webhook(self, click_id=CLICK, order_id=SHOPIFY_ORDER_ID):
            from fastapi import BackgroundTasks

            payload = json.dumps({
                "id": int(order_id), "name": "#1042", "financial_status": "paid",
                "total_price": "33.20", "currency": "USD",
                "note_attributes": [{"name": "pivota_click_id", "value": click_id}],
            }).encode()
            resp = await wr.handle_shopify_webhook(
                merchant_id=MERCHANT_TENANT,
                request=_FakeWebhookRequest(payload),
                background_tasks=BackgroundTasks(),
                x_shopify_hmac_sha256="whatever",
                x_shopify_topic="orders/paid",
                x_shopify_shop_domain="judydoll.myshopify.com",
            )
            assert resp["status"] == "success"

        async def poller(self, click_id=CLICK, order_id=SHOPIFY_ORDER_ID):
            from datetime import datetime, timezone

            return await poller._process_order(
                merchant_id=MERCHANT_TENANT,
                order={
                    "id": int(order_id), "financial_status": "paid",
                    "total_price": "33.20", "currency": "USD",
                    "note_attributes": [{"name": "pivota_click_id", "value": click_id}],
                },
                converted_at_default=datetime.now(timezone.utc),
                shop_domain="judydoll.myshopify.com",
            )

    return _Paths()


async def _finish_reap(purchase_id):
    await active_enrollment()
    assert (await step(purchase_id)).state == "quoting"
    assert (await step(purchase_id)).state == "awaiting_approval"
    assert (await step(purchase_id)).state == "completed"


@pytest.mark.parametrize("channel", ["webhook", "poller"])
async def test_merchant_first_then_reap_leaves_one_edge_the_merchants(
    reap, attribution, merchant_paths, channel
):
    purchase_id = await start()  # the cart-link purchase exists, so its click is claim-scoped
    await getattr(merchant_paths, channel)()
    assert list(attribution.edges) == [(MERCHANT_TENANT, SHOPIFY_ORDER_ID)]

    await _finish_reap(purchase_id)  # Reap completes the SAME sale afterwards

    assert list(attribution.edges) == [(MERCHANT_TENANT, SHOPIFY_ORDER_ID)]
    row = await get(purchase_id)
    assert row["last_error_code"] == "attribution_closed_by_other_channel"
    assert row["state"] == "completed" and row["reap_order_id"] == REAP_ORDER_ID


@pytest.mark.parametrize("channel", ["webhook", "poller"])
async def test_reap_first_then_merchant_leaves_one_edge_reaps(
    reap, attribution, merchant_paths, channel
):
    purchase_id = await completed_via_reap(reap)
    assert list(attribution.edges) == [(SHOP, REAP_ORDER_ID)]
    assert (await get(purchase_id))["last_error_code"] is None

    await getattr(merchant_paths, channel)()
    assert list(attribution.edges) == [(SHOP, REAP_ORDER_ID)]
    # The merchant close was not even attempted — not attempted-and-deduped.
    assert [c["external_order_id"] for c in attribution.calls] == [REAP_ORDER_ID]


async def test_the_webhook_and_the_poller_are_one_claimant(reap, attribution, merchant_paths):
    """Both merchant paths close the SAME Shopify order under the SAME key. The poller, seeing an
    order the webhook already closed, is the owner retrying — it proceeds, and the edge table's
    own (merchant, order) dedupe makes it a replay."""
    await start()
    await merchant_paths.webhook()
    assert await merchant_paths.poller() == "closed"
    assert list(attribution.edges) == [(MERCHANT_TENANT, SHOPIFY_ORDER_ID)]
    assert len(attribution.calls) == 2


async def test_a_second_shopify_order_on_the_same_claimed_click_is_skipped(
    reap, attribution, merchant_paths
):
    """One click, one edge: the claim is per click. Stated as a test so the choice is visible."""
    await start()
    await merchant_paths.webhook(order_id="5550001")
    await merchant_paths.webhook(order_id="5550002")
    assert list(attribution.edges) == [(MERCHANT_TENANT, "5550001")]


@pytest.mark.parametrize("channel", ["webhook", "poller"])
async def test_a_non_reap_click_closes_exactly_as_before_and_never_touches_the_claims(
    reap, attribution, merchant_paths, channel, monkeypatch
):
    """Byte-for-byte: the close receives the same keyword arguments the pre-228 call site sent,
    and no claim statement runs."""
    await start()  # a cart-link purchase exists — for ANOTHER click
    touched = []
    real_claim = ccc.claim_click

    async def _spy(*a, **k):
        touched.append(a)
        return await real_claim(*a, **k)

    monkeypatch.setattr(ccc, "claim_click", _spy)
    await getattr(merchant_paths, channel)(click_id="clk_organic_1")
    assert touched == [] and await claims_count() == 0
    (call,) = attribution.calls
    expected = {
        "merchant_id": MERCHANT_TENANT, "click_id": "clk_organic_1",
        "external_order_id": SHOPIFY_ORDER_ID, "gross_amount_cents": 3320, "currency": "USD",
        "converting_shop_domain": "judydoll.myshopify.com",
    }
    assert {k: call[k] for k in expected} == expected
    assert set(call) == {
        "merchant_id", "click_id", "external_order_id", "gross_amount_cents", "currency",
        "converted_at", "note_attrs_or_payload", "converting_shop_domain",
    }


async def test_a_reap_variant_purchases_click_is_not_claim_scoped(
    reap, attribution, merchant_paths
):
    """Only CART-LINK purchases put the click on the merchant order. A variant-lane row with the
    same click id is not in scope."""
    await ledger.create_purchase(
        buyer_ref="bref_v", agent_id="agent_one", agent_user_ref_hash="hash_v",
        merchant_domain="brand.example", currency="USD", our_price_minor=4250,
        click_id="clk_variant_1",
    )
    await merchant_paths.webhook(click_id="clk_variant_1")
    assert await claims_count() == 0
    assert list(attribution.edges) == [(MERCHANT_TENANT, SHOPIFY_ORDER_ID)]


# ── the error paths: merchant side fails OPEN, Reap side fails CLOSED ────────────────────────


@pytest.mark.parametrize("channel", ["webhook", "poller"])
async def test_a_missing_claims_table_fails_open_on_the_merchant_side(
    reap, attribution, merchant_paths, caplog, channel
):
    await start()
    await database.execute("DROP TABLE conversion_click_claims")
    caplog.set_level(logging.WARNING, logger="services.conversion_click_claims")
    await getattr(merchant_paths, channel)()
    assert list(attribution.edges) == [(MERCHANT_TENANT, SHOPIFY_ORDER_ID)]
    assert any("closing as before" in r.getMessage() for r in caplog.records)


async def test_a_failing_scope_lookup_fails_open_on_the_merchant_side(
    reap, attribution, merchant_paths
):
    """The lookup itself can fail too (here: a pre-226 table with no item_source)."""
    await to_pre_226_shape()
    await merchant_paths.poller()
    assert list(attribution.edges) == [(MERCHANT_TENANT, SHOPIFY_ORDER_ID)]


async def test_a_missing_claims_table_fails_closed_on_the_reap_side(reap, attribution):
    purchase_id = await to_quoting(reap)
    assert (await step(purchase_id)).state == "awaiting_approval"
    await database.execute("DROP TABLE conversion_click_claims")
    assert (await step(purchase_id)).state == "completed"
    assert (await get(purchase_id))["last_error_code"] == "attribution_claim_unavailable"
    assert attribution.edges == {} and attribution.calls == []


async def test_a_reap_close_that_raises_gives_the_claim_back(reap, attribution, merchant_paths):
    """We own the click but wrote no edge: the merchant side must still be able to close it."""
    attribution.raises = RuntimeError("edge table unavailable")
    await completed_via_reap(reap)
    assert attribution.edges == {} and await claims_count() == 0
    attribution.raises = None
    await merchant_paths.webhook()
    assert list(attribution.edges) == [(MERCHANT_TENANT, SHOPIFY_ORDER_ID)]


async def test_a_merchant_close_that_raises_gives_the_claim_back(
    reap, attribution, merchant_paths
):
    purchase_id = await start()
    attribution.raises = RuntimeError("edge table unavailable")
    await merchant_paths.webhook()  # the webhook swallows it, as before
    assert attribution.edges == {} and await claims_count() == 0
    attribution.raises = None
    await _finish_reap(purchase_id)
    assert list(attribution.edges) == [(SHOP, REAP_ORDER_ID)]


async def test_a_reap_completion_that_loses_its_fence_gives_the_claim_back(
    reap, attribution, monkeypatch
):
    purchase_id = await to_quoting(reap)
    assert (await step(purchase_id)).state == "awaiting_approval"

    async def _lost(*a, **k):
        return None

    monkeypatch.setattr(ledger, "transition_as_holder", _lost)
    assert (await step(purchase_id)).outcome == "lost_claim"
    assert await claims_count() == 0 and attribution.calls == []


# ── the primitive ────────────────────────────────────────────────────────────────────────────


async def test_the_claim_is_first_writer_wins_with_an_owner_retry():
    assert await ccc.claim_click("clk_p", claimed_by="reap_agentic", external_order_id="o1")
    assert await ccc.claim_click("clk_p", claimed_by="reap_agentic", external_order_id="o1")
    assert not await ccc.claim_click("clk_p", claimed_by="reap_agentic", external_order_id="o2")
    assert not await ccc.claim_click("clk_p", claimed_by="merchant_order", external_order_id="o1")
    row = await database.fetch_one(
        "SELECT claimed_by, external_order_id FROM conversion_click_claims "
        "WHERE click_id = 'clk_p'"
    )
    assert (row["claimed_by"], row["external_order_id"]) == ("reap_agentic", "o1")


async def test_a_claim_is_released_only_by_its_owner_for_its_order():
    await ccc.claim_click("clk_r", claimed_by="merchant_order", external_order_id="o1")
    await ccc.release_click_claim("clk_r", claimed_by="reap_agentic", external_order_id="o1")
    await ccc.release_click_claim("clk_r", claimed_by="merchant_order", external_order_id="o2")
    assert await claims_count() == 1
    await ccc.release_click_claim("clk_r", claimed_by="merchant_order", external_order_id="o1")
    assert await claims_count() == 0


@pytest.mark.parametrize(
    "click_id,claimed_by,order",
    [("", "reap_agentic", "o"), ("c", "reap_agentic", ""), ("c", "someone_else", "o")],
)
async def test_a_claim_refuses_blank_ids_and_unknown_claimants(click_id, claimed_by, order):
    with pytest.raises(ValueError):
        await ccc.claim_click(click_id, claimed_by=claimed_by, external_order_id=order)
    assert await claims_count() == 0


async def test_the_database_refuses_an_unknown_claimant_from_any_writer():
    with pytest.raises(Exception) as caught:
        await database.execute(
            "INSERT INTO conversion_click_claims (click_id, claimed_by) VALUES ('c', 'nobody')"
        )
    assert "ck_conversion_click_claims_claimed_by" in str(caught.value)


def test_the_228_self_heal_twins_declare_the_migrations_table():
    migration = (MIGRATIONS_DIR / "228_conversion_click_claims.sql").read_text()
    guard = (ROOT / "db/schema_guard.py").read_text()

    def _create(text: str, start_at: int = 0) -> str:
        start = text.index("CREATE TABLE IF NOT EXISTS conversion_click_claims", start_at)
        return _norm(text[start: text.index(";", start) + 1])

    from_migration = _create(migration)
    assert _create(guard) == from_migration
    assert _create(guard, guard.rindex("if IS_SQLITE:")) == from_migration.replace(
        "TIMESTAMPTZ NOT NULL DEFAULT now()", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
    )
    index = "CREATE INDEX IF NOT EXISTS idx_reap_agentic_purchases_click_id"
    assert index in migration
    assert _norm(guard).count(_norm(index + " ON reap_agentic_purchases (click_id);")) == 1
    assert (index + " \"\n                        \"ON reap_agentic_purchases (click_id);") in guard


def test_the_228_down_migration_drops_both():
    down = (MIGRATIONS_DIR / "down/228_conversion_click_claims_down.sql").read_text()
    assert "DROP TABLE IF EXISTS conversion_click_claims" in down
    assert "DROP INDEX IF EXISTS idx_reap_agentic_purchases_click_id" in down


async def test_the_228_heal_lands_after_a_failing_sibling():
    """Its own try: the claims table must exist even when an earlier self-heal statement raised
    (two active enrollments for one buyer make the mig-224 unique index fail)."""
    await database.execute("DROP TABLE conversion_click_claims")
    await database.execute("DROP INDEX IF EXISTS uq_reap_agentic_enrollments_one_active")
    for row_id in ("re_d1", "re_d2"):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
            "VALUES (:i, 'b', 'active')",
            {"i": row_id},
        )
    await ensure_required_schema_light()
    assert await claims_count() == 0  # the table exists


# ── the reviewer's repro, through the REAL close primitive ──────────────────────────────────


class _EdgeDB:
    """Just enough database for the real `close_external_order_conversion` (as in
    tests/test_t2_2_external_conversion_closure.FakeDB): the click lookup, and the edge INSERT
    with its ON CONFLICT (merchant_id, external_order_id) DO NOTHING."""

    def __init__(self, click_row=None):
        self.click_row = click_row
        self.edges = {}

    async def fetch_one(self, query, values=None):
        if isinstance(query, str) and "INSERT INTO commerce_attribution_edges" in query:
            params = dict(values or {})
            key = (params["merchant_id"], params["external_order_id"])
            if key in self.edges:
                return None
            self.edges[key] = params
            return {"edge_id": params["edge_id"]}
        return self.click_row

    async def fetch_all(self, query, values=None):
        return []

    async def execute(self, query, values=None):
        return 0


@pytest.fixture
def real_close(monkeypatch):
    edge_db = _EdgeDB()

    async def _noop(*a, **k):
        return {"interaction_id": "int_stub"}

    monkeypatch.setattr(_cas, "close_external_order_conversion", REAL_CLOSE)
    monkeypatch.setattr(_cas, "database", edge_db)
    monkeypatch.setattr(_cas, "record_commerce_event_best_effort", _noop)
    return edge_db


@pytest.mark.parametrize("merchant_first", [True, False], ids=["merchant-first", "reap-first"])
async def test_one_sale_is_one_edge_through_the_real_close(reap, real_close, merchant_first):
    """agent_review2214/test_double_edge_repro.py, FIXED: the same two closes of the same sale
    through the real primitive, each reached through its real call site (the merchant helper,
    the Reap completion). Before mig 228 this was two edges."""

    async def _merchant():
        await ccc.close_merchant_conversion_with_claim(
            REAL_CLOSE, merchant_id=MERCHANT_TENANT, click_id=CLICK,
            external_order_id=SHOPIFY_ORDER_ID, gross_amount_cents=3320, currency="USD",
            converting_shop_domain=SHOP,
        )

    purchase_id = await to_quoting(reap)
    assert (await step(purchase_id)).state == "awaiting_approval"
    if merchant_first:
        await _merchant()
    assert (await step(purchase_id)).state == "completed"
    if not merchant_first:
        await _merchant()
    expected = (MERCHANT_TENANT, SHOPIFY_ORDER_ID) if merchant_first else (SHOP, REAP_ORDER_ID)
    assert list(real_close.edges) == [expected]


# ══ 7. REVIEW P2s ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "options",
    [
        [{}],
        [{"id": "x"}],
        [{"id": "x", "price": {"amount": 5.00, "currency": "EUR"}}],
        [{"id": "x", "price": {"amount": -1.00, "currency": "USD"}}],
        [{"id": "x", "price": {"amount": 5.001, "currency": "USD"}}],
        [{"id": "", "price": {"amount": 5.00, "currency": "USD"}}],
        [{"id": 7, "price": {"amount": 5.00, "currency": "USD"}}],
        [{"id": "x", "price": {"currency": "USD"}}],
        [{"id": "x", "price": {"amount": 5.00, "currency": "USD"}}, {}],
    ],
    ids=["empty-object", "id-only", "wrong-currency", "negative", "sub-cent", "blank-id",
         "int-id", "no-amount", "one-good-one-empty"],
)
def test_a_shipping_option_must_carry_an_id_and_a_price_in_the_row_currency(options):
    check = svc.verify_cart_link_quote(cart_quote(shippingOptions=options), _ROW)
    assert (check.ok, check.refusal_reason) == (False, "no_shipping_option")


def test_free_shipping_is_a_priced_option():
    options = [{"id": "free", "name": "Free", "selected": True,
                "price": {"amount": 0.0, "currency": "USD"}}]
    quote = cart_quote(shippingOptions=options)
    quote["amountBreakdown"]["shipping"] = {"amount": 0.0, "currency": "USD"}
    quote["amountBreakdown"]["finalAmount"] = {"amount": 28.20, "currency": "USD"}
    assert svc.verify_cart_link_quote(quote, _ROW).ok


async def test_a_claim_lost_before_the_cart_quote_makes_no_quote(reap, attribution, monkeypatch):
    """R1. The re-read immediately before `request_cart_link_quote` — the call with a side effect
    at the partner. The lease moves AFTER the step's first guard and BEFORE the quote."""
    purchase_id = await to_quoting(reap)
    real = ledger.get_active_enrollment

    async def _steal_then_read(buyer_ref):
        await database.execute(
            "UPDATE reap_agentic_purchases SET claimed_by = 'w_other' WHERE id = :i",
            {"i": purchase_id},
        )
        return await real(buyer_ref)

    monkeypatch.setattr(ledger, "get_active_enrollment", _steal_then_read)
    moved = await step(purchase_id)
    assert moved.outcome == "lost_claim"
    assert reap.named("request_cart_link_quote") == []


@pytest.mark.parametrize(
    "column,value",
    [("merchant_domain", "other.example"), ("market_country", "SG"), ("quantity", 2)],
    ids=["shop", "market", "quantity"],
)
async def test_the_completion_recheck_is_the_full_validator(reap, attribution, column, value):
    """R4. Not the click id alone: another shop, another market or another quantity at the sink
    completes the purchase and writes NO edge."""
    purchase_id = await to_quoting(reap)
    assert (await step(purchase_id)).state == "awaiting_approval"
    await database.execute(
        f"UPDATE reap_agentic_purchases SET {column} = :v WHERE id = :i",
        {"v": value, "i": purchase_id},
    )
    assert (await step(purchase_id)).state == "completed"
    assert (await get(purchase_id))["last_error_code"] == "cart_link_attribution_unverified"
    assert attribution.calls == [] and await claims_count() == 0


def test_neither_dataclass_prints_the_url_or_the_buyer():
    text = repr(item()) + repr(svc.BuyerContact(email=EMAIL, shipping_address=dict(ADDRESS)))
    assert CLICK not in text and "cart/" not in text
    for pii in PII_STRINGS:
        assert pii.lower() not in text.lower()
    assert "judydoll.com" in text and "BuyerContact" in text  # control: the repr still exists


def _cart_url_on(host: str) -> str:
    return CART_URL.replace(f"//{SHOP}/", f"//{host}/")


@pytest.mark.parametrize("host", [SHOP, f"www.{SHOP}"], ids=["apex", "www"])
async def test_the_converting_shop_is_the_urls_own_host(reap, attribution, host):
    await active_enrollment()
    purchase_id = await start(cart_link=item(cart_url=_cart_url_on(host)))
    for _ in range(3):
        await step(purchase_id)
    (call,) = attribution.calls
    assert call["converting_shop_domain"] == host
    assert call["merchant_id"] == SHOP  # the subject is unchanged


@pytest.mark.parametrize("host", [SHOP, f"www.{SHOP}"], ids=["apex", "www"])
async def test_a_www_link_is_not_stamped_seller_mismatch_by_the_real_close(reap, real_close, host):
    """The click was minted for the URL's host (`dest_domain`). The real guard compares it with
    `converting_shop_domain` without folding `www.`, so the bare shop used to exclude the edge."""
    real_close.click_row = {"click_id": CLICK, "merchant_id": None, "dest_domain": host}
    await active_enrollment()
    purchase_id = await start(cart_link=item(cart_url=_cart_url_on(host)))
    for _ in range(3):
        await step(purchase_id)
    (edge,) = real_close.edges.values()
    metadata = json.loads(edge["metadata"])
    assert metadata["click_matched"] is True
    assert metadata.get("seller_mismatch") is not True
    assert metadata["converting_shop_domain"] == host
