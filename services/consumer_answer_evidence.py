"""Validate retained consumer-answer evidence; never classify model diagnostics."""
import hashlib
import json
import re
from urllib.parse import urlparse
from services.brand_alias import text_mentions_brand

SYSTEM = "Answer the shopping question using live web search when useful. Give a helpful answer in ordinary prose with sources. Do not produce audit scores or diagnostic JSON."

PREDICATE = "cited_consumer_answer_brand_literal_v2"


def answer_mention(run, merchant_brand):
    if run.get("evidence_kind") != "consumer_answer" or run.get("prompt_contract") != "consumer_query_v1":
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
    expected_prompt = hashlib.sha256(json.dumps([SYSTEM, run.get("query")], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
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
