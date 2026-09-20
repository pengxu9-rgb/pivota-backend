"""Reject model self-reports that identify another brand as the audited SKU.

The raw probe remains in the audit checkpoint. Report builders consume copied
payloads so a rejected assertion cannot enter scores, actions or excerpts.
"""

from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlparse


def _mentions_brand(text: str, brand: str) -> bool:
    brand = str(brand or "").strip()
    if not brand:
        return False
    return bool(re.search(r"(?<!\w)" + re.escape(brand) + r"(?!\w)", text, re.I))


def _host(url: Any) -> str:
    try:
        text = str(url or "").strip()
        if not text:
            return ""
        return (urlparse(text if "://" in text else "https://" + text).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def guard_probe_identity(payloads: list[dict], product: dict, sku_ctx: dict) -> list[dict]:
    brands = list(dict.fromkeys(
        str(value).strip() for value in (product.get("brand"), product.get("vendor"))
        if str(value or "").strip()
    ))
    own_hosts = {
        _host(value) for value in (
            product.get("canonical_url"), product.get("pdp_url"), product.get("url"),
            sku_ctx.get("canonical_url"), sku_ctx.get("pdp_url"), sku_ctx.get("url"),
        ) if _host(value)
    }
    guarded = copy.deepcopy(payloads)
    if not brands or not own_hosts:
        return guarded
    for payload in guarded:
        runs = payload.get("raw_runs") if isinstance(payload.get("raw_runs"), list) else [payload]
        for run in runs:
            if not isinstance(run, dict):
                continue
            sources = [s for s in run.get("grounding_sources") or [] if isinstance(s, dict)]
            if not sources:
                continue
            if any(_host(s.get("uri") or s.get("url")) in own_hosts for s in sources):
                continue
            parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
            url_match = run.get("url_match") if isinstance(run.get("url_match"), dict) else {}
            self_report = url_match.get("llm_self_report") if isinstance(url_match.get("llm_self_report"), dict) else {}
            if not any(v is True for obj in (run, parsed, self_report) for k, v in obj.items()
                       if k in ("product_visible", "correct_sku", "sku_mentioned")):
                continue
            excerpt = " ".join(str(v or "") for v in (
                run.get("evidence_excerpt"), parsed.get("evidence_excerpt"),
                parsed.get("evidence_text"), parsed.get("answer"),
            ))
            source_text = " ".join(str(s.get(k) or "") for s in sources for k in ("title", "uri", "url"))
            # The query is deliberately excluded: it names the merchant even
            # when the answer silently swaps in a lookalike brand.
            if any(_mentions_brand(excerpt + " " + source_text, brand) for brand in brands):
                continue
            run["identity_mismatch"] = "positive_model_verdict_without_merchant_identity"
            run["product_visible"] = False
            run["parsed"] = {**parsed, "product_visible": False, "correct_sku": False, "sku_mentioned": False}
            run["url_match"] = {**url_match, "in_grounding": False,
                                "llm_self_report": {**self_report, "product_visible": False,
                                                    "correct_sku": False, "sku_mentioned": False}}
    return guarded
