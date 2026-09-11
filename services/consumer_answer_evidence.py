"""Validate retained consumer-answer evidence; never classify model diagnostics."""
import hashlib
import json
import re
from urllib.parse import urlparse
from services.brand_alias import text_mentions_brand

REQUIRED_CONTRACT = "consumer_query_openai_web_required_v2"
REQUIRED_EXECUTION = {"model":"chat-latest","tool":"web_search_preview","tool_choice":"required","max_output_tokens":900}

SYSTEM = "Answer the shopping question using live web search when useful. Give a helpful answer in ordinary prose with sources. Do not produce audit scores or diagnostic JSON."

PREDICATE = "cited_consumer_answer_brand_literal_v2"


def answer_mention(run, merchant_brand):
    if run.get("evidence_kind") != "consumer_answer" or run.get("prompt_contract") not in {"consumer_query_v1", REQUIRED_CONTRACT}:
        return None, "consumer_contract_missing"
    answer = run.get("answer")
    if not isinstance(answer, dict):
        return None, "answer_missing"
    text = answer.get("text")
    provider = run.get("_provider") or run.get("provider") or answer.get("provider")
    finishes = {"gemini": "STOP", "chatgpt": "completed", "claude": "end_turn"}
    if (answer.get("complete") is not True or answer.get("status") != "complete"
            or provider not in finishes or answer.get("provider") != provider
            or answer.get("finish_reason") != finishes[provider]):
        return None, "answer_incomplete"
    if not isinstance(text, str) or not text.strip():
        return None, "answer_missing"
    if hashlib.sha256(text.encode()).hexdigest() != answer.get("sha256"):
        return None, "answer_hash_mismatch"
    prompt_parts = [SYSTEM, run.get("query")]
    if run.get("prompt_contract") == REQUIRED_CONTRACT:
        if provider != 'chatgpt' or answer.get('execution') != REQUIRED_EXECUTION or type(answer.get('web_search_requests')) is not int or answer['web_search_requests'] < 1:
            return None, 'answer_execution_mismatch'
        prompt_parts.append(REQUIRED_EXECUTION)
    expected_prompt = hashlib.sha256(json.dumps(prompt_parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if not isinstance(answer.get("model"), str) or not answer["model"].strip() or answer.get("prompt_sha256") != expected_prompt:
        return None, "answer_provenance_missing"
    # Revalidate retained evidence too; never trust old complete=True alone.
    sources = run.get("grounding_sources") or run.get("cited_sources") or []
    def valid_source(source):
        try:
            uri = urlparse(source.get("uri", "")) if isinstance(source, dict) else None
            return uri is not None and uri.scheme in ("http", "https") and bool(uri.hostname)
        except ValueError:
            return False
    if not isinstance(sources, list) or not any(valid_source(source) for source in sources):
        return None, "answer_sources_missing"
    if not isinstance(merchant_brand, str) or not merchant_brand.strip():
        return None, "brand_missing"
    # Only the verified literal brand, not inferred vendors/domain aliases.
    # Remove Markdown link destinations and bare URLs: URL presence is a
    # separate source signal, not a mention in the prose a shopper reads.
    prose = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    prose = re.sub(r"https?://\S+", "", prose)
    return text_mentions_brand(prose.lower(), (merchant_brand.strip().lower(),)), None
