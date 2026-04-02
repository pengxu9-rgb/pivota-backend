from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.smoke_commerce_channels_signoff as module  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/v1/pivot/query"):
            return _FakeResponse(
                {
                    "total": 1,
                    "items": [
                        {
                            "merchant": {"name": "Chydan"},
                            "product": {"product_key": "prod::1", "title": "Winona Soothing Repair Serum"},
                        }
                    ],
                }
            )
        if url.endswith("/v1/catalog/connectors/shopify/webhooks"):
            return _FakeResponse({"event_id": "catalog_event_123", "status": "pending"})
        if url.endswith("/v1/catalog/sync/jobs"):
            return _FakeResponse({"job_id": "catalog_job_123", "status": "pending"})
        if url.endswith("/v1/catalog/sync/jobs/catalog_job_123"):
            return _FakeResponse({"job_id": "catalog_job_123", "status": "completed"})
        if url.endswith("/payment/internal/canary/merchants/merch_1/order-backed/execute"):
            return _FakeResponse(
                {
                    "success": True,
                    "order_id": "ORD_123",
                    "psp_used": "stripe",
                    "status": "requires_payment_method",
                    "requires_customer_action": True,
                    "payment_action": {
                        "type": "stripe_client_secret",
                        "client_secret": "pi_secret_123",
                    },
                }
            )
        if url.endswith("/merchant/analytics/readiness-state"):
            return _FakeResponse({"merchant_id": "merch_1", "execute_status": "ready"})
        raise AssertionError(f"unexpected {method} {url}")


def _build_args(tmp_path: Path, *, query: str | None = "winona soothing repair serum") -> argparse.Namespace:
    return argparse.Namespace(
        base_url="https://api.example",
        merchant_id="merch_1",
        database_url="postgresql://example/db",
        query=query,
        header=["Authorization: Bearer test"],
        internal_key="internal-test-key",
        timeout_seconds=10.0,
        sync_limit=1,
        sync_wait_seconds=1.0,
        sync_poll_interval_seconds=0.01,
        backfill_limit=1,
        backfill_sample_limit=5,
        backfill_timeout_seconds=60.0,
        payment_amount_minor=100,
        payment_currency="USD",
        payment_order_id="codex_signoff_canary",
        payment_customer_email="ops@example.com",
        payment_customer_name="Codex Signoff",
        payment_description="codex commerce signoff",
        payment_preferred_provider="stripe",
        payment_label="codex_signoff",
        output_json=str(tmp_path / "report.json"),
        output_md=str(tmp_path / "report.md"),
    )


def test_smoke_commerce_channels_signoff_runs_all_channels(monkeypatch, tmp_path: Path) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(
        module,
        "_fetch_merchant_commerce_readiness_evidence",
        lambda **kwargs: {
            "available": True,
            "reason": None,
            "readiness_state": {
                "merchant_id": "merch_1",
                "foundation_status": "ready",
                "discover_status": "ready",
                "signals_status": "ready",
                "execute_status": "ready",
            },
            "domain_statuses": {
                "foundation": "ready",
                "discover": "ready",
                "signals": "ready",
                "execute": "ready",
            },
            "blocker_counts": {
                "foundation": 0,
                "discover": 0,
                "signals": 0,
                "execute": 0,
            },
            "ledger_counts": {
                "interaction_count": 3,
                "event_count": 5,
            },
        },
    )
    monkeypatch.setattr(
        module,
        "_run_backfill_subprocess",
        lambda **kwargs: {
            "returncode": 0,
            "elapsed_ms": 12.3,
            "body": {
                "summary": {
                    "apply_stats": {"products_failed": 0},
                    "verify": {"missing_product_keys_count": 0},
                }
            },
        }
        if kwargs["mode"] == "apply"
        else {
            "returncode": 0,
            "elapsed_ms": 8.4,
            "body": {"summary": {"verify": {"missing_product_keys_count": 0}}},
        },
    )
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["overall_ok"] is True
    assert payload["summary"]["catalog_read_ok"] is True
    assert payload["summary"]["catalog_write_ok"] is True
    assert payload["summary"]["payment_order_ok"] is True
    assert payload["summary"]["readiness_state_available"] is True
    assert payload["summary"]["readiness_domain_statuses"]["signals"] == "ready"
    assert payload["ledger_counts"]["interaction_count"] == 3
    payment_step = next(step for step in payload["steps"] if step["step"] == "payment_order_backed_canary")
    assert payment_step["body"]["payment_action"]["client_secret"] == "[REDACTED]"
    step_names = [step["step"] for step in payload["steps"]]
    assert step_names == [
        "catalog_read_query",
        "catalog_webhook_ingest",
        "catalog_sync_job_create",
        "catalog_sync_job_final",
        "catalog_backfill_apply",
        "catalog_backfill_verify",
        "payment_order_backed_canary",
        "merchant_commerce_readiness_refresh",
        "merchant_commerce_readiness_snapshot",
    ]


def test_smoke_commerce_channels_signoff_can_derive_query(monkeypatch, tmp_path: Path) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(module, "_derive_query", lambda database_url, merchant_id: "derived product title")
    monkeypatch.setattr(
        module,
        "_fetch_merchant_commerce_readiness_evidence",
        lambda **kwargs: {
            "available": False,
            "reason": "missing_readiness_row",
            "readiness_state": None,
            "domain_statuses": {},
            "blocker_counts": {},
            "ledger_counts": {},
        },
    )
    monkeypatch.setattr(
        module,
        "_run_backfill_subprocess",
        lambda **kwargs: {
            "returncode": 0,
            "elapsed_ms": 1.0,
            "body": {"summary": {"apply_stats": {"products_failed": 0}, "verify": {"missing_product_keys_count": 0}}},
        },
    )
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, query=None))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["query"] == "derived product title"
    assert any(step["step"] == "merchant_commerce_readiness_refresh" for step in payload["steps"])


def test_smoke_commerce_channels_signoff_fails_when_backfill_verify_fails(monkeypatch, tmp_path: Path) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(
        module,
        "_fetch_merchant_commerce_readiness_evidence",
        lambda **kwargs: {
            "available": False,
            "reason": "missing_readiness_row",
            "readiness_state": None,
            "domain_statuses": {},
            "blocker_counts": {},
            "ledger_counts": {},
        },
    )

    def _fake_backfill(**kwargs):
        if kwargs["mode"] == "apply":
            return {
                "returncode": 0,
                "elapsed_ms": 1.0,
                "body": {"summary": {"apply_stats": {"products_failed": 0}, "verify": {"missing_product_keys_count": 0}}},
            }
        return {
            "returncode": 0,
            "elapsed_ms": 1.0,
            "body": {"summary": {"verify": {"missing_product_keys_count": 2}}},
        }

    monkeypatch.setattr(module, "_run_backfill_subprocess", _fake_backfill)
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path))

    exit_code = module.main()

    assert exit_code == 1
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["catalog_write_ok"] is False
    verify_step = next(step for step in payload["steps"] if step["step"] == "catalog_backfill_verify")
    assert verify_step["ok"] is False
    refresh_step = next(step for step in payload["steps"] if step["step"] == "merchant_commerce_readiness_refresh")
    assert refresh_step["ok"] is True


def test_smoke_commerce_channels_signoff_uses_detected_wix_platform(monkeypatch, tmp_path: Path) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(
        module,
        "_fetch_merchant_commerce_readiness_evidence",
        lambda **kwargs: {
            "available": True,
            "reason": None,
            "readiness_state": {
                "merchant_id": "merch_1",
                "primary_platform": "wix",
                "foundation_status": "ready",
                "discover_status": "ready",
                "signals_status": "ready",
                "execute_status": "ready",
            },
            "domain_statuses": {
                "foundation": "ready",
                "discover": "ready",
                "signals": "ready",
                "execute": "ready",
            },
            "blocker_counts": {
                "foundation": 0,
                "discover": 0,
                "signals": 0,
                "execute": 0,
            },
            "ledger_counts": {"interaction_count": 1},
        },
    )

    def _fake_request(method, url, **kwargs):
        fake_session.calls.append((method, url, kwargs))
        if url.endswith("/v1/pivot/query"):
            return _FakeResponse(
                {
                    "total": 1,
                    "items": [
                        {
                            "merchant": {"merchant_id": "merch_1", "primary_platform": "wix"},
                            "product": {
                                "product_key": "prod::merch_1::wix::prod_123",
                                "title": "Sports TEE quick dry",
                            },
                        }
                    ],
                }
            )
        if url.endswith("/v1/catalog/sync/jobs"):
            assert kwargs["json"]["connector"] == "wix"
            assert kwargs["json"]["platform"] == "wix"
            return _FakeResponse({"job_id": "catalog_job_123", "status": "pending"})
        if url.endswith("/v1/catalog/sync/jobs/catalog_job_123"):
            return _FakeResponse({"job_id": "catalog_job_123", "status": "completed"})
        if url.endswith("/payment/internal/canary/merchants/merch_1/order-backed/execute"):
            return _FakeResponse({"success": True, "status": "requires_payment_method"})
        if url.endswith("/merchant/analytics/readiness-state"):
            return _FakeResponse({"merchant_id": "merch_1", "execute_status": "ready"})
        raise AssertionError(f"unexpected {method} {url}")

    fake_session.request = _fake_request  # type: ignore[assignment]
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)

    backfill_calls = []

    def _fake_backfill(**kwargs):
        backfill_calls.append(kwargs)
        return {
            "returncode": 0,
            "elapsed_ms": 1.0,
            "body": {"summary": {"apply_stats": {"products_failed": 0}, "verify": {"missing_product_keys_count": 0}}},
        }

    monkeypatch.setattr(module, "_run_backfill_subprocess", _fake_backfill)
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, query="Sports TEE quick dry"))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["catalog_platform"] == "wix"
    webhook_step = next(step for step in payload["steps"] if step["step"] == "catalog_webhook_ingest")
    assert webhook_step["ok"] is True
    assert webhook_step["body"]["skipped"] is True
    assert webhook_step["body"]["platform"] == "wix"
    assert all(call["platform"] == "wix" for call in backfill_calls)
    assert not any("/v1/catalog/connectors/shopify/webhooks" in url for _, url, _ in fake_session.calls)
