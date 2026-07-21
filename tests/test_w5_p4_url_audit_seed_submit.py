"""W5 P4.2 — url_audit runs auto-submit their seeded canonical URLs.

url_audit runs complete on the synthetic path and never reach _run_verifiers, so
without this hook a seed's minted Pivota canonical URL (P3) is never submitted to
Google's Indexing API. The hook fires on the synthetic completion path, reads the
seeds' STORED pivota_canonical_url from catalog_products, and batch-submits under
the Pivota credential. It self-gates on gsc_pivota_submit_enabled so it lands
INERT until the flag flips (P5).
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest


@pytest.mark.asyncio
async def test_seed_submit_fires_when_flag_on(monkeypatch):
    import services.audit_run_worker as worker
    import services.gsc_integration as gsc
    from config.settings import settings
    from db import database as db_mod

    monkeypatch.setattr(settings, "gsc_pivota_submit_enabled", True, raising=False)

    submitted: List[Dict[str, Any]] = []

    async def fake_submit(*, merchant_id, urls, audit_run_id=None):
        submitted.append(
            {"merchant_id": merchant_id, "urls": urls, "audit_run_id": audit_run_id}
        )
        return [{"status": "submitted", "url": u} for u in urls]

    async def fake_fetch_all(query):
        # The seed row(s) carrying the minted Pivota canonical URL.
        return [
            {"pivota_canonical_url": "https://agent.pivota.cc/products/sig_seed1"},
        ]

    monkeypatch.setattr(gsc, "submit_pivota_canonical_urls", fake_submit)
    monkeypatch.setattr(db_mod.database, "fetch_all", fake_fetch_all)

    await worker._submit_url_audit_seed_canonical_urls(
        merchant_id="m1",
        synthetic_products=[{"canonical_url": "https://brand.example/p1"}],
        run_id="run_1",
    )

    assert len(submitted) == 1
    assert submitted[0]["merchant_id"] == "m1"
    assert submitted[0]["audit_run_id"] == "run_1"
    assert submitted[0]["urls"] == [
        "https://agent.pivota.cc/products/sig_seed1"
    ]


@pytest.mark.asyncio
async def test_seed_submit_noop_when_flag_off(monkeypatch):
    """Inert until GSC_PIVOTA_SUBMIT_ENABLED flips: the early gate short-circuits
    before ANY DB read or submit."""
    import services.audit_run_worker as worker
    import services.gsc_integration as gsc
    from config.settings import settings
    from db import database as db_mod

    monkeypatch.setattr(settings, "gsc_pivota_submit_enabled", False, raising=False)

    fetch_calls: List[Any] = []
    submit_calls: List[Any] = []

    async def fake_fetch_all(query):
        fetch_calls.append(query)
        return []

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return []

    monkeypatch.setattr(db_mod.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gsc, "submit_pivota_canonical_urls", fake_submit)

    await worker._submit_url_audit_seed_canonical_urls(
        merchant_id="m1",
        synthetic_products=[{"canonical_url": "https://brand.example/p1"}],
        run_id="run_1",
    )

    assert fetch_calls == []   # no DB read while inert
    assert submit_calls == []  # nothing submitted


@pytest.mark.asyncio
async def test_seed_submit_noop_when_no_stored_url(monkeypatch):
    """Flag on but no seed row (intake off / guard-skipped) -> nothing to submit,
    never a fabricated URL."""
    import services.audit_run_worker as worker
    import services.gsc_integration as gsc
    from config.settings import settings
    from db import database as db_mod

    monkeypatch.setattr(settings, "gsc_pivota_submit_enabled", True, raising=False)

    submit_calls: List[Any] = []

    async def fake_fetch_all(query):
        return []  # no seeded catalog_products rows

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return []

    monkeypatch.setattr(db_mod.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gsc, "submit_pivota_canonical_urls", fake_submit)

    await worker._submit_url_audit_seed_canonical_urls(
        merchant_id="m1",
        synthetic_products=[{"canonical_url": "https://brand.example/p1"}],
        run_id="run_1",
    )

    assert submit_calls == []
