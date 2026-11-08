"""S-29: the agent_user_ref hash gets its own key (BUYER_IDENTITY_LINK_SECRET).

Before this, with that key unset, the hash fell back to a token-signing key. A checkout token is
HMAC-SHA256(key, payload_b64) and agent_user_ref is a string the agent chooses, so a stored hash
was a valid token signature for a payload the agent picked. The shared cases (SQLite here,
Postgres in the `_postgres` twin) hold the legacy read; this file adds the dialect-free cases.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import pathlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from buyer_identity_hash_cases import *  # noqa: E402,F401,F403
from buyer_identity_hash_cases import AGENT, DEDICATED, TOKEN_KEY, USER_REF, _hmac_hex  # noqa: E402

import db.buyer_vault as buyer_vault  # noqa: E402


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _forged_payload_b64() -> str:
    body = {"v": 1, "iat": 1, "exp": 4_102_444_800, "agent_id": AGENT, "scopes": ["checkout"]}
    return _b64(json.dumps(body, separators=(",", ":")).encode())


def test_with_the_dedicated_key_a_stored_hash_is_no_token_signature(monkeypatch):
    """The oracle: an agent sends a token payload as its agent_user_ref and reads back the hash."""
    payload_b64 = _forged_payload_b64()
    signature_hex = hmac.new(TOKEN_KEY.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()

    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    assert buyer_vault.hash_agent_user_ref(payload_b64) == signature_hex  # the bug, when unset

    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)
    assert buyer_vault.hash_agent_user_ref(payload_b64) == _hmac_hex(DEDICATED, payload_b64)
    assert buyer_vault.hash_agent_user_ref(payload_b64) != signature_hex


@pytest.mark.parametrize(
    "env,key",
    [
        ({"BUYER_ACTION_TOKEN_SECRET": "a", "CHECKOUT_TOKEN_SECRET": "c", "AGENT_CHECKOUT_TOKEN_SECRET": "g"}, "a"),
        ({"CHECKOUT_TOKEN_SECRET": "c", "AGENT_CHECKOUT_TOKEN_SECRET": "g"}, "c"),
        ({"AGENT_CHECKOUT_TOKEN_SECRET": "g"}, "g"),
    ],
    ids=["action-key", "checkout-key", "agent-checkout-key"],
)
def test_without_the_dedicated_key_the_hash_is_unchanged(monkeypatch, env, key):
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    monkeypatch.delenv("CHECKOUT_TOKEN_SECRET")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(key, USER_REF)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) is None


def test_with_no_key_at_all_the_hash_is_the_local_dev_sha256(monkeypatch):
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    monkeypatch.delenv("CHECKOUT_TOKEN_SECRET")

    assert buyer_vault.hash_agent_user_ref(USER_REF) == hashlib.sha256(USER_REF.encode()).hexdigest()


async def test_without_the_dedicated_key_resolve_makes_no_query(monkeypatch):
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")

    async def no_query(*_a, **_k):
        raise AssertionError("no legacy lookup without the dedicated key")

    monkeypatch.setattr(buyer_vault.database, "fetch_one", no_query)

    assert await buyer_vault.resolve_agent_user_ref_hash(AGENT, USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)


@pytest.mark.parametrize("value", ["0", "false", "off", "OFF", "closed"])
def test_the_window_closes_on_an_off_value(monkeypatch, value):
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_READ", value)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) is None


@pytest.mark.parametrize("value", [None, "", "1", "on"])
def test_the_window_is_open_by_default(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_READ", value)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)


def test_a_blank_ref_hashes_to_nothing():
    assert buyer_vault.hash_agent_user_ref("  ") == ""
    assert buyer_vault.legacy_agent_user_ref_hash("") is None


def test_no_route_hashes_a_ref_without_the_legacy_read():
    """Every route resolves (dual-read); a bare hash call would miss legacy rows after the deploy."""
    offenders = []
    for path in sorted((REPO_ROOT / "routes").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "hash_agent_user_ref(" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []
    assert pathlib.Path(REPO_ROOT / "routes" / "agent_commerce_reap.py").exists()


# --- the missing-key report ---------------------------------------------------------------------


@pytest.fixture
def platform(monkeypatch):
    import config.platform as cp

    state = {"production": True, "deployed": True}
    monkeypatch.setattr(cp, "is_production", lambda *a, **k: state["production"])
    monkeypatch.setattr(cp, "is_deployed", lambda *a, **k: state["deployed"])
    monkeypatch.setattr(buyer_vault, "_missing_secret_reported_at", None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(buyer_vault.time, "monotonic", lambda: clock["t"])
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    return state, clock


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def records():
    cap = _Capture()
    buyer_vault.logger.addHandler(cap)
    try:
        yield cap.records
    finally:
        buyer_vault.logger.removeHandler(cap)


def _missing(records):
    return [r for r in records if "buyer_identity_hash_secret_missing" in r.getMessage()]


def test_production_without_the_key_logs_one_error_an_hour_and_keeps_hashing(platform, records):
    _state, clock = platform

    for _ in range(3):
        assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)
    clock["t"] += 3599
    buyer_vault.hash_agent_user_ref(USER_REF)
    assert [r.levelno for r in _missing(records)] == [logging.ERROR]

    clock["t"] += 2
    buyer_vault.hash_agent_user_ref(USER_REF)
    assert [r.levelno for r in _missing(records)] == [logging.ERROR, logging.ERROR]


def test_the_report_never_carries_a_key_or_a_ref(platform, records):
    buyer_vault.hash_agent_user_ref(USER_REF)
    message = _missing(records)[0].getMessage()
    for secret in (TOKEN_KEY, USER_REF, _hmac_hex(TOKEN_KEY, USER_REF)):
        assert secret not in message


def test_the_boot_report_logs_even_inside_the_hour(platform, records):
    buyer_vault.hash_agent_user_ref(USER_REF)
    assert buyer_vault.report_missing_buyer_identity_hash_secret(force=True) is True
    assert len(_missing(records)) == 2


def test_a_deployed_non_production_host_warns(platform, records):
    state, _clock = platform
    state["production"] = False

    buyer_vault.hash_agent_user_ref(USER_REF)
    assert [r.levelno for r in _missing(records)] == [logging.WARNING]


def test_a_laptop_says_nothing(platform, records):
    state, _clock = platform
    state["production"] = state["deployed"] = False

    buyer_vault.hash_agent_user_ref(USER_REF)
    assert _missing(records) == []


def test_with_the_key_set_nothing_is_reported(platform, records, monkeypatch):
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)

    buyer_vault.hash_agent_user_ref(USER_REF)
    assert buyer_vault.report_missing_buyer_identity_hash_secret(force=True) is False
    assert _missing(records) == []


def test_the_lookup_covers_every_table_keyed_on_the_hash():
    """A new owner-keyed table that the legacy read misses would lose its legacy rows on deploy."""
    import re

    from buyer_identity_hash_cases import OWNER_TABLES

    sources = list((REPO_ROOT / "db" / "migrations").glob("*.sql")) + list((REPO_ROOT / "db").glob("*.py"))
    found = set()
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\s*\)", text, re.S):
            if re.search(r"^\s*agent_user_ref_hash\b", match.group(2), re.M):
                found.add(match.group(1))
    if "buyer_identity_links" in (REPO_ROOT / "db" / "buyer_vault.py").read_text(encoding="utf-8"):
        found.add("buyer_identity_links")
    assert found == set(OWNER_TABLES) == set(buyer_vault._OWNER_KEYED_TABLES)


def test_the_app_reports_a_missing_key_at_boot():
    text = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    lifespan = text[text.index("async def app_lifespan("):]
    lifespan = lifespan[: lifespan.index("await startup_event()")]
    assert "report_missing_buyer_identity_hash_secret(force=True)" in lifespan
