"""The identity truth set: labelled listing pairs that measure a same-product judge (plan Step 4).

No person audits the live judge's matches (the owner's decision, 26 November), so this set is the
only measure of its error rate before a partner sees a match. Each pair is two listings and a label:

  label  "same"       one buyable item: the same product, size and shade (grain "exact")
         "different"  not one buyable item
  grade  "gold"       a person labelled it (the 2026-07-10 step-5 review fixture)
         "silver"     derived from barcodes plus the deterministic vetoes, never from a model
         "inferred"   a person's label read for a different question (see from_tier3_fixture)
  basis  where the label comes from (BASES)

Sources, all deterministic and offline:
  * from_tier3_fixture: the human-labelled 2026-07-10 fixture (252 groups of 2-13 listings). Its
    pairs are near-duplicate titles, mostly within one store: it tests that a judge does not
    confirm what it cannot see (a size missing from both titles), not matching across stores.
  * from_owner_pair_labels: the owner's side-by-side labels of cross-store pairs (October review).
  * from_barcode_rows: live listings that share a barcode (the read-only SQL in
    scripts/ops_identity_truth_set_barcode_rows.sql). A clean barcode group (one brand, every
    listing the same size, shade and conflict tokens) gives "same" pairs across hosts; a polluted
    group (one barcode, listings whose titles state different sizes, shades or conflict tokens)
    gives "different" pairs, the trap a barcode-trusting judge falls into; two clean groups of one
    brand and one title core with different stated sizes give "different" pairs (size variants).

The data holds real store and product names, so it never lives in this public repository:
write_jsonl refuses any path inside it. Only this code and synthetic tests are committed.

judge_view is what a judge may see: both listings without the barcode, the label or its basis.
score compares a judge's verdicts ("confirmed" | "likely" | "not_shown") with the labels.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.product_match_normalizer import (
    canonical_brand,
    gtin_canonical,
    gtin_is_restricted,
    size_key,
    title_core,
    veto_reasons,
)

SCHEMA_VERSION = "ts.v1"
LABELS = ("same", "different")
GRADES = ("gold", "silver", "inferred")
VERDICTS = ("confirmed", "likely", "not_shown")
BASES = (
    "tier3_collapse",                # gold: the reviewer merged the group
    "tier3_size_variants",           # gold: kept apart as different sizes
    "tier3_distinct_products",       # gold: kept apart as different products
    "tier3_kept_rows_same_product",  # inferred: kept as separate rows (seller / storefront), one product
    "owner_pair_label",              # gold: the owner judged a cross-store pair side by side
    "barcode_clean",                # silver: one barcode, one brand, nothing vetoes
    "barcode_collision",             # silver: one barcode, titles state different items
    "size_variant",                  # silver: one brand and title core, two barcodes, two sizes
)
# The fixture's sources whose keep_separate label is about ROWS, not identity: the reviewer kept one
# product's listings apart (one per seller, one per regional storefront). For "is this one buyable
# item" they are the same product, so they are kept as grade "inferred", outside the gold gate.
TIER3_SAME_PRODUCT_KEEPS = frozenset({"KEEP_multi_seller_observation", "KEEP_regional_storefronts"})
TIER3_DIFFERENT_KEEPS = {"KEEP_size_variants": "tier3_size_variants",
                         "KEEP_title_collision_distinct_products": "tier3_distinct_products"}
MAX_PAIRS_PER_GROUP = 10
MIN_CORE_OVERLAP = 0.5  # barcode_clean also needs the two title cores to share half their words
SIDE_FIELDS = ("listing_key", "host", "brand", "title", "variant_title", "price", "currency",
               "availability", "image_url", "url")
WITHHELD_FIELDS = ("gtin",)  # kept in the file, never shown to a judge


class TruthSetError(ValueError):
    pass


@dataclass
class TruthPair:
    pair_id: str
    a: Dict[str, Any]
    b: Dict[str, Any]
    label: str
    grade: str
    basis: str
    grain: str = "exact"
    source: str = ""
    note: Dict[str, Any] = field(default_factory=dict)
    schema: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.label not in LABELS or self.grade not in GRADES or self.basis not in BASES:
            raise TruthSetError(f"bad pair {self.pair_id}: {self.label}/{self.grade}/{self.basis}")


def _side(row: Mapping[str, Any]) -> Dict[str, Any]:
    side = {k: row.get(k) for k in SIDE_FIELDS if row.get(k) not in (None, "")}
    for k in WITHHELD_FIELDS:
        if row.get(k) not in (None, ""):
            side[k] = row[k]
    return side


def _key(row: Mapping[str, Any]) -> str:
    """A listing's identity inside the set: its listing key, else host + url + title."""
    if row.get("listing_key"):
        return str(row["listing_key"])
    return "|".join(str(row.get(k) or "") for k in ("host", "url", "title"))


def pair_id(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    x, y = sorted((_key(a), _key(b)))
    return "tsp_" + hashlib.sha256(f"{x}\n{y}".encode("utf-8")).hexdigest()[:24]


def _pair(a: Mapping[str, Any], b: Mapping[str, Any], **kw: Any) -> TruthPair:
    sa, sb = sorted((_side(a), _side(b)), key=_key)
    return TruthPair(pair_id=pair_id(a, b), a=sa, b=sb, **kw)


def _pairs_of(rows: Sequence[Mapping[str, Any]], limit: int = MAX_PAIRS_PER_GROUP):
    distinct = {_key(r): r for r in rows}
    combos = sorted(itertools.combinations(sorted(distinct), 2))
    return [(distinct[x], distinct[y]) for x, y in combos[:limit]]


# ---------------------------------------------------------------------------
# Source 1: the human-labelled tier-3 fixture
# ---------------------------------------------------------------------------

def from_tier3_fixture(fixture: Mapping[str, Any]) -> Tuple[List[TruthPair], Dict[str, int]]:
    """Pairs from the 2026-07-10 fixture ({"groups": [{group_id, label, source, rows}]}).
    collapse -> same/gold; KEEP_size_variants and KEEP_title_collision_distinct_products kept
    apart -> different/gold (a relabelled 'collapse' stays same); the row-level keeps
    (TIER3_SAME_PRODUCT_KEEPS) -> same/inferred. Anything else is skipped and counted."""
    pairs: List[TruthPair] = []
    counts: Dict[str, int] = {}
    for g in fixture.get("groups") or []:
        label, src, rows = g.get("label"), str(g.get("source") or ""), g.get("rows") or []
        rows = [{"host": r.get("domain"), "brand": r.get("brand"), "title": r.get("title"),
                 "url": r.get("url")} for r in rows]
        if label == "collapse":
            kw = {"label": "same", "grade": "gold", "basis": "tier3_collapse"}
        elif label == "keep_separate" and src in TIER3_DIFFERENT_KEEPS:
            kw = {"label": "different", "grade": "gold", "basis": TIER3_DIFFERENT_KEEPS[src]}
        elif label == "keep_separate" and src in TIER3_SAME_PRODUCT_KEEPS:
            kw = {"label": "same", "grade": "inferred", "basis": "tier3_kept_rows_same_product"}
        else:
            counts[f"skipped:{label}:{src}"] = counts.get(f"skipped:{label}:{src}", 0) + 1
            continue
        for a, b in _pairs_of(rows):
            if _key(a) == _key(b):
                continue
            pairs.append(_pair(a, b, grain="exact", source=f"tier3:{g.get('group_id')}",
                               note={"fixture_source": src}, **kw))
            counts[kw["basis"]] = counts.get(kw["basis"], 0) + 1
    return pairs, counts


# ---------------------------------------------------------------------------
# Source 1b: the owner's side-by-side pair labels (the cross-merchant review page)
# ---------------------------------------------------------------------------

OWNER_LABELS = {"correct": "same", "wrong": "different"}


def _page_side(s: Mapping[str, Any]) -> Dict[str, Any]:
    return {"host": s.get("host"), "brand": s.get("brand"), "title": s.get("title"),
            "variant_title": s.get("size"), "price": s.get("price"), "currency": s.get("cur"),
            "image_url": s.get("img"), "url": s.get("url")}


def from_owner_pair_labels(page_pairs: Sequence[Mapping[str, Any]], labels: Iterable[Mapping[str, Any]], *,
                           reverted_proposals: Iterable[str] = ()) -> Tuple[List[TruthPair], Dict[str, int]]:
    """Gold pairs from a review page's pairs ({key, pid, L, A}: the moved listing and its anchor)
    and the owner's label documents ({pair_key, label: correct|wrong|unsure, proposal_id}).
    'unsure' is skipped. A pair of a proposal later reverted is skipped: the owner labelled it
    before a fault the page did not show was found, so the label is not trusted. The page's own
    model verdict ('g') is never read."""
    reverted = set(reverted_proposals)
    by_key = {str(p.get("key")): p for p in page_pairs}
    pairs: List[TruthPair] = []
    counts: Dict[str, int] = {}

    def bump(k: str) -> None:
        counts[k] = counts.get(k, 0) + 1

    for lab in labels:
        page = by_key.get(str(lab.get("pair_key")))
        if page is None:
            bump("label_without_page_pair")
            continue
        if str(lab.get("proposal_id") or page.get("pid")) in reverted or str(page.get("pid")) in reverted:
            bump("skipped_reverted_proposal")
            continue
        label = OWNER_LABELS.get(str(lab.get("label")))
        if label is None:
            bump(f"skipped_label:{lab.get('label')}")
            continue
        a, b = _page_side(page.get("L") or {}), _page_side(page.get("A") or {})
        if not a.get("title") or not b.get("title"):
            bump("skipped_missing_side")
            continue
        pairs.append(_pair(a, b, label=label, grade="gold", basis="owner_pair_label",
                           source=f"owner:{lab.get('pair_key')}", note={"tier": page.get("tier")}))
        bump(f"owner_{label}")
    return pairs, counts


# ---------------------------------------------------------------------------
# Source 2: live listings that share a barcode
# ---------------------------------------------------------------------------

def _text(row: Mapping[str, Any]) -> str:
    title, variant = str(row.get("title") or ""), str(row.get("variant_title") or "")
    if variant and variant.lower() not in title.lower() and variant.lower() not in ("default title", "default"):
        return f"{title} {variant}"
    return title


@dataclass
class _Read:
    row: Mapping[str, Any]
    gtin: str
    brand: str
    core: Any  # TitleCore

    @property
    def host(self) -> str:
        return str(self.row.get("host") or "").lower()


def _read(row: Mapping[str, Any]) -> Optional[_Read]:
    g = gtin_canonical(row.get("gtin"))
    if not g or gtin_is_restricted(g):
        return None
    brand = canonical_brand(row.get("brand"))
    if not brand:
        return None
    return _Read(row=row, gtin=g, brand=brand, core=title_core(row.get("brand"), _text(row)))


def _overlap(a: Any, b: Any) -> float:
    if not a.tokens or not b.tokens:
        return 0.0
    return len(a.tokens & b.tokens) / len(a.tokens | b.tokens)


def _concrete(reasons: Sequence[str], a: Any, b: Any) -> List[str]:
    """The veto reasons that STATE a difference: both sizes known and unequal, both shades named
    and unequal, a conflict token. 'size_unknown' and a one-sided shade prove nothing."""
    out = [r for r in reasons if r.startswith("conflict:")]
    if "size" in reasons and size_key(a.size) is not None and size_key(b.size) is not None:
        out.append("size")
    if "shade" in reasons and a.shade_tokens and b.shade_tokens:
        out.append("shade")
    return out


def from_barcode_rows(rows: Iterable[Mapping[str, Any]]) -> Tuple[List[TruthPair], Dict[str, int]]:
    """Silver pairs from listings sharing a barcode (rows: listing_key, host, brand, title,
    variant_title, gtin, price, currency, availability, image_url, url)."""
    counts: Dict[str, int] = {}

    def bump(k: str, n: int = 1) -> None:
        counts[k] = counts.get(k, 0) + n

    groups: Dict[str, List[_Read]] = {}
    for row in rows:
        r = _read(row)
        if r is None:
            bump("rows_skipped_no_valid_barcode_or_brand")
            continue
        groups.setdefault(r.gtin, []).append(r)
    pairs: List[TruthPair] = []
    clean: Dict[str, List[_Read]] = {}
    for g, members in sorted(groups.items()):
        if len({m.host for m in members}) < 2:
            bump("groups_one_host")
            continue
        if len({m.brand for m in members}) > 1:
            bump("groups_skipped_brands_disagree")  # one barcode, two brands: a data error, no label
            continue
        cross = [(x, y, veto_reasons(x.core, y.core)) for x, y in itertools.combinations(members, 2)
                 if x.host != y.host]
        stated = [(x, y, _concrete(reasons, x.core, y.core)) for x, y, reasons in cross]
        if any(why for _x, _y, why in stated):
            bump("groups_polluted")
            n = 0
            for x, y, why in stated:
                if why and n < MAX_PAIRS_PER_GROUP:
                    pairs.append(_pair(x.row, y.row, label="different", grade="silver", basis="barcode_collision",
                                       source=f"barcode:{g}", note={"stated": why}))
                    bump("barcode_collision")
                    n += 1
            continue
        clean[g] = members
        n = 0
        for x, y, reasons in cross:
            ov = _overlap(x.core, y.core)
            if reasons or ov < MIN_CORE_OVERLAP:
                bump("clean_pairs_unproven")  # e.g. a size one side does not state: no label
                continue
            if n < MAX_PAIRS_PER_GROUP:
                pairs.append(_pair(x.row, y.row, label="same", grade="silver", basis="barcode_clean",
                                   source=f"barcode:{g}", note={"core_overlap": round(ov, 2)}))
                bump("barcode_clean")
                n += 1
    # size variants: two clean barcodes, one brand, one title core, both sizes stated and unequal
    by_core: Dict[Tuple[str, str, frozenset, frozenset], List[Tuple[str, _Read]]] = {}
    for g, members in clean.items():
        m = members[0]
        if m.core.core and size_key(m.core.size) is not None:
            by_core.setdefault((m.brand, m.core.core, m.core.shade_tokens, m.core.conflicts), []).append((g, m))
    for _k, items in sorted(by_core.items(), key=lambda kv: kv[0][:2]):
        n = 0
        for (g1, x), (g2, y) in itertools.combinations(items, 2):
            if g1 != g2 and size_key(x.core.size) != size_key(y.core.size) and n < MAX_PAIRS_PER_GROUP:
                pairs.append(_pair(x.row, y.row, label="different", grade="silver", basis="size_variant",
                                   source=f"barcodes:{min(g1, g2)}+{max(g1, g2)}", note={"stated": ["size"]}))
                bump("size_variant")
                n += 1
    return pairs, counts


# ---------------------------------------------------------------------------
# Merge, judge input, scoring, files
# ---------------------------------------------------------------------------

_GRADE_RANK = {"gold": 0, "silver": 1, "inferred": 2}


def merge(*sets: Iterable[TruthPair]) -> Tuple[List[TruthPair], Dict[str, int]]:
    """One pair per pair_id. Two sources that disagree on the label drop the pair (counted);
    two that agree keep the higher grade."""
    by_id: Dict[str, TruthPair] = {}
    dropped: set = set()
    counts = {"conflicting_labels_dropped": 0, "duplicates_merged": 0}
    for p in itertools.chain(*sets):
        if p.pair_id in dropped:
            continue
        have = by_id.get(p.pair_id)
        if have is None:
            by_id[p.pair_id] = p
        elif have.label != p.label:
            del by_id[p.pair_id]
            dropped.add(p.pair_id)
            counts["conflicting_labels_dropped"] += 1
        else:
            counts["duplicates_merged"] += 1
            if _GRADE_RANK[p.grade] < _GRADE_RANK[have.grade]:
                by_id[p.pair_id] = p
    return [by_id[k] for k in sorted(by_id)], counts


def judge_view(p: TruthPair) -> Dict[str, Any]:
    """What a judge may see: both listings, never the barcode, the label, its grade or basis."""
    def strip(side: Mapping[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in side.items() if k in SIDE_FIELDS and k != "listing_key"}
    return {"pair_id": p.pair_id, "a": strip(p.a), "b": strip(p.b)}


def score(pairs: Sequence[TruthPair], verdicts: Mapping[str, str]) -> Dict[str, Any]:
    """Compare verdicts {pair_id: confirmed|likely|not_shown} with the labels, per grade.
    wrong_confirmed (a "different" pair confirmed) is the error that shows a buyer the wrong item;
    the gate is zero of them in gold and silver. With zero errors in n "different" pairs the 95%
    upper bound on the rate is about 3/n (the rule of three), reported so a clean run on a small
    set is not read as proof."""
    bad = {v for v in verdicts.values() if v not in VERDICTS}
    if bad:
        raise TruthSetError(f"unknown verdicts: {sorted(bad)}")
    out: Dict[str, Any] = {"schema": SCHEMA_VERSION, "by_grade": {}, "wrong_confirmed": [], "missing": 0}
    for grade in GRADES:
        ps = [p for p in pairs if p.grade == grade]
        same = [p for p in ps if p.label == "same"]
        diff = [p for p in ps if p.label == "different"]
        v = lambda p: verdicts.get(p.pair_id)  # noqa: E731
        wc = [p.pair_id for p in diff if v(p) == "confirmed"]
        wl = [p.pair_id for p in diff if v(p) == "likely"]
        conf = [p for p in ps if v(p) == "confirmed"]
        row = {
            "same": len(same), "different": len(diff),
            "same_confirmed": sum(v(p) == "confirmed" for p in same),
            "same_likely": sum(v(p) == "likely" for p in same),
            "same_not_shown": sum(v(p) == "not_shown" for p in same),
            "wrong_confirmed": len(wc), "wrong_likely": len(wl),
            "confirmed_precision": (round(sum(p.label == "same" for p in conf) / len(conf), 4) if conf else None),
            "wrong_confirmed_rate_upper95": (round(min(1.0, 3 / len(diff)), 4) if diff and not wc else None),
            "missing": sum(v(p) is None for p in ps),
        }
        out["by_grade"][grade] = row
        out["missing"] += row["missing"]
        if grade in ("gold", "silver"):
            out["wrong_confirmed"].extend(wc)
    out["gate"] = "pass" if not out["wrong_confirmed"] and not out["missing"] else "fail"
    return out


def _repo_root() -> str:
    return os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))


def _inside_repo(path: str, root: Optional[str] = None) -> bool:
    root = os.path.realpath(root or _repo_root())
    target = os.path.realpath(os.path.abspath(path))
    return os.path.commonpath([root, target]) == root


def write_jsonl(path: str, rows: Iterable[Mapping[str, Any]], *, repo_root: Optional[str] = None) -> int:
    """Write rows as JSON lines. Refuses any path inside this public repository: the set holds
    real store and product names."""
    if _inside_repo(path, repo_root):
        raise TruthSetError(f"refused: {path} is inside the public repository; write the truth set elsewhere")
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            n += 1
    return n


def to_row(p: TruthPair) -> Dict[str, Any]:
    return asdict(p)


def from_row(r: Mapping[str, Any]) -> TruthPair:
    if r.get("schema") != SCHEMA_VERSION:
        raise TruthSetError(f"schema {r.get('schema')!r} is not {SCHEMA_VERSION}")
    return TruthPair(**{k: r[k] for k in ("pair_id", "a", "b", "label", "grade", "basis", "grain",
                                          "source", "note", "schema")})


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def summary(pairs: Sequence[TruthPair]) -> Dict[str, Any]:
    """Counts only (no names): safe to print in a job log or a PR."""
    out: Dict[str, Any] = {"pairs": len(pairs)}
    for p in pairs:
        k = f"{p.grade}:{p.label}:{p.basis}"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))
