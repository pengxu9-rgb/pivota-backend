from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestBriefsV0:
    def test_clarify_max_two_questions(self, client: TestClient):
        resp = client.post(
            "/agent/v1/briefs/clarify",
            headers={"X-API-Key": "test-agent-key"},
            json={
                "raw_query": "我是油痘肌，想去闭口，预算500块。",
                "market": "CN",
                "locale": "zh-CN",
                "currency": "CNY",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["suggested_vertical"] == "beauty"
        assert isinstance(data.get("questions"), list)
        assert len(data["questions"]) <= 2

    def test_build_is_idempotent_when_key_provided(self, client: TestClient):
        payload = {
            "raw_query": "我是油痘肌，想去闭口，预算500块，给我早晚流程。",
            "market": "CN",
            "locale": "zh-CN",
            "currency": "CNY",
            "telemetry": {"session_id": "sess_test"},
        }
        r1 = client.post(
            "/agent/v1/briefs/build",
            headers={"X-API-Key": "test-agent-key", "Idempotency-Key": "idem_brief_build_1"},
            json=payload,
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["status"] == "success"
        assert d1["brief"]["schema_version"] == "0.1.0"
        assert d1["brief"]["brief_id"].startswith("brf_")

        r2 = client.post(
            "/agent/v1/briefs/build",
            headers={"X-API-Key": "test-agent-key", "Idempotency-Key": "idem_brief_build_1"},
            json=payload,
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["brief"]["brief_id"] == d1["brief"]["brief_id"]

    def test_compatibility_veto_for_impaired_barrier_and_retinol(self, client: TestClient):
        build = client.post(
            "/agent/v1/briefs/build",
            headers={"X-API-Key": "test-agent-key", "Idempotency-Key": "idem_brief_build_veto"},
            json={
                "raw_query": "我是重度敏感肌，最近换季脸颊有点泛红刺痛。",
                "market": "CN",
                "locale": "zh-CN",
                "currency": "CNY",
            },
        )
        assert build.status_code == 200
        brief = build.json()["brief"]

        resp = client.post(
            "/agent/v1/briefs/compatibility/check",
            headers={"X-API-Key": "test-agent-key"},
            json={
                "brief": brief,
                "candidate_items": [
                    {
                        "merchant_id": "merch_test",
                        "platform": "shopify",
                        "product_id": "p1",
                        "variant_id": "v1",
                        "title": "Murad Retinol Youth Renewal Serum",
                        "tags": ["retinol"],
                        "price": 520,
                        "currency": "CNY",
                        "in_stock": True,
                        "orderable": True,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert len(data["results"]) == 1
        assert data["results"][0]["fit_score"] == 0.0
        assert "veto_barrier_impaired" in (data["results"][0].get("risk_tags") or [])

