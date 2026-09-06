"""P0 item 5 — a merchant may DECLARE an additional official domain.

WHY THIS EXISTS, measured in production 2026-09-06: `merchant_official_domains`
held ONE row across 42 merchants, and 16 of 17 audited merchants fell back
entirely to inference. That is the exact condition the evidence base measured as
a 13-point error on Anua's headline — inference knew `anua.com` and not
`anua.us`, so 7 citations of a byte-identical storefront scored as retailer
traffic.

THE DISTINCTION THIS FILE DEFENDS. `verified` and `asserted` both mean CONTROL
WAS PROVEN. A self-declaration proves nothing, so it gets its own source and is
deliberately excluded from OFFICIAL_SOURCES: stored so the portal can offer to
verify it, never counted until it is. Recording an unproven host in a tier whose
meaning is proof is how a metric silently becomes fiction.
"""
from __future__ import annotations

import pytest

from db import merchant_official_domains as mod
from services import brand_claim_service as svc


# ---------------------------------------------------------------------
# The invariant: declaring must never widen the official set
# ---------------------------------------------------------------------


def test_declared_is_not_an_official_source():
    """The single line that keeps a declaration from counting. If
    SOURCE_DECLARED ever joins OFFICIAL_SOURCES, a merchant who declared a
    retailer reclassifies that retailer's citations as their own and inflates
    their official share."""
    assert mod.SOURCE_DECLARED in mod.VALID_SOURCES
    assert mod.SOURCE_DECLARED not in mod.OFFICIAL_SOURCES


def test_the_proven_tiers_still_are_official():
    """The positive counterpart — the exclusion must not have taken the real
    tiers with it."""
    assert mod.SOURCE_VERIFIED in mod.OFFICIAL_SOURCES
    assert mod.SOURCE_ASSERTED in mod.OFFICIAL_SOURCES
    assert mod.SOURCE_INFERRED not in mod.OFFICIAL_SOURCES


@pytest.mark.asyncio
async def test_a_declared_domain_does_not_join_the_owned_set(monkeypatch):
    """End to end through the function the audit actually calls:
    `merchant_owned_domains` feeds build_authority_map(merchant_extra_hosts=…),
    which decides `first_party` on every cited host."""
    async def _no_inference(_mid):
        return set()

    async def _stored(_mid):
        return [
            {"domain": "brand.com", "source": mod.SOURCE_VERIFIED,
             "verification_status": "verified", "liveness_status": "live"},
            {"domain": "retailer.com", "source": mod.SOURCE_DECLARED,
             "verification_status": "pending", "liveness_status": "unchecked"},
        ]

    monkeypatch.setattr(svc, "_inferred_merchant_hosts", _no_inference)
    monkeypatch.setattr(mod, "list_official_domains", _stored)

    owned = await svc.merchant_owned_domains("m1")

    assert "brand.com" in owned
    assert "retailer.com" not in owned, (
        "a DECLARED domain widened the set that decides first_party"
    )


# ---------------------------------------------------------------------
# The cross-tenant guard
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_domain_another_merchant_PROVED_cannot_be_declared(monkeypatch):
    """Declaration is cheap and unproven, so without this it is a way to attach
    a rival's verified storefront to your own audit."""
    async def _owner(_domain):
        return "someone-else"

    wrote = []

    async def _upsert(**kw):
        wrote.append(kw)
        return True

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _owner)
    monkeypatch.setattr(mod, "upsert_official_domain", _upsert)

    out = await svc.declare_official_domain("m1", "rival.com")

    assert out["status"] == svc.DECLARE_TAKEN
    assert not wrote, "the write happened despite the refusal"


@pytest.mark.asyncio
async def test_a_lookup_failure_refuses_rather_than_grants(monkeypatch):
    """Fail closed: if we cannot tell whether someone else proved this domain,
    the answer is no. The alternative writes an unproven row on a DB blip."""
    async def _boom(_domain):
        raise RuntimeError("db down")

    wrote = []

    async def _upsert(**kw):
        wrote.append(kw)
        return True

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _boom)
    monkeypatch.setattr(mod, "upsert_official_domain", _upsert)

    out = await svc.declare_official_domain("m1", "rival.com")

    assert out["status"] == svc.DECLARE_TAKEN
    assert not wrote


@pytest.mark.asyncio
async def test_declaring_a_domain_you_already_proved_does_not_downgrade_it(
    monkeypatch,
):
    """A verified row must not be overwritten with an unproven one."""
    async def _owner(_domain):
        return "m1"

    async def _stored(_mid):
        return [{"domain": "brand.com", "source": mod.SOURCE_VERIFIED,
                 "verification_status": "verified"}]

    wrote = []

    async def _upsert(**kw):
        wrote.append(kw)
        return True

    async def _not_proven_elsewhere(_domain, _mid):
        return False

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _owner)
    monkeypatch.setattr(mod, "domain_is_proven_by_other_merchant",
                        _not_proven_elsewhere)
    monkeypatch.setattr(mod, "list_official_domains", _stored)
    monkeypatch.setattr(mod, "upsert_official_domain", _upsert)

    out = await svc.declare_official_domain("m1", "brand.com")

    assert out["status"] == svc.DECLARE_ALREADY_PROVEN
    assert not wrote, "a proven row was rewritten as declared"


# ---------------------------------------------------------------------
# What it writes
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declaration_is_written_pending_and_never_verified(monkeypatch):
    """`record_official_domain` hardcodes verification_status=VERIFIED because
    both of ITS sources mean control was proven. This one does not, and stamping
    it verified would make an unproven row indistinguishable from a proven one
    in every later read."""
    async def _owner(_domain):
        return None

    async def _stored(_mid):
        return []

    wrote = {}

    async def _upsert(**kw):
        wrote.update(kw)
        return True

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _owner)
    monkeypatch.setattr(mod, "list_official_domains", _stored)
    monkeypatch.setattr(mod, "upsert_official_domain", _upsert)

    out = await svc.declare_official_domain("m1", "  HTTPS://WWW.Anua.US/shop  ")

    assert out["status"] == svc.DECLARE_OK
    assert out["counts_toward_official_set"] is False
    assert wrote["source"] == mod.SOURCE_DECLARED
    assert wrote["verification_status"] == mod.VERIFICATION_PENDING
    assert wrote["verification_status"] != mod.VERIFICATION_VERIFIED
    # Normalized on the way in, like every other host in this table.
    assert wrote["domain"] == "anua.us"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["", "   ", "not a host", "localhost", "10.0.0.1", "com", None],
)
async def test_junk_is_refused_before_any_lookup(monkeypatch, bad):
    called = []

    async def _owner(_domain):
        called.append(_domain)
        return None

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _owner)

    out = await svc.declare_official_domain("m1", bad)

    assert out["status"] == svc.DECLARE_INVALID_HOST
    assert not called, "a malformed host reached the ownership lookup"


# ---------------------------------------------------------------------
# The blocker every mocked test missed
# ---------------------------------------------------------------------


def test_every_valid_source_is_permitted_by_the_CHECK_constraint():
    """THE GUARD THAT WOULD HAVE CAUGHT IT.

    `SOURCE_DECLARED` was added to VALID_SOURCES and to nothing else. The CHECK
    constraint gating the write is defined in THREE places — the SQLAlchemy
    model, the DDL backstop, and the migration — and none of them knew the new
    value, so every declared write was rejected. `upsert_official_domain` is
    best-effort and swallowed the violation, so the route answered 422 "domain
    must be a valid public hostname": the feature was 100% inert AND blamed the
    merchant's perfectly good hostname for it.

    Every write test monkeypatched `upsert_official_domain`, so the constraint
    was never reached and CI was fully green on a feature that could not write a
    row.
    """
    import re
    from pathlib import Path

    import db.merchant_official_domains as m

    src = Path(m.__file__).read_text(encoding="utf-8")
    # Only CHECK-constraint definitions. A bare `source IN (...)` also appears
    # in ordinary queries (the cross-tenant proof lookup), and matching those
    # made this guard fail on a query that is not a constraint at all — it
    # caught its own imprecision the moment such a query was added.
    checks = re.findall(
        r"(?:CheckConstraint\(\s*\n\s*[\"']|CHECK\s*\()\s*source IN \(([^)]*)\)",
        src,
    )
    assert len(checks) >= 2, (
        f"expected the model AND the DDL backstop to constrain source, "
        f"found {len(checks)}"
    )

    for clause in checks:
        permitted = {v.strip().strip("'\"") for v in clause.split(",")}
        missing = m.VALID_SOURCES - permitted
        assert not missing, (
            f"VALID_SOURCES contains {sorted(missing)} which the CHECK "
            f"constraint rejects — every write of that source fails and is "
            f"swallowed. Clause: source IN ({clause})"
        )


@pytest.mark.asyncio
async def test_a_declaration_reaches_the_real_table():
    """No monkeypatch on the writer. This is the test whose absence let the
    constraint bug ship."""
    from db.database import database
    from db import merchant_official_domains as m

    await database.connect()
    try:
        await m.ensure_merchant_official_domains_table()
        ok = await m.upsert_official_domain(
            merchant_id="test-declare-merchant",
            domain="declared-example.com",
            source=m.SOURCE_DECLARED,
            verification_status=m.VERIFICATION_PENDING,
        )
        assert ok is True, (
            "a declared row could not be written to the real table — the "
            "source CHECK constraint almost certainly rejects it"
        )
        rows = {
            r["domain"]: r
            for r in (await m.list_official_domains("test-declare-merchant") or [])
        }
        assert rows["declared-example.com"]["source"] == m.SOURCE_DECLARED
        assert (
            rows["declared-example.com"]["verification_status"]
            == m.VERIFICATION_PENDING
        )
    finally:
        await database.disconnect()


# ---------------------------------------------------------------------
# Second review: the leaks a declared row opened elsewhere
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_domain_is_not_recorded_in_the_audit_basis(monkeypatch):
    """`record_audit_basis` filtered only on liveness, so it snapshotted the
    STORED set rather than the USED set. Its own comment says "the set recorded
    must be the set used". `official_domains` is a COMPARABILITY key on an
    INSERT-ONLY table, so one declaration permanently recorded a false claim
    about which domains a run measured AND made that run non-comparable with
    every prior one while moving no number.

    ASSERTED ON THE RECORDED VALUE. The first version of this test checked that
    the string "OFFICIAL_SOURCES" appeared in the function's source — which the
    import line satisfies on its own, so deleting the filter left it green.
    """
    import db.merchant_official_domains as real_mod
    import services.audit_evidence_builder as aeb

    async def _rows(_mid):
        return [
            {"domain": "brand.com", "source": real_mod.SOURCE_VERIFIED,
             "liveness_status": "live"},
            {"domain": "second.com", "source": real_mod.SOURCE_ASSERTED,
             "liveness_status": "unchecked"},
            {"domain": "retailer.com", "source": real_mod.SOURCE_DECLARED,
             "liveness_status": "unchecked"},
            # Inference is NOT an official SOURCE but its hosts ARE used, so
            # they must stay in the basis. Allowlisting OFFICIAL_SOURCES here
            # dropped them and broke an existing basis test — the recorded set
            # has to mirror what merchant_owned_domains actually returns.
            {"domain": "inferred.com", "source": real_mod.SOURCE_INFERRED,
             "liveness_status": "unchecked"},
            {"domain": "gone.com", "source": real_mod.SOURCE_VERIFIED,
             "liveness_status": "dead"},
        ]

    monkeypatch.setattr(real_mod, "list_official_domains", _rows)

    basis = await aeb.record_audit_basis(
        audit_run_id="r1",
        brand_report={"brand_rollup": {}},
        merchant_id="m1",
        persist=False,
    )

    recorded = sorted((basis or {}).get("official_domains") or [])
    assert recorded == ["brand.com", "inferred.com", "second.com"], (
        f"the basis recorded {recorded}; a DECLARED domain must not appear as "
        f"one the run measured, and a dead one must not either"
    )


def test_the_liveness_sweep_does_not_probe_declared_hosts():
    """A declaration is the ONLY path that puts a fully merchant-chosen host
    into an outbound GET. `probe_host_liveness` follows redirects and is gated
    only by `is_valid_public_hostname`, which checks shape and does not
    resolve — so sweeping declarations hands any merchant a blind liveness
    oracle for internal hosts, and lets one merchant's rows starve the global
    due queue (never-checked rows sort first)."""
    for sql in (mod.DUE_FOR_LIVENESS_SQL, mod.DUE_FOR_LIVENESS_FOR_MERCHANT_SQL):
        assert "source <> 'declared'" in sql, (
            "the liveness due queue admits declared rows"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    ["myshopify.com", "shopify.com", "squarespace.com", "wixsite.com", "co.uk"],
)
async def test_a_public_suffix_or_platform_host_cannot_be_declared(
    monkeypatch, host,
):
    """`myshopify.com` is not a storefront anyone owns — one tenant of it is.
    This module already keeps the suffix list for exactly this class of
    widening and the declaration path was not consulting it."""
    async def _never(*a, **k):
        raise AssertionError("reached the ownership lookup for a public suffix")

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _never)

    out = await svc.declare_official_domain("m1", host)
    assert out["status"] == svc.DECLARE_NOT_REGISTRABLE


@pytest.mark.asyncio
async def test_a_domain_another_merchant_ASSERTED_is_also_refused(monkeypatch):
    """`asserted` means control was proven too — it is simply unbound. The
    cross-tenant guard checked only `verified`, so someone else's PROVEN domain
    could still be declared. A proof is a proof for the purpose of refusing an
    unproven declaration."""
    async def _no_verified_owner(_domain):
        return None

    async def _proven_elsewhere(_domain, _mid):
        return True

    wrote = []

    async def _upsert(**kw):
        wrote.append(kw)
        return True

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain",
                        _no_verified_owner)
    monkeypatch.setattr(mod, "domain_is_proven_by_other_merchant",
                        _proven_elsewhere)
    monkeypatch.setattr(mod, "upsert_official_domain", _upsert)

    out = await svc.declare_official_domain("m1", "rival.com")

    assert out["status"] == svc.DECLARE_TAKEN
    assert not wrote


@pytest.mark.asyncio
async def test_the_proof_lookup_also_fails_closed(monkeypatch):
    async def _none(_domain):
        return None

    async def _boom(_domain, _mid):
        raise RuntimeError("db down")

    wrote = []

    async def _upsert(**kw):
        wrote.append(kw)
        return True

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _none)
    monkeypatch.setattr(mod, "domain_is_proven_by_other_merchant", _boom)
    monkeypatch.setattr(mod, "upsert_official_domain", _upsert)

    out = await svc.declare_official_domain("m1", "rival.com")

    assert out["status"] == svc.DECLARE_TAKEN
    assert not wrote


@pytest.mark.asyncio
async def test_declarations_are_capped_per_merchant(monkeypatch):
    """Each declaration is a free write that every later reader must skip, and
    unbounded rows are a denial-of-service on the readers as well as a mess."""
    async def _none(_domain):
        return None

    async def _not_proven(_domain, _mid):
        return False

    async def _stored(_mid):
        return [
            {"domain": f"d{i}.com", "source": mod.SOURCE_DECLARED,
             "verification_status": "pending"}
            for i in range(svc._MAX_DECLARED_PER_MERCHANT)
        ]

    wrote = []

    async def _upsert(**kw):
        wrote.append(kw)
        return True

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _none)
    monkeypatch.setattr(mod, "domain_is_proven_by_other_merchant", _not_proven)
    monkeypatch.setattr(mod, "list_official_domains", _stored)
    monkeypatch.setattr(mod, "upsert_official_domain", _upsert)

    out = await svc.declare_official_domain("m1", "one-too-many.com")

    assert out["status"] == svc.DECLARE_TOO_MANY
    assert not wrote
