from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.smoke_commerce_telemetry_canary as module  # noqa: E402


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def _request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)

    def get(self, url: str, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._request("POST", url, **kwargs)


def _args(tmp_path: Path, *, write: bool) -> argparse.Namespace:
    return argparse.Namespace(
        base_url="https://api.example",
        merchant_id="merch_canary",
        platform="cafe24",
        store_id="store_canary",
        expected_git_sha="abc123",
        merchant_jwt="jwt-secret-value",
        merchant_api_key="merchant-hmac-secret" if write else None,
        write_canary=write,
        confirm_dedicated_canary_store="store_canary" if write else None,
        run_id="run_20260831",
        amount_minor=100,
        refund_minor=25,
        currency="USD",
        poll_timeout_seconds=0.0,
        poll_interval_seconds=0.0,
        timeout_seconds=5.0,
        output_json=str(tmp_path / "report.json"),
        output_md=str(tmp_path / "report.md"),
    )


def _funnel_payload(stages: dict | None = None) -> dict:
    return {
        "event_funnel": {
            "available": True,
            "summary": {
                "events_total": 8,
                "stages": stages or {},
                "event_type_breakdown": {
                    name: 1 for name in module.EXPECTED_EVENT_TYPES
                },
            },
        }
    }


def _stores_payload(*, active: bool = True) -> dict:
    return {
        "status": "success",
        "data": {
            "stores": [
                {
                    "id": "store_canary",
                    "platform": "cafe24",
                    "status": "active" if active else "disconnected",
                    "is_active": active,
                    "is_connected": active,
                }
            ]
        },
    }


def test_build_canary_chain_has_one_stitched_interaction_and_safe_namespace(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, write=True)
    events = module._build_canary_events(args)

    assert [event["event_type"] for event in events] == list(
        module.EXPECTED_EVENT_TYPES
    )
    assert len({event["interaction_id"] for event in events}) == 1
    assert all(event["event_id"].startswith("telemetry_canary_") for event in events)
    assert events[-2]["amount_cents"] == 100
    assert events[-1]["amount_cents"] == 25
    assert not any("email" in json.dumps(event).lower() for event in events)


def test_signed_batch_uses_exact_compact_body() -> None:
    body, signature = module._signed_batch([{"event_id": "evt_1"}], "secret")

    assert body == b'{"events":[{"event_id":"evt_1"}]}'
    assert signature == hmac.new(b"secret", body, hashlib.sha256).hexdigest()


def test_signed_batch_marks_the_probe_synthetic_at_batch_level() -> None:
    body, signature = module._signed_batch(
        [{"event_id": "evt_1"}], "secret", synthetic=True
    )

    assert body == b'{"events":[{"event_id":"evt_1"}],"synthetic":true}'
    assert signature == hmac.new(b"secret", body, hashlib.sha256).hexdigest()


def test_read_only_audit_never_posts_and_redacts_jwt(tmp_path: Path) -> None:
    args = _args(tmp_path, write=False)
    session = _Session(
        [
            _Response({"status": "ok"}),
            _Response({"full_sha": "abc123456789"}),
            _Response(_stores_payload()),
            _Response(_funnel_payload()),
            _Response({"issues": []}),
        ]
    )

    report = module.run(args, session=session)

    assert report["overall_ok"] is True
    assert [method for method, _url, _kwargs in session.calls] == [
        "GET",
        "GET",
        "GET",
        "GET",
        "GET",
    ]
    assert session.calls[0][1] == "https://api.example/health"
    assert "Authorization" not in session.headers
    assert "headers" not in session.calls[0][2]
    assert "headers" not in session.calls[1][2]
    assert session.calls[2][2]["headers"] == {
        "Authorization": "Bearer jwt-secret-value"
    }
    output = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert args.merchant_jwt not in output


def test_write_canary_proves_ingest_replay_trace_and_stages(tmp_path: Path) -> None:
    args = _args(tmp_path, write=True)
    expected_stages = {stage: 1 for stage in module.EXPECTED_STAGES}
    trace = {
        "interaction": {
            "interaction_id": "int_canary",
            "merchant_id": args.merchant_id,
        },
        "events": [{"event_type": name} for name in module.EXPECTED_EVENT_TYPES],
    }
    session = _Session(
        [
            _Response({"status": "ok"}),
            _Response({"full_sha": "abc123456789"}),
            _Response(_stores_payload()),
            _Response(_funnel_payload()),
            _Response({"issues": []}),
            _Response({"accepted": 8, "duplicates": 0}),
            _Response({"accepted": 0, "duplicates": 8}),
            _Response(trace),
            _Response(_funnel_payload(expected_stages)),
        ]
    )

    report = module.run(
        args, session=session, sleep=lambda _seconds: None, monotonic=lambda: 0.0
    )

    assert report["overall_ok"] is True
    funnel_calls = [
        call
        for call in session.calls
        if call[0] == "GET" and call[1].endswith("/merchant/analytics/commerce-funnel")
    ]
    assert funnel_calls
    assert all(call[2]["params"]["surface"] == "ops_canary" for call in funnel_calls)
    post_calls = [call for call in session.calls if call[0] == "POST"]
    assert len(post_calls) == 2
    assert all("Authorization" not in call[2]["headers"] for call in post_calls)
    assert post_calls[0][2]["data"] == post_calls[1][2]["data"]
    # The probe declares itself synthetic at batch level so the ledger stamps
    # the column; the surface string alone is caller-supplied and not trusted.
    assert json.loads(post_calls[0][2]["data"])["synthetic"] is True
    assert (
        post_calls[0][2]["headers"]["X-Pivota-Signature"]
        == post_calls[1][2]["headers"]["X-Pivota-Signature"]
    )
    assert "merchant-hmac-secret" not in (tmp_path / "report.json").read_text(
        encoding="utf-8"
    )


def test_write_canary_rejects_missing_or_mismatched_dedicated_store_confirmation(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, write=True)
    args.confirm_dedicated_canary_store = "another-store"

    try:
        module.run(args, session=_Session([]))
    except ValueError as exc:
        assert "dedicated canary store" in str(exc)
    else:
        raise AssertionError("write canary accepted an unconfirmed store")


def test_missing_trace_event_fails_closed(tmp_path: Path) -> None:
    args = _args(tmp_path, write=True)
    trace = {
        "interaction": {"merchant_id": args.merchant_id},
        "events": [{"event_type": name} for name in module.EXPECTED_EVENT_TYPES[:-1]],
    }
    session = _Session(
        [
            _Response({"status": "ok"}),
            _Response({"full_sha": "abc123456789"}),
            _Response(_stores_payload()),
            _Response(_funnel_payload()),
            _Response({"issues": []}),
            _Response({"accepted": 8, "duplicates": 0}),
            _Response({"accepted": 0, "duplicates": 8}),
            _Response(trace),
            _Response(_funnel_payload({stage: 1 for stage in module.EXPECTED_STAGES})),
        ]
    )

    report = module.run(
        args, session=session, sleep=lambda _seconds: None, monotonic=lambda: 0.0
    )

    assert report["overall_ok"] is False
    trace_step = next(
        step for step in report["steps"] if step["step"] == "stitched_interaction_trace"
    )
    assert "refund.succeeded" in trace_step["detail"]


def test_sensitive_fields_are_recursively_redacted() -> None:
    value = module._redact_sensitive(
        {
            "Authorization": "Bearer secret",
            "nested": {"merchant_api_key": "mk_secret", "client_secret": "cs_secret"},
        }
    )

    assert value["Authorization"] == "[REDACTED]"
    assert value["nested"]["merchant_api_key"] == "[REDACTED]"
    assert value["nested"]["client_secret"] == "[REDACTED]"


def test_disconnected_store_blocks_all_canary_writes(tmp_path: Path) -> None:
    args = _args(tmp_path, write=True)
    session = _Session(
        [
            _Response({"status": "ok"}),
            _Response({"full_sha": "abc123456789"}),
            _Response(_stores_payload(active=False)),
            _Response(_funnel_payload()),
            _Response({"issues": []}),
        ]
    )

    report = module.run(args, session=session)

    assert report["overall_ok"] is False
    assert not [call for call in session.calls if call[0] == "POST"]
