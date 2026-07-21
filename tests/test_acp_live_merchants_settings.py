"""#1308 — agent_acp_live_capture_merchants must never crash settings load and
must tolerate both a bare comma list and a legacy JSON-array value.

The field is a raw `str` (parsed in a property), so pydantic-settings never tries
to JSON-decode a frozenset from the env — a bare `merch_a,merch_b` value used to
crash-loop the app. These exercise the property's parsing directly.
"""

from __future__ import annotations

from config.settings import Settings  # noqa: E402


def _merchants(raw: str) -> frozenset:
    return Settings(agent_acp_live_capture_merchants_raw=raw).agent_acp_live_capture_merchants


def test_bare_single_merchant_no_crash():
    assert _merchants("merch_efbc46b4619cfbdf") == frozenset({"merch_efbc46b4619cfbdf"})


def test_bare_comma_list():
    assert _merchants("merch_a, merch_b ,merch_c") == frozenset({"merch_a", "merch_b", "merch_c"})


def test_legacy_json_array_value():
    assert _merchants('["merch_x","merch_y"]') == frozenset({"merch_x", "merch_y"})


def test_empty_is_empty_set():
    assert _merchants("") == frozenset()


def test_malformed_json_falls_back_to_comma_split_without_crashing():
    # A "[" that isn't valid JSON must degrade to comma-split, not raise.
    out = _merchants("[oops")
    assert isinstance(out, frozenset) and out == frozenset({"[oops"})
