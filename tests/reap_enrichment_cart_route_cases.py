"""The cart-link lane's ENRICHMENT-row branch (option 2, PR C), written ONCE, collected under BOTH
dialects.

NOT A TEST MODULE ITSELF (the name does not match `test_*.py`). Two thin modules collect it:

    tests/test_reap_enrichment_cart_route.py            SQLite -- skips itself under Postgres
    tests/test_reap_enrichment_cart_route_postgres.py   Postgres -- what the dialect gate runs

The same functions under both engines (the tests/reap_cart_link_cases.py pattern), so the two arms
cannot drift. Fixture names carry no leading underscore because the collectors `import *`.

WHAT IS REAL: the route (`POST /agent/v2/commerce/reap/purchases` through the router and the
app-wide error middleware -- `main` is NOT imported, see
tests/test_agent_commerce_reap_routes_postgres.py for why the Postgres gate must never do that),
the rail's tables (SQLite: the self-heal; Postgres: the migrations), the catalog tables from the
repo's own `metadata`, the proof table from its own `ensure_table()`, the verifier, the seller
derivation, the price check. WHAT IS FAKE: the two auth dependencies. No network: an
`httpx.AsyncClient` anywhere under a request raises.

THE ROWS ARE SHAPED LIKE PROD'S (read-only census, option2-design q1..q3, 2026-09-29): enrichment
products are `platform = 'external_seed'`, `source_system = 'catalog_enrichment_agent_v1'`,
`seller_ref` NULL, `merchant_id` the observed `merch_obs_` id; their offers sit under
`agent_seed::…` with `source_ref` = the listing's page; `canonical_url` and `source_domain` carry
no `www.`. The merchant ids below are the live ones and a test pins that they re-derive.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
import pytest
from fastapi import FastAPI

import db.enrichment_cart_variant_proofs as proofs
import routes.agent_commerce_reap as routes_reap
import services.reap_agentic_purchase as svc
from db.buyer_vault import hash_agent_user_ref  # noqa: F401  (kept importable for collectors)
from db.database import IS_POSTGRES, database, metadata
from middleware.error_handler import ErrorHandlerMiddleware
from routes.agent_auth import get_agent_context
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from services.reap_enrichment_cart_proof import derive_enrichment_seller

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "db/migrations"
#: The rail's migrations, IN ORDER (the Postgres arm builds the rail from these, exactly as
#: tests/test_agent_commerce_reap_routes_postgres.py does; SQLite uses the self-heal).
MIGRATIONS = (
    MIGRATIONS_DIR / "224_reap_agentic_ledger.sql",
    MIGRATIONS_DIR / "225_reap_agentic_purchase_hints.sql",
    MIGRATIONS_DIR / "226_reap_agentic_routes.sql",
    MIGRATIONS_DIR / "227_reap_agentic_buyer_consent.sql",
    MIGRATIONS_DIR / "228_tierb_cart_link_eligibility.sql",
    MIGRATIONS_DIR / "229_reap_agentic_purchase_item_source.sql",
    MIGRATIONS_DIR / "230_conversion_click_claims.sql",
    MIGRATIONS_DIR / "232_tierb_verdict_vocabulary.sql",
    MIGRATIONS_DIR / "233_reap_agentic_purchase_consent.sql",
    MIGRATIONS_DIR / "247_reap_agentic_purchase_offer_code.sql",
)
SEEDS_MIGRATION = MIGRATIONS_DIR / "044_external_product_seeds.sql"
SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
#: Where the SQLite arm parks a pre-existing external_product_seeds for the duration of a test.
PARKED_SEEDS = "external_product_seeds_parked_by_enrichment_route_cases"

RAIL_TABLES = (
    "reap_agentic_purchase_keys",
    "reap_agentic_buyer_refs",
    "reap_agentic_eligibility",
    "reap_agentic_purchases",
    "reap_agentic_enrollments",
    "tierb_cart_link_eligibility",
    "conversion_click_claims",
)
#: Shared with other suites that neither create nor drop them: emptied at setup AND teardown, in
#: FK-safe order, so the next file in the Postgres gate starts from what this one found.
SHARED_TABLES = (
    "catalog_offers", "catalog_skus", "catalog_products", "catalog_merchants",
    "buyer_identity_links", "surface_click_events",
)

BASE = "/agent/v2/commerce/reap"
AGENT = "agent_reap_enrich"
USER_REF = "enrich-user-ada"
EMAIL = "ada-enrich@example.test"
CONSENT = "reap-agentic-v1"
ENRICH = "catalog_enrichment_agent_v1"
FLAG = "REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED"

ADDRESS_US = {
    "firstName": "Ada", "lastName": "Lovelace", "phone": "+15550100",
    "addressLine1": "900 Brannan St", "city": "San Francisco", "country": "US",
    "postalCode": "94103",
}
ADDRESS_SG = {
    "firstName": "Ada", "lastName": "Lovelace", "phone": "+6565550100",
    "addressLine1": "1 Raffles Place", "city": "Singapore", "country": "SG",
    "postalCode": "048616",
}

# ── the live-shaped worlds ───────────────────────────────────────────────────────────────────

TARTE_HOST = "tartecosmetics.com"
TARTE_PK = "ext:tarte-flat-blush-brush::1a2b3c4d"
TARTE_HANDLE = "flat-blush-brush"
TARTE_URL = f"https://{TARTE_HOST}/products/{TARTE_HANDLE}"
TARTE_MERCHANT = "merch_obs_c75008da8d4366d6"   # live: (tarte, tartecosmetics.com)
TARTE_OFFER_MERCHANT = "agent_seed::tarte"
TARTE_VARIANT = "41234567890123"
TARTE_SKU = f"{TARTE_PK}::v:{TARTE_VARIANT}"
TARTE_PLACEHOLDER = f"{TARTE_PK}::canonical"

MAC_HOST = "maccosmetics.com"
MAC_MERCHANT = "merch_obs_28b3afd14edf211f"     # live: (MAC Cosmetics, maccosmetics.com)
MAC_OFFER_MERCHANT = "agent_seed::mac-cosmetics"
MAC_PARENT = "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control"
MAC_PK = f"ext:mac-cosmetics-{MAC_PARENT}::63815275"
MAC_URL = f"https://{MAC_HOST}/products/{MAC_PARENT}"
#: (numeric id, shade handle) -- the live folded shades: each shade is its own Shopify product.
MAC_SHADES = (
    ("54057377923267", f"{MAC_PARENT}-nc10"),
    ("54057377988803", f"{MAC_PARENT}-nc5"),
    ("54057378218179", f"{MAC_PARENT}-nw10"),
)
MAC_STUB_PARENT = "powder-kiss-liquid-lipcolour"
MAC_STUB_PK = f"ext:mac-cosmetics-{MAC_STUB_PARENT}::0a1b2c3d"
MAC_STUB_URL = f"https://{MAC_HOST}/products/{MAC_STUB_PARENT}"
MAC_STUB_VARIANT = "54000000000001"             # the parent handle's one "Default Title" stub

BM_HOST = "bluemercury.com"
BM_MERCHANT = "merch_obs_a2e07b1e8a08148b"      # live: make_observed_retailer_id(bluemercury.com)
BM_OFFER_MERCHANT = "agent_seed::retailer::bluemercury.com"
BM_PK = "ext:retailer:37065ae3db2eba22d6b959e19dbb1284"
BM_HANDLE = "nars-16-blush-brush"
BM_URL = f"https://{BM_HOST}/products/{BM_HANDLE}"
BM_SKU = f"{BM_PK}::v:retailer-04135f0544a19b871664b06d14c8bf6b"
BM_VARIANT = "32903948173387"
BM2_PK = "ext:retailer:41087cfb3a611475c4b50d4b097450f2"
BM2_HANDLE = "kiehls-since-1851-facial-fuel-energizing-face-wash"
BM2_URL = f"https://{BM_HOST}/products/{BM2_HANDLE}"
BM2_SKUS = (  # (sku_key, numeric id, title, price)
    (f"{BM2_PK}::v:retailer-559902939bce237c4a5b8826d342a649", "39763585663051", "16.9 oz", "45.00"),
    (f"{BM2_PK}::v:retailer-e440397ba11ff2d25ba45ea816bb0351", "39763585630283", "8 oz", "28.00"),
)

JSM_HOST = "jsmbeauty.sg"
JSM_MERCHANT = "merch_obs_88382424262f3e0f"     # live: (Jungsaemmool, jsmbeauty.sg)
JSM_OFFER_MERCHANT = "agent_seed::jungsaemmool"
JSM_PK = "ext:jungsaemmool-essential-mool-stick-glow::c9ca9cdb"
JSM_HANDLE = "essential-mool-stick-glow"
JSM_URL = f"https://{JSM_HOST}/products/{JSM_HANDLE}"
JSM_VARIANT = "43123456789012"
JSM_SKU = f"{JSM_PK}::v:{JSM_VARIANT}"


# ── app + client ─────────────────────────────────────────────────────────────────────────────

app = FastAPI()
app.add_middleware(ErrorHandlerMiddleware)
app.include_router(routes_reap.router)
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _Caller:
    agent_id = AGENT
    agent_user_ref: Optional[str] = USER_REF
    agent_name = "Reap Enrichment Route"
    allowed_merchants = None
    session_id = "session_reap_enrich"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


CALLER = _Caller()


@pytest.fixture
def client():
    async def _agent():
        return CALLER

    def _user():
        return AgentUserContext(agent_user_ref=CALLER.agent_user_ref)

    app.dependency_overrides[get_agent_context] = _agent
    app.dependency_overrides[get_agent_user_context] = _user

    class _Harness:
        async def post(self, url, **kwargs):
            transport = httpx.ASGITransport(app=app)
            async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
                return await http.post(url, **kwargs)

    try:
        yield _Harness()
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)


def error_of(resp) -> Optional[str]:
    payload = resp.json()
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return detail.get("error")
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("details"), dict):
        return error["details"].get("error")
    return None


def body(*, host: str, product_key: str, variant_key: Optional[str] = None,
         address: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the gateway (PIVOTA-Agent #2329) sends for an enrichment row: the catalog keys, the
    storefront target's host, NO variant_key unless a test says so."""
    return {
        "merchant_domain": host, "product_key": product_key, "variant_key": variant_key,
        "quantity": 1, "item_source": "cart_link",
        "buyer": {"email": EMAIL, "shipping_address": dict(address or ADDRESS_US),
                  "consent_version": CONSENT},
    }


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


def _assert_throwaway_database() -> None:
    url = (os.getenv("DATABASE_URL") or "").strip()
    dbname = url.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in url for m in SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _apply(paths: Iterable[Path]) -> None:
    from db.sql_migrations import split_statements

    for path in paths:
        for statement in split_statements(path.read_text(encoding="utf-8")):
            await database.execute(statement)


async def _clear(tables: Iterable[str]) -> None:
    for table in tables:
        try:
            await database.execute(f"DELETE FROM {table}")
        except Exception:  # noqa: BLE001 - a table this run never built is not a leak
            continue


async def _drop_proofs() -> None:
    await database.execute(f"DROP TABLE IF EXISTS {proofs.TABLE}")
    proofs._reset_for_tests()


async def proofs_table_exists() -> bool:
    if IS_POSTGRES:
        return bool(await database.fetch_val(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :t", {"t": proofs.TABLE}))
    return bool(await database.fetch_val(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = :t",
        {"t": proofs.TABLE}))


@pytest.fixture(autouse=True)
async def enrichment_db():
    if IS_POSTGRES:
        _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    import sqlalchemy
    from db.buyer_vault import buyer_identity_links
    from db.catalog import catalog_merchants, catalog_offers, catalog_products, catalog_skus
    from db.commerce_attribution import surface_click_events

    had_seeds = True
    if IS_POSTGRES:
        for table in RAIL_TABLES:
            await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await _apply(MIGRATIONS)
        await database.execute("DROP TABLE IF EXISTS external_product_seeds")
        await _apply([SEEDS_MIGRATION])
        url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    else:
        from db.schema_guard import ensure_required_schema_light

        for table in RAIL_TABLES:
            await database.execute(f"DROP TABLE IF EXISTS {table}")
        await ensure_required_schema_light()
        # THE SHARED SQLite FILE MAY ALREADY HOLD AN external_product_seeds, in whatever shape the
        # suite before this one built (the full sweep found one without `attached_variant_id`).
        # So it is PARKED under another name, this file's own table is built, and at teardown
        # this table is dropped and the parked one renamed back: the file is left as found.
        had_seeds = await database.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'external_product_seeds'"
        ) is not None
        await database.execute(f"DROP TABLE IF EXISTS {PARKED_SEEDS}")
        if had_seeds:
            await database.execute(f"ALTER TABLE external_product_seeds RENAME TO {PARKED_SEEDS}")
        await database.execute(
            "CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, status TEXT, "
            "domain TEXT, market TEXT, destination_url TEXT, canonical_url TEXT, "
            "attached_product_key TEXT, attached_variant_id TEXT, seed_data TEXT)"
        )
        url = (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://")
    engine = sqlalchemy.create_engine(url)
    metadata.create_all(engine, tables=[
        catalog_products, catalog_skus, catalog_offers, catalog_merchants,
        buyer_identity_links, surface_click_events,
    ], checkfirst=True)
    engine.dispose()
    await _clear(SHARED_TABLES)
    await database.execute("DELETE FROM external_product_seeds WHERE id = :id", {"id": LIVE_SEED_ID})
    # THE PROOF TABLE THROUGH ITS OWN SELF-HEAL, the path production takes.
    await _drop_proofs()
    await proofs.ensure_table()
    try:
        yield
    finally:
        await _clear(SHARED_TABLES)
        await _clear(tuple(reversed(RAIL_TABLES)))
        await _drop_proofs()
        if IS_POSTGRES:
            await database.execute("DROP TABLE IF EXISTS external_product_seeds")
        else:
            await database.execute("DROP TABLE IF EXISTS external_product_seeds")
            if had_seeds:
                await database.execute(
                    f"ALTER TABLE {PARKED_SEEDS} RENAME TO external_product_seeds")
        if IS_POSTGRES and not was_connected and database.is_connected:
            await database.disconnect()


@pytest.fixture(autouse=True)
def enrichment_env(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    for name in ("REAP_RETURN_URL_HOSTS", "REAP_AGENTIC_RETURN_URL", "BUYER_IDENTITY_LINK_SECRET",
                 "MERCHANT_PURCHASABILITY_ENFORCE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def enrichment_no_network(monkeypatch):
    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a route reached the network; this rail's routes make no call")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden, raising=True)


# ── seeds ────────────────────────────────────────────────────────────────────────────────────


def _jsonb(param: str) -> str:
    return f"CAST(:{param} AS JSONB)" if IS_POSTGRES else f":{param}"


def _ts(value: datetime) -> Any:
    """A TIMESTAMPTZ bind: asyncpg takes the datetime, SQLite stores the ISO text."""
    return value if IS_POSTGRES else value.isoformat()


async def seed_product(*, pk: str, merchant: str, brand: str, domain: str, url: str, title: str,
                       seller_ref: Optional[str] = None, source_system: str = ENRICH) -> None:
    await database.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, "
        "title, brand, category, product_type, source_domain, source_system, canonical_url, "
        "seller_ref) VALUES (:pk, :m, 'external_seed', :spid, :title, :brand, 'makeup', "
        "'Makeup', :domain, :ss, :url, :seller)",
        {"pk": pk, "m": merchant, "spid": pk[:128], "title": title, "brand": brand,
         "domain": domain, "ss": source_system, "url": url, "seller": seller_ref},
    )


async def seed_sku(*, pk: str, sku_key: str, merchant: str, svid: str, title: str,
                   source_handle: Optional[str] = None, payload: Optional[Dict[str, Any]] = None,
                   suppressed: Optional[str] = None) -> None:
    """`suppressed`: None (live), "at" (suppressed_at only), "reason" (suppression_reason only) or
    "both" -- each column alone suppresses a row, so each is exercised alone."""
    sku_payload = {"agent_version": ENRICH, "variant_id": svid, "source_handle": source_handle}
    sku_payload.update(payload or {})
    await database.execute(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, "
        "source_variant_id, title, currency, sku_payload) VALUES (:sk, :pk, :m, 'external_seed', "
        f":spid, :svid, :title, 'USD', {_jsonb('payload')})",
        {"sk": sku_key, "pk": pk, "m": merchant, "spid": pk[:128], "svid": svid[:128],
         "title": title, "payload": json.dumps(sku_payload)},
    )
    if suppressed in ("at", "both"):
        await database.execute(
            "UPDATE catalog_skus SET suppressed_at = CURRENT_TIMESTAMP WHERE sku_key = :sk",
            {"sk": sku_key})
    if suppressed in ("reason", "both"):
        await database.execute(
            "UPDATE catalog_skus SET suppression_reason = 'test_suppressed' WHERE sku_key = :sk",
            {"sk": sku_key})


async def seed_offer(*, oid: str, pk: str, sku_key: str, merchant: str, price: Optional[str],
                     source_ref: str, currency: str = "USD", source_system: str = ENRICH,
                     availability: str = "in_stock", suppressed_at: bool = False,
                     suppression_reason: bool = False) -> None:
    await database.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency, "
        "merchant_effective_price, estimated_best_price, list_price, availability, source_system, "
        "source_ref) VALUES (:oid, :sk, :pk, :m, :cur, :price, :price, :price, :av, :ss, :ref)",
        {"oid": oid, "sk": sku_key, "pk": pk, "m": merchant, "cur": currency, "price": price,
         "av": availability, "ss": source_system, "ref": source_ref},
    )
    if suppressed_at:
        await database.execute(
            "UPDATE catalog_offers SET suppressed_at = CURRENT_TIMESTAMP WHERE offer_id = :oid",
            {"oid": oid})
    if suppression_reason:
        await database.execute(
            "UPDATE catalog_offers SET suppression_reason = 'test' WHERE offer_id = :oid",
            {"oid": oid})


async def seed_proof(*, pk: str, sku_key: str, shop_host: str, handle: str, variant_id: str,
                     price_minor: int, live_variant_count: int = 1, currency: str = "USD",
                     available: bool = True, age: timedelta = timedelta(hours=1),
                     outcome: str = "ok", source: str = "products_json_v1") -> None:
    now = datetime.now(timezone.utc)
    await database.execute(
        f"INSERT INTO {proofs.TABLE} (product_key, sku_key, shop_host, handle, shopify_product_id, "
        "variant_id, live_variant_count, available, live_price_minor, currency, source, "
        "checked_at, outcome, updated_at) VALUES (:pk, :sk, :host, :handle, '7000000000001', :vid, "
        ":lvc, :avail, :price, :cur, :src, :checked, :outcome, :checked)",
        {"pk": pk, "sk": sku_key, "host": shop_host, "handle": handle, "vid": variant_id,
         "lvc": live_variant_count, "avail": available, "price": price_minor, "cur": currency,
         "src": source, "checked": _ts(now - age), "outcome": outcome},
    )


async def seed_tierb(domain: str, market: str = "US") -> None:
    await database.execute(
        "INSERT INTO tierb_cart_link_eligibility (shop_domain, market, verdict, checked_at, "
        "consecutive_same) VALUES (:d, :m, 'ELIGIBLE', CURRENT_TIMESTAMP, 1)",
        {"d": domain, "m": market},
    )


async def seed_tarte(*, host: str = TARTE_HOST, domain: str = TARTE_HOST, url: str = TARTE_URL,
                     proof_host: str = TARTE_HOST, offer_ref: str = TARTE_URL,
                     proof_price: int = 3000, offer_price: str = "30.00",
                     merchant: str = TARTE_MERCHANT, seller_ref: Optional[str] = None,
                     pk: str = TARTE_PK, proof: bool = True, tierb: bool = True) -> Dict[str, str]:
    """tarte Flat Blush Brush: ONE real variant beside the placeholder (prod: 204 of 417 tarte rows
    are 1-variant). Offers on both skus under `agent_seed::tarte`; proof on the real sku."""
    sku = f"{pk}::v:{TARTE_VARIANT}"
    placeholder = f"{pk}::canonical"
    await seed_product(pk=pk, merchant=merchant, brand="tarte", domain=domain, url=url,
                       title="Flat Blush Brush", seller_ref=seller_ref)
    await seed_sku(pk=pk, sku_key=placeholder, merchant=merchant, svid=pk, title="Flat Blush Brush")
    await seed_sku(pk=pk, sku_key=sku, merchant=merchant, svid=TARTE_VARIANT, title="Default Title")
    for oid, sku_key in (("off_tarte_c", placeholder), ("off_tarte_v", sku)):
        await seed_offer(oid=oid, pk=pk, sku_key=sku_key, merchant=TARTE_OFFER_MERCHANT,
                         price=offer_price, source_ref=offer_ref)
    if proof:
        await seed_proof(pk=pk, sku_key=sku, shop_host=proof_host, handle=TARTE_HANDLE,
                         variant_id=TARTE_VARIANT, price_minor=proof_price)
    if tierb:
        await seed_tierb(host.removeprefix("www."))
    return {"sku": sku, "placeholder": placeholder}


#: Per-shade prices (major, minor) -- DIFFERENT on purpose, so a shade is priced from its own
#: offer and its own proof and never a neighbour's.
MAC_SHADE_PRICES = (("39.00", 3900), ("41.00", 4100), ("43.00", 4300))


async def seed_mac_folded(*, live_variant_count: int = 1, suppressed_shades: int = 0) -> None:
    """MAC Studio Fix Fluid, FOLDED: each shade is its own Shopify product (sku_payload.source_handle),
    the canonical_url is the parent handle, every offer's source_ref is the parent page. Each shade
    has its own price and its own proof. The LAST `suppressed_shades` shades are suppressed."""
    await seed_product(pk=MAC_PK, merchant=MAC_MERCHANT, brand="MAC Cosmetics", domain=MAC_HOST,
                       url=MAC_URL, title="Studio Fix Fluid SPF 15 24HR Matte Foundation")
    await seed_sku(pk=MAC_PK, sku_key=f"{MAC_PK}::canonical", merchant=MAC_MERCHANT, svid=MAC_PK,
                   title="Studio Fix Fluid SPF 15 24HR Matte Foundation")
    for n, (vid, handle) in enumerate(MAC_SHADES):
        sku_key = f"{MAC_PK}::v:{vid}"
        price, minor = MAC_SHADE_PRICES[n]
        await seed_sku(pk=MAC_PK, sku_key=sku_key, merchant=MAC_MERCHANT, svid=vid,
                       title=handle.rsplit("-", 1)[-1].upper(), source_handle=handle,
                       suppressed="both" if n >= len(MAC_SHADES) - suppressed_shades else None)
        await seed_offer(oid=f"off_mac_{n}", pk=MAC_PK, sku_key=sku_key, merchant=MAC_OFFER_MERCHANT,
                         price=price, source_ref=MAC_URL)
        await seed_proof(pk=MAC_PK, sku_key=sku_key, shop_host=MAC_HOST, handle=handle,
                         variant_id=vid, price_minor=minor, live_variant_count=live_variant_count)
    await seed_offer(oid="off_mac_c", pk=MAC_PK, sku_key=f"{MAC_PK}::canonical",
                     merchant=MAC_OFFER_MERCHANT, price="39.00", source_ref=MAC_URL)
    await seed_tierb(MAC_HOST)


async def seed_mac_stub(*, suppressed_variant_skus: int, mode: str = "both") -> None:
    """A MAC parent whose shades were folded away: the placeholder is its only LIVE sku; the parent
    handle's one variant is a "Default Title" stub. `suppressed_variant_skus` `::v:` skus remain in
    the catalog, suppressed."""
    await seed_product(pk=MAC_STUB_PK, merchant=MAC_MERCHANT, brand="MAC Cosmetics",
                       domain=MAC_HOST, url=MAC_STUB_URL, title="Powder Kiss Liquid Lipcolour")
    placeholder = f"{MAC_STUB_PK}::canonical"
    await seed_sku(pk=MAC_STUB_PK, sku_key=placeholder, merchant=MAC_MERCHANT, svid=MAC_STUB_PK,
                   title="Powder Kiss Liquid Lipcolour")
    for n in range(suppressed_variant_skus):
        vid = f"5405737{n:07d}"
        await seed_sku(pk=MAC_STUB_PK, sku_key=f"{MAC_STUB_PK}::v:{vid}", merchant=MAC_MERCHANT,
                       svid=vid, title=f"SHADE {n}", source_handle=f"{MAC_STUB_PARENT}-{n}",
                       suppressed=mode)
    await seed_offer(oid="off_mac_stub", pk=MAC_STUB_PK, sku_key=placeholder,
                     merchant=MAC_OFFER_MERCHANT, price="26.00", source_ref=MAC_STUB_URL)
    await seed_proof(pk=MAC_STUB_PK, sku_key=placeholder, shop_host=MAC_HOST,
                     handle=MAC_STUB_PARENT, variant_id=MAC_STUB_VARIANT, price_minor=2600)
    await seed_tierb(MAC_HOST)


async def seed_bluemercury() -> None:
    """bluemercury #16 Blush Brush, the retailer lane: `ext:retailer:` key, the `::v:retailer-<hash>`
    sku whose Shopify id lives only in source_variant_id, offers under
    `agent_seed::retailer::bluemercury.com`."""
    await seed_product(pk=BM_PK, merchant=BM_MERCHANT, brand="NARS", domain=BM_HOST, url=BM_URL,
                       title="#16 Blush Brush")
    await seed_sku(pk=BM_PK, sku_key=f"{BM_PK}::canonical", merchant=BM_MERCHANT, svid=BM_PK,
                   title="#16 Blush Brush")
    await seed_sku(pk=BM_PK, sku_key=BM_SKU, merchant=BM_MERCHANT, svid=BM_VARIANT,
                   title="Default Title")
    for oid, sku_key in (("off_bm_c", f"{BM_PK}::canonical"), ("off_bm_v", BM_SKU)):
        await seed_offer(oid=oid, pk=BM_PK, sku_key=sku_key, merchant=BM_OFFER_MERCHANT,
                         price="36.00", source_ref=BM_URL)
    await seed_proof(pk=BM_PK, sku_key=BM_SKU, shop_host=BM_HOST, handle=BM_HANDLE,
                     variant_id=BM_VARIANT, price_minor=3600)
    await seed_tierb(BM_HOST)


async def seed_bluemercury_two_sizes(*, suppress_first: bool = False,
                                     catalog_has_first: bool = True) -> None:
    """bluemercury Facial Fuel: TWO real skus (16.9 oz, 8 oz), each priced, a named proof each
    (the storefront handle has both: live_variant_count 2). `suppress_first` suppresses the 16.9 oz
    sku; `catalog_has_first=False` leaves it out of the catalog entirely (the catalog knows one of
    the storefront's two sizes)."""
    await seed_product(pk=BM2_PK, merchant=BM_MERCHANT, brand="Kiehl's Since 1851", domain=BM_HOST,
                       url=BM2_URL, title="Facial Fuel Energizing Face Wash")
    await seed_sku(pk=BM2_PK, sku_key=f"{BM2_PK}::canonical", merchant=BM_MERCHANT, svid=BM2_PK,
                   title="Facial Fuel Energizing Face Wash")
    await seed_offer(oid="off_bm2_c", pk=BM2_PK, sku_key=f"{BM2_PK}::canonical",
                     merchant=BM_OFFER_MERCHANT, price="28.00", source_ref=BM2_URL)
    for n, (sku_key, vid, title, price) in enumerate(BM2_SKUS):
        if n == 0 and not catalog_has_first:
            continue
        await seed_sku(pk=BM2_PK, sku_key=sku_key, merchant=BM_MERCHANT, svid=vid, title=title,
                       suppressed="both" if n == 0 and suppress_first else None)
        await seed_offer(oid=f"off_bm2_{n}", pk=BM2_PK, sku_key=sku_key, merchant=BM_OFFER_MERCHANT,
                         price=price, source_ref=BM2_URL)
        await seed_proof(pk=BM2_PK, sku_key=sku_key, shop_host=BM_HOST, handle=BM2_HANDLE,
                         variant_id=vid, price_minor=int(price.replace(".", "")),
                         live_variant_count=2)
    await seed_tierb(BM_HOST)


async def seed_jsm(*, market: str) -> None:
    """jsmbeauty.sg: an SGD storefront; offers and proof in SGD."""
    await seed_product(pk=JSM_PK, merchant=JSM_MERCHANT, brand="Jungsaemmool", domain=JSM_HOST,
                       url=JSM_URL, title="Essential Mool Stick Glow")
    await seed_sku(pk=JSM_PK, sku_key=f"{JSM_PK}::canonical", merchant=JSM_MERCHANT, svid=JSM_PK,
                   title="Essential Mool Stick Glow")
    await seed_sku(pk=JSM_PK, sku_key=JSM_SKU, merchant=JSM_MERCHANT, svid=JSM_VARIANT,
                   title="Default Title")
    await seed_offer(oid="off_jsm_v", pk=JSM_PK, sku_key=JSM_SKU, merchant=JSM_OFFER_MERCHANT,
                     price="32.00", currency="SGD", source_ref=JSM_URL)
    await seed_proof(pk=JSM_PK, sku_key=JSM_SKU, shop_host=JSM_HOST, handle=JSM_HANDLE,
                     variant_id=JSM_VARIANT, price_minor=3200, currency="SGD")
    await seed_tierb(JSM_HOST, market)


async def purchase_count() -> int:
    return int(await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") or 0)


async def click_count() -> int:
    return int(await database.fetch_val("SELECT COUNT(*) FROM surface_click_events") or 0)


async def purchase_of(resp) -> Dict[str, Any]:
    row = await database.fetch_one(
        "SELECT cart_url, our_price_minor, currency, market_country, merchant_domain, product_key, "
        "product_name, variant_title, click_id, item_source FROM reap_agentic_purchases "
        "WHERE id = :id", {"id": resp.json()["purchase_id"]})
    assert row is not None
    return dict(row)


async def click_of(click_id: str) -> Dict[str, Any]:
    row = await database.fetch_one(
        "SELECT merchant_id, dest_domain, context FROM surface_click_events WHERE click_id = :c",
        {"c": click_id})
    assert row is not None
    out = dict(row)
    if isinstance(out["context"], str):
        out["context"] = json.loads(out["context"])
    return out


async def assert_refused(resp, reason: str) -> None:
    assert resp.status_code == 409, resp.text
    assert error_of(resp) == reason, resp.text
    # BEFORE ANY PURCHASE OPENS: no purchase row, and no click row either.
    assert await purchase_count() == 0
    assert await click_count() == 0


def cart_url_for(host: str, variant: str, click: str, country: str = "US") -> str:
    return f"https://{host}/cart/{variant}:1?attributes[pivota_click_id]={click}&country={country}"


# ── the live merchant ids re-derive (so the fixtures above are not tautological) ─────────────


@pytest.mark.parametrize("pk,brand,url,merchant", [
    (TARTE_PK, "tarte", TARTE_URL, TARTE_MERCHANT),
    (MAC_PK, "MAC Cosmetics", MAC_URL, MAC_MERCHANT),
    (BM_PK, "NARS", BM_URL, BM_MERCHANT),
    (JSM_PK, "Jungsaemmool", JSM_URL, JSM_MERCHANT),
])
def test_the_live_merchant_ids_are_what_the_seller_derivation_gives(pk, brand, url, merchant):
    assert derive_enrichment_seller({"product_key": pk, "source_system": ENRICH, "brand": brand,
                                     "canonical_url": url}) == merchant


# ── ACCEPT ───────────────────────────────────────────────────────────────────────────────────


async def test_tarte_sole_variant_is_bought_at_the_proven_price(client):
    await seed_tarte()
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["item_source"] == "cart_link"
    assert purchase["cart_url"] == cart_url_for(TARTE_HOST, TARTE_VARIANT, purchase["click_id"])
    assert purchase["our_price_minor"] == 3000 and purchase["currency"] == "USD"
    assert purchase["market_country"] == "US"
    assert purchase["product_key"] == TARTE_PK and purchase["product_name"] == "Flat Blush Brush"
    assert purchase["variant_title"] is None and resp.json()["variant_title"] is None
    # THE SELLER OF RECORD on the click: product.merchant_id, never the `agent_seed::` offer owner.
    click = await click_of(purchase["click_id"])
    assert click["merchant_id"] == TARTE_MERCHANT and click["dest_domain"] == TARTE_HOST
    assert click["context"]["seller_ref"] == TARTE_MERCHANT
    assert click["context"]["product_key"] == TARTE_PK


async def test_a_mac_folded_shade_named_by_the_caller_is_bought_on_its_own_handle(client):
    """The shade's proof is for ITS handle (sku_payload.source_handle), and the price is its own
    sku's offer, whose source_ref is the parent listing page."""
    for lvc in (1, 2):  # sole variant on the shade's handle, and one named among two sizes
        await _reset_catalog()
        await seed_mac_folded(live_variant_count=lvc)
        vid, _handle = MAC_SHADES[0]
        resp = await client.post(f"{BASE}/purchases", json=body(
            host=MAC_HOST, product_key=MAC_PK, variant_key=f"{MAC_PK}::v:{vid}"))
        assert resp.status_code == 202, resp.text
        purchase = await purchase_of(resp)
        assert purchase["cart_url"] == cart_url_for(MAC_HOST, vid, purchase["click_id"])
        assert purchase["our_price_minor"] == 3900
        assert (await click_of(purchase["click_id"]))["merchant_id"] == MAC_MERCHANT


async def test_bluemercury_retailer_row_is_bought_on_its_source_variant_id(client):
    await seed_bluemercury()
    resp = await client.post(f"{BASE}/purchases", json=body(host=BM_HOST, product_key=BM_PK))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["cart_url"] == cart_url_for(BM_HOST, BM_VARIANT, purchase["click_id"])
    assert purchase["our_price_minor"] == 3600
    assert (await click_of(purchase["click_id"]))["merchant_id"] == BM_MERCHANT


async def test_a_two_size_product_is_bought_on_the_size_the_caller_names(client):
    await seed_bluemercury_two_sizes()
    sku_key, vid, _title, _price = BM2_SKUS[0]
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=BM_HOST, product_key=BM2_PK, variant_key=sku_key))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["cart_url"] == cart_url_for(BM_HOST, vid, purchase["click_id"])
    assert purchase["our_price_minor"] == 4500


async def test_a_placeholder_only_product_is_bought_through_its_sole_variant_proof(client):
    """The control for the MAC-stub refusal below: NO `::v:` sku anywhere, a proof of one live
    variant -- the placeholder buys it."""
    await seed_mac_stub(suppressed_variant_skus=0)
    resp = await client.post(f"{BASE}/purchases", json=body(host=MAC_HOST, product_key=MAC_STUB_PK))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["cart_url"] == cart_url_for(MAC_HOST, MAC_STUB_VARIANT, purchase["click_id"])
    assert purchase["our_price_minor"] == 2600


@pytest.mark.parametrize("posted,domain,url,proof_host,offer_ref", [
    # the gateway posts the storefront target's host, which may carry the `www.` the rows lack
    (f"www.{TARTE_HOST}", TARTE_HOST, TARTE_URL, TARTE_HOST, TARTE_URL),
    # the rows carry it and the post does not
    (TARTE_HOST, f"www.{TARTE_HOST}", TARTE_URL, TARTE_HOST, TARTE_URL),
    (TARTE_HOST, TARTE_HOST, f"https://www.{TARTE_HOST}/products/{TARTE_HANDLE}", TARTE_HOST,
     TARTE_URL),
    (TARTE_HOST, TARTE_HOST, TARTE_URL, f"www.{TARTE_HOST}", TARTE_URL),
    (TARTE_HOST, TARTE_HOST, TARTE_URL, TARTE_HOST, f"https://www.{TARTE_HOST}/products/{TARTE_HANDLE}"),
    # and a trailing-case difference, which the one owner of the rule also folds
    (TARTE_HOST, "TarteCosmetics.com", TARTE_URL, TARTE_HOST, TARTE_URL),
], ids=["post-www", "domain-www", "canonical-www", "proof-www", "offer-www", "domain-case"])
async def test_hosts_that_differ_only_by_www_are_one_storefront(client, posted, domain, url,
                                                                 proof_host, offer_ref):
    await seed_tarte(host=posted, domain=domain, url=url, proof_host=proof_host, offer_ref=offer_ref)
    resp = await client.post(f"{BASE}/purchases", json=body(host=posted, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    # THE CART IS BUILT ON THE HOST AS POSTED (the lane's rule for every row).
    assert purchase["cart_url"] == cart_url_for(posted, TARTE_VARIANT, purchase["click_id"])


async def test_an_sg_buyer_on_an_sgd_storefront_is_priced_in_sgd(client):
    await seed_jsm(market="SG")
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=JSM_HOST, product_key=JSM_PK, address=ADDRESS_SG))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["currency"] == "SGD" and purchase["our_price_minor"] == 3200
    assert purchase["cart_url"] == cart_url_for(JSM_HOST, JSM_VARIANT, purchase["click_id"], "SG")


async def test_a_product_key_longer_than_128_characters_keeps_its_placeholder_apart(client):
    """`source_variant_id` is varchar(128): a long product key's placeholder stores a TRUNCATED id,
    so `_is_placeholder_sku` (id == product_key) reads it as a real sku -- and a placeholder beside
    one real sku would become "two real skus". The key test keeps them apart."""
    long_pk = "ext:tarte-" + ("x" * 140) + "::1a2b3c4d"
    assert len(long_pk) > 128
    await seed_tarte(pk=long_pk)
    placeholder = {"sku_key": f"{long_pk}::canonical", "source_variant_id": long_pk[:128]}
    assert not routes_reap._is_placeholder_sku(placeholder, long_pk)  # the misreading, pinned
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=long_pk))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["cart_url"] == cart_url_for(TARTE_HOST, TARTE_VARIANT, purchase["click_id"])


async def test_a_product_seller_ref_equal_to_its_merchant_is_accepted(client):
    await seed_tarte(seller_ref=TARTE_MERCHANT)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text


async def test_a_variant_title_the_sku_payload_carries_reaches_the_purchase(client):
    """DISPLAY ONLY. No live source carries one today (None above); when the payload does, the
    purchase and the 202 say it, as the mirror lane does with its proof's title -- cleaned by the
    same rule (#2462): the bidi override and zero-width space dropped, the newline folded."""
    await seed_bluemercury_two_sizes()
    sku_key = BM2_SKUS[0][0]
    await database.execute(
        f"UPDATE catalog_skus SET sku_payload = {_jsonb('p')} WHERE sku_key = :sk",
        {"sk": sku_key, "p": json.dumps({"variant_title": " 16.9\u202e\n oz\u200b ",
                                         "source_handle": None})})
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=BM_HOST, product_key=BM2_PK, variant_key=sku_key))
    assert resp.status_code == 202, resp.text
    assert resp.json()["variant_title"] == "16.9 oz"
    assert (await purchase_of(resp))["variant_title"] == "16.9 oz"


async def test_a_dirty_catalog_title_is_stored_clean_on_the_enrichment_path(client):
    """The product title is merchant-typed: `clean_product_name` (#2467) -- the bidi override and
    zero-width space dropped, the newline and tab folded -- on the purchase row."""
    await seed_tarte()
    await database.execute(
        "UPDATE catalog_products SET title = :t WHERE product_key = :pk",
        {"pk": TARTE_PK, "t": "  Flat\u202e Blush\n\tBrush\u200b  "})
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text
    assert (await purchase_of(resp))["product_name"] == "Flat Blush Brush"


async def test_the_route_creates_the_proof_table_and_refuses_without_a_proof(client):
    """No table yet (PR B's writer has not run anywhere): the reader's self-heal creates it, and an
    empty table is a missing proof."""
    await seed_tarte(proof=False)
    await _drop_proofs()
    assert not await proofs_table_exists()
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_variant_unverified")
    assert await proofs_table_exists()


# ── REFUSE ───────────────────────────────────────────────────────────────────────────────────


async def test_a_catalog_price_that_drifted_from_the_proof_is_stale(client):
    await seed_tarte(offer_price="32.00")  # proof read 30.00 live
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_price_stale")


async def test_two_real_skus_and_no_variant_key_is_ambiguous(client):
    await seed_bluemercury_two_sizes()
    resp = await client.post(f"{BASE}/purchases", json=body(host=BM_HOST, product_key=BM2_PK))
    await assert_refused(resp, "row_variant_ambiguous")


async def test_mac_folded_shades_with_no_variant_key_are_ambiguous(client):
    await seed_mac_folded()
    resp = await client.post(f"{BASE}/purchases", json=body(host=MAC_HOST, product_key=MAC_PK))
    await assert_refused(resp, "row_variant_ambiguous")


@pytest.mark.parametrize("mode", ["both", "at", "reason"])
async def test_a_mac_placeholder_never_buys_the_parent_stub_when_shade_skus_were_suppressed(
    client, mode,
):
    """THE #2460 CARRY-OVER: the `::v:` sku suppressed, so a LIVE-only count is 0 and the
    placeholder would buy the parent handle's "Default Title" stub at the first shade's price. The
    count includes suppressed skus, and the verifier refuses. (ONE suppressed sku, so the no-key
    count rule -- two or more -- is not what refuses here; the verifier's placeholder gate is.)"""
    await seed_mac_stub(suppressed_variant_skus=1, mode=mode)
    resp = await client.post(f"{BASE}/purchases", json=body(host=MAC_HOST, product_key=MAC_STUB_PK))
    await assert_refused(resp, "row_variant_unverified")
    # ...and the caller naming the placeholder does not get round it.
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=MAC_HOST, product_key=MAC_STUB_PK, variant_key=f"{MAC_STUB_PK}::canonical"))
    await assert_refused(resp, "row_variant_unverified")


# ── NO variant_key ON A MULTI-VARIANT PRODUCT (review of #2465, P1) ──────────────────────────
#
# The gateway sends no variant_key, and each of these rows LOOKS single-variant to it. Before the
# fix every one opened a purchase for one variant nobody picked.


async def test_a1_mac_shades_two_of_three_suppressed_no_key_is_ambiguous(client, monkeypatch):
    """A1: 3 folded shades, 2 suppressed. The live-only choice saw ONE real sku (NC10), whose own
    proof is SOLE (the shade's handle has one variant) -- the verifier cannot catch it. The count
    of every `::v:` sku does, and it refuses before any proof is read."""
    await seed_mac_folded(suppressed_shades=2)
    seen = spy_statements(monkeypatch)
    resp = await client.post(f"{BASE}/purchases", json=body(host=MAC_HOST, product_key=MAC_PK))
    await assert_refused(resp, "row_variant_ambiguous")
    assert proofs._SELECT_PROOF_SQL not in seen


async def test_a2_two_sizes_one_suppressed_no_key_is_ambiguous(client):
    """A2: bluemercury Facial Fuel with the 16.9 oz sku suppressed: not "the 8 oz product"."""
    await seed_bluemercury_two_sizes(suppress_first=True)
    resp = await client.post(f"{BASE}/purchases", json=body(host=BM_HOST, product_key=BM2_PK))
    await assert_refused(resp, "row_variant_ambiguous")


async def test_a3_catalog_knows_one_of_two_storefront_sizes_no_key_is_ambiguous(client):
    """A3: the catalog has ONE `::v:` sku, but the storefront handle has two (proof
    live_variant_count 2, the verifier's named_variant mode). Nobody named it: refused."""
    await seed_bluemercury_two_sizes(catalog_has_first=False)
    resp = await client.post(f"{BASE}/purchases", json=body(host=BM_HOST, product_key=BM2_PK))
    await assert_refused(resp, "row_variant_ambiguous")


async def test_a3_the_same_sku_named_by_the_caller_is_bought(client):
    """The control for A3: the caller NAMES the one catalog size -> the named proof buys it."""
    await seed_bluemercury_two_sizes(catalog_has_first=False)
    sku_key, vid, _title, _price = BM2_SKUS[1]
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=BM_HOST, product_key=BM2_PK, variant_key=sku_key))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["cart_url"] == cart_url_for(BM_HOST, vid, purchase["click_id"])
    assert purchase["our_price_minor"] == 2800


async def seed_mac_single_folded_shade() -> str:
    """A7: a folded MAC family whose catalog holds ONE shade. canonical_url is the PARENT handle;
    the only `::v:` sku is NC10, `sku_payload.source_handle = <parent>-nc10`, with its own sole
    proof (the shade's handle has one variant) and its own offer. Returns that sku_key."""
    await seed_product(pk=MAC_PK, merchant=MAC_MERCHANT, brand="MAC Cosmetics", domain=MAC_HOST,
                       url=MAC_URL, title="Studio Fix Fluid SPF 15 24HR Matte Foundation")
    await seed_sku(pk=MAC_PK, sku_key=f"{MAC_PK}::canonical", merchant=MAC_MERCHANT, svid=MAC_PK,
                   title="Studio Fix Fluid SPF 15 24HR Matte Foundation")
    vid, handle = MAC_SHADES[0]
    sku_key = f"{MAC_PK}::v:{vid}"
    await seed_sku(pk=MAC_PK, sku_key=sku_key, merchant=MAC_MERCHANT, svid=vid, title="NC10",
                   source_handle=handle)
    await seed_offer(oid="off_mac_a7", pk=MAC_PK, sku_key=sku_key, merchant=MAC_OFFER_MERCHANT,
                     price="39.00", source_ref=MAC_URL)
    await seed_proof(pk=MAC_PK, sku_key=sku_key, shop_host=MAC_HOST, handle=handle,
                     variant_id=vid, price_minor=3900)
    await seed_tierb(MAC_HOST)
    return sku_key


async def test_a7_the_only_catalog_shade_of_a_folded_family_is_not_bought_unnamed(client, monkeypatch):
    """A7 (review of #2465, P2): catalog count 1, the shade's proof sole_variant -- every other
    rule passes, and NC10 is a shade nobody chose. Refused before the proof is read."""
    await seed_mac_single_folded_shade()
    seen = spy_statements(monkeypatch)
    resp = await client.post(f"{BASE}/purchases", json=body(host=MAC_HOST, product_key=MAC_PK))
    await assert_refused(resp, "row_variant_ambiguous")
    assert proofs._SELECT_PROOF_SQL not in seen


async def test_a7_the_same_folded_shade_named_by_the_caller_is_bought(client):
    sku_key = await seed_mac_single_folded_shade()
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=MAC_HOST, product_key=MAC_PK, variant_key=sku_key))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["cart_url"] == cart_url_for(MAC_HOST, MAC_SHADES[0][0], purchase["click_id"])


@pytest.mark.parametrize("source_handle", [TARTE_HANDLE, "", "   "])
async def test_a_source_handle_equal_to_the_canonical_handle_or_blank_is_not_a_folded_shade(
    client, source_handle,
):
    """The fold rule reads only a NON-EMPTY source_handle that DIFFERS from the canonical handle:
    the same handle, or a blank one, is the product's own page (tarte stays 202 with no key)."""
    await seed_tarte()
    await database.execute(
        f"UPDATE catalog_skus SET sku_payload = {_jsonb('p')} WHERE sku_key = :sk",
        {"sk": TARTE_SKU, "p": json.dumps({"source_handle": source_handle})})
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text


async def test_two_suppressed_variant_skus_behind_a_placeholder_are_ambiguous(client):
    """The MAC stub with TWO suppressed `::v:` skus: the catalog knows two variants -> the no-key
    rule refuses before the placeholder is even tried."""
    await seed_mac_stub(suppressed_variant_skus=2)
    resp = await client.post(f"{BASE}/purchases", json=body(host=MAC_HOST, product_key=MAC_STUB_PK))
    await assert_refused(resp, "row_variant_ambiguous")


@pytest.mark.parametrize("mode", ["at", "reason"])
async def test_a_suppressed_variant_sku_is_never_the_no_key_choice(client, mode):
    """ONE `::v:` sku, suppressed (by either column alone), with a valid proof and offer of its
    own: the no-key choice reads LIVE skus, so the placeholder is chosen and the verifier refuses
    it (the catalog knows a variant). The suppressed sku is never bought."""
    await seed_mac_stub(suppressed_variant_skus=1, mode=mode)
    vid = f"5405737{0:07d}"
    sku_key = f"{MAC_STUB_PK}::v:{vid}"
    await seed_offer(oid="off_mac_stub_v", pk=MAC_STUB_PK, sku_key=sku_key,
                     merchant=MAC_OFFER_MERCHANT, price="26.00", source_ref=MAC_STUB_URL)
    await seed_proof(pk=MAC_STUB_PK, sku_key=sku_key, shop_host=MAC_HOST,
                     handle=f"{MAC_STUB_PARENT}-0", variant_id=vid, price_minor=2600)
    resp = await client.post(f"{BASE}/purchases", json=body(host=MAC_HOST, product_key=MAC_STUB_PK))
    await assert_refused(resp, "row_variant_unverified")


async def test_a_second_live_sku_of_another_spelling_is_ambiguous(client):
    """The live-sku read is LIMIT 3 because the choice needs to see a SECOND real sku even behind
    the placeholder: placeholder + a non-`::v:` spelling + the one `::v:` sku is two real skus."""
    await seed_tarte()
    await seed_sku(pk=TARTE_PK, sku_key=f"{TARTE_PK}::sku_crawl01", merchant=TARTE_MERCHANT,
                   svid=f"ext_x:{TARTE_VARIANT}", title="Default Title")
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_variant_ambiguous")


@pytest.mark.parametrize("shade", [0, 1, 2])
async def test_each_named_mac_shade_is_priced_from_its_own_offer_and_proof(client, shade):
    """Shade-price isolation: 39.00 / 41.00 / 43.00. The named shade's own offer and own proof,
    never a neighbour's (a shade priced from the placeholder or another shade would read 39.00)."""
    await seed_mac_folded()
    vid, _handle = MAC_SHADES[shade]
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=MAC_HOST, product_key=MAC_PK, variant_key=f"{MAC_PK}::v:{vid}"))
    assert resp.status_code == 202, resp.text
    purchase = await purchase_of(resp)
    assert purchase["cart_url"] == cart_url_for(MAC_HOST, vid, purchase["click_id"])
    assert purchase["our_price_minor"] == MAC_SHADE_PRICES[shade][1]


async def test_a_legacy_collapsed_8_hex_key_is_refused_before_anything_is_read(client, monkeypatch):
    """`ext:unknown::<8 hex>` was shared by many products: refused (defence in depth beside the
    gateway), before the seller, sku, count, proof or offer reads."""
    legacy = "ext:unknown::0123abcd"
    await seed_tarte(pk=legacy)
    seen = spy_statements(monkeypatch)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=legacy))
    await assert_refused(resp, "row_not_found")
    assert [sql for sql in seen if sql in _ENRICHMENT_SQL] == [routes_reap._CART_ENRICHMENT_PRODUCT_SQL]


@pytest.mark.parametrize("pk", ["ext:unknown::0123456789abcdef", "ext:unknown::0123abcd0",
                                "ext:unknown::0123ABCD"])
async def test_keys_that_are_not_the_legacy_8_hex_shape_are_not_refused_by_that_rule(client, pk):
    """The distinct 16-hex successor (pivota-backend#2461) and near-misses are ordinary keys."""
    await seed_tarte(pk=pk)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=pk))
    assert resp.status_code == 202, resp.text


def test_the_legacy_collapsed_key_shape():
    rule = routes_reap._LEGACY_COLLAPSED_ENRICHMENT_KEY.fullmatch
    assert rule("ext:unknown::0123abcd")
    for key in ("ext:unknown::0123456789abcdef", "ext:unknown::0123abcd0", "ext:unknown::0123abc",
                "ext:unknown::0123ABCD", "ext:unknownx::0123abcd", "xext:unknown::0123abcd",
                TARTE_PK):
        assert not rule(key), key


# ── the storefront-proof table's self-heal, on the request path (review of #2465, F3) ────────


async def test_a_failed_proof_table_create_is_not_retried_on_every_request(client, monkeypatch):
    """A role that cannot CREATE: the first request tries the DDL once and refuses; requests in
    the next `FETCH_DDL_RETRY_SECONDS` refuse WITHOUT re-running it; after the window it is tried
    once more."""
    await seed_tarte(proof=False)
    await _drop_proofs()
    calls = []

    async def _failing_ddl(*args, **kwargs):
        calls.append(1)
        return False

    real_ddl = proofs.apply_ddl_statements
    monkeypatch.setattr(proofs, "apply_ddl_statements", _failing_ddl)
    clock = [1000.0]
    monkeypatch.setattr(proofs.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
        await assert_refused(resp, "row_variant_unverified")
    assert len(calls) == 1
    clock[0] += proofs.FETCH_DDL_RETRY_SECONDS - 1
    assert await proofs.fetch_proof(TARTE_PK, TARTE_SKU) is None and len(calls) == 1
    clock[0] += 2
    assert await proofs.fetch_proof(TARTE_PK, TARTE_SKU) is None and len(calls) == 2
    # The DDL works again: the next call past the window creates the table and reads normally.
    monkeypatch.setattr(proofs, "apply_ddl_statements", real_ddl)
    clock[0] += proofs.FETCH_DDL_RETRY_SECONDS + 1
    assert await proofs.fetch_proof(TARTE_PK, TARTE_SKU) is None
    assert await proofs_table_exists() and proofs._FETCH_DDL_FAILED_AT is None


async def test_a_successful_create_clears_the_remembered_failure(monkeypatch):
    await _drop_proofs()
    clock = [5000.0]
    monkeypatch.setattr(proofs.time, "monotonic", lambda: clock[0])
    proofs._FETCH_DDL_FAILED_AT = clock[0] - proofs.FETCH_DDL_RETRY_SECONDS - 1  # window over
    assert await proofs.fetch_proof(TARTE_PK, TARTE_SKU) is None
    assert proofs._FETCH_DDL_FAILED_AT is None
    assert await proofs_table_exists()


@pytest.mark.parametrize("posted", [
    "eviltartecosmetics.com",          # a lookalike prefix
    "tartecosmetics.com.evil.io",      # a suffix under another registrable
    "shop.tartecosmetics.com",         # a subdomain is another store
    "www.www.tartecosmetics.com",      # ONE `www.` fold, not two
])
async def test_a_lookalike_posted_host_is_not_this_product(client, posted):
    await seed_tarte(host=posted)
    resp = await client.post(f"{BASE}/purchases", json=body(host=posted, product_key=TARTE_PK))
    await assert_refused(resp, "row_not_found")


async def test_a_source_domain_on_another_store_is_not_this_product(client):
    """The canonical_url host matches the post; the row's source_domain does not."""
    await seed_tarte(domain="eviltartecosmetics.com")
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_not_found")


async def test_a_canonical_url_on_another_store_is_not_this_product(client):
    await seed_tarte(url=f"https://eviltartecosmetics.com/products/{TARTE_HANDLE}")
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_not_found")


async def test_a_canonical_url_that_is_not_a_product_page_is_not_this_product(client):
    await seed_tarte(url=f"https://{TARTE_HOST}/collections/brushes")
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_not_found")


async def test_a_proof_read_on_a_lookalike_host_is_not_a_proof(client):
    await seed_tarte(proof_host="eviltartecosmetics.com")
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_variant_unverified")


@pytest.mark.parametrize("merchant,seller_ref", [
    ("merch_obs_0000000000000000", None),      # not what (tarte, tartecosmetics.com) derives
    (TARTE_OFFER_MERCHANT, None),              # the offer owner is never the seller
    (TARTE_MERCHANT, "merch_obs_0000000000000000"),  # a seller_ref that disagrees
])
async def test_a_seller_that_does_not_re_derive_is_refused(client, merchant, seller_ref):
    await seed_tarte(merchant=merchant, seller_ref=seller_ref)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "seller_identity_unverified")


async def test_a_named_sku_must_be_a_live_sku_of_this_product(client):
    ids = await seed_tarte()
    await seed_bluemercury()
    for variant_key in (BM_SKU, f"{TARTE_PK}::v:99999999999999"):
        resp = await client.post(f"{BASE}/purchases", json=body(
            host=TARTE_HOST, product_key=TARTE_PK, variant_key=variant_key))
        await assert_refused(resp, "row_not_found")
    await database.execute(
        "UPDATE catalog_skus SET suppressed_at = CURRENT_TIMESTAMP WHERE sku_key = :sk",
        {"sk": ids["sku"]})
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=TARTE_HOST, product_key=TARTE_PK, variant_key=ids["sku"]))
    await assert_refused(resp, "row_not_found")
    await database.execute(
        "UPDATE catalog_skus SET suppressed_at = NULL, suppression_reason = 'gone' "
        "WHERE sku_key = :sk", {"sk": ids["sku"]})
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=TARTE_HOST, product_key=TARTE_PK, variant_key=ids["sku"]))
    await assert_refused(resp, "row_not_found")


async def test_a_suppressed_product_is_not_found(client):
    await seed_tarte()
    for column, value in (("suppressed_at", "CURRENT_TIMESTAMP"), ("suppression_reason", "'gone'")):
        await database.execute(f"UPDATE catalog_products SET suppressed_at = NULL, "
                               f"suppression_reason = NULL WHERE product_key = :pk", {"pk": TARTE_PK})
        await database.execute(f"UPDATE catalog_products SET {column} = {value} "
                               "WHERE product_key = :pk", {"pk": TARTE_PK})
        resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
        await assert_refused(resp, "row_not_found")


async def test_a_real_sku_with_no_proof_is_unverified(client):
    await seed_tarte(proof=False)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_variant_unverified")


async def test_a_stale_proof_is_unverified(client):
    await seed_tarte(proof=False)
    await seed_proof(pk=TARTE_PK, sku_key=TARTE_SKU, shop_host=TARTE_HOST, handle=TARTE_HANDLE,
                     variant_id=TARTE_VARIANT, price_minor=3000, age=timedelta(hours=73))
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_variant_unverified")


async def test_a_proof_naming_another_variant_is_unverified(client):
    """The cart variant comes ONLY from the verifier: a proof for this sku that names another id
    than the sku's own is a contradiction, not a variant to buy."""
    await seed_tarte(proof=False)
    await seed_proof(pk=TARTE_PK, sku_key=TARTE_SKU, shop_host=TARTE_HOST, handle=TARTE_HANDLE,
                     variant_id="41234567890999", price_minor=3000)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_variant_unverified")


async def test_a_proof_for_another_sku_of_the_product_is_not_this_skus_proof(client):
    """The proof is read for exactly (product_key, chosen sku_key): a proof sitting on the
    placeholder does not prove the real sku."""
    await seed_tarte(proof=False)
    await seed_proof(pk=TARTE_PK, sku_key=TARTE_PLACEHOLDER, shop_host=TARTE_HOST,
                     handle=TARTE_HANDLE, variant_id=TARTE_VARIANT, price_minor=3000)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_variant_unverified")


async def test_a_proof_on_the_placeholder_beside_the_real_skus_proof_changes_nothing(client):
    await seed_tarte()
    await seed_proof(pk=TARTE_PK, sku_key=TARTE_PLACEHOLDER, shop_host=TARTE_HOST,
                     handle=TARTE_HANDLE, variant_id=TARTE_VARIANT, price_minor=2500)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text
    assert (await purchase_of(resp))["our_price_minor"] == 3000


async def test_a_us_buyer_on_an_sgd_storefront_is_a_currency_mismatch(client):
    await seed_jsm(market="US")
    resp = await client.post(f"{BASE}/purchases", json=body(host=JSM_HOST, product_key=JSM_PK))
    await assert_refused(resp, "row_currency_mismatch")


# ── the listing-identity offer filter: each DECOY would move the price if it were read ───────
#
# Every case seeds tarte (proof 30.00, listing offer 30.00) plus ONE decoy offer at 99.00 on the
# real sku's neighbourhood. Read, the decoy makes the verdict `row_price_ambiguous` (two prices);
# filtered, the purchase opens at 30.00. So each filter below is shown to be load-bearing.

_DECOYS: List[Tuple[str, Dict[str, Any]]] = [
    ("other_source_system", {"source_system": "shopify_catalog_sync_v1"}),
    ("seller_of_record_not_seed_namespace", {"merchant": TARTE_MERCHANT}),
    ("seed_namespace_lookalike", {"merchant": "agent_seedx:tarte"}),
    ("suppressed_at", {"suppressed_at": True}),
    ("suppression_reason", {"suppression_reason": True}),
    ("out_of_stock", {"availability": "out_of_stock"}),
    ("sold_out", {"availability": "SOLD_OUT"}),
    ("another_handle", {"source_ref": f"https://{TARTE_HOST}/products/flat-blush-brush-set"}),
    ("lookalike_host", {"source_ref": f"https://eviltartecosmetics.com/products/{TARTE_HANDLE}"}),
    ("subdomain_host", {"source_ref": f"https://uk.{TARTE_HOST}/products/{TARTE_HANDLE}"}),
    ("not_a_product_page", {"source_ref": f"https://{TARTE_HOST}/collections/{TARTE_HANDLE}"}),
    ("another_sku", {"sku_key": TARTE_PLACEHOLDER}),
    ("another_product", {"pk": BM_PK}),
    ("unpriced", {"price": None}),
]


@pytest.mark.parametrize("decoy", [d for _n, d in _DECOYS], ids=[n for n, _d in _DECOYS])
async def test_only_the_listings_own_offer_is_the_price(client, decoy):
    await seed_tarte()
    await seed_offer(**{"oid": "off_decoy", "pk": TARTE_PK, "sku_key": TARTE_SKU,
                        "merchant": TARTE_OFFER_MERCHANT, "price": "99.00",
                        "source_ref": TARTE_URL, **decoy})
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text
    assert (await purchase_of(resp))["our_price_minor"] == 3000


async def test_a_second_listing_offer_at_another_price_is_ambiguous(client):
    """The control for the decoy table: the SAME decoy with nothing that disqualifies it is read,
    and refuses."""
    await seed_tarte()
    await seed_offer(oid="off_decoy", pk=TARTE_PK, sku_key=TARTE_SKU, merchant=TARTE_OFFER_MERCHANT,
                     price="99.00", source_ref=TARTE_URL)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_price_ambiguous")


async def test_an_offer_only_off_the_listing_leaves_the_row_unpriced(client):
    await seed_tarte(offer_ref=f"https://{TARTE_HOST}/products/flat-blush-brush-set")
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_unpriced")


async def test_more_offers_than_one_listing_carries_is_ambiguous(client):
    await seed_tarte()
    for n in range(routes_reap._CART_ENRICHMENT_MAX_OFFERS):
        await seed_offer(oid=f"off_many_{n:03d}", pk=TARTE_PK, sku_key=TARTE_SKU,
                         merchant=TARTE_OFFER_MERCHANT, price="30.00", source_ref=TARTE_URL)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    await assert_refused(resp, "row_price_ambiguous")


async def test_exactly_the_bound_of_agreeing_offers_is_still_one_price(client):
    await seed_tarte()  # one listing offer on the real sku already
    for n in range(routes_reap._CART_ENRICHMENT_MAX_OFFERS - 1):
        await seed_offer(oid=f"off_many_{n:03d}", pk=TARTE_PK, sku_key=TARTE_SKU,
                         merchant=TARTE_OFFER_MERCHANT, price="30.00", source_ref=TARTE_URL)
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text


# ── the sku rule, unit ───────────────────────────────────────────────────────────────────────


def test_the_enrichment_sku_choice_rule():
    choose = routes_reap._enrichment_sku_choice
    pk = TARTE_PK
    ph = {"sku_key": f"{pk}::canonical", "source_variant_id": pk}
    a = {"sku_key": f"{pk}::v:1", "source_variant_id": "1"}
    b = {"sku_key": f"{pk}::v:2", "source_variant_id": "2"}
    assert choose([ph, a], pk, 1) == a
    assert choose([a], pk, 1) == a
    assert choose([ph], pk, 0) == ph
    assert choose([ph], pk, 1) == ph   # one suppressed `::v:`: the VERIFIER's placeholder gate refuses
    # two live real skus -> ambiguous, whatever the count says (a non-`::v:` spelling is real too)
    for rows, count in (([ph, a, b], 2), ([a, b], 2), ([a, b], 1), ([a, b], 0)):
        with pytest.raises(svc.PurchaseRefused) as caught:
            choose(rows, pk, count)
        assert caught.value.reason == "row_variant_ambiguous"
    # the catalog knows two or more variants (suppressed ones count) -> ambiguous, even with ONE
    # live real sku, or none (review of #2465, P1)
    for rows in ([ph, a], [a], [ph]):
        with pytest.raises(svc.PurchaseRefused) as caught:
            choose(rows, pk, 2)
        assert caught.value.reason == "row_variant_ambiguous"
    with pytest.raises(svc.PurchaseRefused) as caught:
        choose([], pk, 0)
    assert caught.value.reason == "row_not_found"
    # a placeholder spelled for ANOTHER product is a real sku here, never this product's placeholder
    other = {"sku_key": f"{BM_PK}::canonical", "source_variant_id": BM_PK}
    assert choose([other], pk, 0) == other


# ── FLAG OFF: byte-identical to main ─────────────────────────────────────────────────────────
#
# With REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED off (unset, empty, "0", misspelt) the lane is
# main's: an enrichment row whose source_domain equals the post is found by `_CART_PRODUCT_SQL` and
# refused `row_variant_unverified` ("external seed source is unknown"); any other post is
# `row_not_found`. These expectations were ALSO run against origin/main's route module (PR body).
# And not one enrichment statement runs: the spy below sees every SQL the request issues.

_ENRICHMENT_SQL = (
    routes_reap._CART_ENRICHMENT_PRODUCT_SQL, routes_reap._CART_ENRICHMENT_SKU_BY_KEY_SQL,
    routes_reap._CART_ENRICHMENT_LIVE_SKUS_SQL, routes_reap._CART_ENRICHMENT_VARIANT_SKU_COUNT_SQL,
    routes_reap._CART_ENRICHMENT_OFFERS_SQL, proofs._SELECT_PROOF_SQL,
)


def spy_statements(monkeypatch) -> List[str]:
    seen: List[str] = []
    for name in ("fetch_one", "fetch_all", "fetch_val", "execute"):
        real = getattr(database, name)

        def _spy(query, *args, _real=real, **kwargs):
            seen.append(str(query))
            return _real(query, *args, **kwargs)

        monkeypatch.setattr(database, name, _spy)
    return seen


async def _reset_catalog() -> None:
    await _clear(SHARED_TABLES)
    await _clear(("tierb_cart_link_eligibility",))
    await database.execute(f"DELETE FROM {proofs.TABLE}")


#: (world, posted host, product_key, variant_key, main's answer)
_FLAG_OFF_CASES = [
    ("tarte_sole", TARTE_HOST, TARTE_PK, None, "row_variant_unverified"),
    ("tarte_drift", TARTE_HOST, TARTE_PK, None, "row_variant_unverified"),
    ("bm_two_sizes", BM_HOST, BM2_PK, None, "row_variant_unverified"),
    ("mac_folded_named", MAC_HOST, MAC_PK, f"{MAC_PK}::v:{MAC_SHADES[0][0]}", "row_variant_unverified"),
    ("mac_stub", MAC_HOST, MAC_STUB_PK, None, "row_variant_unverified"),
    ("bluemercury", BM_HOST, BM_PK, None, "row_variant_unverified"),
    ("www_post", f"www.{TARTE_HOST}", TARTE_PK, None, "row_not_found"),
    ("lookalike", "eviltartecosmetics.com", TARTE_PK, None, "row_not_found"),
]


async def _seed_world(world: str, host: str) -> None:
    if world in ("tarte_sole", "www_post", "lookalike"):
        await seed_tarte(host=host)
    elif world == "tarte_drift":
        await seed_tarte(offer_price="32.00")
    elif world == "bm_two_sizes":
        await seed_bluemercury_two_sizes()
    elif world == "mac_folded_named":
        await seed_mac_folded()
    elif world == "mac_stub":
        await seed_mac_stub(suppressed_variant_skus=2)
    elif world == "bluemercury":
        await seed_bluemercury()
    else:  # pragma: no cover - a typo in the table
        raise AssertionError(world)


@pytest.mark.parametrize("dial", [None, "0", "ture"])
@pytest.mark.parametrize("world,host,pk,variant_key,main_reason", _FLAG_OFF_CASES,
                         ids=[c[0] for c in _FLAG_OFF_CASES])
async def test_flag_off_every_world_answers_exactly_what_main_answers(
    client, monkeypatch, dial, world, host, pk, variant_key, main_reason,
):
    if dial is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, dial)
    await _seed_world(world, host)
    await _drop_proofs()
    seen = spy_statements(monkeypatch)
    resp = await client.post(f"{BASE}/purchases", json=body(
        host=host, product_key=pk, variant_key=variant_key))
    await assert_refused(resp, main_reason)
    assert not any(sql in _ENRICHMENT_SQL for sql in seen)
    assert not await proofs_table_exists()   # the reader never ran, so nothing created it


async def test_the_enrichment_flag_alone_arms_nothing(client, monkeypatch):
    """Enrichment flag ON, cart-link dial OFF: the lane is the rail's 404, as on main."""
    await seed_tarte()
    monkeypatch.delenv("REAP_AGENTIC_CART_LINK_ENABLED", raising=False)
    assert not svc.is_cart_link_enrichment_enabled()
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 404 and error_of(resp) == "not_available_on_this_rail"
    assert await purchase_count() == 0


def test_the_enrichment_flag_parse(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    for value, expected in ((None, False), ("", False), ("0", False), ("ture", False),
                            ("1", True), ("true", True), (" ON ", True), ("yes", True)):
        if value is None:
            monkeypatch.delenv(FLAG, raising=False)
        else:
            monkeypatch.setenv(FLAG, value)
        assert svc.is_cart_link_enrichment_enabled() is expected, value


# ── the mirror / Shopify regression set: the same answers with the flag on and off ───────────

LIVE_DOMAIN = "judydoll.com"
LIVE_EXT_ID = "ext_0f95730ee5ba05a6b7957ada"
LIVE_PK = f"prod::external_seed::external_seed::{LIVE_EXT_ID}"
LIVE_MERCHANT = "merch_obs_a25cbba37ef98c52"
LIVE_SEED_ID = "epsv_38ad88d436c32e24ba7c6446"
LIVE_VARIANT = "49819267301653"
SHOPIFY_PK = "prod::m_enrich::shopify::3001"
SHOPIFY_DOMAIN = "brand-enrich.example"


async def _seed_mirror(*, merchant: str = LIVE_MERCHANT, checked_at: Optional[str] = None) -> None:
    await database.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, "
        "brand, category, product_type, source_domain, source_system, source_ref, seller_ref, "
        "seed_kind) VALUES (:pk, :m, 'external_seed', :ext, 'Silky Matte Lip Ink', 'Judydoll', "
        "'makeup', 'Lipstick', :d, 'external_product_seeds_mirror_v1', :seed, NULL, 'self')",
        {"pk": LIVE_PK, "m": merchant, "ext": LIVE_EXT_ID, "d": LIVE_DOMAIN, "seed": LIVE_SEED_ID},
    )
    for sku_key, vid in ((f"{LIVE_PK}::canonical", LIVE_PK),
                         (f"{LIVE_PK}::sku_58ae6f8de2c8797993f2", f"{LIVE_EXT_ID}:{LIVE_VARIANT}"),
                         (f"{LIVE_PK}::v:{LIVE_VARIANT}", LIVE_VARIANT)):
        await database.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, "
            "source_product_id, source_variant_id, title, currency) VALUES "
            "(:sk, :pk, :m, 'external_seed', :ext, :vid, 'Silky Matte Lip Ink', 'USD')",
            {"sk": sku_key, "pk": LIVE_PK, "m": merchant, "ext": LIVE_EXT_ID, "vid": vid})
        await seed_offer(oid=f"off_mirror_{sku_key[-12:]}", pk=LIVE_PK, sku_key=sku_key,
                         merchant=merchant, price="13.99", source_ref="", source_system=None)
    seed_data = {"snapshot": {
        "brand": "Judydoll", "storefront_platform": "shopify",
        "storefront_platform_source": "products_js_v1",
        "variants": [{"title": "Default Title", "shopify_variant_id": LIVE_VARIANT}],
        "shopify_cart_proof": {
            "source": "products_js_v1",
            "product_js_url": f"https://{LIVE_DOMAIN}/products/silky-matte-lip-ink.js",
            "live_variant_count": 1, "variant_id": LIVE_VARIANT,
            "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        }}}
    await database.execute(
        "INSERT INTO external_product_seeds (id, status, market, destination_url, domain, "
        f"attached_product_key, attached_variant_id, seed_data) VALUES (:id, 'active', 'US', :url, "
        f":d, :pk, NULL, {_jsonb('sd')})",
        {"id": LIVE_SEED_ID, "url": f"https://{LIVE_DOMAIN}/products/silky-matte-lip-ink",
         "d": LIVE_DOMAIN, "pk": LIVE_PK, "sd": json.dumps(seed_data)})
    await seed_tierb(LIVE_DOMAIN)


async def _seed_shopify() -> None:
    await database.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, "
        "brand, source_domain, seller_ref, seed_kind) VALUES (:pk, 'm_enrich', 'shopify', '3001', "
        "'Standard Eau de Parfum', 'Brand', :d, 'm_enrich', 'self')",
        {"pk": SHOPIFY_PK, "d": SHOPIFY_DOMAIN})
    await database.execute(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, "
        "source_variant_id, title, currency) VALUES (:sk, :pk, 'm_enrich', 'shopify', '3001', "
        "'50041364447509', 'Standard', 'USD')", {"sk": f"{SHOPIFY_PK}::v1", "pk": SHOPIFY_PK})
    await seed_offer(oid="off_shopify", pk=SHOPIFY_PK, sku_key=f"{SHOPIFY_PK}::v1",
                     merchant="m_enrich", price="42.50", source_ref="", source_system=None)
    await seed_tierb(SHOPIFY_DOMAIN)


_REGRESSION = [
    ("mirror_sole_proof", 202, None, 1399),
    ("mirror_stale_proof", 409, "row_variant_unverified", None),
    ("mirror_wrong_seller", 409, "seller_identity_unverified", None),
    ("mirror_named_placeholder_missing", 409, "row_not_found", None),
    ("shopify_sole_variant", 202, None, 4250),
]


@pytest.mark.parametrize("dial", ["1", "0"], ids=["flag-on", "flag-off"])
@pytest.mark.parametrize("case,status,reason,price", _REGRESSION, ids=[c[0] for c in _REGRESSION])
async def test_mirror_and_shopify_rows_answer_the_same_with_the_flag_on_or_off(
    client, monkeypatch, dial, case, status, reason, price,
):
    monkeypatch.setenv(FLAG, dial)
    if case == "shopify_sole_variant":
        await _seed_shopify()
        req = body(host=SHOPIFY_DOMAIN, product_key=SHOPIFY_PK)
    else:
        await _seed_mirror(
            merchant="merch_obs_0000000000000000" if case == "mirror_wrong_seller" else LIVE_MERCHANT,
            checked_at="2026-01-01T00:00:00+00:00" if case == "mirror_stale_proof" else None)
        req = body(host=LIVE_DOMAIN, product_key=LIVE_PK,
                   variant_key=f"{LIVE_PK}::nope" if case == "mirror_named_placeholder_missing" else None)
    seen = spy_statements(monkeypatch)
    resp = await client.post(f"{BASE}/purchases", json=req)
    if status == 202:
        assert resp.status_code == 202, resp.text
        purchase = await purchase_of(resp)
        assert purchase["our_price_minor"] == price
    else:
        await assert_refused(resp, reason)
    # With the flag on, a non-enrichment row costs exactly ONE extra read (the enrichment product
    # lookup, which finds nothing) and nothing else of the branch.
    enrichment_reads = [sql for sql in seen if sql in _ENRICHMENT_SQL]
    assert enrichment_reads == ([routes_reap._CART_ENRICHMENT_PRODUCT_SQL] if dial == "1" else [])
