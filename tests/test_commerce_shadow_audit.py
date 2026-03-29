from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import requests

import scripts.commerce_shadow_audit as module


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def test_commerce_shadow_audit_builds_report_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = tmp_path / "corpus.json"
    output_json = tmp_path / "audit.json"
    output_md = tmp_path / "audit.md"
    corpus.write_text(
        json.dumps(
            [
                {
                    "case_id": "fragrance-top1-match",
                    "query": "woody cologne",
                    "source": "shopping_agent",
                    "page": 1,
                    "limit": 5,
                    "semantic_class": "fragrance",
                },
                {
                    "case_id": "default-no-result",
                    "query": "vintage fig accord extrait 1987",
                    "source": "shopping-agent-ui",
                    "page": 1,
                    "limit": 5,
                    "semantic_class": "default",
                    "expected_nonempty": False,
                },
            ]
        ),
        encoding="utf-8",
    )

    def fake_post(url: str, json: dict, headers: dict, timeout: float):
        if url.endswith("/agent/shop/v1/invoke"):
            query = json["payload"]["search"]["query"]
            if query == "woody cologne":
                return _FakeResponse(
                    {
                        "products": [
                            {
                                "merchant_id": "m1",
                                "product_id": "prod_1",
                                "canonical_url": "https://example.com/products/woody-cologne",
                                "title": "Woody Cologne",
                                "price": 72.0,
                            }
                        ],
                        "metadata": {
                            "query_source": "cache_multi_intent",
                            "pivot_rollout_mode": "shadow",
                            "pivot_rollout_guard_passed": True,
                            "route_health": {"query_semantic_class": "fragrance"},
                        },
                    }
                )
            return _FakeResponse(
                {
                    "products": [],
                    "metadata": {
                        "query_source": "cache_multi_intent",
                        "pivot_rollout_mode": "shadow",
                        "pivot_rollout_guard_passed": True,
                        "route_health": {"query_semantic_class": "default"},
                    },
                }
            )
        query = json["query"]
        if query == "woody cologne":
            return _FakeResponse(
                {
                    "items": [
                        {
                            "product": {
                                "merchant_id": "m1",
                                "source_product_id": "prod_1",
                                "canonical_url": "https://example.com/products/woody-cologne",
                                "title": "Woody Cologne",
                            },
                            "offers": [
                                {
                                    "pricing": {
                                        "estimated_best_price": 72.0,
                                        "merchant_effective_price": 72.0,
                                    }
                                }
                            ],
                        }
                    ],
                    "metadata": {"query_source": "pivot_semantic_core_multi"},
                }
            )
        return _FakeResponse({"items": [], "metadata": {"query_source": "pivot_semantic_core_multi"}})

    monkeypatch.setattr(module.requests, "post", fake_post)
    args = argparse.Namespace(
        corpus=str(corpus),
        base_url=None,
        gateway_base_url="https://api.example.com",
        pivot_base_url="https://pivot.example.com",
        timeout_seconds=5.0,
        header=[],
        gateway_header=[],
        pivot_header=[],
        output_json=str(output_json),
        output_md=str(output_md),
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["summary"]["case_count"] == 2
    assert payload["summary"]["top1_matches"] == 1
    assert payload["summary"]["top1_evaluable"] == 1
    assert payload["summary"]["gateway_nonempty"] == 1
    assert payload["summary"]["pivot_nonempty"] == 1
    assert payload["summary"]["no_result_mismatch_cases"] == 0
    assert payload["summary"]["source_summary"]["shopping_agent"]["top1_match_rate"] == 1.0
    assert payload["summary"]["semantic_class_summary"]["fragrance"]["top1_match_rate"] == 1.0


def test_commerce_shadow_audit_records_timeout_as_case_level_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = tmp_path / "corpus.json"
    output_json = tmp_path / "audit.json"
    output_md = tmp_path / "audit.md"
    corpus.write_text(
        json.dumps(
            [
                {
                    "case_id": "timeout-case",
                    "query": "merino wool cardigan",
                    "source": "shopping_agent",
                    "page": 1,
                    "limit": 5,
                    "semantic_class": "default",
                }
            ]
        ),
        encoding="utf-8",
    )

    def fake_post(url: str, json: dict, headers: dict, timeout: float):
        raise requests.ReadTimeout("timed out")

    monkeypatch.setattr(module.requests, "post", fake_post)
    args = argparse.Namespace(
        corpus=str(corpus),
        base_url=None,
        gateway_base_url="https://api.example.com",
        pivot_base_url="https://pivot.example.com",
        timeout_seconds=1.0,
        header=[],
        gateway_header=[],
        pivot_header=[],
        output_json=str(output_json),
        output_md=str(output_md),
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["summary"]["gateway_timed_out_cases"] == 1
    assert payload["summary"]["pivot_timed_out_cases"] == 1
    assert payload["cases"][0]["gateway_request_timed_out"] is True
    assert payload["cases"][0]["pivot_request_timed_out"] is True
