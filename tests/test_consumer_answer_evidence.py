import hashlib
import json
import pytest
from services.consumer_answer_evidence import answer_mention, SYSTEM
from services.selection_measurement import response_observations


def run(text="Consider Anua for your routine."):
    return {"query":"best serum", "axis_metadata":{"axis":"category"}, "_provider":"chatgpt",
            "evidence_kind":"consumer_answer", "prompt_contract":"consumer_query_v1",
            "grounding_sources":[{"uri":"https://example.com/source"}],
            "answer":{"text":text,"sha256":hashlib.sha256(text.encode()).hexdigest(),
                      "complete":True,"status":"complete","provider":"chatgpt","finish_reason":"completed",
                      "model":"fixture-model","prompt_sha256":hashlib.sha256(json.dumps([SYSTEM,"best serum"],separators=(",", ":")).encode()).hexdigest()}}


@pytest.mark.parametrize("text,expected", [("Anua serum",True),("manuka serum",False),("Other serum",False),
    ("See [this product](https://anua.com/item)",False),("Anua: https://anua.com",True)])
def test_independent_prose_mention(text, expected):
    assert answer_mention(run(text), "Anua") == (expected, None)


@pytest.mark.parametrize("field,value", [("complete",False),("finish_reason","length"),("sha256","wrong"),
    ("model",None),("prompt_sha256",None),("provider","gemini"),("status","refusal")])
def test_unverifiable_answers_are_unknown(field,value):
    item=run();item["answer"][field]=value
    assert answer_mention(item,"Anua")[0] is None


def test_diagnostic_boolean_cannot_override_consumer_prose():
    item=run("Other serum");item["parsed"]={"brand_mentioned":True}
    result=response_observations([item],sku_key="sku",merchant_host="anua.com",merchant_brand="Anua")[0]
    assert result["brand_mentioned"] is False
    assert result["mention_basis"] == "cited_consumer_answer_brand_literal_v2"


def test_changed_query_invalidates_prompt_provenance():
    item=run();item['query']='Anua reviews'
    assert answer_mention(item,'Anua')[0] is None


def test_consumer_metrics_do_not_mix_in_diagnostic_denominators():
    from services.selection_measurement import selection_measurement
    rows=response_observations([run(), {
        'query':'best serum', 'axis_metadata':{'axis':'category'},
        'evidence_kind':'merchant_context_diagnostic', 'grounding_sources':[],
    }],sku_key='sku',merchant_host='anua.com',merchant_brand='Anua')
    result=selection_measurement(rows)
    assert result['observations'] == 1
    assert result['excluded_diagnostics'] == 1
    assert result['tiers']['unbranded']['brand_mentioned']['n'] == 1


def test_retained_legacy_boolean_becomes_unknown_without_mutating_report():
    from services.selection_measurement import report_observations
    old={'observation_id':'old','status':'answered','brand_mentioned':True,'source_visible':True}
    report={'per_sku_reports':[{'selection_observations':[old]}]}
    result=report_observations(report)[0]
    assert result['brand_mentioned'] is None
    assert result['source_visible'] is True
    assert old['brand_mentioned'] is True


def test_retained_consumer_answer_is_revalidated_against_body():
    from services.selection_measurement import report_observations
    rows=response_observations([run()],sku_key='sku',merchant_host='anua.com',merchant_brand='Anua')
    report={'per_sku_reports':[{'selection_observations':rows}]}
    assert report_observations(report)[0]['brand_mentioned'] is True
    rows[0]['answer_evidence']['text']='tampered'
    assert report_observations(report)[0]['brand_mentioned'] is None


def test_old_canonical_measurement_is_not_recertified_by_projection_rebuild():
    from services.selection_measurement import selection_measurement
    from services.audit_projection_builder import build_revenue_recovery_projection
    old=selection_measurement([{'observation_id':'legacy','tier':'unbranded','status':'answered','brand_mentioned':True,'source_visible':True}])
    old['version']='1'
    result=build_revenue_recovery_projection(evidence=[],actions=[],findings=[{
        'finding_type':'recovery_measurement','payload':{'selection':old},
    }])['selection']
    bucket=result['tiers']['unbranded']
    assert bucket['brand_mentioned']['n'] == 0
    assert bucket['brand_mentioned']['unknown'] == 1
    assert bucket['source_visible']['n'] == 1
    assert old['tiers']['unbranded']['brand_mentioned']['n'] == 1


def test_gateway_fixture_survives_retained_report_revalidation():
    import json
    from pathlib import Path
    from services.selection_measurement import report_observations
    item=json.loads(Path('tests/fixtures_consumer_answer_gateway.json').read_text())
    rows=response_observations([item],sku_key='fixture-sku',merchant_host='anua.com',merchant_brand='Anua')
    assert report_observations({'per_sku_reports':[{'selection_observations':rows}]})[0]['brand_mentioned'] is True


def test_completed_preamble_without_citations_is_unmeasured():
    item=run("I’ll check current U.S. availability and manufacturer information.")
    item['grounding_sources']=[]
    assert answer_mention(item, 'Anua') == (None, 'answer_sources_missing')


@pytest.mark.parametrize('uri', ['javascript:alert(1)', 'https://', 'https://[broken'])
def test_invalid_citation_does_not_qualify_answer(uri):
    item=run();item['grounding_sources']=[{'uri':uri}]
    assert answer_mention(item,'Anua')[0] is None
