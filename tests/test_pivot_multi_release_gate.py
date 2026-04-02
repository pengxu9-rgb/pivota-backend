from __future__ import annotations

import argparse
import json

import pytest
import requests

import scripts.pivot_multi_release_gate as module


def test_release_gate_summaries_are_source_and_semantic_class_aware() -> None:
    cases = [
        {
            "source": "shopping_agent",
            "query_semantic_class": "default",
            "pivot_rollout_mode": "shadow",
            "pass": True,
        },
        {
            "source": "shopping_agent",
            "query_semantic_class": "fragrance",
            "pivot_rollout_mode": "shadow",
            "pass": True,
        },
        {
            "source": "shopping-agent-ui",
            "query_semantic_class": "default",
            "pivot_rollout_mode": "legacy",
            "pass": False,
        },
    ]

    source_summary = module._summarize_by_source(cases)
    semantic_summary = module._summarize_by_semantic_class(cases)

    assert source_summary["shopping_agent"]["source_stage"] == "stage_1"
    assert source_summary["shopping_agent"]["ready_for_canary"] is True
    assert source_summary["shopping-agent-ui"]["failed_cases"] == 1
    assert semantic_summary["default"]["sample_count"] == 2
    assert semantic_summary["default"]["sources"]["shopping_agent"] == 1
    assert semantic_summary["default"]["sources"]["shopping-agent-ui"] == 1


def test_release_gate_can_filter_corpus_by_source() -> None:
    corpus = [
        {"case_id": "a", "source": "shopping_agent", "query": "x"},
        {"case_id": "b", "source": "shopping-agent-ui", "query": "y"},
    ]

    filtered = module._filter_corpus_by_source(corpus, ["shopping-agent-ui"])

    assert [case["case_id"] for case in filtered] == ["b"]


def test_perform_request_retries_transient_transport_error() -> None:
    class FakeResponse:
        status_code = 200

    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise requests.exceptions.ChunkedEncodingError("incomplete read")
            return FakeResponse()

    session = FakeSession()

    response, attempts = module._perform_request(
        session=session,
        base_url="https://api.example",
        headers={"Content-Type": "application/json"},
        request_payload={"ok": True},
        timeout_seconds=1.0,
        request_retries=1,
        retry_sleep_seconds=0.0,
    )

    assert response.status_code == 200
    assert attempts == 2
    assert session.calls == 2


def test_release_gate_main_records_request_failures_without_aborting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps(
            [
                {
                    "case_id": "timeout-case",
                    "source": "shopping_agent",
                    "query": "vitamin c serum",
                    "page": 1,
                    "limit": 10,
                }
            ]
        ),
        encoding="utf-8",
    )
    output_json = tmp_path / "report.json"
    output_md = tmp_path / "report.md"

    class FakeSession:
        def post(self, *_args, **_kwargs):
            raise requests.Timeout("timed out")

    monkeypatch.setattr(module.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(
        module,
        "_parse_args",
        lambda: argparse.Namespace(
            base_url="https://api.example",
            corpus=str(corpus),
            timeout_seconds=1.0,
            request_retries=1,
            retry_sleep_seconds=0.0,
            output_json=str(output_json),
            output_md=str(output_md),
            header=[],
            default_rollout_mode=None,
            source_filter=[],
        ),
    )

    exit_code = module.main()

    assert exit_code == 1
    report = json.loads(output_json.read_text(encoding="utf-8"))
    assert report["summary"]["failed_cases"] == 1
    case = report["cases"][0]
    assert case["request_failed"] is True
    assert case["request_timed_out"] is True
    assert case["request_attempts"] == 2
    assert case["http_status"] is None
