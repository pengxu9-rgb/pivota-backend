from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.smoke_real_click_order_funnel_signoff as module  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict | None = None, *, status_code: int = 200, headers: dict | None = None, text: str | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class _ScenarioSession:
    def __init__(self, responses: list[_FakeResponse], *, merchant_id: str, click_id: str, order_id: str) -> None:
        self.headers = {}
        self._responses = list(responses)
        self.calls = []
        self.merchant_id = merchant_id
        self.click_id = click_id
        self.order_id = order_id

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/api/links/resolve"):
            body = kwargs["json"]
            assert body["candidates"]["skuId"] == "var_1"
        if url.endswith("/agent/v2/commerce/checkouts"):
            body = kwargs["json"]
            expected_interaction_id = module._interaction_id_from_click(self.merchant_id, self.click_id)
            expected_cp = module._canonical_product_id(self.merchant_id, "shopify", "prod_1")
            expected_cv = module._canonical_variant_id(self.merchant_id, "shopify", "prod_1", "var_1")
            assert body["interaction_id"] == expected_interaction_id
            assert body["metadata"]["pvt_click_id"] == self.click_id
            assert body["metadata"]["pvt_product_id"] == expected_cp
            assert body["metadata"]["pvt_variant_id"] == expected_cv
            assert body["items"][0]["product_id"] == "prod_1"
            assert body["items"][0]["variant_id"] == "var_1"
        if not self._responses:
            raise AssertionError(f"unexpected request without prepared response: {method} {url}")
        return self._responses.pop(0)


def _build_args(tmp_path: Path, *, poll_timeout_seconds: float = 0.0) -> argparse.Namespace:
    return argparse.Namespace(
        base_url="https://api.example",
        merchant_id="merch_1",
        surface="ucp",
        analytics_surface=None,
        market="US",
        tool=None,
        internal_key="internal-test-key",
        agent_api_key="ak_live_" + "1" * 64,
        merchant_jwt="merchant-jwt",
        internal_header=[],
        agent_header=[],
        merchant_header=[],
        offer_id=None,
        product_id=None,
        variant_id=None,
        sku_id=None,
        brand=None,
        category=None,
        title=None,
        unit_price=None,
        currency=None,
        canonical_product_id=None,
        canonical_variant_id=None,
        quantity=1,
        prompt_cluster="hydration",
        preferred_psp=None,
        skip_impression=False,
        buyer_email="ops@example.com",
        customer_name="Ops Buyer",
        address_name="Ops Buyer",
        address_line1="1 Market St",
        address_line2="",
        city="San Francisco",
        state="CA",
        postal_code="94105",
        country="US",
        phone="",
        timeout_seconds=5.0,
        poll_timeout_seconds=poll_timeout_seconds,
        poll_interval_seconds=0.01,
        run_id="20260330T000000Z",
        output_json=str(tmp_path / "report.json"),
        output_md=str(tmp_path / "report.md"),
    )


def _happy_path_responses(*, merchant_id: str, click_id: str, order_id: str, trace_payload: dict | None = None, issues_payload: dict | None = None) -> list[_FakeResponse]:
    interaction_id = module._interaction_id_from_click(merchant_id, click_id)
    canonical_product_id = module._canonical_product_id(merchant_id, "shopify", "prod_1")
    canonical_variant_id = module._canonical_variant_id(merchant_id, "shopify", "prod_1", "var_1")
    destination_url = (
        "https://merchant.example/products/serum"
        f"?pvt_click_id={click_id}"
        "&pvt_surface=ucp"
        f"&pvt_product_id={canonical_product_id}"
        f"&pvt_variant_id={canonical_variant_id}"
        "&pvt_prompt_cluster=hydration"
    )
    if trace_payload is None:
        trace_payload = {
            "interaction": {"interaction_id": interaction_id, "merchant_id": merchant_id},
            "events": [
                {"event_type": "surface.click", "payload": {"click_id": click_id}},
                {"event_type": "checkout.created", "payload": {"checkout_id": order_id, "order_id": order_id}},
                {"event_type": "order.created", "payload": {"click_id": click_id, "order_id": order_id}},
            ],
        }
    if issues_payload is None:
        issues_payload = {"issues": []}
    return [
        _FakeResponse({"status": "ok", "build": {"git_sha": "abc123", "deployment_id": "dep_1"}}),
        _FakeResponse({"merchant_id": merchant_id, "primary_platform": "shopify", "execute_status": "ready"}),
        _FakeResponse({"merchant_id": merchant_id, "summary": {"ready_variant_count": 1, "blocked_variant_count": 0}}),
        _FakeResponse(
            {
                "merchant_id": merchant_id,
                "channel": "ucp",
                "merchant_alpha_mode": "real_merchant_alpha",
                "offers": [
                    {
                        "offer_id": f"ucp:{merchant_id}:prod_1:var_1",
                        "product_id": "prod_1",
                        "variant_id": "var_1",
                        "title": "Soothing Serum",
                        "brand": "Winona",
                        "category": "serum",
                        "price": {"amount": "24.00", "currency": "USD"},
                    }
                ],
            }
        ),
        _FakeResponse(
            {
                "summary": {
                    "indexed_exposure": 0,
                    "surfaced_exposure": 0,
                    "clicked_exposure": 0,
                    "clicked_events_total": 0,
                    "ordered_conversion": 0,
                    "refunded_orders": 0,
                    "refunded_amount": "0",
                }
            }
        ),
        _FakeResponse(
            {
                "matched": True,
                "resolved": {
                    "destinationUrl": destination_url,
                    "redirectUrl": "https://api.example/r?token=tok_123",
                    "purchaseEnabled": True,
                },
            }
        ),
        _FakeResponse({"ok": True}),
        _FakeResponse(status_code=302, headers={"location": destination_url}, text=""),
        _FakeResponse(
            {
                "checkout_id": order_id,
                "order_id": order_id,
                "payment_action": {"type": "client_secret", "client_secret": "cs_live_123"},
            }
        ),
        _FakeResponse({"checkout_id": order_id, "order_id": order_id, "payment_status": "awaiting_payment"}),
        _FakeResponse(
            {
                "summary": {
                    "indexed_exposure": 0,
                    "surfaced_exposure": 1,
                    "clicked_exposure": 1,
                    "clicked_events_total": 1,
                    "ordered_conversion": 1,
                    "refunded_orders": 0,
                    "refunded_amount": "0",
                }
            }
        ),
        _FakeResponse(trace_payload),
        _FakeResponse(issues_payload),
    ]


def test_click_order_funnel_signoff_happy_path(monkeypatch, tmp_path: Path) -> None:
    merchant_id = "merch_1"
    click_id = "clk_123"
    order_id = "ord_1"
    fake_session = _ScenarioSession(
        _happy_path_responses(merchant_id=merchant_id, click_id=click_id, order_id=order_id),
        merchant_id=merchant_id,
        click_id=click_id,
        order_id=order_id,
    )
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, poll_timeout_seconds=0.1))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["overall_ok"] is True
    assert payload["identifiers"]["click_id"] == click_id
    assert payload["identifiers"]["interaction_id"] == module._interaction_id_from_click(merchant_id, click_id)
    assert payload["identifiers"]["payment_action"]["client_secret"] == "[REDACTED]"
    assert payload["summary"]["trace_ok"] is True
    assert payload["summary"]["issues_ok"] is True
    assert payload["funnel"]["delta"]["clicked_exposure"] == 1
    assert payload["funnel"]["delta"]["ordered_conversion"] == 1
    export_step = next(step for step in payload["steps"] if step["step"] == "readiness_export_full")
    assert export_step["body"]["summary"]["offer_count"] == 1
    assert "offers" not in export_step["body"]
    checkout_step = next(step for step in payload["steps"] if step["step"] == "commerce_checkout_create")
    assert checkout_step["body"]["payment_action"]["client_secret"] == "[REDACTED]"


def test_click_order_funnel_signoff_falls_back_to_summary_export_sample(monkeypatch, tmp_path: Path) -> None:
    merchant_id = "merch_1"
    click_id = "clk_123"
    order_id = "ord_1"
    responses = _happy_path_responses(merchant_id=merchant_id, click_id=click_id, order_id=order_id)
    responses[3:5] = [
        _FakeResponse(
            {
                "status": "error",
                "code": "UPSTREAM_TIMEOUT",
            },
            status_code=502,
        ),
        _FakeResponse(
            {
                "merchant_id": merchant_id,
                "channel": "ucp",
                "merchant_alpha_mode": "real_merchant_alpha",
                "summary": {
                    "offer_count": 1,
                    "offer_ids_sample": [f"ucp:{merchant_id}:prod_1:var_1"],
                    "product_ids_sample": ["prod_1"],
                },
            }
        ),
        _FakeResponse(
            {
                "merchant_id": merchant_id,
                "channel": "ucp",
                "merchant_alpha_mode": "real_merchant_alpha",
                "products": [
                    {
                        "product_id": "prod_1",
                        "title": "Soothing Serum",
                        "brand": "Winona",
                        "category": "serum",
                        "variants": [
                            {
                                "variant_id": "var_1",
                                "title": "Default",
                                "price": {"amount": "24.00", "currency": "USD"},
                                "channel_coverage": {"ucp": "ready"},
                            }
                        ],
                    }
                ],
            }
        ),
        responses[4],
    ]
    fake_session = _ScenarioSession(
        responses,
        merchant_id=merchant_id,
        click_id=click_id,
        order_id=order_id,
    )
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, poll_timeout_seconds=0.1))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["overall_ok"] is True
    assert payload["selected_offer"]["selected_via"] == "summary_export_fallback"
    export_step = next(step for step in payload["steps"] if step["step"] == "readiness_export_full")
    assert export_step["status_code"] == 502
    summary_step = next(step for step in payload["steps"] if step["step"] == "readiness_export_summary_fallback")
    assert summary_step["ok"] is True
    report_step = next(step for step in payload["steps"] if step["step"] == "readiness_report_full_fallback")
    assert report_step["ok"] is True


def test_click_order_funnel_signoff_fails_when_critical_issue_matches_interaction(monkeypatch, tmp_path: Path) -> None:
    merchant_id = "merch_1"
    click_id = "clk_123"
    order_id = "ord_1"
    interaction_id = module._interaction_id_from_click(merchant_id, click_id)
    fake_session = _ScenarioSession(
        _happy_path_responses(
            merchant_id=merchant_id,
            click_id=click_id,
            order_id=order_id,
            issues_payload={
                "issues": [
                    {
                        "code": "TRACE_BROKEN",
                        "sample_interaction_ids": [interaction_id],
                    }
                ]
            },
        ),
        merchant_id=merchant_id,
        click_id=click_id,
        order_id=order_id,
    )
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, poll_timeout_seconds=0.0))

    exit_code = module.main()

    assert exit_code == 1
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["overall_ok"] is False
    assert payload["summary"]["issues_ok"] is False
    assert payload["summary"]["critical_issue_codes_for_interaction"] == ["TRACE_BROKEN"]


def test_click_order_funnel_signoff_retries_transient_checkout_failure(monkeypatch, tmp_path: Path) -> None:
    merchant_id = "merch_1"
    click_id = "clk_123"
    order_id = "ord_1"
    responses = _happy_path_responses(merchant_id=merchant_id, click_id=click_id, order_id=order_id)
    responses[8:10] = [
        _FakeResponse(
            {
                "status": "error",
                "detail": {
                    "error": "TEMPORARY_UNAVAILABLE",
                    "message": "Temporary database busy. Please retry shortly.",
                },
                "error": {
                    "code": "EXTERNAL_SERVICE_ERROR",
                    "message": "TEMPORARY_UNAVAILABLE",
                },
            },
            status_code=503,
        ),
        responses[8],
        responses[9],
    ]
    fake_session = _ScenarioSession(
        responses,
        merchant_id=merchant_id,
        click_id=click_id,
        order_id=order_id,
    )
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, poll_timeout_seconds=0.1))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    retry_step = next(step for step in payload["steps"] if step["step"] == "commerce_checkout_create_attempt_1")
    assert retry_step["status_code"] == 503
    checkout_step = next(step for step in payload["steps"] if step["step"] == "commerce_checkout_create")
    assert checkout_step["status_code"] == 200
    assert checkout_step["attempt"] == 2


def test_click_order_funnel_signoff_fails_when_trace_does_not_reuse_click_id(monkeypatch, tmp_path: Path) -> None:
    merchant_id = "merch_1"
    click_id = "clk_123"
    order_id = "ord_1"
    fake_session = _ScenarioSession(
        _happy_path_responses(
            merchant_id=merchant_id,
            click_id=click_id,
            order_id=order_id,
            trace_payload={
                "interaction": {"interaction_id": module._interaction_id_from_click(merchant_id, click_id), "merchant_id": merchant_id},
                "events": [
                    {"event_type": "surface.click", "payload": {"click_id": click_id}},
                    {"event_type": "checkout.created", "payload": {"checkout_id": order_id, "order_id": order_id}},
                    {"event_type": "order.created", "payload": {"click_id": "clk_other", "order_id": order_id}},
                ],
            },
        ),
        merchant_id=merchant_id,
        click_id=click_id,
        order_id=order_id,
    )
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, poll_timeout_seconds=0.0))

    exit_code = module.main()

    assert exit_code == 1
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["overall_ok"] is False
    assert payload["summary"]["trace_ok"] is False
    trace_step = next(step for step in payload["steps"] if step["step"] == "merchant_commerce_interaction_trace")
    assert trace_step["trace_verdict"]["order_payload_ok"] is False
