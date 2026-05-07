"""
Phase A: pitch_draft on per-editorial-host playbook actions.

When the audit surfaces an editorial-pitch playbook AND the host has
a registered pitch_recipient, the action carries a pre-filled email
draft the merchant can one-click via mailto: (or open the submission
URL when no email exists).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def reset_caches():
    from services.audit_playbook_engine import reset_playbook_cache
    from services.cited_host_classifier import reset_registry_cache
    reset_playbook_cache()
    reset_registry_cache()
    yield
    reset_playbook_cache()
    reset_registry_cache()


def _cited(host: str, type_: str = "editorial", subtype: str = "review_site",
           applies: bool = True, times_cited: int = 1):
    """Use the REAL classification from the registry by calling
    classify_host. Otherwise pitch_recipient won't be on the entry
    we pass in."""
    from services.cited_host_classifier import classify_host
    out = classify_host(host, merchant_category="sleepwear")
    out["times_cited"] = times_cited
    # Override applies if test asks (some tests need to assert filtering).
    if applies is False:
        out["applies_to_merchant_category"] = False
    return out


def _failed_query(query: str, host: str, *, competitors: List[str] | None = None) -> Dict[str, Any]:
    return {
        "query": query,
        "top_cited_url": f"https://{host}/x",
        "top_cited_host": host,
        "host_classification": {"type": "editorial", "subtype": "review_site"},
        "competitors_named": list(competitors or []),
    }


def test_nymag_action_carries_pitch_draft_with_email_recipient():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            # We can't use the test helper because we need the REAL
            # registry-derived pitch_recipient on the entry. Use
            # classify_host (in the helper) — but the helper needs a
            # host actually in the registry to surface pitch_recipient.
            # Use nymag.com directly.
            _registry_entry_for("nymag.com"),
        ],
        failed_queries_detailed=[
            _failed_query(
                "best women's pajamas under 100",
                host="nymag.com",
                competitors=["Lunya", "Eberjey", "Hill House Home"],
            ),
        ],
        merchant_name="TestSleepwear",
        merchant_category="sleepwear",
    )
    assert len(actions) == 1
    pd = actions[0]["pitch_draft"]
    assert pd is not None
    assert pd["recipient_email"] == "tips@nymag.com"
    assert pd["recipient_note"]
    # No recipient_url field — submission-form fallbacks were dropped to
    # avoid breaking the one-click promise.
    assert "recipient_url" not in pd
    # Subject contains merchant name
    assert "TestSleepwear" in pd["subject"]
    # Body contains merchant name + competitors
    assert "TestSleepwear" in pd["body"]
    assert "Lunya" in pd["body"]
    # Body has the TODO placeholder for differentiation (merchant fills in)
    assert "TODO" in pd["body"]


def test_wirecutter_action_has_no_pitch_draft():
    """nytimes.com (Wirecutter) has only a submission_url — no email.
    We deliberately don't surface a 'pitch draft' button for these:
    a button that opens a generic form for the merchant to fill out
    breaks the one-click execution-layer promise. The action still
    emits with concrete_next_step ('submit via Wirecutter form'), but
    no pitch_draft."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_registry_entry_for("nytimes.com")],
        failed_queries_detailed=[],
        merchant_name="TestBrand",
        merchant_category="fashion",
    )
    assert len(actions) == 1
    assert actions[0]["pitch_draft"] is None


def test_unregistered_host_emits_action_without_pitch_draft():
    """Hosts not in the registry have no pitch_recipient → pitch_draft
    is None. Action still emits (the body / concrete_next_step still
    apply); just no draft button."""
    from services.audit_playbook_engine import select_playbooks
    # Unregistered editorial host:
    actions = select_playbooks(
        cited_hosts_detailed=[{
            "host": "unregistered-editorial.example",
            "times_cited": 1,
            "type": "editorial", "subtype": "review_site",
            "categories": ["sleepwear"],
            "coverage_note": "Coverage.",
            "outreach_hint": "Hint.",
            "applies_to_merchant_category": True,
            # No pitch_recipient on this entry.
        }],
        failed_queries_detailed=[],
        merchant_name="TestBrand",
        merchant_category="sleepwear",
    )
    assert len(actions) == 1
    assert actions[0]["pitch_draft"] is None


def test_retailer_action_has_no_pitch_draft():
    """Wholesale playbooks don't have pitch_template (different lever
    — vendor portal, not editorial pitch). No pitch_draft."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_registry_entry_for("nordstrom.com")],
        failed_queries_detailed=[],
        merchant_name="TestBrand",
        merchant_category="fashion",
    )
    assert len(actions) == 1
    assert actions[0]["lever"] == "wholesale_onboarding"
    assert actions[0]["pitch_draft"] is None


def test_pitch_draft_uses_competitors_from_failed_query():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_registry_entry_for("forbes.com")],
        failed_queries_detailed=[
            _failed_query(
                "best loungewear",
                host="forbes.com",
                competitors=["Eberjey", "Cuyana", "Cosabella"],
            ),
        ],
        merchant_name="TestSleepwear",
        merchant_category="sleepwear",
    )
    pd = actions[0]["pitch_draft"]
    assert pd is not None
    # Subject + body reference at least one named competitor.
    body_lower = pd["body"].lower()
    assert "eberjey" in body_lower or "cuyana" in body_lower


def test_pitch_draft_omits_emit_when_no_competitors_named():
    """Even without competitors, pitch_draft still emits — the
    template uses 'other brands in this category' as the fallback."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_registry_entry_for("nymag.com")],
        failed_queries_detailed=[],
        merchant_name="TestBrand",
        merchant_category="sleepwear",
    )
    pd = actions[0]["pitch_draft"]
    assert pd is not None
    assert "other brands in this category" in pd["body"].lower()


# Helpers


def _registry_entry_for(host: str) -> Dict[str, Any]:
    """Build a test cited_hosts_detailed entry by going through the
    real classify_host (which reads the registry) — so pitch_recipient
    is on the entry. Adds times_cited=1."""
    from services.cited_host_classifier import classify_host
    out = classify_host(host, merchant_category="sleepwear")
    out["times_cited"] = 1
    # classify_host returns the registry's `pitch_recipient` field
    # only if we add it to the schema there. Currently classify_host
    # strips fields it doesn't know about. We need to re-attach it
    # from the raw registry.
    from services.cited_host_classifier import _load_registry
    raw = _load_registry().get((host or "").lower())
    if raw and isinstance(raw, dict) and "pitch_recipient" in raw:
        out["pitch_recipient"] = raw["pitch_recipient"]
    return out
