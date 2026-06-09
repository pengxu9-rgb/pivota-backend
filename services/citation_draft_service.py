"""
Citation draft service — the metered revenue piece of the citation operator.

Given a `citation_target` (a cited source the merchant is absent from) and an
action type, generate the content the merchant will use to close the gap:

  - reddit_reply        : an authentic, disclosed, genuinely-useful community reply
  - owned_faq           : a durable Q&A block for the merchant's own site
  - owned_evidence_page : a factual evidence/explainer page (esp. supplements)
  - editorial_pitch     : a short outreach pitch to an editor
  - review_nudge        : a post-purchase review-request snippet

Posture (founder call): be direct and aggressive WHERE IT GENUINELY HELPS. For
community content that means quality == efficacy — a reply that reads as an ad is
removed by moderators and never gets cited, so "authentic + useful + disclosed"
is the way to actually win the citation, not timidity.

Metering: drafting is real LLM COGS, so it bills through the canonical
`credit_consumption_service` (paid-tier gated, idempotent). Free-tier merchants
are gated BEFORE generation — we neither spend COGS nor give the paid artifact away.

The LLM call (`_generate`) and the metering/persistence calls are isolated so the
prompt construction + flow are unit-testable without a key, a balance, or a DB.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

from config.settings import settings
from services import citation_operator_service as cos
from services import credit_consumption_service as ccs

logger = logging.getLogger(__name__)

DRAFT_OPERATION_TYPE = "agent_citation_draft"
DEFAULT_DRAFT_PROVIDER = "deepseek"
DRAFT_MAX_OUTPUT_TOKENS = 800
DRAFT_TIMEOUT_S = 40.0
DRAFT_TEMPERATURE = 0.5

SUPPORTED_ACTION_TYPES = (
    "reddit_reply", "owned_faq", "owned_evidence_page", "editorial_pitch", "review_nudge",
)

# Per-action system prompts. Every one forbids inventing facts and requires
# grounding in the supplied product facts.
_GROUNDING_RULE = (
    "Ground every product claim in the PRODUCT FACTS provided. Never invent specs, "
    "ingredients, studies, prices, or results. If a fact isn't provided, don't state it."
)

_SYSTEM_PROMPTS: Dict[str, str] = {
    "reddit_reply": (
        "You are helping a brand participate authentically in an online community thread. "
        "Write a genuinely helpful reply to the QUESTION. Rules: "
        "(1) Lead with real, specific help that answers the question — valuable even to "
        "someone who never buys. "
        "(2) " + _GROUNDING_RULE + " "
        "(3) Disclose the affiliation plainly in one short clause (e.g. 'full disclosure, "
        "I work with <brand>'). "
        "(4) No marketing voice, no hype, no superlatives, no links unless one is directly "
        "useful. Sound like a knowledgeable community member, not an ad. "
        "(5) A reply that reads as promotion gets removed by moderators and never cited — "
        "usefulness is the entire point. Keep it concise (under ~180 words)."
    ),
    "owned_faq": (
        "Write a durable FAQ entry for the merchant's OWN website answering the recurring "
        "buyer QUESTION, so AI assistants can cite it. Output a clear question header and a "
        "factual, self-contained answer. " + _GROUNDING_RULE + " Neutral, informative tone; "
        "no hype. ~120-200 words."
    ),
    "owned_evidence_page": (
        "Write a factual evidence/explainer section for the merchant's OWN site addressing "
        "the QUESTION, suitable to be cited by AI assistants. Where clinical/scientific "
        "EVIDENCE is provided, summarize it accurately and conservatively; do not overstate. "
        + _GROUNDING_RULE + " Cite the provided evidence references inline by name. ~200-300 words."
    ),
    "editorial_pitch": (
        "Write a short, specific outreach pitch (3-5 sentences) to an editor/writer who covers "
        "this category, proposing the product as relevant to a piece answering the QUESTION. "
        + _GROUNDING_RULE + " Concrete and non-promotional; lead with why it's genuinely "
        "interesting to their readers."
    ),
    "review_nudge": (
        "Write a brief, friendly post-purchase message (2-3 sentences) asking a verified buyer "
        "to share an honest review covering the aspect raised in the QUESTION. No incentives, "
        "no scripting of the verdict. " + _GROUNDING_RULE
    ),
}


def supported_action_type(action_type: str) -> bool:
    return action_type in SUPPORTED_ACTION_TYPES


def _facts_block(product: Optional[Mapping[str, Any]]) -> str:
    if not product:
        return "(no structured product facts provided)"
    lines: List[str] = []
    for k, v in product.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) or "(no structured product facts provided)"


def build_messages(action_type: str, context: Mapping[str, Any]) -> Tuple[str, str]:
    """Pure: (system, user) messages for the LLM. Raises on unsupported action_type."""
    if not supported_action_type(action_type):
        raise ValueError(f"unsupported action_type: {action_type}")
    system = _SYSTEM_PROMPTS[action_type]
    brand = (context.get("brand") or "the brand").strip() or "the brand"
    question = (context.get("question") or "").strip()
    subreddit = context.get("subreddit")
    evidence = context.get("evidence") or []
    parts = [f"BRAND: {brand}"]
    if subreddit:
        parts.append(f"COMMUNITY: r/{subreddit}")
    parts.append(f"QUESTION:\n{question or '(none provided)'}")
    parts.append("PRODUCT FACTS:\n" + _facts_block(context.get("product")))
    if evidence:
        ev = "\n".join(f"- {e}" for e in evidence)
        parts.append("CLINICAL/SCIENTIFIC EVIDENCE (summarize accurately, do not overstate):\n" + ev)
    system_with_brand = system.replace("<brand>", brand)
    return system_with_brand, "\n\n".join(parts)


async def _generate(
    *, system: str, user: str, provider: str, max_tokens: int = DRAFT_MAX_OUTPUT_TOKENS,
) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """Call the drafting LLM. Returns (content, in_tok, out_tok) or None when no key
    is configured. Raises on transport/HTTP errors (caller catches). Isolated so
    tests can monkeypatch it."""
    if provider != "deepseek":
        # V1 drafts on deepseek (cheap COGS). Other providers wired later.
        raise ValueError(f"unsupported draft provider: {provider}")
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": DRAFT_TEMPERATURE,
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    async with httpx.AsyncClient(timeout=DRAFT_TIMEOUT_S) as client:
        response = await client.post(f"{base_url}/v1/chat/completions", json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        content = (data["choices"][0]["message"]["content"] or "").strip()
        usage = data.get("usage") or {}
        return content, usage.get("prompt_tokens"), usage.get("completion_tokens")


async def generate_draft(
    *,
    merchant_id: str,
    target_id: str,
    action_type: str,
    context: Mapping[str, Any],
    sku_key: Optional[str] = None,
    provider: str = DEFAULT_DRAFT_PROVIDER,
    persist: bool = True,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate (and optionally persist) a metered draft for a citation target.

    Flow: paid-tier gate -> generate -> persist as a `draft` action -> meter.
    `idempotency_key`, if given, is a BASE discriminator applied to BOTH the
    metering event and the action insert (default `{target_id}:{action_type}`).
    Returns a dict with billing_mode, the draft, credits charged, and action_id.
    """
    if not supported_action_type(action_type):
        raise ValueError(f"unsupported action_type: {action_type}")

    # Revenue gate FIRST: free-tier merchants don't get the paid artifact and we
    # don't spend COGS generating it. (meter_agent_workflow re-checks; this avoids
    # the LLM call entirely for free tier.)
    if not await ccs.merchant_is_paid_tier(merchant_id):
        return {"billing_mode": "preview_only", "reason": "not_paid_tier",
                "draft_content": None, "credits": 0, "action_id": None}

    system, user = build_messages(action_type, context)
    try:
        gen = await _generate(system=system, user=user, provider=provider)
    except Exception as exc:
        logger.warning("citation draft generation failed merchant=%s target=%s: %s",
                       merchant_id, target_id, exc)
        return {"billing_mode": "preview_only", "reason": "generation_failed",
                "error": str(exc), "draft_content": None, "credits": 0, "action_id": None}
    if gen is None:  # no API key configured -> nothing ran, nothing to meter
        return {"billing_mode": "preview_only", "reason": "no_api_key",
                "draft_content": None, "credits": 0, "action_id": None}

    content, _in_tok, _out_tok = gen
    content = (content or "").strip()
    if not content:  # 200 with empty/whitespace body (refusal/truncation) -> never charge
        return {"billing_mode": "preview_only", "reason": "empty_generation",
                "draft_content": None, "credits": 0, "action_id": None}

    # Both idempotency namespaces co-vary on one base key (a custom key must reach
    # BOTH the metering store and the action insert, or a custom-key call collides
    # on persist while still metering -> double charge for one row).
    base_key = idempotency_key or f"{target_id}:{action_type}"

    # Persist BEFORE metering: the action row is the merchant's receipt. If metering
    # then fails, the merchant keeps a saved, UNcharged draft (favorable) instead of
    # being charged for an artifact we failed to record. NOTE(v1): each call re-runs
    # the LLM (COGS) before this point; metering is idempotent so the *merchant* is
    # never double-charged, but Pivota eats COGS on retries. TODO(GA): per-merchant
    # daily COGS cap (see pdp_copy_review._DAILY_COST_CAP_USD) + generation cache.
    action_id: Optional[str] = None
    if persist:
        action_id = await cos.record_merchant_action(
            target_id=target_id, merchant_id=merchant_id, action_type=action_type,
            draft_content=content, draft_model=settings.deepseek_model,
            draft_credits=None, sku_key=sku_key,
            idempotency_key=f"draft:{base_key}",
        )

    try:
        meter = await ccs.meter_agent_workflow(
            merchant_id, DRAFT_OPERATION_TYPE, provider=provider, units=1,
            idempotency_key=f"citation_draft:{base_key}",
        )
    except Exception as exc:  # debit/balance error -> keep the draft, don't charge
        logger.warning("citation draft metering failed merchant=%s target=%s: %s",
                       merchant_id, target_id, exc)
        meter = {"billing_mode": "preview_only", "credits": 0, "reason": "metering_failed"}

    billing_mode = meter.get("billing_mode", "preview_only")
    credits = int(meter.get("credits", 0) or 0)
    if persist and action_id and credits:
        try:
            await cos.set_action_draft_credits(action_id=action_id, credits=credits)
        except Exception:  # cosmetic receipt update; never fail the call over it
            logger.warning("failed to record draft_credits on action=%s", action_id)

    return {
        "billing_mode": billing_mode,
        "credits": credits,
        "draft_content": content,
        "draft_model": settings.deepseek_model,
        "action_id": action_id,
        "action_type": action_type,
        "reason": meter.get("reason"),
    }
