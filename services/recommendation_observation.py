"""Project reviewed US shopper answers without inferring recommendations from mentions."""

import logging
import os

from db._jsonb_safe import coerce_jsonb_to_dict
from services.claim_safety import review_source_url
from services.selection_measurement import report_observations

RECOMMENDATION_BASIS = "reviewed_consumer_answers_v1"
VERDICTS = frozenset({"recommended", "other", "unknown"})
# This pilot measures a frozen unbranded US panel. Retained merchant questions
# outside the panel stay available as consumer diagnostics in the same report.
PILOT_QUESTIONS = frozenset({
    "What lightweight hydrating essences are available in the United States for skin that feels dry after cleansing? Explain each pick and cite its product page.",
    "Which lightweight hydrating essences at $25 or less are available in the United States if I avoid snail mucin and snail secretion filtrate? Cite the ingredient list for each pick.",
    "Which watery hydrating essences are available in the United States if I want a light layer under moisturizer? Explain a tradeoff for each and cite its product page.",
})
logger = logging.getLogger(__name__)


def pilot_observations(report):
    """Only natural, unbranded consumer prompts with an explicit US market."""
    titles = {}
    for product in report.get("per_sku_reports") or []:
        if not isinstance(product, dict):
            continue
        key = product.get("product_key")
        sku_key = product.get("sku_key")
        identity = product.get("identity") or {}
        title = identity.get("name") if isinstance(identity, dict) else None
        title = title or product.get("sku_title")
        if not isinstance(title, str) or not title.strip():
            continue
        keys = [key]
        if isinstance(sku_key, str) and sku_key.startswith("urlwedge:"):
            keys.append(sku_key)
        for product_key in keys:
            if isinstance(product_key, str):
                titles.setdefault(product_key, title)

    rows = {}
    for row in report_observations(report):
        key = row.get("product_key")
        observation_id = row.get("observation_id")
        query = row.get("query")
        if (row.get("evidence_kind") != "consumer_answer" or row.get("tier") != "unbranded"
                or not isinstance(key, str) or key not in titles or not isinstance(observation_id, str)
                or not isinstance(query, str) or query not in PILOT_QUESTIONS):
            continue
        evidence = row.get("answer_evidence") or {}
        verified = row.get("status") == "answered" and row.get("answer_unknown_reason") is None
        rows[observation_id] = {
            "observation_id": observation_id, "product_key": key, "product_title": titles[key],
            "provider": row.get("provider"), "model": evidence.get("model") if verified else None,
            "market": "US", "prompt_contract": row.get("prompt_contract"), "query": query,
            "status": row.get("status"), "reviewable": verified,
            "answer_text": evidence.get("text") if verified else None,
            "answer_sha256": evidence.get("sha256") if verified else None,
            "cited_urls": [source["uri"] for source in row.get("cited_sources") or []
                           if isinstance(source, dict) and isinstance(source.get("uri"), str)
                           and review_source_url(source["uri"]) == source["uri"]] if verified else [],
        }
    return rows


def validate_review(observation, *, answer_sha256, verdict, excerpt=None, citation_url=None):
    """Require an exact retained answer, verbatim recommendation and retained citation."""
    if not observation.get("reviewable") or answer_sha256 != observation.get("answer_sha256"):
        raise ValueError("The retained answer is unavailable or changed.")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise ValueError("Unknown recommendation verdict.")
    excerpt = excerpt.strip() if isinstance(excerpt, str) else None
    citation_url = citation_url.strip() if isinstance(citation_url, str) else None
    if excerpt and excerpt not in observation["answer_text"]:
        raise ValueError("The excerpt must appear verbatim in the retained answer.")
    if citation_url and citation_url not in observation["cited_urls"]:
        raise ValueError("The citation must be one of the answer's retained sources.")
    if verdict == "recommended" and not (excerpt and citation_url):
        raise ValueError("A recommendation requires a fit excerpt and exact-product citation.")
    return {"answer_sha256": answer_sha256, "verdict": verdict, "excerpt": excerpt,
            "citation_url": citation_url}


def attach_recommendation_reviews(selection, report, findings):
    """Count only staff verdicts, while keeping unreviewed/failed answers visible."""
    reviews = {}
    for finding in findings:
        payload = coerce_jsonb_to_dict(finding.get("payload_jsonb")) or {}
        if (payload.get("basis") == RECOMMENDATION_BASIS and payload.get("reviewed_by")
                and isinstance(payload.get("observation_id"), str)):
            reviews[payload.get("observation_id")] = payload
    observations = pilot_observations(report)
    for answer in selection.get("answers") or []:
        if answer.get("observation_id") in observations:
            answer["recommendation_pilot_eligible"] = True
    if not reviews:
        return
    valid_reviews = {}
    for observation_id, review in reviews.items():
        observation = observations.get(observation_id)
        if not observation or review.get("product_key") != observation["product_key"]:
            continue
        try:
            validated = validate_review(observation, **{
                key: review.get(key) for key in ("answer_sha256", "verdict", "excerpt", "citation_url")
            })
        except (ValueError, TypeError):
            continue
        valid_reviews[observation_id] = {"market": observation["market"], **{
            key: validated[key] for key in ("verdict", "excerpt", "citation_url") if validated[key]
        }}
    if not valid_reviews:
        return

    reviewed_cohorts = {(observations[key]["product_key"], observations[key]["provider"]) for key in valid_reviews}
    groups = {}
    fields = ("product_key", "product_title", "provider", "model", "market", "prompt_contract")
    for observation_id, observation in observations.items():
        if (observation["product_key"], observation["provider"]) not in reviewed_cohorts:
            continue
        identity = {field: observation[field] or "Unavailable" for field in fields}
        group = groups.setdefault(tuple(identity.values()), {
            **identity, "attempted": 0, "recommended": 0, "other": 0, "unknown": 0, "failed": 0, "cited": 0,
        })
        review = valid_reviews.get(observation_id) or {}
        outcome = "failed" if observation["status"] == "provider_failed" else review.get("verdict", "unknown")
        group["attempted"] += 1
        group[outcome] += 1
        group["cited"] += bool(review.get("citation_url"))

    selection["recommendations"] = {
        "basis": RECOMMENDATION_BASIS,
        "rows": [groups[key] for key in sorted(groups)],
    }
    for answer in selection.get("answers") or []:
        review = valid_reviews.get(answer.get("observation_id"))
        if review:
            answer["recommendation_review"] = review


async def enrich_revenue_recovery(projection, run):
    if os.getenv("PIVOTA_RECOMMENDATION_AUDIT_PILOT") != "true":
        return
    selection = projection.get("selection")
    report = run.get("report_jsonb")
    if (not isinstance(selection, dict) or not isinstance(report, dict)
            or not report.get("consumer_selection_observations") or not run.get("run_id")):
        return

    from db.audit_evidence import list_review_findings

    try:
        findings = await list_review_findings(
            audit_run_id=run["run_id"], merchant_id=run["merchant_id"],
        )
    except Exception:
        logger.warning("Recommendation review read failed for audit %s", run["run_id"], exc_info=True)
        return
    attach_recommendation_reviews(selection, report, findings)
