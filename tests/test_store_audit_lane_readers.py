"""The diagnostics readers, EXECUTED — not patched out.

Every test in test_store_audit_ops_diagnostics.py replaces both readers with
fakes, so their SQL never ran and a reader that unconditionally answered
"lookup FAILED" shipped green. It did: `Record.values()` does not exist on the
databases==0.7.0 sqlite backend, so summarize_ucp_route_merchant_coverage raised
inside its own try and returned -1 on every call, while asyncpg's deprecated
.values() hid it on Postgres. These tests drive the real queries against the
real schema.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

import db.audit_evidence as ae
from db import merchant_official_domains as dom
from db.database import database, engine, metadata

_NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def _db():
    # The model builds these, not ensure_audit_evidence_tables(): that path
    # emits Postgres-only DDL which sqlite refuses statement by statement (the
    # _ddl_guard logs "skip stmt: near ( syntax error" and leaves no table).
    # create_all is checkfirst, and every table is named explicitly rather than
    # free-riding on whichever sibling module happened to run first.
    metadata.create_all(engine, tables=[
        ae.execution_routes, ae.verification_runs, dom.merchant_official_domains,
    ])
    if not database.is_connected:
        await database.connect()
    await database.execute(ae.verification_runs.delete())
    await database.execute(ae.execution_routes.delete())
    await database.execute(dom.merchant_official_domains.delete())
    yield
    await database.execute(ae.verification_runs.delete())
    await database.execute(ae.execution_routes.delete())
    await database.execute(dom.merchant_official_domains.delete())


async def _route(domain, *, kind=ae.ROUTE_KIND_UCP, active=True, audit_run=True):
    rid = str(uuid.uuid4())
    await database.execute(ae.execution_routes.insert().values(
        execution_route_id=rid, normalized_domain=domain, route_kind=kind,
        endpoint_normalized=f"https://{domain}/api/ucp/mcp",
        last_audit_run_id=str(uuid.uuid4()) if audit_run else None,
        first_detected_at=_NOW, is_active=active, created_at=_NOW, updated_at=_NOW))
    return rid


async def _run(rid, status, *, err=None, ev=None, pk=None):
    vid = str(uuid.uuid4())
    await database.execute(ae.verification_runs.insert().values(
        verify_id=vid, audit_run_id=str(uuid.uuid4()), execution_route_id=rid,
        verifier_id=ae.VERIFIER_UCP_PROBE, status=status, error_message=err,
        evidence_jsonb=ev, product_key=pk, created_at=_NOW))
    return vid


async def _official(merchant, domain, *, status, source, liveness=None):
    values = {"merchant_id": merchant, "domain": domain, "source": source,
              "verification_status": status}
    if liveness is not None:
        values["liveness_status"] = liveness
        values["last_checked_at"] = _NOW
    await database.execute(dom.merchant_official_domains.insert().values(**values))


# ---------------------------------------------------------------------
# History reader
# ---------------------------------------------------------------------


def test_history_returns_the_failure_reason_which_is_the_whole_point():
    async def scenario():
        rid = await _route("shop.example")
        await _run(rid, "blocked", err="tool_error",
                   ev={"reason": "profile_redirected"})
        return await ae.fetch_verification_history_for_domain(
            normalized_domain="shop.example", verifier_id=ae.VERIFIER_UCP_PROBE,
            route_kinds=(ae.ROUTE_KIND_UCP, ae.ROUTE_KIND_UCP_DISCOVERY), limit=10)

    rows = asyncio.run(scenario())
    assert len(rows) == 1
    assert rows[0]["status"] == "blocked"
    assert rows[0]["error_message"] == "tool_error"
    assert rows[0]["evidence_jsonb"]["reason"] == "profile_redirected"


def test_history_covers_discovery_routes_too():
    """A cold domain gets a ucp_discovery placeholder; excluding that kind would
    make the endpoint blind to exactly the funnel arrivals it was built for."""
    async def scenario():
        rid = await _route("cold.example", kind=ae.ROUTE_KIND_UCP_DISCOVERY)
        await _run(rid, "blocked", err="profile_unreachable")
        return await ae.fetch_verification_history_for_domain(
            normalized_domain="cold.example", verifier_id=ae.VERIFIER_UCP_PROBE,
            route_kinds=(ae.ROUTE_KIND_UCP, ae.ROUTE_KIND_UCP_DISCOVERY), limit=10)

    assert len(asyncio.run(scenario())) == 1


def test_history_is_scoped_to_the_domain_asked_about():
    async def scenario():
        await _run(await _route("mine.example"), "blocked", err="mine")
        await _run(await _route("theirs.example"), "blocked", err="theirs")
        return await ae.fetch_verification_history_for_domain(
            normalized_domain="mine.example", verifier_id=ae.VERIFIER_UCP_PROBE,
            route_kinds=(ae.ROUTE_KIND_UCP,), limit=10)

    rows = asyncio.run(scenario())
    assert [r["error_message"] for r in rows] == ["mine"]


def test_history_respects_its_limit():
    async def scenario():
        rid = await _route("shop.example")
        for _ in range(5):
            await _run(rid, "blocked")
        return await ae.fetch_verification_history_for_domain(
            normalized_domain="shop.example", verifier_id=ae.VERIFIER_UCP_PROBE,
            route_kinds=(ae.ROUTE_KIND_UCP,), limit=2)

    assert len(asyncio.run(scenario())) == 2


# ---------------------------------------------------------------------
# Coverage reader — the one that shipped answering -1
# ---------------------------------------------------------------------


def test_coverage_actually_counts_instead_of_reporting_a_failed_lookup():
    """The regression test for `Record.values()`. Before the fix this returned
    {-1, -1} on sqlite and every caller read it as "lookup FAILED"."""
    async def scenario():
        await _route("shop.example")
        return await ae.summarize_ucp_route_merchant_coverage()

    out = asyncio.run(scenario())
    assert out["active_ucp_routes"] == 1
    assert out["routes_with_proven_merchant"] == 0


def test_coverage_counts_a_route_whose_merchant_proved_the_domain():
    async def scenario():
        await _route("proven.example")
        await _official("m1", "proven.example",
                        status=dom.VERIFICATION_VERIFIED, source=dom.SOURCE_VERIFIED)
        return await ae.summarize_ucp_route_merchant_coverage()

    assert asyncio.run(scenario())["routes_with_proven_merchant"] == 1


@pytest.mark.parametrize("kwargs,label", [
    (dict(status=dom.VERIFICATION_PENDING, source=dom.SOURCE_VERIFIED), "pending"),
    (dict(status=dom.VERIFICATION_VERIFIED, source=dom.SOURCE_ASSERTED), "asserted"),
    (dict(status=dom.VERIFICATION_VERIFIED, source=dom.SOURCE_VERIFIED,
          liveness=dom.LIVENESS_DEAD), "dead"),
])
def test_coverage_excludes_what_the_gate_excludes(kwargs, label):
    async def scenario():
        await _route(f"{label}.example")
        await _official("m1", f"{label}.example", **kwargs)
        return await ae.summarize_ucp_route_merchant_coverage()

    assert asyncio.run(scenario())["routes_with_proven_merchant"] == 0, label


def test_coverage_excludes_a_domain_two_merchants_both_proved():
    """The gate refuses ambiguity (RESOLVE_VERIFIED_MERCHANT_SQL LIMIT 2,
    len != 1). A count that ignored it would report a route the tier skips."""
    async def scenario():
        await _route("shared.example")
        await _official("m1", "shared.example",
                        status=dom.VERIFICATION_VERIFIED, source=dom.SOURCE_VERIFIED)
        await _official("m2", "shared.example",
                        status=dom.VERIFICATION_VERIFIED, source=dom.SOURCE_VERIFIED)
        return await ae.summarize_ucp_route_merchant_coverage()

    assert asyncio.run(scenario())["routes_with_proven_merchant"] == 0


def test_coverage_excludes_a_merchant_with_two_storefronts():
    """anua.com + anua.us: the gate refuses because canonical_variants has no
    store key. Counting them said "the tier will fire twice"; it fires zero."""
    async def scenario():
        await _route("anua.com")
        await _route("anua.us")
        await _official("m1", "anua.com",
                        status=dom.VERIFICATION_VERIFIED, source=dom.SOURCE_VERIFIED)
        await _official("m1", "anua.us",
                        status=dom.VERIFICATION_VERIFIED, source=dom.SOURCE_VERIFIED)
        return await ae.summarize_ucp_route_merchant_coverage()

    out = asyncio.run(scenario())
    assert out["active_ucp_routes"] == 2
    assert out["routes_with_proven_merchant"] == 0


def test_coverage_excludes_a_route_the_reprobe_selector_cannot_pick_up():
    """list_due_ucp_routes skips a route with no last_audit_run_id, so counting
    one would promise a probe that never gets enqueued."""
    async def scenario():
        await _route("nopointer.example", audit_run=False)
        await _official("m1", "nopointer.example",
                        status=dom.VERIFICATION_VERIFIED, source=dom.SOURCE_VERIFIED)
        return await ae.summarize_ucp_route_merchant_coverage()

    assert asyncio.run(scenario())["routes_with_proven_merchant"] == 0


def test_coverage_ignores_inactive_and_non_ucp_routes():
    async def scenario():
        await _route("inactive.example", active=False)
        await _route("discovery.example", kind=ae.ROUTE_KIND_UCP_DISCOVERY)
        return await ae.summarize_ucp_route_merchant_coverage()

    assert asyncio.run(scenario())["active_ucp_routes"] == 0
