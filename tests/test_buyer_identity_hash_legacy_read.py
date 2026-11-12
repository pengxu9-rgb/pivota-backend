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
from buyer_identity_hash_cases import AGENT, DEDICATED, LEGACY_KEY, TOKEN_KEY, USER_REF, _hmac_hex  # noqa: E402

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
    monkeypatch.delenv("BUYER_IDENTITY_LINK_LEGACY_SECRET")
    assert buyer_vault.hash_agent_user_ref(payload_b64) == signature_hex  # the bug, when unset

    # The mount: the copy and the dedicated key together (the dedicated key alone is refused).
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_SECRET", TOKEN_KEY)
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
    monkeypatch.delenv("BUYER_IDENTITY_LINK_LEGACY_SECRET")
    monkeypatch.delenv("CHECKOUT_TOKEN_SECRET")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(key, USER_REF)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) is None


def test_with_no_key_at_all_the_hash_is_the_local_dev_sha256(monkeypatch):
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    monkeypatch.delenv("BUYER_IDENTITY_LINK_LEGACY_SECRET")
    monkeypatch.delenv("CHECKOUT_TOKEN_SECRET")

    assert buyer_vault.hash_agent_user_ref(USER_REF) == hashlib.sha256(USER_REF.encode()).hexdigest()


async def test_without_the_dedicated_key_resolve_makes_no_query(monkeypatch):
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    monkeypatch.delenv("BUYER_IDENTITY_LINK_LEGACY_SECRET")

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
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) == _hmac_hex(LEGACY_KEY, USER_REF)


# --- the legacy key and the token keys (S-29 P2-2) ---------------------------------------------


@pytest.mark.parametrize(
    "token_env",
    [
        {"CHECKOUT_TOKEN_SECRET": TOKEN_KEY},
        {"BUYER_ACTION_TOKEN_SECRET": "action-token-test-key", "CHECKOUT_TOKEN_SECRET": TOKEN_KEY},
        {"AGENT_CHECKOUT_TOKEN_SECRET": "agent-checkout-token-test-key"},
        {},
    ],
    ids=["checkout-key", "action-key-first", "agent-checkout-key", "no-token-key"],
)
def test_the_legacy_hash_uses_the_legacy_copy_and_never_a_token_key(monkeypatch, token_env):
    for name in ("BUYER_ACTION_TOKEN_SECRET", "CHECKOUT_TOKEN_SECRET", "AGENT_CHECKOUT_TOKEN_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in token_env.items():
        monkeypatch.setenv(name, value)

    legacy = buyer_vault.legacy_agent_user_ref_hash(USER_REF)

    assert legacy == _hmac_hex(LEGACY_KEY, USER_REF)
    for token in token_env.values():
        assert legacy != _hmac_hex(token, USER_REF)


@pytest.mark.parametrize("copy", [TOKEN_KEY, LEGACY_KEY, "a-wrong-copy-test-key"], ids=["matching", "other", "wrong"])
def test_without_the_dedicated_key_the_copy_never_changes_a_hash(monkeypatch, copy):
    """S-29b N-1: the copy mounted alone (step 2 of the rollout) writes exactly today's hash, so a
    wrong copy changes nothing; the boot check is how ops sees it is wrong."""
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_SECRET", copy)

    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) is None


@pytest.mark.parametrize(
    "equal_to",
    ["CHECKOUT_TOKEN_SECRET", "BUYER_ACTION_TOKEN_SECRET", "AGENT_CHECKOUT_TOKEN_SECRET", "BUYER_IDENTITY_LINK_LEGACY_SECRET"],
)
def test_a_dedicated_key_equal_to_a_token_key_or_the_legacy_copy_is_refused(monkeypatch, equal_to):
    """Refused = treated as unset: the hash stays on the pre-dedicated key and the legacy read is off.
    With the key equal to a token key, keeping it would keep every hash a token signature."""
    monkeypatch.setenv(equal_to, DEDICATED)
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", "  " + DEDICATED + " ")

    assert buyer_vault._dedicated_hash_secret() == ""
    # Today's write key: the first token key that is set (BUYER_ACTION, CHECKOUT, AGENT_CHECKOUT).
    first_token = DEDICATED if equal_to in ("BUYER_ACTION_TOKEN_SECRET", "CHECKOUT_TOKEN_SECRET") else TOKEN_KEY
    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(first_token, USER_REF)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) is None


@pytest.mark.parametrize("copy", [None, "", "  "], ids=["unset", "empty", "whitespace"])
def test_a_dedicated_key_without_the_copy_is_refused_while_the_window_is_open(monkeypatch, copy):
    """S-29b N-1: no buyer moves onto the dedicated key unless the legacy read can find their old
    rows. Refused = today's hash, for old and new buyers alike."""
    if copy is None:
        monkeypatch.delenv("BUYER_IDENTITY_LINK_LEGACY_SECRET")
    else:
        monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_SECRET", copy)

    assert buyer_vault._dedicated_key_problem() == "legacy_copy_missing"
    assert buyer_vault._dedicated_hash_secret() == ""
    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) is None

    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_READ", "off")  # the intended end state
    assert buyer_vault._dedicated_key_problem() is None
    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(DEDICATED, USER_REF)


@pytest.mark.parametrize("value", ["", "  ", "\t\n"], ids=["empty", "spaces", "tab-newline"])
def test_a_whitespace_only_value_counts_as_unset(monkeypatch, value):
    # The dedicated key.
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", value)
    assert buyer_vault._dedicated_hash_secret() == ""
    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)
    # The legacy copy: no copy, so a real dedicated key is now refused (the window is open).
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_SECRET", value)
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)
    assert buyer_vault._dedicated_key_problem() == "legacy_copy_missing"
    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)
    # A token key: skipped, the next one is used.
    monkeypatch.setenv("BUYER_ACTION_TOKEN_SECRET", value)
    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)


def test_a_padded_value_is_the_same_key(monkeypatch):
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", f"  {DEDICATED}\n")
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_SECRET", f"\t{LEGACY_KEY} ")

    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(DEDICATED, USER_REF)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) == _hmac_hex(LEGACY_KEY, USER_REF)


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
    monkeypatch.setattr(buyer_vault, "_reported_problem", None)
    monkeypatch.setattr(buyer_vault, "_errored_problems", set())
    clock = {"t": 1000.0}
    monkeypatch.setattr(buyer_vault.time, "monotonic", lambda: clock["t"])
    # Today's prod: neither the dedicated key nor the legacy copy is mounted.
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    monkeypatch.delenv("BUYER_IDENTITY_LINK_LEGACY_SECRET")
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


def _config(records):
    return [r for r in records if "event=buyer_identity_hash_" in r.getMessage() and "lookup_failed" not in r.getMessage()]


def test_production_without_the_key_logs_one_warning_an_hour_and_keeps_hashing(platform, records):
    """WARNING, not ERROR: the key being unset is the planned state between the deploy and the
    mount. The alerts here match log text and Sentry makes every ERROR an event, so an hourly ERROR
    per instance would page for a known state."""
    _state, clock = platform

    for _ in range(3):
        assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)
    clock["t"] += 3599
    buyer_vault.hash_agent_user_ref(USER_REF)
    assert [r.levelno for r in _missing(records)] == [logging.WARNING]

    clock["t"] += 2
    buyer_vault.hash_agent_user_ref(USER_REF)
    assert [r.levelno for r in _missing(records)] == [logging.WARNING, logging.WARNING]


@pytest.mark.parametrize(
    "equal_to,event",
    [
        ("CHECKOUT_TOKEN_SECRET", "reason=dedicated_equals_token_key"),
        ("BUYER_IDENTITY_LINK_LEGACY_SECRET", "reason=dedicated_equals_legacy_key"),
    ],
    ids=["token-key", "legacy-copy"],
)
def test_a_refused_dedicated_key_is_one_error_then_hourly_warnings(platform, records, monkeypatch, equal_to, event):
    _state, clock = platform
    monkeypatch.setenv(equal_to, DEDICATED)
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)

    assert buyer_vault.report_missing_buyer_identity_hash_secret(force=True) is True  # the boot line
    buyer_vault.hash_agent_user_ref(USER_REF)  # inside the hour: nothing more
    clock["t"] += 3601
    buyer_vault.hash_agent_user_ref(USER_REF)

    lines = _config(records)
    assert [r.levelno for r in lines] == [logging.ERROR, logging.WARNING]
    assert all(event in r.getMessage() for r in lines)
    for line in lines:
        for secret in (DEDICATED, LEGACY_KEY, TOKEN_KEY, USER_REF, _hmac_hex(DEDICATED, USER_REF)):
            assert secret not in line.getMessage()


def test_a_missing_legacy_copy_with_the_window_open_is_an_error_at_first_use(platform, records, monkeypatch):
    _state, clock = platform
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)

    for _ in range(2):
        assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)
    clock["t"] += 3601
    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(TOKEN_KEY, USER_REF)

    lines = _config(records)
    assert [r.levelno for r in lines] == [logging.ERROR, logging.WARNING]
    assert all("buyer_identity_hash_secret_refused reason=legacy_copy_missing" in r.getMessage() for r in lines)
    assert TOKEN_KEY not in lines[0].getMessage() and DEDICATED not in lines[0].getMessage()


# --- the boot copy check (S-29b N-1) -----------------------------------------------------------


def _copy_check(records):
    return [r for r in records if "buyer_identity_hash_legacy_copy_check" in r.getMessage()]


@pytest.mark.parametrize(
    "token_env,copy,answer",
    [
        ({"CHECKOUT_TOKEN_SECRET": "same-test-key"}, "same-test-key", "yes"),
        ({"CHECKOUT_TOKEN_SECRET": "same-test-key"}, " same-test-key\n", "yes"),
        ({"CHECKOUT_TOKEN_SECRET": "same-test-key"}, "a-wrong-copy-test-key", "no"),
        ({"BUYER_ACTION_TOKEN_SECRET": "action-test-key", "CHECKOUT_TOKEN_SECRET": "same-test-key"}, "same-test-key", "no"),
        ({}, "same-test-key", "no"),
    ],
    ids=["match", "padded-match", "wrong-copy", "not-the-first-token-key", "no-token-key"],
)
def test_the_boot_check_says_whether_the_copy_matches_todays_write_key(records, monkeypatch, token_env, copy, answer):
    for name in ("BUYER_ACTION_TOKEN_SECRET", "CHECKOUT_TOKEN_SECRET", "AGENT_CHECKOUT_TOKEN_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in token_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_SECRET", copy)

    assert buyer_vault.report_legacy_copy_matches_token_key() == answer
    [line] = _copy_check(records)
    assert f"legacy_copy_matches_token_key={answer}" in line.getMessage()
    assert "dedicated_mounted=yes" in line.getMessage()
    assert line.levelno == logging.WARNING
    for secret in (copy.strip(), *token_env.values(), DEDICATED):
        assert secret not in line.getMessage()


@pytest.mark.parametrize(
    "dedicated,mounted,problem",
    [
        (None, "no", None),
        ("   ", "no", None),
        (DEDICATED, "yes", None),
        (TOKEN_KEY, "yes", "dedicated_equals_token_key"),
        (LEGACY_KEY, "yes", "dedicated_equals_legacy_key"),
    ],
    ids=["unset", "blank", "set", "set-but-refused-equals-token-key", "set-but-refused-equals-copy"],
)
def test_the_boot_check_says_whether_the_dedicated_key_is_mounted(records, monkeypatch, dedicated, mounted, problem):
    """Ops reads this field at step 3 (must be no) and step 5 (must be yes), on web and worker. It says
    whether the key is SET, not whether it is usable: a set but refused key still says yes, and its
    own `buyer_identity_hash_secret_refused` line says why."""
    if dedicated is None:
        monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET")
    else:
        monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", dedicated)
    assert buyer_vault._dedicated_key_problem() == problem

    buyer_vault.report_legacy_copy_matches_token_key()
    [line] = _copy_check(records)
    assert f"dedicated_mounted={mounted}" in line.getMessage()
    for secret in (TOKEN_KEY, LEGACY_KEY, DEDICATED):
        assert secret not in line.getMessage()


def test_the_boot_check_is_silent_without_a_copy(records, monkeypatch):
    monkeypatch.delenv("BUYER_IDENTITY_LINK_LEGACY_SECRET")

    assert buyer_vault.report_legacy_copy_matches_token_key() is None
    assert _copy_check(records) == []


def test_a_missing_legacy_copy_with_the_window_closed_is_the_intended_end_state(platform, records, monkeypatch):
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_READ", "off")

    assert buyer_vault.report_missing_buyer_identity_hash_secret(force=True) is False
    assert buyer_vault.hash_agent_user_ref(USER_REF) == _hmac_hex(DEDICATED, USER_REF)
    assert _config(records) == []


def test_a_new_problem_is_reported_at_once_not_an_hour_later(platform, records, monkeypatch):
    buyer_vault.hash_agent_user_ref(USER_REF)  # dedicated missing: a WARNING
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", DEDICATED)  # same hour, a different problem
    buyer_vault.hash_agent_user_ref(USER_REF)

    assert [r.levelno for r in _config(records)] == [logging.WARNING, logging.ERROR]


def test_a_non_production_host_never_logs_an_error(platform, records, monkeypatch):
    state, _clock = platform
    state["production"] = False
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", DEDICATED)

    assert buyer_vault.report_missing_buyer_identity_hash_secret(force=True) is True
    assert [r.levelno for r in _config(records)] == [logging.WARNING]


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
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_SECRET", LEGACY_KEY)

    buyer_vault.hash_agent_user_ref(USER_REF)
    assert buyer_vault.legacy_agent_user_ref_hash(USER_REF) == _hmac_hex(LEGACY_KEY, USER_REF)
    assert buyer_vault.report_missing_buyer_identity_hash_secret(force=True) is False
    assert _config(records) == []


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
    assert "report_legacy_copy_matches_token_key()" in lifespan
