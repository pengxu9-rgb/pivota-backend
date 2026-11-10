"""Sentry never carries frame locals, and payment/rail events carry no request body (S-12).

The rail clients hold provider answers (`parsed`, `raw`) in locals before their credential stripper
runs, so a captured frame's variables could carry a network token, cryptogram or buyer data. Sentry
is off while SENTRY_DSN is unset; this pins the guard before any DSN is set:

- `sentry_sdk.init(include_local_variables=False, ...)`;
- `filter_sensitive_data` drops `vars` from every payment/card/rail frame and the request body of any
  event touching one (or posted to a payment/rail path), and leaves other frames as they were.
"""

from __future__ import annotations

import copy
import sys
import types

import config.sentry_config as sentry_config


def _init_kwargs(monkeypatch) -> dict:
    captured: dict = {}
    fake_sdk = types.ModuleType("sentry_sdk")
    fake_sdk.init = lambda **kwargs: captured.update(kwargs)  # type: ignore[attr-defined]
    fake_sdk.set_tag = lambda k, v: None  # type: ignore[attr-defined]
    fake_fastapi = types.ModuleType("sentry_sdk.integrations.fastapi")
    fake_fastapi.FastApiIntegration = lambda *a, **k: object()  # type: ignore[attr-defined]
    fake_sqla = types.ModuleType("sentry_sdk.integrations.sqlalchemy")
    fake_sqla.SqlalchemyIntegration = lambda *a, **k: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", types.ModuleType("sentry_sdk.integrations"))
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", fake_fastapi)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.sqlalchemy", fake_sqla)
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    assert sentry_config.init_sentry() is True
    return captured


def test_init_turns_off_frame_locals_and_keeps_the_scrubber_and_no_pii(monkeypatch):
    kwargs = _init_kwargs(monkeypatch)
    assert kwargs["include_local_variables"] is False
    assert kwargs["send_default_pii"] is False
    assert kwargs["before_send"] is sentry_config.filter_sensitive_data


def test_no_dsn_means_no_init(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert sentry_config.init_sentry() is False


SECRET_VARS = {"parsed": "{'networkToken': {'value': '5204731620064587', 'cryptogram': 'AOHH=='}}", "raw": "b'...'"}


def _frame(module, filename=None, **extra):
    frame = {"module": module, "filename": filename or module.replace(".", "/") + ".py",
             "function": "f", "vars": dict(SECRET_VARS)}
    frame.update(extra)
    return frame


def _event(frames, url="https://api.example/agent/v1/products/search", data='{"q": "x"}'):
    return {
        "exception": {"values": [{"type": "RuntimeError", "stacktrace": {"frames": frames}}]},
        "request": {"url": url, "method": "POST", "data": data, "headers": {"Authorization": "Bearer x"},
                    "cookies": "session=abc", "query_string": "token=abc"},
    }


def test_payment_and_rail_frames_lose_their_locals_and_the_event_loses_its_body():
    frames = [
        _frame("services.payment_orchestration.a2a_rail"),
        _frame("services.payment_orchestration.a2a_rail_worker"),
        _frame("services.payment_orchestration.visa_vdp_client"),
        _frame("services.reap_agentic_client"),
        _frame("routes.reap_webhooks"),
        _frame("routes.agent_cards"),
        _frame("services.card_issuers.reap_issuer"),
        _frame("routes.payment_routes"),
        _frame("services.psp_payment_finalizer"),
        _frame("routes.refund_api"),
        _frame("routes.agent_commerce_purchases"),
    ]
    event = sentry_config.filter_sensitive_data(_event(frames), {})
    for frame in event["exception"]["values"][0]["stacktrace"]["frames"]:
        assert "vars" not in frame, frame["module"]
    assert event["request"]["data"] == "[REDACTED]"
    assert event["request"]["cookies"] == "[REDACTED]"
    assert event["request"]["headers"]["Authorization"] == "[REDACTED]"


def test_a_frame_known_only_by_its_path_is_scrubbed_too():
    frame = {"filename": "services/payment_orchestration/a2a_rail.py",
             "abs_path": "/app/services/payment_orchestration/a2a_rail.py", "vars": dict(SECRET_VARS)}
    event = sentry_config.filter_sensitive_data(_event([frame]), {})
    assert "vars" not in event["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert event["request"]["data"] == "[REDACTED]"


def test_thread_stacks_are_scrubbed_like_exception_stacks():
    event = {"threads": {"values": [{"stacktrace": {"frames": [_frame("services.reap_webhooks")]}}]}}
    out = sentry_config.filter_sensitive_data(event, {})
    assert "vars" not in out["threads"]["values"][0]["stacktrace"]["frames"][0]


def test_a_body_posted_to_a_payment_or_rail_path_is_dropped_even_without_a_rail_frame():
    for path in ("/webhooks/reap", "/webhooks/reap/authorize", "/agent/v1/cards", "/agent/v1/purchases",
                 "/payments/confirm", "/agent/v1/checkout/sessions"):
        event = sentry_config.filter_sensitive_data(_event([_frame("routes.misc")], url=f"https://api.example{path}"), {})
        assert event["request"]["data"] == "[REDACTED]", path


def test_other_frames_and_bodies_are_left_as_they_were():
    original = _event([_frame("routes.agent_api"), _frame("services.catalog_sync_service")])
    event = sentry_config.filter_sensitive_data(copy.deepcopy(original), {})
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    assert [f["vars"] for f in frames] == [SECRET_VARS, SECRET_VARS]
    assert event["request"]["data"] == original["request"]["data"]
    # The pre-existing header/query redaction still runs.
    assert event["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert "token" not in event["request"]["query_string"]


def test_odd_shapes_do_not_raise():
    for event in ({}, {"exception": None}, {"exception": {"values": None}}, {"exception": {"values": [None]}},
                  {"exception": [{"stacktrace": {"frames": [None, "x"]}}]}, {"stacktrace": {}}):
        assert sentry_config.filter_sensitive_data(copy.deepcopy(event), {}) == event
    assert sentry_config.filter_sensitive_data(None, {}) is None
