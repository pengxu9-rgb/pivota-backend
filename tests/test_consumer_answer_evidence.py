import hashlib
import json
import pytest
from services.consumer_answer_evidence import answer_mention, SYSTEM
from services.selection_measurement import response_observations


def run(text="Consider Anua for your routine."):
    return {"query":"best serum", "axis_metadata":{"axis":"category"}, "_provider":"chatgpt",
            "evidence_kind":"consumer_answer", "prompt_contract":"consumer_query_v1",
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
    assert result["mention_basis"] == "complete_consumer_answer_brand_literal_v1"


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
