"""ADR-027 deal-lane storage (db/deal_lane.py, migrations 275 and 276): the constraints that keep an
unverifiable deal from ever looking verified, the flags' defaults, and the pins that keep the click-row
columns out of the backend until migration 276 is applied by hand. The same tables on real Postgres,
built from the migration itself: tests/test_deal_lane_storage_postgres.py."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import deal_lane  # noqa: E402
from db.commerce_attribution import surface_click_events  # noqa: E402
from db.database import metadata  # noqa: E402
from db.sql_migrations import (  # noqa: E402
    is_hand_applied_migration,
    list_hand_applied_migration_files,
    list_migration_files,
    needs_autocommit,
)

_REPO = Path(__file__).resolve().parents[1]
_MIG = _REPO / "db" / "migrations"
_MIG_275 = _MIG / "275_deal_lane_tables.sql"
_MIG_276 = _MIG / "276_surface_click_events_discount_code.sql.disabled"

TABLES = (
    deal_lane.observed_store_offers,
    deal_lane.observed_store_offer_verifications,
    deal_lane.deal_program_permissions,
    deal_lane.deal_program_permission_hosts,
    deal_lane.deal_network_links,
    deal_lane.deal_verify_host_daily_carts,
    deal_lane.deal_lane_stops,
    deal_lane.deal_seller_optouts,
)
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=list(TABLES))
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def _deal(**overrides):
    row = {
        "deal_id": "deal_1",
        "seller_id": "seller_a",
        "market": "US",
        "code": "GLOW10",
        "platform_rule_ref": None,
        "program_id": None,
        "kind": "percent",
        "value": 10,
        "currency": "USD",
        "source": "merchant_published",
        "observed_at": NOW,
    }
    row.update(overrides)
    row.setdefault(
        "deal_key",
        deal_lane.observed_store_offer_key(
            seller_id=row["seller_id"],
            market=row["market"],
            code=row["code"],
            platform_rule_ref=row["platform_rule_ref"],
            program_id=row["program_id"],
        ),
    )
    return row


def _verified(**overrides):
    row = {
        "deal_id": "deal_1",
        "variant_id": "4401",
        "state": "checkout_verified",
        "verified_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
        "verified_via": "shopify_storefront_connected",
        "authorized_by": "connected",
        "ref_unit_price": 40,
        "ref_subtotal": 40,
        "ref_discount_amount": 4,
        "ref_currency": "USD",
    }
    row.update(overrides)
    return row


def _rejected(conn, table, row):
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(insert(table).values(**row))


# ---- registration --------------------------------------------------------------------------------


def test_every_deal_table_is_registered_and_main_imports_the_module():
    for table in TABLES:
        assert metadata.tables[table.name] is table
    # create_all at startup builds only what main.py imported (prod skips numbered migrations).
    assert "import db.deal_lane  # noqa: F401" in (_REPO / "main.py").read_text()


def test_no_deal_table_revives_a_promotions_name():
    # ADR-022 deleted the promotions lane; TestPromotionsLaneStaysDeleted pins its modules and routes.
    assert not [t.name for t in TABLES if "promotion" in t.name]


# ---- deals ---------------------------------------------------------------------------------------


def test_a_published_code_is_stored(conn):
    conn.execute(insert(deal_lane.observed_store_offers).values(**_deal()))
    row = conn.execute(select(deal_lane.observed_store_offers)).mappings().one()
    # Eligibility defaults to "not limited, and not known": unknown is never emitted (D3).
    assert (row["new_customer_only"], row["usage_limited"], row["eligibility_known"]) == (False, False, False)
    assert (row["orders_seen"], row["orders_applied"]) == (0, 0)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "cashback"}, id="unknown-kind"),
        pytest.param({"source": "coupon_aggregator"}, id="aggregator-source-D1"),
        pytest.param({"source": "checkout_automatic"}, id="automatic-with-a-code"),
        pytest.param({"code": None, "platform_rule_ref": "rule_1"}, id="published-without-a-code"),
        pytest.param({"source": "authorized_feed"}, id="feed-code-without-its-program"),
        pytest.param({"program_id": "prog_1"}, id="program-on-a-non-feed-code"),
        pytest.param({"currency": "US"}, id="currency-not-iso"),
        pytest.param({"scope_kind": "everything"}, id="unknown-scope"),
    ],
)
def test_a_deal_that_breaks_its_source_rules_is_refused(conn, overrides):
    _rejected(conn, deal_lane.observed_store_offers, _deal(**overrides))


def test_an_automatic_discount_needs_a_rule_ref(conn):
    _rejected(
        conn,
        deal_lane.observed_store_offers,
        _deal(source="checkout_automatic", code=None, platform_rule_ref=None),
    )
    conn.execute(
        insert(deal_lane.observed_store_offers).values(
            **_deal(source="checkout_automatic", code=None, platform_rule_ref="Autumn 10% off")
        )
    )


def test_a_feed_code_names_its_program_and_two_programs_are_two_rows(conn):
    for i, program in enumerate(("prog_1", "prog_2")):
        conn.execute(
            insert(deal_lane.observed_store_offers).values(
                **_deal(deal_id=f"deal_{i}", source="authorized_feed", program_id=program)
            )
        )
    assert len(conn.execute(select(deal_lane.observed_store_offers)).all()) == 2


def test_the_same_code_in_another_case_is_the_same_deal(conn):
    conn.execute(insert(deal_lane.observed_store_offers).values(**_deal()))
    _rejected(conn, deal_lane.observed_store_offers, _deal(deal_id="deal_2", code="glow10"))


def test_the_deal_key_compares_codes_and_markets_case_insensitively_and_nothing_else():
    def key(**kw):
        base = {"seller_id": "s", "market": "US", "code": "GLOW10", "platform_rule_ref": None, "program_id": None}
        base.update(kw)
        return deal_lane.observed_store_offer_key(**base)

    assert key() == key(code=" glow10 ") == key(market="us")
    assert key() != key(seller_id="t")
    assert key() != key(market="SG")
    assert key() != key(market=None)
    assert key() != key(program_id="prog_1")
    assert key(code=None, platform_rule_ref="Rule") != key(code=None, platform_rule_ref="rule")
    assert len(key()) == 64


# ---- verifications -------------------------------------------------------------------------------


def test_a_complete_reference_cart_is_verified(conn):
    conn.execute(insert(deal_lane.observed_store_offers).values(**_deal()))
    conn.execute(insert(deal_lane.observed_store_offer_verifications).values(**_verified()))
    for authorized_by in ("q2_approval", "program:prog_1"):
        conn.execute(
            insert(deal_lane.observed_store_offer_verifications).values(
                **_verified(variant_id=f"v_{authorized_by}", authorized_by=authorized_by)
            )
        )


def test_an_unverified_row_needs_no_reference_cart(conn):
    conn.execute(insert(deal_lane.observed_store_offers).values(**_deal()))
    conn.execute(
        insert(deal_lane.observed_store_offer_verifications).values(deal_id="deal_1", variant_id="4401")
    )
    row = conn.execute(select(deal_lane.observed_store_offer_verifications)).mappings().one()
    assert (row["state"], row["ref_quantity"]) == ("unverified", 1)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"verified_at": None}, id="no-verified_at"),
        pytest.param({"expires_at": None}, id="no-expiry"),
        pytest.param({"expires_at": NOW}, id="expires-when-verified"),
        pytest.param({"verified_via": None}, id="no-route"),
        pytest.param({"authorized_by": None}, id="no-permission"),
        pytest.param({"ref_unit_price": None}, id="no-unit-price"),
        pytest.param({"ref_currency": None}, id="no-currency"),
        pytest.param({"ref_discount_amount": None}, id="no-allocation"),
        pytest.param({"ref_discount_amount": 0}, id="zero-allocation"),
        pytest.param({"ref_quantity": 2}, id="quantity-two"),
        pytest.param({"authorized_by": "program:"}, id="program-without-an-id"),
        pytest.param({"authorized_by": "nonconnected"}, id="unknown-permission"),
        pytest.param({"verified_via": "browser_checkout"}, id="unknown-route"),
        pytest.param({"state": "applied"}, id="unknown-state"),
    ],
)
def test_a_verified_row_without_its_whole_reference_cart_is_refused(conn, overrides):
    conn.execute(insert(deal_lane.observed_store_offers).values(**_deal()))
    _rejected(conn, deal_lane.observed_store_offer_verifications, _verified(**overrides))


# ---- permissions, links, counter, stops ----------------------------------------------------------


def _permission(**overrides):
    row = {
        "program_id": "prog_1",
        "network": "network_a",
        "membership_status": "active",
        "permission_source": "program_terms",
        "permission_ref": "terms read 2026-11-02",
        "permission_dated": NOW,
        "confirmed_at": NOW,
        "confirmed_by": "peng",
        "reconfirm_by": NOW + timedelta(days=90),
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"membership_status": "approved"}, id="unknown-membership"),
        pytest.param({"permission_source": "assumed"}, id="unknown-source"),
        pytest.param({"reconfirm_by": NOW}, id="reconfirm-not-after-confirm"),
    ],
)
def test_a_program_permission_must_say_where_it_came_from(conn, overrides):
    _rejected(conn, deal_lane.deal_program_permissions, _permission(**overrides))


def test_a_program_permission_lists_its_hosts(conn):
    conn.execute(insert(deal_lane.deal_program_permissions).values(**_permission()))
    conn.execute(insert(deal_lane.deal_program_permission_hosts).values(program_id="prog_1", host="shop.example"))
    _rejected(conn, deal_lane.deal_program_permission_hosts, {"program_id": "prog_1", "host": "shop.example"})


def _links(**overrides):
    row = {
        "link_row_id": "nl_1",
        "deal_id": "deal_1",
        "variant_id": "4401",
        "market": "US",
        "program_id": "prog_1",
        "tracking_link": "https://track.example/c/1?u=https%3A%2F%2Fshop.example%2Fproducts%2Fx",
        "tracking_generated_at": NOW,
        "coded_link": "https://track.example/c/1?u=https%3A%2F%2Fshop.example%2Fcart%2F4401%3A1%3Fdiscount%3DGLOW10",
        "coded_generated_at": NOW,
    }
    row.update(overrides)
    return row


def test_network_links_are_one_row_per_deal_variant_market_and_never_the_same_link(conn):
    conn.execute(
        insert(deal_lane.observed_store_offers).values(**_deal(source="authorized_feed", program_id="prog_1"))
    )
    conn.execute(insert(deal_lane.deal_network_links).values(**_links()))
    _rejected(conn, deal_lane.deal_network_links, _links(link_row_id="nl_2"))
    _rejected(
        conn,
        deal_lane.deal_network_links,
        _links(link_row_id="nl_3", market="SG", coded_link=_links()["tracking_link"]),
    )


def test_the_daily_cart_counter_is_one_row_per_host_and_day(conn):
    table = deal_lane.deal_verify_host_daily_carts
    conn.execute(insert(table).values(host="shop.example", utc_day=date(2026, 11, 2)))
    assert conn.execute(select(table.c.carts)).scalar_one() == 0
    _rejected(conn, table, {"host": "shop.example", "utc_day": date(2026, 11, 2)})
    _rejected(conn, table, {"host": "shop.example", "utc_day": date(2026, 11, 3), "carts": -1})


def test_a_market_has_at_most_one_active_stop_and_only_a_person_clears_it(conn):
    table = deal_lane.deal_lane_stops
    conn.execute(insert(table).values(stop_id="stop_1", market="US", stop_trigger="absolute"))
    _rejected(conn, table, {"stop_id": "stop_2", "market": "US", "stop_trigger": "rate"})
    conn.execute(insert(table).values(stop_id="stop_3", market="SG", stop_trigger="rate"))
    _rejected(
        conn,
        table,
        {"stop_id": "stop_4", "market": "JP", "stop_trigger": "manual", "cleared_at": NOW},
    )
    _rejected(conn, table, {"stop_id": "stop_5", "market": "AU", "stop_trigger": "hunch"})
    conn.execute(
        table.update()
        .where(table.c.stop_id == "stop_1")
        .values(cleared_at=NOW, cleared_by="peng", clear_note="reviewed")
    )
    conn.execute(insert(table).values(stop_id="stop_6", market="US", stop_trigger="rate"))


def test_an_opt_out_is_one_row_per_seller(conn):
    table = deal_lane.deal_seller_optouts
    conn.execute(insert(table).values(seller_id="seller_a", requested_at=NOW, recorded_by="peng"))
    _rejected(conn, table, {"seller_id": "seller_a", "requested_at": NOW, "recorded_by": "peng"})


# ---- migrations ----------------------------------------------------------------------------------


def test_migration_275_runs_as_one_transaction_and_is_applied_by_the_runner():
    body = _MIG_275.read_text()
    assert needs_autocommit(body) is False
    assert str(_MIG_275) in list_migration_files()
    for table in TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table.name} (" in body
    # No table here references an existing table, so creating them locks nothing that serves traffic.
    assert {fk.column.table.name for t in TABLES for fk in t.foreign_keys} <= {t.name for t in TABLES}


def test_migration_276_is_hand_applied_bounded_and_off_the_self_heal():
    body = _MIG_276.read_text()
    assert is_hand_applied_migration(str(_MIG_276))
    assert str(_MIG_276) in list_hand_applied_migration_files()
    assert needs_autocommit(body) is False  # SET LOCAL needs the transaction
    assert "SET LOCAL lock_timeout" in body
    assert "schema-guard-exempt:" in body
    guard = (_REPO / "db" / "schema_guard.py").read_text()
    assert "discount_code" not in guard and "discount_carried" not in guard


def test_the_click_row_does_not_name_the_276_columns_until_they_are_applied():
    """Every select(surface_click_events) names every Table column. Putting these on the Table before
    migration 276 is applied in prod would fail those selects on UndefinedColumn. The PR that adds them
    here (and the first writer) follows the hand apply; update this pin in that PR."""
    assert {"discount_code", "discount_carried"}.isdisjoint(surface_click_events.c.keys())
    offenders = []
    for path in _REPO.rglob("*.py"):
        rel = path.relative_to(_REPO).as_posix()
        if rel.startswith(("tests/", ".venv/", "venv/")) or "/site-packages/" in rel:
            continue
        if "discount_carried" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(rel)
    assert offenders == []


# ---- flags ---------------------------------------------------------------------------------------

_FLAG_ENV = (
    "DEAL_LANE_EMIT_ENABLED",
    "DEAL_LANE_RANKING_ENABLED",
    "DEAL_LANE_DISCOUNT_PARAM_ENABLED",
    "DEAL_VERIFY_JOB_ENABLED",
    "DEAL_VERIFY_NONCONNECTED_ENABLED",
    "DEAL_VERIFY_PROGRAM_ENABLED",
    "DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY",
    "DEAL_VERIFY_TTL_HOURS",
)


def test_every_deal_lane_flag_defaults_off(monkeypatch, tmp_path):
    from config.settings import Settings

    for name in _FLAG_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env file
    s = Settings()
    assert not any(
        (
            s.deal_lane_emit_enabled,
            s.deal_lane_ranking_enabled,
            s.deal_lane_discount_param_enabled,
            s.deal_verify_job_enabled,
            s.deal_verify_nonconnected_enabled,
            s.deal_verify_program_enabled,
        )
    )
    assert (s.deal_verify_max_carts_per_host_per_day, s.deal_verify_ttl_hours) == (20, 24)
