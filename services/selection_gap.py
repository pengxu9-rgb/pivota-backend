"""C1 — the selection gap: products you sell × queries you lose.

A rate ("your unbranded visibility is 25%") is not actionable. A LIST is:
"you sell a Niacinamide 10 TXA 4 Serum and AI never names you for *best
affordable niacinamide serum*". This module joins the two things the audit
already holds — the queries the merchant LOST (`_failing_prompts`) and the
merchant's own catalog rows — and emits, per lost query, the products that
answer it, each with the reason it matched.

Measured 2026-09-01 across 7 brand cohorts (840 grounded responses): at
temperature 0 a neutral unbranded query resolved 3/3 or 0/3 — a brand owns a
query or is absent from it. So the output is a two-sided LIST of won and lost
queries and deliberately computes NO rate: there is no meaningful fraction to
report, and a fraction is not a thing a merchant can act on.

Deterministic by construction — no LLM call (`services/pdp_matcher/llm_match.py`
is deliberately NOT used here). Two reasons: (1) the failure mode to avoid is a
FALSE POSITIVE — telling a merchant they sell something for a query when they do
not is a fabricated finding on a merchant-facing surface, and much worse than a
miss; (2) determinism means the same audit re-read twice yields the same gaps.

`SELECTION_GAP_VERSION` is stamped into the OUTPUT, not into `audit_basis`. The
basis records what a run was MEASURED with — which prompts, which engines, which
models. The selection gap changes none of that: it is a READ-TIME interpretation
of an already-completed measurement. Bumping this version must never make two
runs look like they were probed differently, so it must never reach the basis
and `bases_are_comparable` must never see it.

The matching rule, as a merchant would be told it:

    We only name a product for a query when the query asks for BOTH a product
    FORM you actually sell (the "serum"/"toner"/"cream" word) AND at least one
    DISTINGUISHING term that product carries in its name or tags (the
    "niacinamide"/"bha"/"ceramide" word). A query that only shares the form word
    ("best retinol serum" against your niacinamide serum) is NOT a match, and a
    query we cannot match this way produces no gap at all rather than a weak one.

Both vocabularies are drawn from the merchant's OWN catalog — the form words are
the words their `product_type`/`category` columns use plus the word each product
title ends on; the distinguishing words are everything else in their titles and
tags minus their own brand name. Nothing here is a hardcoded ingredient list.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from services.pdp_matcher.deterministic import _normalize_token

SELECTION_GAP_VERSION = 1

# Shortest term allowed into either vocabulary. Length 3 is deliberate: "bha",
# "aha", "pha", "spf" are real ingredient/format terms, while length-2 fragments
# ("10", "ml", "4") are catalog noise.
_MIN_TERM_LEN = 3

# The rule's two halves. Both are required; either one alone is a weak match and
# a weak match must produce NO gap.
MIN_FORM_MATCHES = 1
MIN_DISTINCTIVE_MATCHES = 1

# Query filler and marketing/packaging words. Applied to BOTH sides so a title
# reading "Best Seller Set" cannot lend "best" or "set" to a match. This is the
# only hardcoded list in the module and it is deliberately merchant-independent:
# it contains no ingredient, no format and no category word.
_NOISE_TERMS = frozenset({
    "and", "for", "the", "with", "your", "you", "our", "are", "any", "all",
    "best", "top", "good", "great", "better", "cheap", "cheapest", "affordable",
    "budget", "recommend", "recommended", "recommendation", "what", "which",
    "who", "how", "why", "when", "where", "should", "buy", "buying", "shop",
    "shopping", "online", "store", "brand", "brands", "product", "products",
    "item", "items", "new", "sale", "off", "free", "gift", "set", "kit",
    "bundle", "pack", "packs", "value", "size", "mini", "travel", "full",
    "official", "authentic", "genuine", "original", "premium", "luxury",
    "quality", "review", "reviews", "rated", "rating", "seller", "bestseller",
    "sellers", "use", "using", "used", "get", "one", "two", "three",
})

# Measurement/packaging units — never a form and never a distinguishing term.
_UNIT_TERMS = frozenset({
    "ml", "mls", "oz", "fl", "gram", "grams", "kg", "lb", "lbs", "count",
    "ct", "pcs", "pc", "piece", "pieces", "each", "per", "pack",
})

_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# term primitives
# ---------------------------------------------------------------------------


def normalize_query(value: Any) -> str:
    """Same normalization `_failing_prompts` keys its dedupe on, so a query from
    the failing list and the same query from a per-prompt row join cleanly."""
    return _WS_RE.sub(" ", str(value or "").strip().lower())


def _terms(value: Any) -> List[str]:
    """Significant terms of a free-text field, in order, deduped."""
    out: List[str] = []
    seen: Set[str] = set()
    for tok in _normalize_token(value).split():
        if not _is_significant(tok):
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _is_significant(tok: str) -> bool:
    if len(tok) < _MIN_TERM_LEN:
        return False
    if tok.isdigit():
        return False
    if tok in _NOISE_TERMS or tok in _UNIT_TERMS:
        return False
    # "50ml", "30g" — a number glued to a unit is packaging, not a term.
    if re.fullmatch(r"\d+[a-z]{1,4}", tok):
        return False
    return True


def _singular(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("es"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s"):
        return tok[:-1]
    return tok


def _terms_match(query_term: str, catalog_term: str) -> bool:
    """Exact after singularization. Nothing fuzzier, deliberately.

    `services/pdp_matcher/deterministic.trigram_similarity` was the obvious
    reuse and was measured against this job before being rejected: it is a
    WHOLE-STRING measure, and on single tokens it either fires for pairs
    `_singular` already equates or not at all. A mid-word substitution costs
    three trigrams, so the spelling variants a bridge would exist for score far
    under the repo's 0.85 `TITLE_SIMILARITY_THRESHOLD` —
    "niacinamide"/"niacinimide" 0.60, "moisturising"/"moisturizing" 0.63,
    "exfoliating"/"exfoliation" 0.60. Lowering the threshold to reach them is
    what would fabricate a gap: "retinol"/"retinal" — different actives — score
    0.45, so any threshold loose enough to bridge a typo also collapses two
    ingredients into one. A miss is cheap here; a false positive is not."""
    return _singular(query_term) == _singular(catalog_term)


def _json_terms(value: Any) -> List[str]:
    """Terms from a JSONB column that may arrive as a list, a dict, a JSON
    string, or plain text."""
    raw = value
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return _terms(value)
    items: List[Any]
    if isinstance(raw, Mapping):
        items = list(raw.keys()) + [v for v in raw.values() if isinstance(v, str)]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    elif raw is None:
        items = []
    else:
        items = [raw]
    out: List[str] = []
    seen: Set[str] = set()
    for item in items:
        if not isinstance(item, (str, int, float)):
            continue
        for tok in _terms(item):
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


# ---------------------------------------------------------------------------
# catalog vocabularies
# ---------------------------------------------------------------------------

_FORM_FIELDS = ("product_type", "category", "category_label", "category_path")
_DISTINCTIVE_FIELDS = ("title",)
_DISTINCTIVE_JSON_FIELDS = ("tags", "use_case_tags")


def _brand_vocabulary(
    catalog_rows: Sequence[Mapping[str, Any]],
    merchant_name: Optional[str],
) -> Set[str]:
    """Every word the merchant's own name(s) are made of. A brand word is not a
    distinguishing term — "anua" appears on every Anua product, so matching a
    query on it would say nothing about WHICH product answers the query."""
    vocab: Set[str] = set()
    for tok in _terms(merchant_name):
        vocab.add(tok)
    for row in catalog_rows:
        for field in ("brand", "vendor", "merchant_name"):
            for tok in _terms(row.get(field)):
                vocab.add(tok)
    return vocab


def _form_vocabulary(catalog_rows: Sequence[Mapping[str, Any]]) -> Set[str]:
    """The merchant's own product-FORM words, drawn from the catalog:

      * every term in `product_type` / `category` / `category_label` /
        `category_path` — these columns exist to say what KIND of thing the row
        is, and
      * the last significant word of each title — product names overwhelmingly
        END on the form ("... Serum", "... Toner", "... Cream"), so this
        recovers the form for merchants whose type column is a coarse
        "Skincare".

    No hardcoded format list is involved.
    """
    vocab: Set[str] = set()
    for row in catalog_rows:
        for field in _FORM_FIELDS:
            for tok in _terms(row.get(field)):
                vocab.add(tok)
        title_terms = _terms(row.get("title"))
        if title_terms:
            vocab.add(title_terms[-1])
    return vocab


def _product_terms(
    row: Mapping[str, Any],
    *,
    form_vocab: Set[str],
    brand_vocab: Set[str],
) -> Optional[Dict[str, Any]]:
    """Split one catalog row into its FORM terms and its DISTINGUISHING terms.
    Returns None for a row we cannot describe (no key, or no terms at all)."""
    product_key = str(row.get("product_key") or "").strip()
    title = str(row.get("title") or "").strip()
    if not product_key or not title:
        return None

    own_terms: List[str] = []
    for field in _DISTINCTIVE_FIELDS:
        own_terms.extend(_terms(row.get(field)))
    for field in _DISTINCTIVE_JSON_FIELDS:
        own_terms.extend(_json_terms(row.get(field)))

    forms: List[str] = []
    seen_forms: Set[str] = set()
    for field in _FORM_FIELDS:
        for tok in _terms(row.get(field)):
            if tok not in seen_forms:
                seen_forms.add(tok)
                forms.append(tok)
    # A form word sitting inside this row's own title/tags counts too — that is
    # how "Niacinamide 10 TXA 4 Serum" is known to BE a serum.
    for tok in own_terms:
        if tok in form_vocab and tok not in seen_forms:
            seen_forms.add(tok)
            forms.append(tok)

    distinctive: List[str] = []
    seen_dist: Set[str] = set()
    for tok in own_terms:
        if tok in seen_forms or tok in form_vocab or tok in brand_vocab:
            continue
        if tok in seen_dist:
            continue
        seen_dist.add(tok)
        distinctive.append(tok)

    if not forms and not distinctive:
        return None
    return {
        "product_key": product_key,
        "title": title,
        "forms": forms,
        "distinctive": distinctive,
    }


def build_catalog_index(
    catalog_rows: Sequence[Mapping[str, Any]],
    *,
    merchant_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Catalog rows → per-product {product_key, title, forms, distinctive}."""
    rows = [r for r in (catalog_rows or []) if isinstance(r, Mapping)]
    form_vocab = _form_vocabulary(rows)
    brand_vocab = _brand_vocabulary(rows, merchant_name)
    index: List[Dict[str, Any]] = []
    for row in rows:
        entry = _product_terms(row, form_vocab=form_vocab, brand_vocab=brand_vocab)
        if entry is not None:
            index.append(entry)
    return index


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


_SIZE_SUFFIX_RE = re.compile(r"\s*\((?:\d+(?:\.\d+)?\s*(?:ml|g|oz|fl\s*oz)|[^)]*\bsize\b[^)]*)\)\s*$", re.I)


def _size_family(title: str) -> str:
    """A title with its trailing size qualifier removed, for de-duplicating the
    same product sold at several sizes. Deliberately narrow: only a trailing
    parenthetical that is a measurement or says "size" is stripped, so a real
    product distinction inside brackets is never collapsed."""
    return _normalize_token(_SIZE_SUFFIX_RE.sub("", str(title or "")).strip())


def match_products_for_query(
    query: str,
    catalog_index: Sequence[Mapping[str, Any]],
    *,
    max_products: int = 3,
) -> List[Dict[str, Any]]:
    """Products that ANSWER `query`, each carrying why it matched.

    A product qualifies only when the query names BOTH a form the product is and
    a distinguishing term the product carries. Either half alone is a weak match
    and returns nothing — a query with no confident product match must produce
    no gap rather than a weak one.
    """
    query_terms = _terms(query)
    if not query_terms:
        return []
    scored: List[Tuple[int, int, str, Dict[str, Any]]] = []
    for entry in catalog_index:
        forms = entry.get("forms") or []
        distinctive = entry.get("distinctive") or []
        form_hits = [
            q for q in query_terms if any(_terms_match(q, f) for f in forms)
        ]
        dist_hits = [
            q
            for q in query_terms
            if q not in form_hits and any(_terms_match(q, d) for d in distinctive)
        ]
        if len(form_hits) < MIN_FORM_MATCHES:
            continue
        if len(dist_hits) < MIN_DISTINCTIVE_MATCHES:
            continue
        title = str(entry.get("title") or "")
        matched_form = form_hits[0]
        reason = (
            f'Your "{title}" is a {matched_form} and carries '
            f'{", ".join(sorted(dist_hits))} — both named in this query.'
        )
        scored.append((
            -len(dist_hits),
            -len(form_hits),
            str(entry.get("product_key") or ""),
            {
                "product_key": entry.get("product_key"),
                "title": title,
                "matched_form": matched_form,
                "matched_terms": sorted(dist_hits),
                "match_reason": reason,
            },
        ))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    # One product per SIZE FAMILY. A merchant's catalogue carries the same
    # product at several sizes ("... Serum" and "... Serum (10ml)" — 8 such
    # groups in the Anua catalogue measured 2026-09-02), and listing both makes
    # one gap read as two products the merchant already sells. The best-scoring
    # variant wins because `scored` is already ordered.
    out: List[Dict[str, Any]] = []
    seen_families: set = set()
    for item in scored:
        family = _size_family(item[3].get("title") or "")
        if family in seen_families:
            continue
        seen_families.add(family)
        out.append(item[3])
        if len(out) >= max(0, int(max_products)):
            break
    return out


# ---------------------------------------------------------------------------
# report adapters (pure)
# ---------------------------------------------------------------------------


def _evidence(
    *,
    grounded_responses: Optional[int],
    responses_citing_your_product: int,
    engines: Sequence[str],
) -> Dict[str, Any]:
    return {
        "grounded_responses": grounded_responses,
        "responses_citing_your_product": int(responses_citing_your_product),
        "engines": sorted({str(e).strip().lower() for e in engines if str(e).strip()}),
    }


def per_prompt_evidence(per_sku_reports: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """normalized query -> the counts the per-prompt rows already carry.

    Read-only over `opportunity.per_prompt` — no re-derivation of anything the
    scorer computed. `runs_with_citations` is the number of probe responses that
    grounded on any source; `sku_cited_runs` is how many of those actually cited
    the merchant's product.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for report in per_sku_reports or []:
        if not isinstance(report, Mapping):
            continue
        opportunity = report.get("opportunity")
        rows = opportunity.get("per_prompt") if isinstance(opportunity, Mapping) else None
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            key = normalize_query(row.get("normalized_query") or row.get("query"))
            if not key:
                continue
            summary = row.get("source_summary")
            summary = summary if isinstance(summary, Mapping) else {}
            grounded = _as_int(summary.get("runs_with_citations"))
            cited = _as_int(summary.get("sku_cited_runs")) or 0
            slot = out.setdefault(
                key,
                {
                    "query": str(row.get("query") or row.get("normalized_query") or key),
                    "grounded_responses": 0,
                    "responses_citing_your_product": 0,
                    "engines": set(),
                },
            )
            slot["grounded_responses"] += grounded or 0
            slot["responses_citing_your_product"] += cited
            verdicts = row.get("provider_verdicts")
            if isinstance(verdicts, Mapping):
                slot["engines"].update(str(p).strip().lower() for p in verdicts.keys())
    return out


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def lost_queries_from_reports(
    per_sku_reports: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """The union of every per-SKU `failing_prompts` list, deduped by query.

    Consumes `_failing_prompts`' output as-is: it already emits one entry per
    UNIQUE failing query, already excludes internal-first comparison runs and
    upstream-errored runs. Nothing is re-derived here.
    """
    out: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for report in per_sku_reports or []:
        if not isinstance(report, Mapping):
            continue
        for entry in report.get("failing_prompts") or []:
            if not isinstance(entry, Mapping):
                continue
            query = str(entry.get("query") or "").strip()
            key = normalize_query(query)
            if not key:
                continue
            engines = [
                str(p) for p in (entry.get("providers") or []) if str(p or "").strip()
            ] or ([str(entry["provider"])] if entry.get("provider") else [])
            existing = seen.get(key)
            if existing is not None:
                existing["engines"] = sorted(set(existing["engines"]) | set(engines))
                continue
            row = {"query": query, "normalized_query": key, "engines": sorted(set(engines))}
            seen[key] = row
            out.append(row)
    return out


def won_queries_from_reports(
    per_sku_reports: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Queries where a probe response actually cited one of the merchant's
    products — the other side of the two-sided list."""
    evidence = per_prompt_evidence(per_sku_reports)
    out: List[Dict[str, Any]] = []
    for key, slot in evidence.items():
        if slot["responses_citing_your_product"] <= 0:
            continue
        out.append({"query": slot["query"], "normalized_query": key})
    out.sort(key=lambda r: r["normalized_query"])
    return out


# ---------------------------------------------------------------------------
# the report section
# ---------------------------------------------------------------------------


def build_selection_gap(
    *,
    catalog_rows: Sequence[Mapping[str, Any]],
    lost_queries: Sequence[Mapping[str, Any]],
    won_queries: Sequence[Mapping[str, Any]] = (),
    query_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    merchant_name: Optional[str] = None,
    max_products_per_query: int = 3,
    max_gaps: int = 25,
) -> Dict[str, Any]:
    """Join the merchant's catalog to the queries the audit measured.

    Returns a versioned two-sided LIST — the lost queries a product of theirs
    answers (`gaps`), the lost queries nothing in the catalog confidently
    answers (`lost_queries_without_product`), and the queries they win
    (`won_queries`). Deliberately no rate: see the module docstring.
    """
    catalog_index = build_catalog_index(catalog_rows, merchant_name=merchant_name)
    evidence_map = dict(query_evidence or {})

    won_keys: Set[str] = set()
    won_out: List[Dict[str, Any]] = []
    for entry in won_queries or []:
        if not isinstance(entry, Mapping):
            continue
        key = normalize_query(entry.get("normalized_query") or entry.get("query"))
        if not key or key in won_keys:
            continue
        won_keys.add(key)
        ev = evidence_map.get(key) or {}
        won_out.append({
            "query": str(entry.get("query") or key),
            "evidence": _evidence(
                grounded_responses=_as_int(ev.get("grounded_responses")),
                responses_citing_your_product=_as_int(
                    ev.get("responses_citing_your_product")
                ) or 0,
                engines=ev.get("engines") or entry.get("engines") or [],
            ),
        })
    won_out.sort(key=lambda r: normalize_query(r["query"]))

    gaps: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    seen_lost: Set[str] = set()
    for entry in lost_queries or []:
        if not isinstance(entry, Mapping):
            continue
        query = str(entry.get("query") or "").strip()
        key = normalize_query(entry.get("normalized_query") or query)
        if not key or key in seen_lost:
            continue
        # A query some SKU actually won is never reported as a gap — telling a
        # merchant they lose a query they win is the same fabrication class as
        # a false product match.
        if key in won_keys:
            continue
        seen_lost.add(key)
        ev = evidence_map.get(key) or {}
        row = {
            "query": query or key,
            "evidence": _evidence(
                grounded_responses=_as_int(ev.get("grounded_responses")),
                # 0 by construction: `_failing_prompts` only lists a query when
                # no run produced a first-party or correct-SKU grounded citation.
                responses_citing_your_product=0,
                engines=entry.get("engines") or ev.get("engines") or [],
            ),
        }
        products = match_products_for_query(
            query or key, catalog_index, max_products=max_products_per_query
        )
        if not products:
            unmatched.append(row)
            continue
        row["matched_products"] = products
        gaps.append(row)

    gaps.sort(key=lambda r: (-len(r["matched_products"]), normalize_query(r["query"])))
    unmatched.sort(key=lambda r: normalize_query(r["query"]))

    return {
        "version": SELECTION_GAP_VERSION,
        "available": bool(gaps or unmatched or won_out),
        "gaps": gaps[: max(0, int(max_gaps))],
        "lost_queries_without_product": unmatched[: max(0, int(max_gaps))],
        "won_queries": won_out[: max(0, int(max_gaps))],
        "counts": {
            "catalog_products_indexed": len(catalog_index),
            "lost_queries": len(gaps) + len(unmatched),
            "lost_queries_with_matched_product": len(gaps),
            "won_queries": len(won_out),
        },
    }
