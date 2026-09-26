"""Opt-in real PostgreSQL contract test, restricted to a dedicated local database.

RUN_RECOVERY_POSTGRES=1 DATABASE_URL=postgresql://.../recovery_contract_test pytest ...
No production URL is accepted. Database accessors/builders/HTTP route are real;
only merchant authentication and billing-plan lookup are substituted.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.skipif(os.getenv('RUN_RECOVERY_POSTGRES') != '1', reason='dedicated local PostgreSQL required')


@pytest.mark.asyncio
async def test_report_jsonb_to_canonical_to_projection_to_http(monkeypatch):
    url = urlparse(os.environ['DATABASE_URL'])
    assert url.hostname in {'localhost', '127.0.0.1'} and url.path == '/recovery_contract_test'
    from db.database import database
    import db.audit_evidence as ae
    import db.merchant_audit_runs as runs
    from services.audit_evidence_builder import extract_evidence_items, extract_findings
    from services.audit_projection_builder import build_and_persist_all_projections, coerce_jsonb_to_dict
    from services.revenue_recovery_report import recovery_from_report
    from services.selection_measurement import response_observations
    import routes.audit_runs_routes as routes
    import routes.merchant_audit_routes as merchant_routes
    from utils.auth import get_current_merchant

    run_id, merchant = str(uuid4()), 'recovery-contract-local'
    await database.connect()
    try:
        await runs.ensure_merchant_audit_runs_table()
        await ae.ensure_audit_evidence_tables()
        report = json.loads(Path('tests/fixtures_real_per_sku_brand_report.json').read_text())
        report['catalog_dimensions_available'] = False
        report.setdefault('per_sku_reports', [{}])[0]['selection_observations'] = response_observations([
            {'query': 'Anua dupe', 'axis_metadata': {'axis': 'dupe'}, '_provider': 'gemini',
             'parsed': {'brand_mentioned': False}, 'grounding_sources': []},
            {'query': 'best serum', 'axis_metadata': {'axis': 'category'}, '_provider': 'chatgpt',
             'raw': '__error__:timeout'},
        ], sku_key='url|contract', merchant_host='anua.com', merchant_brand='Anua')
        await database.execute(runs.merchant_audit_runs.insert().values(
            run_id=run_id, merchant_id=merchant, status='succeeded', stage='completed',
            subject_type='merchant_url', product_keys=['url|contract'], report_jsonb=report,
        ))
        stored_run = await runs.fetch_audit_run_by_id(run_id=run_id)
        stored_report = coerce_jsonb_to_dict(stored_run['report_jsonb'])
        assert stored_report == report
        for row in extract_evidence_items(stored_report):
            assert await ae.insert_evidence_item(audit_run_id=run_id, merchant_id=merchant, **row)
        for row in extract_findings(stored_report):
            assert await ae.insert_finding(audit_run_id=run_id, merchant_id=merchant, **row)
        for _ in range(2):  # Exercise insert AND unique-conflict update paths.
            result = await build_and_persist_all_projections(audit_run_id=run_id, strict=True)
            assert result['projections_failed'] == 0 and result['projections_built'] == 6
        stored = await ae.fetch_projection(audit_run_id=run_id, audience='revenue_recovery')
        expected = recovery_from_report(report, run_id=run_id)
        assert coerce_jsonb_to_dict(stored['payload_jsonb']) == expected
        assert stored['merchant_id'] == merchant

        async def balance(*args, **kwargs):
            return {'plan_tier': 'pro'}
        monkeypatch.setattr(merchant_routes, 'get_balance', balance)
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[get_current_merchant] = lambda: merchant
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(f'/api/audits/{run_id}?audience=revenue_recovery')
            assert response.status_code == 200
            assert response.json()['selection'] == expected['selection']
            assert response.json()['catalog_available'] is False
            if os.getenv('RECOVERY_HTTP_FIXTURE'):
                Path(os.environ['RECOVERY_HTTP_FIXTURE']).write_text(json.dumps(response.json(), indent=2))
            app.dependency_overrides[get_current_merchant] = lambda: 'other-merchant'
            assert (await client.get(f'/api/audits/{run_id}?audience=revenue_recovery')).status_code == 404
        async def unreadable_findings(**kwargs):
            return []
        monkeypatch.setattr(ae, 'list_findings_for_run', unreadable_findings)
        failed = await build_and_persist_all_projections(audit_run_id=run_id, strict=True)
        assert failed['projections_built'] == 0 and failed['projections_failed'] == 6
    finally:
        # Remove only this test's UUID, preserving concurrent test runs.
        for table in (ae.report_projections, ae.action_plan_items, ae.readiness_findings, ae.evidence_items):
            await database.execute(table.delete().where(table.c.audit_run_id == run_id))
        await database.execute(runs.merchant_audit_runs.delete().where(runs.merchant_audit_runs.c.run_id == run_id))
        await database.disconnect()
