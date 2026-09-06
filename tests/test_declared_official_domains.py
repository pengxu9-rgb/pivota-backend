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

    monkeypatch.setattr(mod, "resolve_verified_merchant_for_domain", _owner)
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
