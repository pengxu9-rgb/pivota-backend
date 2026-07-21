from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from services.agent_governance import AgentGovernance, validate_request_compat


def test_validate_request_supports_fail_closed_keyword() -> None:
    params = AgentGovernance.validate_request.__code__.co_varnames
    assert "fail_closed" in params


async def _call_legacy_signature() -> list[str]:
    calls: list[str] = []

    class _LegacyGovernance:
        async def validate_request(self, agent_id: str) -> None:
            calls.append(agent_id)

    await validate_request_compat(_LegacyGovernance(), "agent_hotfix", fail_closed=True)
    return calls


def test_validate_request_compat_accepts_legacy_signature() -> None:
    import asyncio

    calls = asyncio.run(_call_legacy_signature())
    assert calls == ["agent_hotfix"]
