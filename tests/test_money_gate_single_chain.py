"""
ADR-021's two-lane invariant, made executable.

The ADR asserts: "The two transact lanes are explicit … Both funnel through the
same kill-switch/caps; neither can drift from the other's policy." Until now
that was prose plus one well-factored module. These tests make drift a CI
failure:

1. STRUCTURE — the PSP charge primitive (`capture_offsession`) is invoked only
   by an allowlist: the shared gate module `services/acp_offsession_payment`
   (the ONE runtime caller), its defining module, and the manual sandbox
   conformance harness. The whole repo is scanned (not just routes/+services/),
   and importing the primitive under an alias is flagged too. A new call site
   or import (a second, gate-free charge path) fails the allowlist.
2. STRUCTURE — the kill-switch evaluator is called only from the shared gate
   module and the two known PRE-gates (session claim + lane routing), and both
   lane entrypoints import the shared executor.
3. BEHAVIOR — a kill-switch denial refuses the charge with 403 BEFORE any
   capture-lane resolution; an engaged-over-cap charge refuses; and a permitted
   non-engaged evaluation returns disengaged gates without raising.
"""
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]

# The ONE runtime module allowed to invoke the PSP charge primitive.
CHARGE_GATE_MODULE = "services/acp_offsession_payment.py"
# Full allowlist for the charge primitive: the gate module, its defining module
# (internal helpers), and the sandbox conformance harness (manual, sandbox-only,
# with its own live-key refusal — review finding on #1730 that the original
# routes/+services/-only scan missed it entirely).
CAPTURE_ALLOWLIST = {
    CHARGE_GATE_MODULE,
    "services/acp_offsession_capture.py",
    "scripts/acp_spt_sandbox_conformance.py",
}
# Pre-charge gate consultations (routing / session-claim); they decide, never charge.
KILL_SWITCH_PREGATES = {
    CHARGE_GATE_MODULE,
    "services/acp_checkout_session_service.py",
    "services/tier2_acp_lane.py",
}
# The two transact lanes ADR-021 names.
LANE_FILES = {
    "routes/agent_payment_sdk.py",          # external agents -> gateway -> this route
    "services/acp_checkout_session_service.py",  # in-chat Tier-2 session completion
}

# Whole-repo scan (a gate-free charge path under jobs/, mvp/, scripts/ or
# main.py must fail too), excluding only non-runtime trees.
SCAN_EXCLUDE_PARTS = {
    "tests", "__pycache__", ".venv", ".git", ".claude", ".pytest_cache",
    ".codex-worktrees",
}


def _py_files():
    for path in REPO_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(part in SCAN_EXCLUDE_PARTS for part in rel_parts):
            continue
        yield path


def _call_sites(symbol: str) -> set:
    """Repo-relative files containing a CALL of symbol (whole repo, non-test)."""
    pattern = re.compile(rf"\b{re.escape(symbol)}\(")
    hits = set()
    for path in _py_files():
        rel = str(path.relative_to(REPO_ROOT))
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if f"def {symbol}(" in stripped:
                continue
            if pattern.search(stripped):
                hits.add(rel)
                break
    return hits


def _symbol_bindings(symbol: str) -> set:
    """Files that BIND the symbol via an import line (catches
    `from x import capture_offsession as alias` — the alias-evasion vector the
    call-site regex cannot see, confirmed adversarially in review)."""
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    hits = set()
    for path in _py_files():
        rel = str(path.relative_to(REPO_ROOT))
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if ("import" in stripped) and pattern.search(stripped):
                hits.add(rel)
                break
    return hits


class TestSingleChargeChain:
    def test_capture_offsession_callers_are_allowlisted(self):
        callers = _call_sites("capture_offsession")
        assert callers <= CAPTURE_ALLOWLIST, (
            f"unexpected capture_offsession call site(s): {callers - CAPTURE_ALLOWLIST}. "
            "The PSP charge primitive may only be invoked by the shared gate "
            "module (services/acp_offsession_payment.py); a new call site is a "
            "charge path that skips the Tier-2 gate chain."
        )
        assert CHARGE_GATE_MODULE in callers, "the gate module stopped calling the primitive?"

    def test_capture_offsession_is_not_imported_under_an_alias(self):
        bindings = _symbol_bindings("capture_offsession")
        assert bindings <= CAPTURE_ALLOWLIST, (
            f"capture_offsession imported outside the allowlist: {bindings - CAPTURE_ALLOWLIST}. "
            "Importing the charge primitive (aliased or not) into another module "
            "is the first step of a gate-free charge path."
        )

    def test_kill_switch_is_consulted_only_by_known_gates(self):
        assert _call_sites("evaluate_tier2_charge") == KILL_SWITCH_PREGATES, (
            "evaluate_tier2_charge grew or lost a caller. Pre-gates are fine, "
            "but every CHARGE must still flow through acp_offsession_payment; "
            "update this allowlist only with that invariant re-verified."
        )

    def test_both_lanes_import_the_shared_executor(self):
        for rel in sorted(LANE_FILES):
            src = (REPO_ROOT / rel).read_text(errors="replace")
            assert "from services.acp_offsession_payment import" in src, rel
            assert "execute_acp_offsession_payment" in src, (
                f"{rel} no longer routes its charge through the shared executor"
            )


def _denying_kill_switch():
    return SimpleNamespace(
        allowed=False,
        guarded=True,
        protocol="acp",
        reason="TIER2_CHARGE_BLOCKED",
        strict=True,
        submit_payment_enabled=False,
        as_error_detail=lambda: {"error": "TIER2_CHARGE_BLOCKED"},
    )


def _permitting_kill_switch():
    return SimpleNamespace(
        allowed=True,
        guarded=True,
        protocol="acp",
        reason="allowed",
        strict=True,
        submit_payment_enabled=True,
        as_error_detail=lambda: {"error": "none"},
    )


class TestGateChainBehavior:
    def test_kill_switch_denial_refuses_before_any_capture_lane(self):
        from services import acp_offsession_payment as gate

        test_lane = MagicMock()
        live_lane = MagicMock()
        with patch(
            "services.agent_checkout_kill_switch.evaluate_tier2_charge",
            return_value=_denying_kill_switch(),
        ), patch(
            "services.agent_checkout_kill_switch.resolve_acp_test_capture", test_lane
        ), patch(
            "services.agent_checkout_kill_switch.resolve_acp_live_capture", live_lane
        ):
            with pytest.raises(HTTPException) as exc:
                gate.evaluate_acp_offsession_gates(
                    protocol_name="acp", merchant_id="m_1", amount_cents=100, order_id="o_1"
                )
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "TIER2_CHARGE_BLOCKED"}
        # Denial happens BEFORE the capture lanes are even resolved.
        test_lane.assert_not_called()
        live_lane.assert_not_called()

    def test_engaged_over_cap_refuses(self):
        from services import acp_offsession_payment as gate

        over_cap = SimpleNamespace(
            engaged=True, within_cap=False, max_cents=500,
            bypass_live_readiness=False, amount_cents=10_000, reason="engaged",
        )
        with patch(
            "services.agent_checkout_kill_switch.evaluate_tier2_charge",
            return_value=_permitting_kill_switch(),
        ), patch(
            "services.agent_checkout_kill_switch.resolve_acp_test_capture",
            return_value=over_cap,
        ):
            with pytest.raises(HTTPException) as exc:
                gate.evaluate_acp_offsession_gates(
                    protocol_name="acp", merchant_id="m_1", amount_cents=10_000, order_id="o_2"
                )
        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "TIER2_TEST_CAPTURE_OVER_CAP"

    def test_permitted_but_not_engaged_returns_disengaged_gates(self):
        from services import acp_offsession_payment as gate

        idle_test = SimpleNamespace(
            engaged=False, within_cap=True, max_cents=2000,
            bypass_live_readiness=False, amount_cents=100, reason="not_engaged",
        )
        idle_live = SimpleNamespace(
            engaged=False, within_cap=True, max_cents=500,
            allow_live=False, amount_cents=100, reason="not_engaged",
        )
        with patch(
            "services.agent_checkout_kill_switch.evaluate_tier2_charge",
            return_value=_permitting_kill_switch(),
        ), patch(
            "services.agent_checkout_kill_switch.resolve_acp_test_capture",
            return_value=idle_test,
        ), patch(
            "services.agent_checkout_kill_switch.resolve_acp_live_capture",
            return_value=idle_live,
        ):
            gates = gate.evaluate_acp_offsession_gates(
                protocol_name="acp", merchant_id="m_1", amount_cents=100, order_id="o_3"
            )
        assert gates.engaged is False
        assert gates.bypass_live_readiness is False
        assert gates.allow_live is False
