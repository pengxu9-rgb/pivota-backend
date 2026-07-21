from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI


_ACTIVE_STAGES = {
    "queued", "discovering", "probing", "scoring", "materializing", "verifying",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _AuditStore:
    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.next_id = 1
        self.initial_credits = 1_000_000
        self.balance: Dict[str, Any] = {
            "credits": self.initial_credits,
            "allowance_credits": self.initial_credits,
            "overage_pending_credits": 0,
            "overage_charged_credits": 0,
            "overage_blocked_until_payment": False,
            "usd_cogs_internal": Decimal("0"),
            "plan_tier": "growth",
            "purchased_credits": 0,
            "updated_at": None,
            "version": 0,
        }
        self.debit_attempts: List[Dict[str, Any]] = []
        self.debits: List[Dict[str, Any]] = []
        self.credits: List[Dict[str, Any]] = []
        self.probe_calls: List[Dict[str, Any]] = []
        self.mock_provider: Optional[str] = None
        self._debit_replays: Dict[tuple, Dict[str, Any]] = {}
        self._enqueue_lock = asyncio.Lock()
        self.force_find_miss_count = 0
        self._find_calls = 0
        self._find_barrier: Optional[asyncio.Event] = None

    def _run_id(self) -> str:
        run_id = f"run-v3-{self.next_id}"
        self.next_id += 1
        return run_id

    def _active_idempotent_run(self, idempotency_key: Optional[str]) -> Optional[str]:
        if not idempotency_key:
            return None
        for row in self.rows.values():
            if (
                row.get("idempotency_key") == idempotency_key
                and row.get("stage") in _ACTIVE_STAGES
            ):
                return row["run_id"]
        return None

    async def find_idempotent(self, *, idempotency_key: str) -> Optional[str]:
        self._find_calls += 1
        if self.force_find_miss_count and self._find_calls <= self.force_find_miss_count:
            if self._find_barrier is None:
                self._find_barrier = asyncio.Event()
            if self._find_calls == self.force_find_miss_count:
                self._find_barrier.set()
            await self._find_barrier.wait()
            return None
        return self._active_idempotent_run(idempotency_key)

    async def enqueue(self, **kwargs):
        async with self._enqueue_lock:
            existing = self._active_idempotent_run(kwargs.get("idempotency_key"))
            if existing:
                return existing, True
            run_id = self._run_id()
            row = {
                "run_id": run_id,
                "merchant_id": kwargs["merchant_id"],
                "subject_type": kwargs.get("subject_type") or "merchant",
                "stage": "queued",
                "status": "running",
                "stage_updated_at": _now(),
                "requested_at": _now(),
                "completed_at": None,
                "cancelled_at": None,
                "product_keys": list(kwargs.get("product_keys") or []),
                "verdict_labels": [],
                "visibility_score_avg": None,
                "attribution_score_avg": None,
                "category_visibility_score_avg": None,
                "audited_via_pivota_canonical": [],
                "partial_result_jsonb": deepcopy(
                    kwargs.get("request_options_jsonb") or {}
                ),
                "report_jsonb": None,
                "cost_summary_jsonb": None,
                "error_jsonb": None,
                "error_message": None,
                "idempotency_key": kwargs.get("idempotency_key"),
                "claimed_by_worker": None,
            }
            self.rows[run_id] = row
            return run_id, False

    async def claim_next_pending_run(self, *, worker_id: str):
        for row in self.rows.values():
            if row.get("stage") in _ACTIVE_STAGES:
                row["claimed_by_worker"] = worker_id
                return deepcopy(row)
        return None

    async def transition_stage(
        self,
        *,
        run_id: str,
        from_stage: str,
        to_stage: str,
        worker_id: str,
        **kwargs,
    ) -> bool:
        row = self.rows[run_id]
        if row.get("stage") != from_stage:
            return False
        row["stage"] = to_stage
        row["stage_updated_at"] = _now()
        if "error_jsonb" in kwargs:
            row["error_jsonb"] = deepcopy(kwargs["error_jsonb"])
            row["error_message"] = kwargs["error_jsonb"].get("message")
        if "cost_summary_jsonb" in kwargs:
            row["cost_summary_jsonb"] = deepcopy(kwargs["cost_summary_jsonb"])
        if to_stage in {"completed", "failed", "cancelled"}:
            row["completed_at"] = _now()
            row["status"] = "succeeded" if to_stage == "completed" else "failed"
        return True

    async def record_partial_result(
        self,
        *,
        run_id: str,
        worker_id: str,
        partial_result_jsonb: Dict[str, Any],
    ) -> bool:
        current = self.rows[run_id].setdefault("partial_result_jsonb", {})
        current.update(deepcopy(partial_result_jsonb or {}))
        return True

    async def extend_lease(self, **_kwargs) -> bool:
        return True

    async def fetch(self, *, run_id: str):
        row = self.rows.get(run_id)
        return deepcopy(row) if row else None

    async def recent(self, *, merchant_id: str, limit: int):
        rows = [
            deepcopy(row)
            for row in self.rows.values()
            if row.get("merchant_id") == merchant_id
        ]
        return rows[:limit]

    async def cancel(self, *, run_id: str) -> bool:
        row = self.rows.get(run_id)
        if not row:
            return False
        row["cancelled_at"] = _now()
        return True

    async def complete_legacy_fields(self, *, run_id: str, **kwargs) -> None:
        row = self.rows[run_id]
        row["status"] = kwargs.get("status", row.get("status"))
        row["verdict_labels"] = list(kwargs.get("verdict_labels") or [])
        row["visibility_score_avg"] = kwargs.get("visibility_score_avg")
        row["attribution_score_avg"] = kwargs.get("attribution_score_avg")
        row["category_visibility_score_avg"] = kwargs.get(
            "category_visibility_score_avg"
        )
        row["audited_via_pivota_canonical"] = list(
            kwargs.get("audited_via_pivota_canonical") or []
        )
        row["report_jsonb"] = deepcopy(kwargs.get("report_jsonb"))

    async def get_balance(self, _merchant_id: str):
        return dict(self.balance)

    async def debit(self, merchant_id, kind, amount, idempotency_key, **kwargs):
        key = (merchant_id, kind, idempotency_key)
        self.debit_attempts.append({
            "merchant_id": merchant_id,
            "kind": kind,
            "amount": int(amount),
            "idempotency_key": idempotency_key,
        })
        if key in self._debit_replays:
            return {**self._debit_replays[key], "replay": True}
        self.balance["credits"] = int(self.balance["credits"]) - int(amount)
        self.balance["version"] = int(self.balance["version"]) + 1
        result = {
            **self.balance,
            "replay": False,
            "purchased_credits_debited": 0,
        }
        self._debit_replays[key] = dict(result)
        self.debits.append({
            "merchant_id": merchant_id,
            "kind": kind,
            "amount": int(amount),
            "idempotency_key": idempotency_key,
            "usd_cogs": kwargs.get("usd_cogs"),
        })
        return result

    async def credit(self, merchant_id, kind, amount, source_event_id, **kwargs):
        self.balance["credits"] = int(self.balance["credits"]) + int(amount)
        self.balance["version"] = int(self.balance["version"]) + 1
        event = {
            "merchant_id": merchant_id,
            "kind": kind,
            "amount": int(amount),
            "source_event_id": source_event_id,
            "purchased_credits": int(kwargs.get("purchased_credits") or 0),
        }
        self.credits.append(event)
        return {**self.balance, "replay": False}

    async def aggregate_cost(self, *, audit_run_id: str):
        providers = []
        seen = set()
        for call in self.probe_calls:
            provider = str(call.get("provider") or "").lower()
            if provider and provider not in seen:
                seen.add(provider)
                providers.append({
                    "provider": provider,
                    "llm_calls": 1,
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "estimated_cost_usd": 0.01,
                })
        return {
            "audit_run_id": audit_run_id,
            "llm_calls": len(self.probe_calls),
            "providers": providers,
            "total_input_tokens": 20 * len(self.probe_calls),
            "total_output_tokens": 10 * len(self.probe_calls),
            "estimated_cost_usd": round(0.01 * len(self.probe_calls), 4),
        }


def _sku_context(sku_key: str) -> Dict[str, Any]:
    idx = str(sku_key).split("-")[-1]
    title = f"Pivota Test Serum {idx}"
    canonical_url = f"https://merchant.example/products/{sku_key}"
    return {
        "sku_key": sku_key,
        "product_key": sku_key,
        "content_key": f"content-{sku_key}",
        "product": {
            "product_key": sku_key,
            "content_key": f"content-{sku_key}",
            "title": title,
            "brand": "Pivota Test",
            "product_type": "beauty serum",
            "canonical_url": canonical_url,
            "pivota_canonical_url": f"https://pivota.example/p/{sku_key}",
            "pivota_signature_id": f"sig-{sku_key}",
            "description": (
                "A lightweight serum with reviewed ingredients, clear usage "
                "guidance, and enough PDP detail for AI answer engines to "
                "identify the exact SKU and route shoppers to checkout."
            ),
            "product_payload": {
                "facts": {"size": "30ml", "skin_type": "all"},
                "ingredients": ["water", "niacinamide", "glycerin"],
                "usage": "Apply after cleansing.",
                "watchouts": "Patch test before use.",
                "substantiation": "Dermatologist reviewed.",
            },
            "image_url": f"https://merchant.example/images/{sku_key}.jpg",
            "freshness_json": {"current": True},
            "readiness_tier": "commerce_ready",
            "truth_tier": "primary",
            "sync_status": "live",
        },
        "sku": {
            "sku_key": sku_key,
            "title": title,
            "sku": f"SKU-{idx}",
            "barcode": f"000000000{idx}",
            "visible_option_labels": ["Size"],
            "visible_attributes": {"Size": "30ml"},
        },
        "index_pipeline_state": {
            "serving_eligible": True,
            "identity_resolved": True,
            "product_group_id": f"group-{sku_key}",
        },
        "product_group_members": [{"product_group_id": f"group-{sku_key}"}],
        "all_skus": [{
            "sku_key": sku_key,
            "sku": f"SKU-{idx}",
            "barcode": f"000000000{idx}",
            "visible_option_labels": ["Size"],
        }],
        "content_key_peers": [{
            "product_key": sku_key,
            "product_group_id": f"group-{sku_key}",
            "brand": "Pivota Test",
            "title": title,
        }],
        "product_enrichment": {
            "summary_short": "Reviewed serum for hydrated skin.",
            "bullet_points": ["Hydrating", "Lightweight", "Reviewed"],
            "usage_scenarios": "Daily routine",
            "audience_tags": ["skincare"],
            "llm_safety_flags": [],
        },
        "product_quality_snapshot": {
            "content_quality_score": 92,
            "model_readiness_score": 90,
        },
        "offers": [{
            "offer_id": f"offer-{sku_key}",
            "sku_key": sku_key,
            "availability": "in_stock",
            "inventory_quantity": 20,
            "offer_mode": "merchant_checkout",
            "list_price": 29,
            "merchant_effective_price": 29,
            "estimated_best_price": 29,
            "currency": "USD",
            "price_confidence": 0.95,
            "truth_tier": "primary",
            "offer_payload": {"ship_to_market": ["US"]},
        }],
        "merchant": {
            "verification_status": "verified",
            "country": "US",
            "metadata_json": {
                "terms_url": "https://merchant.example/terms",
                "shipping_policy": "Ships in the US",
                "refund_policy": "30 day returns",
            },
        },
        "policies": [
            {"policy_type": "shipping"},
            {"policy_type": "refund"},
            {"policy_type": "terms"},
        ],
    }


async def _missing_product_keys(*, merchant_id: str, product_keys: List[str]):
    return []


def _recursive_assert_absent(value: Any, needle: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert key != needle
            _recursive_assert_absent(item, needle)
    elif isinstance(value, list):
        for item in value:
            _recursive_assert_absent(item, needle)


@pytest.fixture
def audit_harness(monkeypatch):
    from db import merchant_audit_runs as mar
    from routes import audit_runs_routes
    from services import agent_center_bd_report_service as bd
    from services import audit_run_worker as worker
    from utils import auth as auth_module

    store = _AuditStore()
    monkeypatch.setattr(
        audit_runs_routes.settings, "deepseek_api_key", "test-deepseek-key",
        raising=False,
    )
    monkeypatch.setattr(
        audit_runs_routes, "find_in_flight_by_idempotency_key",
        store.find_idempotent,
    )
    monkeypatch.setattr(
        audit_runs_routes, "enqueue_audit_run_with_replay", store.enqueue,
    )
    monkeypatch.setattr(audit_runs_routes, "fetch_audit_run_by_id", store.fetch)
    monkeypatch.setattr(audit_runs_routes, "recent_runs_for_merchant", store.recent)
    monkeypatch.setattr(audit_runs_routes, "cancel_audit_run", store.cancel)
    monkeypatch.setattr(
        audit_runs_routes, "_missing_product_keys_for_merchant",
        _missing_product_keys,
    )
    monkeypatch.setattr(audit_runs_routes, "get_balance", store.get_balance)
    monkeypatch.setattr(audit_runs_routes, "debit", store.debit)
    monkeypatch.setattr(audit_runs_routes, "credit", store.credit)

    async def _require_verified_payment_method(_merchant_id):
        return None

    monkeypatch.setattr(
        audit_runs_routes, "require_verified_payment_method",
        _require_verified_payment_method,
    )

    async def _rate_limit(_merchant_id):
        return 1

    monkeypatch.setattr(audit_runs_routes, "_check_audit_rate_limit", _rate_limit)

    # T2b readiness gate is covered by test_audit_v3_readiness_gate.py; no-op it
    # here so the end-to-end pipeline test doesn't need readiness DB fixtures.
    async def _no_readiness_gate(*_a, **_k):
        return None

    monkeypatch.setattr(
        audit_runs_routes, "_enforce_audit_readiness", _no_readiness_gate,
    )

    monkeypatch.setattr(mar, "claim_next_pending_run", store.claim_next_pending_run)
    monkeypatch.setattr(mar, "transition_stage", store.transition_stage)
    monkeypatch.setattr(mar, "record_partial_result", store.record_partial_result)
    monkeypatch.setattr(mar, "extend_lease", store.extend_lease)
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", store.fetch)
    monkeypatch.setattr(
        mar, "record_audit_run_completed", store.complete_legacy_fields,
    )

    import services.merchant_credit_balance_service as credit_service

    monkeypatch.setattr(credit_service, "credit", store.credit)

    async def _resolve_products(*, merchant_id: str, product_keys: List[str]):
        products = [
            {
                "product_key": key,
                "sku_key": key,
                "title": _sku_context(key)["product"]["title"],
                "vendor": "Pivota Test",
                "product_type": "beauty serum",
                "pdp_url": _sku_context(key)["product"]["canonical_url"],
                "url_source": "merchant_canonical_url",
            }
            for key in product_keys
        ]
        return (
            "Pivota Test",
            "https://merchant.example",
            products,
            [],
            {"shopify_connected": True},
        )

    monkeypatch.setattr(worker, "_resolve_merchant_and_products", _resolve_products)

    async def _materialize(**_kwargs):
        return {"tasks_materialized": 0, "executors_dispatched": 0}

    async def _run_verifiers(**_kwargs):
        return {"co_occurrence_verified": False, "gsc_submitted": False}

    monkeypatch.setattr(worker, "_materialize_tasks_and_executors", _materialize)
    monkeypatch.setattr(worker, "_run_verifiers", _run_verifiers)

    async def _load_sku_context(sku_key: str, merchant_id: str):
        return _sku_context(sku_key)

    async def _fetch_one_dict(_query: str, params: Dict[str, Any]):
        return await store.fetch(run_id=params["audit_run_id"])

    monkeypatch.setattr(bd, "load_sku_context", _load_sku_context)
    monkeypatch.setattr(bd, "_fetch_one_dict", _fetch_one_dict)

    async def _fake_probe(**kwargs):
        store.probe_calls.append(dict(kwargs))
        provider = str(kwargs.get("provider") or "").lower()
        if provider == "deepseek":
            return {
                "scan_mode": kwargs["scan_mode"],
                "provider": "deepseek",
                "usage": {"input_tokens": 20, "output_tokens": 8},
                "raw_runs": [{
                    "query": (kwargs.get("context") or {}).get("verify_query"),
                    "parsed": {
                        "supports_recommendation": True,
                        "misstates_facts": False,
                        "note": "Grounding supports the recommendation.",
                    },
                }],
            }
        context = kwargs.get("context") or {}
        product = context.get("product") or {}
        title = product.get("title") or "Pivota Test Serum"
        target_url = context.get("merchant_pdp_url") or "https://merchant.example"
        actual_provider = (
            store.mock_provider if provider == "gemini" and store.mock_provider
            else provider
        )
        raw_runs = []
        for query in context.get("queries") or ["best test serum"]:
            raw_runs.append({
                "query": query,
                "answer": f"{title} is cited with first-party evidence.",
                "parsed": {
                    "product_visible": True,
                    "sku_mentioned": True,
                    "correct_sku": True,
                    "authority_near_variant_found": True,
                    "evidence_text": f"{title} appears in grounded sources.",
                },
                "url_match": {
                    "target_url": target_url,
                    "in_grounding": True,
                    "llm_self_report": {
                        "sku_mentioned": True,
                        "correct_sku": True,
                        "product_visible": True,
                    },
                },
                "grounding_sources": [
                    {"uri": target_url, "title": title},
                    {
                        "uri": f"https://reviews.example/{title.replace(' ', '-')}",
                        "title": "Independent review",
                    },
                ],
                "grounding_chunks": [f"{title} exact SKU cited."],
            })
        return {
            "scan_mode": kwargs["scan_mode"],
            "provider": actual_provider,
            "model": kwargs.get("model"),
            "model_is_override": bool(kwargs.get("model_is_override")),
            "scores": {"visibility_score": 92},
            "usage": {"input_tokens": 120, "output_tokens": 60},
            "raw_runs": raw_runs,
            "findings": [],
        }

    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)

    import db.llm_probe_runs as llm_probe_runs

    monkeypatch.setattr(llm_probe_runs, "aggregate_cost_for_run", store.aggregate_cost)

    async def _persist_canonical(**_kwargs):
        return {"evidence_items_inserted": 0, "findings_inserted": 0}

    async def _build_projections(**_kwargs):
        return {"built": 0}

    async def _enqueue_verifications(**_kwargs):
        return {"enqueued": 0}

    import services.audit_evidence_builder as evidence_builder
    import services.audit_projection_builder as projection_builder
    import services.audit_verification_enqueuer as verification_enqueuer

    monkeypatch.setattr(
        evidence_builder, "persist_canonical_evidence", _persist_canonical,
    )
    monkeypatch.setattr(
        projection_builder, "build_and_persist_all_projections",
        _build_projections,
    )
    monkeypatch.setattr(
        verification_enqueuer, "enqueue_verifications_for_completed_audit",
        _enqueue_verifications,
    )

    app = FastAPI()
    app.include_router(audit_runs_routes.router)
    app.dependency_overrides[auth_module.get_current_merchant] = lambda: "merch-A"
    return store, app, worker


@pytest.mark.asyncio
async def test_v3_audit_happy_path_returns_per_sku_report(audit_harness):
    store, app, worker = audit_harness
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/audits",
            json={
                "merchant_id": "merch-A",
                "product_keys": ["pk-1", "pk-2", "pk-3"],
                "coverage_profile": "us_shopper",
                "prompts_per_sku": 40,
            },
        )
        assert created.status_code == 202, created.text
        assert await worker.process_one_audit_run() is True
        fetched = await client.get(f"/api/audits/{created.json()['run_id']}")

    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    report = body["report_jsonb"]
    assert body["stage"] == "completed"
    assert report["audit_mode"] == "per_sku"
    assert len(report["per_sku_reports"]) == 3
    for sku_report in report["per_sku_reports"]:
        assert set(sku_report["scores"]) == {
            "identity", "content_richness", "routability", "citation",
        }
        assert {"gemini", "chatgpt"}.issubset(
            set(sku_report["citation_by_provider"])
        )
    assert report["brand_rollup"]["skus_audited"] == 3
    assert report["verify_summary"]["status"] == "completed"
    provider_names = {
        item["provider"] if isinstance(item, dict) else item
        for item in report["cost_summary"]["providers"]
    }
    assert {"gemini", "chatgpt", "deepseek"}.issubset(provider_names)
    _recursive_assert_absent(body, "usd_cogs_internal")


@pytest.mark.asyncio
async def test_v3_audit_rejects_mock_fallback_and_refunds(audit_harness):
    store, app, worker = audit_harness
    store.mock_provider = "mock_fallback_no_gemini_key"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/audits",
            json={
                "merchant_id": "merch-A",
                "product_keys": ["pk-1"],
                "coverage_profile": "us_shopper",
                "prompts_per_sku": 40,
            },
        )
        assert created.status_code == 202, created.text
        assert await worker.process_one_audit_run() is True
        fetched = await client.get(f"/api/audits/{created.json()['run_id']}")

    body = fetched.json()
    assert body["stage"] == "failed"
    assert body["error_jsonb"]["code"] == "upstream_mock_fallback"
    assert store.balance["credits"] == store.initial_credits
    assert [credit["kind"] for credit in store.credits] == ["audit"]


@pytest.mark.asyncio
async def test_v3_audit_product_key_cap_50_accepts_51_rejects(audit_harness):
    _store, app, _worker = audit_harness
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post(
            "/api/audits",
            json={
                "merchant_id": "merch-A",
                "product_keys": [f"pk-{idx}" for idx in range(50)],
            },
        )
        too_many = await client.post(
            "/api/audits",
            json={
                "merchant_id": "merch-A",
                "product_keys": [f"pk-{idx}" for idx in range(51)],
            },
        )
    assert ok.status_code == 202, ok.text
    assert too_many.status_code == 422


@pytest.mark.asyncio
async def test_v3_audit_concurrent_idempotency_single_debit(audit_harness):
    store, app, _worker = audit_harness
    store.force_find_miss_count = 2
    transport = httpx.ASGITransport(app=app)
    payload = {
        "merchant_id": "merch-A",
        "product_keys": ["pk-1", "pk-2", "pk-3"],
        "coverage_profile": "us_shopper",
        "prompts_per_sku": 40,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.post("/api/audits", json=payload),
            client.post("/api/audits", json=payload),
        )

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    bodies = [first.json(), second.json()]
    assert {body["run_id"] for body in bodies} == {"run-v3-1"}
    assert sorted(body["idempotent_replay"] for body in bodies) == [False, True]
    assert len(store.rows) == 1
    assert [debit["kind"] for debit in store.debits] == ["audit"]
