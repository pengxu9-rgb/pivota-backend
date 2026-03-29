from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

import scripts.beauty_ranking_audit as beauty_ranking_audit


@pytest.mark.asyncio
async def test_fetch_raw_external_rows_fast_mode_skips_stage_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def fake_fetch_external_seed_rows(**kwargs):
        calls.append(bool(kwargs.get("include_seed_data_text_match")))
        return {
            "rows": [],
            "query_ms": 5,
            "query_timeout": False,
            "table_missing": False,
        }

    monkeypatch.setattr(
        beauty_ranking_audit,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )

    result = await beauty_ranking_audit._fetch_raw_external_rows(
        query="gentle cleanser",
        limit=5,
        market="US",
        stage_a_timeout_seconds=0.1,
        stage_b_timeout_seconds=0.2,
        seed_fetch_mode="fast",
    )

    assert calls == [False]
    assert result["stage_b"]["executed"] is False
    assert result["raw_rows"] == []


@pytest.mark.asyncio
async def test_fetch_raw_external_rows_deep_mode_executes_stage_b_when_stage_a_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def fake_fetch_external_seed_rows(**kwargs):
        include_seed_data = bool(kwargs.get("include_seed_data_text_match"))
        calls.append(include_seed_data)
        if not include_seed_data:
            return {
                "rows": [],
                "query_ms": 5,
                "query_timeout": False,
                "table_missing": False,
            }
        return {
            "rows": [
                {
                    "id": "seed_1",
                    "external_product_id": "ext_hyaluronic",
                    "title": "Hyaluronic Acid Hydrating Serum",
                    "canonical_url": "https://example.com/products/hyaluronic-acid-hydrating-serum",
                    "destination_url": "https://example.com/products/hyaluronic-acid-hydrating-serum",
                    "domain": "example.com",
                    "price_amount": 24.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "updated_at": "2026-03-29T00:00:00Z",
                    "seed_data": {
                        "title": "Hyaluronic Acid Hydrating Serum",
                        "description": "Hydrating serum.",
                        "category": "Serum",
                        "visible_attributes": {"product_category": ["serum"], "skin_concern": ["hydrating"]},
                        "reviewed_ingredient_ids": ["hyaluronic_acid"],
                    },
                }
            ],
            "query_ms": 8,
            "query_timeout": False,
            "table_missing": False,
        }

    monkeypatch.setattr(
        beauty_ranking_audit,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )

    result = await beauty_ranking_audit._fetch_raw_external_rows(
        query="hyaluronic acid hydrating serum",
        limit=5,
        market="US",
        stage_a_timeout_seconds=0.1,
        stage_b_timeout_seconds=0.2,
        seed_fetch_mode="deep",
    )

    assert calls == [False, True]
    assert result["stage_b"]["executed"] is True
    assert len(result["raw_rows"]) == 1
    assert result["ranking_audit"]["ranked_candidates"][0]["title"] == "Hyaluronic Acid Hydrating Serum"


def test_build_report_sync_mode_uses_sqlalchemy_path_without_async_database_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "beauty_audit.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE external_product_seeds (
                  id TEXT PRIMARY KEY,
                  external_product_id TEXT,
                  market TEXT,
                  tool TEXT,
                  utm_template TEXT,
                  partner_type TEXT,
                  disclosure_text TEXT,
                  destination_url TEXT,
                  canonical_url TEXT,
                  domain TEXT,
                  title TEXT,
                  image_url TEXT,
                  price_amount REAL,
                  price_currency TEXT,
                  availability TEXT,
                  seed_data TEXT,
                  status TEXT,
                  notes TEXT,
                  created_by_employee_id TEXT,
                  attached_product_key TEXT,
                  attached_variant_id TEXT,
                  created_at TEXT,
                  updated_at TEXT
                )
                """
            )
        )
        seed_data = {
            "title": "Hyaluronic Acid Hydrating Serum",
            "description": "Hydrating serum.",
            "category": "Serum",
            "visible_attributes": {"product_category": ["serum"], "skin_concern": ["hydrating"]},
            "reviewed_ingredient_ids": ["hyaluronic_acid"],
        }
        conn.execute(
            text(
                """
                INSERT INTO external_product_seeds (
                  id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
                  destination_url, canonical_url, domain, title, image_url, price_amount, price_currency,
                  availability, seed_data, status, notes, created_by_employee_id, attached_product_key,
                  attached_variant_id, created_at, updated_at
                ) VALUES (
                  :id, :external_product_id, :market, :tool, :utm_template, :partner_type, :disclosure_text,
                  :destination_url, :canonical_url, :domain, :title, :image_url, :price_amount, :price_currency,
                  :availability, :seed_data, :status, :notes, :created_by_employee_id, :attached_product_key,
                  :attached_variant_id, :created_at, :updated_at
                )
                """
            ),
            {
                "id": "seed_1",
                "external_product_id": "ext_hyaluronic",
                "market": "US",
                "tool": None,
                "utm_template": None,
                "partner_type": None,
                "disclosure_text": None,
                "destination_url": "https://example.com/products/hyaluronic-acid-hydrating-serum",
                "canonical_url": "https://example.com/products/hyaluronic-acid-hydrating-serum",
                "domain": "example.com",
                "title": "Hyaluronic Acid Hydrating Serum",
                "image_url": None,
                "price_amount": 24.0,
                "price_currency": "USD",
                "availability": "in_stock",
                "seed_data": json.dumps(seed_data),
                "status": "active",
                "notes": None,
                "created_by_employee_id": None,
                "attached_product_key": None,
                "attached_variant_id": None,
                "created_at": "2026-03-29T00:00:00Z",
                "updated_at": "2026-03-29T00:00:00Z",
            },
        )
    engine.dispose()

    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        json.dumps([{"query": "hyaluronic acid hydrating serum", "source": "shopping_agent", "page": 1, "limit": 5}]),
        encoding="utf-8",
    )

    async def fail_connect():
        raise AssertionError("async database.connect should not be called in sync mode")

    async def fail_disconnect():
        raise AssertionError("async database.disconnect should not be called in sync mode")

    monkeypatch.setattr(
        beauty_ranking_audit,
        "database",
        SimpleNamespace(is_connected=False, connect=fail_connect, disconnect=fail_disconnect),
    )

    args = argparse.Namespace(
        corpus=str(corpus_path),
        market=None,
        limit=5,
        base_url=None,
        gateway_base_url=None,
        pivot_base_url=None,
        header=[],
        gateway_header=[],
        pivot_header=[],
        timeout_seconds=5.0,
        database_url=f"sqlite:///{db_path}",
        db_mode="sync",
        seed_fetch_mode="fast",
        seed_stage_a_timeout_seconds=0.2,
        seed_stage_b_timeout_seconds=0.3,
        output_json=None,
        output_md=None,
    )

    report = asyncio.run(beauty_ranking_audit._build_report(args))

    assert report["db_mode"] == "sync"
    assert report["seed_fetch_mode"] == "fast"
    assert report["summary"]["case_count"] == 1
    assert report["summary"]["raw_seed_available_cases"] == 1
    assert report["cases"][0]["top1_diff"]["gateway_vs_ranked"]["ranked_top1"] == "Hyaluronic Acid Hydrating Serum"
