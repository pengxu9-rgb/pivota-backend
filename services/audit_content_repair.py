"""Read-time correction for report copy frozen before evidence fixes."""

from __future__ import annotations

import copy
from typing import Any

from services.probe_identity_guard import _host, _mentions_brand


def repair_report_content(report: dict[str, Any], *, merchant_name: str | None = None) -> dict[str, Any]:
    if not isinstance(report, dict):
        return report
    out = copy.deepcopy(report)
    if isinstance(out.get("report_jsonb"), dict):
        out["report_jsonb"] = repair_report_content(out["report_jsonb"], merchant_name=merchant_name)
        return out
    brand = str(out.get("merchant_name") or merchant_name or "").strip()
    narrative = out.get("merchant_narrative")
    invalid_excerpts: list[str] = []
    for sku in out.get("per_sku_reports") or []:
        if not isinstance(sku, dict):
            continue
        title = str(sku.get("sku_title") or "this product")
        for evidence in sku.get("verbatim_grounding_evidence") or []:
            if not isinstance(evidence, dict) or evidence.get("product_visible") is not True:
                continue
            sources = [s for s in evidence.get("grounding_sources") or [] if isinstance(s, dict)]
            excerpt = str(evidence.get("evidence_excerpt") or "")
            source_text = " ".join(str(s.get(k) or "") for s in sources for k in ("title", "uri", "url"))
            if not brand or not sources or _mentions_brand(excerpt + " " + source_text, brand):
                continue
            own_host = _host(out.get("merchant_domain"))
            if own_host and any(_host(s.get("uri") or s.get("url")) == own_host for s in sources):
                continue
            evidence["product_visible"] = False
            evidence["identity_mismatch"] = "positive_model_verdict_without_merchant_identity"
            sku["historical_identity_review_required"] = True
            invalid_excerpts.append(excerpt)
        nba = sku.get("next_best_action")
        if isinstance(nba, dict):
            if sku.get("historical_identity_review_required"):
                nba.pop("strategic_brief", None)
                nba["brief_status"] = "unavailable"
                nba["brief_debug"] = {"outcome": "identity_conflict"}
            _repair_substitution_copy(nba, title)
            _crosscheck_consumer_answers(nba, sku, out.get("consumer_selection_observations"))
    if isinstance(narrative, dict):
        working = narrative.get("whats_working")
        if isinstance(working, dict) and isinstance(working.get("evidence_excerpt"), dict):
            example = working["evidence_excerpt"]
            example_text = str(example.get("excerpt") or "")
            if example_text and any(
                example_text in invalid or invalid in example_text
                for invalid in invalid_excerpts if invalid
            ):
                # The summary must not keep showcasing an excerpt rejected in
                # the underlying evidence. Other first-party evidence stays.
                working["evidence_excerpt"] = None
        story = narrative.get("headline_story")
        if isinstance(story, str) and "AI does not yet recommend you to new shoppers asking the category question" in story:
            narrative["headline_story"] = story.replace(
                "but AI does not yet recommend you to new shoppers asking the category question",
                "and this audit's grounded category probes did not verify an independent recommendation",
            )
        losing = narrative.get("where_youre_losing")
        if isinstance(losing, dict) and isinstance(losing.get("summary"), str):
            losing["summary"] = losing["summary"].replace(
                "When shoppers ask the category question, no independent source recommends",
                "In this audit's grounded category probes, no independent source verified a recommendation of",
            )
        for action in narrative.get("prioritized_actions") or []:
            if not isinstance(action, dict) or action.get("primary_gap") != "substitution_leak":
                continue
            sku = next((s for s in out.get("per_sku_reports") or [] if isinstance(s, dict)
                        and s.get("sku_title") == action.get("sku_title")), None)
            nba = sku.get("next_best_action") if isinstance(sku, dict) else None
            if isinstance(nba, dict) and nba.get("primary_gap") == "substitution_leak":
                action["headline"] = nba.get("headline")
                action["first_move"] = nba.get("first_move")
                action["why_this_first"] = nba.get("why_this_first")
        if any(s.get("historical_identity_review_required") for s in out.get("per_sku_reports") or [] if isinstance(s, dict)):
            limits = narrative.setdefault("honest_limits", [])
            note = "Some older SKU visibility verdicts used model self-reports that conflicted with cited product identity; affected evidence is excluded from positive examples, and historical scores need recalculation."
            if note not in limits:
                limits.append(note)
    return out


def _repair_substitution_copy(nba: dict, title: str) -> None:
    if nba.get("primary_gap") != "substitution_leak":
        return
    evidence = nba.get("evidence_used") or {}
    alert = evidence.get("substitution_alert") or {}
    substitute = str(alert.get("substituted_by") or "").strip()
    prompt = str(alert.get("prompt") or "").strip()
    if not substitute or not prompt:
        return
    alert["evidence_kind"] = "diagnostic_probe"
    if alert.get("broad_head_prompt"):
        return
    nba["headline"] = f"An audit probe named {substitute} without clearly verifying {title}."
    nba["why_this_first"] = (
        f'On the diagnostic question "{prompt}", an AI answer named {substitute} '
        f"without clearly verifying {title}. This is a gap in that tested answer, "
        "not a claim about every shopper question."
    )


def _crosscheck_consumer_answers(nba: dict, sku: dict, observations: Any) -> None:
    if nba.get("primary_gap") != "substitution_leak":
        return
    alert = ((nba.get("evidence_used") or {}).get("substitution_alert") or {})
    substitute = str(alert.get("substituted_by") or "").strip()
    sku_keys = {str(sku.get(k) or "") for k in ("sku_key", "product_key")}
    answers = [o for o in observations or [] if isinstance(o, dict)
               and o.get("product_key") in sku_keys and o.get("tier") == "dupe"
               and o.get("evidence_kind") == "consumer_answer" and o.get("status") == "answered"
               and isinstance(o.get("answer_evidence"), dict)
               and o["answer_evidence"].get("complete") is True
               and isinstance(o["answer_evidence"].get("text"), str)]
    if not substitute or not answers:
        return
    mentioned = any(_mentions_brand(o["answer_evidence"]["text"], substitute) for o in answers)
    nba["consumer_crosscheck"] = {
        "evidence_kind": "consumer_answer", "complete_answers": len(answers),
        "substitute_mentioned": mentioned,
    }
    if not mentioned:
        prompt = str(alert.get("prompt") or "the tested category question").strip()
        nba.pop("strategic_brief", None)
        nba["brief_status"] = "unavailable"
        nba["brief_debug"] = {"outcome": "consumer_capture_conflict"}
        nba["first_move"] = (
            f'Answer "{prompt}" on the product page with verified product facts, '
            "then repeat the same diagnostic probe."
        )
        nba["tracking_metrics"] = [
            f'Repeat the diagnostic question "{prompt}" and verify this exact SKU is identified.',
            "Track complete alternatives answers separately from diagnostic probes.",
        ]
        actions = [a for a in nba.get("self_serve_actions") or [] if isinstance(a, str)]
        nba["self_serve_actions"] = [nba["first_move"]] + [
            a for a in actions if substitute.lower() not in a.lower()
        ]
        note = (
            f" In the separately captured, complete alternatives answer for this product, "
            f"{substitute} was not named."
        )
        if note not in str(nba.get("why_this_first") or ""):
            nba["why_this_first"] = str(nba.get("why_this_first") or "") + note
