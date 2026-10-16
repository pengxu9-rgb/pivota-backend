#!/usr/bin/env python3
"""Repair defective seed MAIN variant lists (`external_product_seeds.seed_data.variants`). Dry run first.

WHY. Production serves the MAIN list only, and until #2634 the JSON-LD extractor wrote three shapes into it that
are not one entry per merchant variant. #2634 stops the extractor writing them; the lists already stored stay as
they are, because the nightly refresh's reconcile cannot fold two stored entries that both claim one read offer
(it re-reads neither). This one-off repairs exactly those three stored shapes and nothing else:

  (ii)  [link, barcode] -- ONE variant stored twice: its JSON-LD `@id` link ("/products/<h>?variant=<N>#offer")
        and an entry keyed by its 13-digit GTIN (or a 12/14/8-digit barcode), at the same price/currency/stock.
        A numeric key is a barcode ONLY with a GS1 length AND a valid check digit; any other numeric key of 8+
        digits may be another variant's id (Shopify ids fail the check digit) and is HELD (`key_may_be_variant_id`).
  (iii) plan list -- subscription selling plans stored as variants: positional `offer_1..offer_N` entries titled
        with plan vocabulary ("One-Time Purchase", "1 month", "Subscribe & Save", "every 6 weeks"...)
        (murad eps_0f70f7177cecd95416d98b53).
  (iv)  [link, sku] -- a link entry plus an entry keyed by the merchant SKU ("114807"), same price/currency/stock
        (jurlique eps_019f2b9eef9a429385cccc5c).

THE RULES ARE #2634's. #2634 (claude/seed-variant-extractor-dedupe) is not on main when this is written, so the
pure rules are re-implemented here with the SAME vocabulary and guards: `_PLAN_NAME`, `_PLAN_CADENCE`,
`_ONE_TIME_NAME` are copied verbatim from its `services/external_offers_service._selling_plan_reasons`, and the
fold guards mirror its `_fold_same_variant_reads` / `_entries_name_different_variants`. The variant-id spellings
come from the shared helpers on main (`services/handover_variant_identity`): `url_named_variant_id`,
`canonical_variant_id`, `stored_entry_claimed_variant_ids`, `snapshot_names_one_variant`,
`namers_agree_on_price` (the last as the post-fold invariant).

CONSERVATIVE: WHEN IN DOUBT THE SEED IS HELD (untouched, listed with its reason).
  (ii)/(iv) fold the pair into ONE entry only when POSITIVELY linked:
     * `link_mark`  -- the non-link entry carries `offer_shopify_variant_id` / a `url` naming the same ?variant=N;
     * `snapshot`   -- exactly one `snapshot.variants` entry carries that SKU/barcode and it names variant N;
     * `link_field` -- the link entry itself already carries that SKU/barcode as a field;
     * `sole_pair`  -- the list is exactly [one link, one non-link] with identical price, currency and stock, the
                       snapshot holds exactly one variant and it is N (`snapshot_names_one_variant`), AND the
                       entry passes the read-time rule `entry_is_the_sole_variant_read_again` (no other
                       merchant-issued-shaped id: a GTIN never folds this way), its title (if any) is the product's
                       name or the kept entry's, and it states no size the kept entry does not. The snapshot is
                       EVIDENCE only, never copied (Peng, 2026-10-16). A sole pair drops the duplicate and writes
                       NOTHING of it onto the kept entry, and is written only with `--include-sole-pair`.
     The kept entry is the LINK entry (its `variant_id` and every other field kept); on positive evidence the folded
     entry's key is added to it as `sku` (iv) or `barcode` (ii), with the folded entry's own `sku`/`barcode` fields
     when the kept one lacks them, and its title only when the kept one has none. Tie evidence is read from
     `sku`/`sku_id` and the barcode fields only, never `mpn` (a product-level model number). A code about to be
     copied onto the kept entry that may name a variant (a `sku` of 8+ digits failing the GS1 check, a `barcode`
     that is not a valid GTIN, or a value equal to another entry's id or ?variant=) holds the whole seed
     (`copied_code_may_be_variant_id`): `sku` is a key the stored-variant matchers read. Never folded: differing SKUs, differing
     barcodes, differing or unknown prices, differing or missing currencies, differing stock, differing titles
     that are not the product's name, two ?variant= ids, a key that is itself another variant's id. Every
     non-link entry of a seed must be tied, or the whole seed is held (a partial repair still serves a duplicate).
  (iii) drop the plan entries (#2634's rules: by name only for positional entries, never a lone entry, never one
     named as the product, never every entry). Exactly one entry left -> kept, retitled to the product's name when
     its title is a purchase mode ("One-Time Purchase"). None left, several left, or a plan-shaped list whose rules
     drop nothing (no one-time anchor) -> HELD and listed in the `reread` line for a fresh page re-read. As in #2634,
     prepaid subscription entries ("3 Month Subscription" beside "Single Box") and day cadences ("30 days" beside
     "One-Time Purchase") are dropped as plans; the dry run's `notes` line says so.

WHAT IT WRITES. Only `seed_data.variants`, rebuilt IN SQL from the stored elements (kept elements by ordinal, plus
a string patch on the kept one), so no price/stock bytes are round-tripped through Python. Never
`snapshot.variants`, never a list where there was none, never `extracted_at`, never `updated_at`: the next refresh
decides freshness.

BOUND TO THE REVIEWED DRY RUN. The dry run pages every active seed in ONE read-only transaction (statement_timeout
60s), detoasting each seed_data once, and plans the candidates (`seed_data.variants` an array of 2+). It prints JSON
lines: counts per kind, held by reason, confirmation counts for shapes needing no action (and active seeds with a
snapshot quality summary but no root one), the `sole_pair` lane on its own line, (iii) seeds whose seed-level price
differs from the kept entry's, `notes`, per-market / per-host top 20, the digest (sha256 over the ordered
(seed_id, before_hash, after_hash) set), the `reread` ids, and 10 before/after samples per kind. `--apply` re-runs that dry
run in the same process and refuses unless its digest equals `--expect-digest`; `--max-seeds` caps the plan BEFORE
the digest (so run the dry run with the same caps you apply with).

CAS. Each write is `WHERE id = :id AND status = 'active' AND <fingerprint> = :fp`, the fingerprint being md5 over
everything the plan read (variants, snapshot.variants, the product-name fields). A seed changed since the dry run
is SKIPPED and counted (`cas_skipped`), never written. Writes go in batches (`--batch-size`, `--sleep` between),
each its own short transaction under lock_timeout 5s / statement_timeout 30s.

AUDIT AND REVERT (the reversible seed-repair pattern of scripts/backfill_seed_variant_id_provenance.py). A run
manifest goes to identity_resolution_events before any write. Each seed's write and its revert record (its exact
before-variants, copied in SQL, plus its post-write fingerprint) are ONE statement, so no seed is written without
its record. One writer_audit_log row per batch carries `dry_run_report_hash` = the digest. `--revert <run_id>`
restores the before-variants of every seed still at its post-write fingerprint AND its apply-time `updated_at` and
`snapshot.extracted_at`; a seed rewritten or re-stamped since (the nightly refresh) is left as it is and reported
`changed_since_apply`, and one an interrupted earlier revert already restored is reported `already_reverted`. Refused while a LATER unreverted run wrote
any of the same seeds.

  Dry run (default; READ ONLY; writes nothing):
    scripts/ops/run_oneoff_job.sh scripts/ops/repair_seed_main_lists.py [--kinds iii] [--max-seeds 10]
  Apply exactly the reviewed plan:
    ... scripts/ops/repair_seed_main_lists.py --apply --expect-digest <digest> --max-seeds <n> [same caps]
  Revert a run:
    ... scripts/ops/repair_seed_main_lists.py --revert mainlist_<hex>

Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh on the pinned merge image (IMAGE=...), the same
image for the dry run and the apply. Never run it from a laptop against production.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlparse, urlsplit

# `python -c` / an exec has no __file__: fall back to the repo root as the working directory.
_HERE = globals().get("__file__") or os.path.join(os.getcwd(), "scripts", "ops", "repair_seed_main_lists.py")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(_HERE)))))

from services.handover_variant_identity import (  # noqa: E402
    canonical_variant_id,
    entry_is_the_sole_variant_read_again,
    namers_agree_on_price,
    snapshot_names_one_variant,
    stored_entry_claimed_variant_ids,
    url_named_variant_id,
)
from utils.availability_vocabulary import normalize_availability  # noqa: E402

WRITER = "seed_main_list_repair_v1"
RUN_ACTION = "seed_main_list_repair_manifest"
SEED_ACTION = "seed_main_list_repair_seed"
APPLIED_ACTION = "seed_main_list_repair_applied"
REVERTED_ACTION = "seed_main_list_repair_reverted"

KINDS = ("ii", "iii", "iv", "ii+iv")
PAGE = 500
SAMPLES = 10
TOP = 20
BATCH_SIZE = 25
SLEEP_S = 1.0
READ_TIMEOUT = "60s"
LOCK_TIMEOUT = "5s"
WRITE_TIMEOUT = "30s"

# ── #2634's selling-plan vocabulary, verbatim (services/external_offers_service on that branch) ───────────────
_SELLING_PLAN_KEYS = ("sellingPlan", "selling_plan", "sellingPlanId", "selling_plan_id")
_PLAN_NAME = re.compile(
    r"\bsubscri(?:be|bed|bing|ption)s?\b"
    r"|\bauto[\s-]?(?:replenish\w*|ship\w*|deliver\w*|renew\w*)"
    r"|\b(?:deliver(?:y|ed|s)?|ship(?:s|ped|ment)?|sent)\s+every\b"
    r"|\bevery\s+\d+\s*-?\s*(?:day|week|month)s?\b",
    re.IGNORECASE,
)
_PLAN_CADENCE = re.compile(r"^\s*(?:every\s+)?\d+\s*-?\s*(?:day|week|month)s?\s*$", re.IGNORECASE)
_ONE_TIME_NAME = re.compile(
    r"^\s*(?:one[\s-]?time(?:\s+(?:purchase|order|buy|delivery))?|buy\s+once|purchase\s+once|single\s+purchase)\s*$",
    re.IGNORECASE,
)

_POSITIONAL = re.compile(r"^offer_\d+$")  # routes/employee_products._POSITIONAL_OFFER_ID
_BARCODE_LENGTHS = (8, 12, 13, 14)
# Codes a snapshot / link entry may carry a SKU or a barcode under.
_SKU_FIELDS = ("sku", "sku_id")
_BARCODE_FIELDS = ("barcode", "gtin13", "gtin", "gtin12", "gtin14", "gtin8")
# The ONLY fields that tie a code to a variant. Never `mpn`: a model number is product-level, shared by every
# variant (review of #2638).
_TIE_FIELDS = _SKU_FIELDS + _BARCODE_FIELDS
# The fields that must agree on the code a fold adds.
_AGREE_FIELDS = {"sku": _SKU_FIELDS, "barcode": _BARCODE_FIELDS}
_STOCK_KEYS = ("availability", "in_stock", "available", "stock", "inventory_status")
# A Shopify single-variant product's own variant title: the product's name for title compatibility.
_PRODUCT_NAME_TITLES = ("default title",)


# ── pure helpers ──────────────────────────────────────────────────────────────────────────────────────────────

def _json(value: Any) -> Any:
    """jsonb reaches a raw `databases`+asyncpg read as TEXT (handover_variant_identity._payload)."""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def _norm(value: Any) -> str:
    return " ".join(_text(value).split()).casefold()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def gs1_valid(digits: str) -> bool:
    """A GTIN-8/12/13/14: a GS1 length AND a valid mod-10 check digit (weights 3,1,... from the right)."""
    if not (digits.isascii() and digits.isdigit()) or len(digits) not in _BARCODE_LENGTHS:
        return False
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == int(digits[-1])


def key_class(entry: Mapping[str, Any]) -> str:
    """What an entry's `variant_id` is: link | positional | barcode | numeric_id | digits | sku | gid | link_other |
    empty.

    A numeric key is a `barcode` ONLY when it is a valid GTIN (GS1 length and check digit). Any other numeric key
    of 8+ digits is `numeric_id`: the shape `services.variant_identity` calls merchant-issued (a Shopify variant id
    is 11-14 digits and fails the check digit 9 times in 10), so it may be ANOTHER variant's id and is never folded
    (review of #2638, H1). A shorter numeric key ("114807") is a merchant SKU (`digits`)."""
    vid = _text(entry.get("variant_id"))
    if not vid:
        return "empty"
    if _POSITIONAL.match(vid):
        return "positional"
    if url_named_variant_id(vid):
        return "link"
    if "variant=" in vid or "selling_plan" in vid or "/products/" in vid or vid.startswith(("http://", "https://", "/")):
        return "link_other"
    if vid.startswith("gid://"):
        return "gid"
    if vid.isascii() and vid.isdigit():
        if gs1_valid(vid):
            return "barcode"
        return "numeric_id" if len(vid) >= 8 else "digits"
    return "sku"


def product_names(row: Mapping[str, Any]) -> List[str]:
    """The names a seed calls its product by, in retitle preference order."""
    out: List[str] = []
    for key in ("sd_title", "sd_product_name", "title", "snapshot_title"):
        name = _text(row.get(key))
        if name and name not in out:
            out.append(name)
    return out


def _is_product_name(title: Any, names: Sequence[str]) -> bool:
    t = _norm(title)
    return bool(t) and (t in {_norm(n) for n in names} or t in _PRODUCT_NAME_TITLES)


def _titles_compatible(a: Any, b: Any, names: Sequence[str]) -> bool:
    """#2634's `_entries_name_different_variants` / `_titles_agree`: two titles name different variants only when
    both are present, differ, and neither is just the product's name."""
    na, nb = _norm(a), _norm(b)
    return not na or not nb or na == nb or _is_product_name(a, names) or _is_product_name(b, names)


_SIZE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*-?\s*(fl\.?\s*oz|ml|cl|l|kg|mg|g|oz|lbs?|ct|count|pcs?|pack|caps(?:ules)?|tabs?)(?![a-z])",
    re.IGNORECASE)


def size_tokens(*texts: Any) -> Set[str]:
    """The sizes a title / SKU states ("50 mL", "SIZE-15ML", "1.7 fl. oz"), normalised ("50ml", "15ml", "1.7floz")."""
    out: Set[str] = set()
    for t in texts:
        for amount, unit in _SIZE.findall(_text(t)):
            amount = amount.replace(",", ".")
            if "." in amount:
                amount = amount.rstrip("0").rstrip(".")
            out.add(amount + re.sub(r"[\s.]", "", unit.lower()))
    return out


def _price(entry: Mapping[str, Any]) -> Optional[float]:
    for key in ("price_amount", "price"):  # handover_variant_identity._entry_price's precedence
        value = entry.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None
    return None


def _currency(entry: Mapping[str, Any]) -> str:
    return _text(entry.get("price_currency") or entry.get("currency")).upper()


def _stock(entry: Mapping[str, Any]) -> tuple:
    return tuple(normalize_availability(entry.get(k)) if k in entry else "<absent>" for k in _STOCK_KEYS) + (
        _text(entry.get("inventory_quantity")),)


def _code(value: Any, *, barcode: bool = False) -> str:
    """One spelling of a code for comparison: a SKU case-folded (#2634's norm), a BARCODE without its leading zeros
    (GTIN-13 vs its GTIN-14 padding). A SKU keeps its zeros: "0123" is not "123" (review of #2638, L2)."""
    t = _text(value)
    if barcode and t.isascii() and t.isdigit():
        return t.lstrip("0") or "0"
    return t.casefold()


def _codes(entry: Mapping[str, Any], fields: Sequence[str]) -> Set[str]:
    return {_code(entry.get(k), barcode=k in _BARCODE_FIELDS) for k in fields if _text(entry.get(k))}


def _key_codes(key: str, cls: str) -> Set[str]:
    """The spellings a stored key may meet a code field in: as a SKU, and as a barcode when it is one."""
    return {_code(key)} | ({_code(key, barcode=True)} if cls == "barcode" else set())


def _structural_plan(entry: Mapping[str, Any]) -> bool:
    """#2634's STRUCTURAL signals, as far as a stored entry can carry them: a truthy selling-plan key, or an
    id/url with a non-empty `selling_plan=` (Shopify's plan permalink)."""
    if any(entry.get(k) and not isinstance(entry.get(k), bool) for k in _SELLING_PLAN_KEYS):
        return True
    for key in ("variant_id", "url", "@id"):
        raw = entry.get(key)
        if not isinstance(raw, str) or "selling_plan" not in raw:
            continue
        try:
            query = parse_qsl(urlparse(raw.strip()).query, keep_blank_values=True)
        except ValueError:
            continue
        if any(k == "selling_plan" and v.strip() for k, v in query):
            return True
    return False


def plan_entries(entries: Sequence[Mapping[str, Any]], names: Sequence[str]) -> Tuple[Dict[int, str], Set[int], bool]:
    """(index -> plan signal, indices of positional one-time entries, plan-shaped?) for ONE stored list.

    #2634's `_selling_plan_reasons` on stored entries: names judge POSITIONAL entries only; a name equal to the
    product's is never a plan; a cadence-only name ("2 months") is a plan only beside a one-time entry or another
    plan; names never drop every entry. `plan-shaped` is True whenever the list LOOKS like a plan list (any plan
    signal or plan-like name), so a list the rules cannot repair is held, not silently passed over.
    """
    reasons: Dict[int, str] = {}
    named: List[Tuple[int, str]] = []
    one_time: Set[int] = set()
    for i, e in enumerate(entries):
        if _structural_plan(e):
            reasons[i] = "selling_plan"
            continue
        if key_class(e) != "positional":
            continue
        title = _text(e.get("title"))
        if not title or _is_product_name(title, names):
            continue
        if _ONE_TIME_NAME.match(title):
            one_time.add(i)
        else:
            named.append((i, title))
    by_name: Dict[int, str] = {}
    if len(entries) > 1:
        by_name = {i: "plan_name" for i, t in named if _PLAN_NAME.search(t)}
        if one_time or by_name or reasons:
            by_name.update({i: "plan_cadence" for i, t in named if i not in by_name and _PLAN_CADENCE.match(t)})
        if by_name and len(reasons) + len(by_name) < len(entries):
            reasons.update(by_name)
    cadence = [i for i, t in named if _PLAN_CADENCE.match(t)]
    shaped = bool(reasons) or bool(by_name) or any(_PLAN_NAME.search(t) for _, t in named) or len(cadence) >= 2 or (
        bool(one_time) and bool(cadence))
    return reasons, (one_time if reasons else set()), shaped


def _held(kind: str, reason: str, **extra: Any) -> Dict[str, Any]:
    return {"action": "held", "kind": kind, "reason": reason, **extra}


def plan_plan_list(entries: List[Dict[str, Any]], names: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Kind (iii), or None when the list is not plan-shaped."""
    plans, _one_time, shaped = plan_entries(entries, names)
    if not shaped:
        return None
    if not plans:
        return _held("iii", "plan_list_no_one_time", reread=True)
    remaining = [i for i in range(len(entries)) if i not in plans]
    if not remaining:
        return _held("iii", "plan_list_no_one_time", reread=True)
    if len(remaining) > 1:
        return _held("iii", "plan_list_ambiguous_remaining", reread=True)
    (keep,) = remaining
    patch: Dict[str, Any] = {}
    title = _text(entries[keep].get("title"))
    if title and _ONE_TIME_NAME.match(title):  # a purchase mode, not a variant: titled as an unnamed offer
        if not names:
            return _held("iii", "no_product_name_to_retitle", reread=True)
        patch["title"] = names[0]
    return {"action": "write", "kind": "iii", "ops": [{"pos": 0, "i": keep, "patch": patch}],
            "evidence": sorted(set(plans.values())), "dropped": sorted(plans)}


def _snapshot_variants(row: Mapping[str, Any]) -> List[Dict[str, Any]]:
    snap = _json(row.get("snapshot_variants"))
    return [s for s in snap if isinstance(s, dict)] if isinstance(snap, list) else []


def _snapshot_claims(s: Mapping[str, Any]) -> Set[str]:
    out = set(stored_entry_claimed_variant_ids(s))
    named = url_named_variant_id(s.get("url"))
    if named:
        out.add(named)
    return {x for x in out if x.isdigit()}


def _tie_of(x: Dict[str, Any], key: str, cls: str, links: Dict[int, str], entries: List[Dict[str, Any]],
            snapshot: List[Dict[str, Any]]) -> Tuple[Optional[str], List[str], Optional[str]]:
    """(variant N the non-link entry is positively tied to, evidence, hold reason)."""
    ns = set(links.values())
    tie: Optional[str] = None
    evidence: List[str] = []

    def take(n: str, source: str) -> Optional[str]:
        nonlocal tie
        if tie is not None and tie != n:
            return "evidence_disagrees"
        tie = n
        evidence.append(source)
        return None

    # (a) its own link marks
    marks = {m for m in (_text(x.get("offer_shopify_variant_id")),) if m.isdigit()}
    for k in ("url", "@id", "link"):
        named = url_named_variant_id(x.get(k))
        if named:
            marks.add(named)
    if len(marks) > 1:
        return None, evidence, "names_two_variants"
    if marks:
        (n,) = marks
        if n not in ns:
            return None, evidence, "names_variant_not_in_list"
        take(n, "link_mark")

    # (b) the key is itself a variant id the snapshot names (another variant, not a code)
    if snapshot and any(canonical_variant_id(key) in _snapshot_claims(s) for s in snapshot):
        return None, evidence, "key_is_a_variant_id"

    # (c) a snapshot entry carrying this code names the variant
    codes = _key_codes(key, cls) | _codes(x, _TIE_FIELDS)
    matches = [s for s in snapshot if codes & _codes(s, _TIE_FIELDS)]
    if matches:
        claimed = set().union(*(_snapshot_claims(s) for s in matches))
        if len(matches) != 1 or len(claimed) != 1:
            return None, evidence, "snapshot_ambiguous"
        (n,) = claimed
        if n not in ns:
            return None, evidence, "snapshot_names_variant_not_in_list"
        why = take(n, "snapshot")
        if why:
            return None, evidence, why

    # (d) a link entry already carries this code as a field
    carriers = {n for i, n in links.items() if _key_codes(key, cls) & _codes(entries[i], _TIE_FIELDS)}
    if len(carriers) > 1:
        return None, evidence, "link_fields_ambiguous"
    if carriers:
        why = take(next(iter(carriers)), "link_field")
        if why:
            return None, evidence, why
    return tie, evidence, None


def _fold_refusal(kept: Mapping[str, Any], x: Mapping[str, Any], names: Sequence[str]) -> Optional[str]:
    pk, px = _price(kept), _price(x)
    if pk is None or px is None:
        return "price_unknown"
    if pk != px:
        return "price_differs"
    ck, cx = _currency(kept), _currency(x)
    if not ck or not cx:
        return "currency_unknown"
    if ck != cx:
        return "currency_differs"
    if _stock(kept) != _stock(x):
        return "stock_differs"
    if kept.get("offer_aggregate") or x.get("offer_aggregate"):
        return "aggregate_offer"
    if not _titles_compatible(kept.get("title"), x.get("title"), names):
        return "title_differs"
    return None


def _copied_code_may_be_variant_id(field: str, value: str, x: Mapping[str, Any],
                                   entries: Sequence[Mapping[str, Any]]) -> bool:
    """May a code about to be written onto the kept entry name a VARIANT? `sku` is a key every stored-variant
    matcher reads (`STORED_VARIANT_ID_KEYS`), so a variant id written there makes the kept entry answer for that
    variant: an offer for B would resolve to A (review of #2638). Refused when:
      * a `sku` of 8+ digits that is not a valid GTIN (the merchant-issued shape), or a `barcode` that is not one;
      * it equals another entry's id, or the ?variant= another entry's id or url names.
    """
    v = _text(value)
    if field == "sku" and v.isascii() and v.isdigit() and len(v) >= 8 and not gs1_valid(v):
        return True
    if field == "barcode" and not gs1_valid(v):
        return True
    others: Set[str] = set()
    for e in entries:
        if e is x:
            continue
        others |= {_text(e.get("variant_id"))} | set(stored_entry_claimed_variant_ids(e))
        named = url_named_variant_id(e.get("url"))
        if named:
            others.add(named)
    return bool({v, canonical_variant_id(v)} & (others - {""}))


def _sole_pair_refusal(kept: Mapping[str, Any], x: Mapping[str, Any], n: str, entries: Sequence[Dict[str, Any]],
                       seed_data: Mapping[str, Any], row: Mapping[str, Any], names: Sequence[str]) -> Optional[str]:
    """Why a sole pair may NOT fold, or None. Its only evidence is a stale snapshot's variant COUNT, so it must
    pass the read-time rule it mirrors and must not look like a second variant (review of #2638, H1/M1):

      * `services.handover_variant_identity.entry_is_the_sole_variant_read_again` (the 10-15 ruling): the entry
        claims no other merchant-issued-shaped id, and its price is the one entry naming N's;
      * its title, when it has one, is the product's name or the kept entry's title (a related "Lip Liner" node
        at the same price is another product, not a second listing);
      * no size its title or SKU states ("SIZE-15ML") that the kept entry / product name does not state.
    """
    product = row.get("platform_product_id")
    seed = {**seed_data, "price_amount": row.get("seed_price")}
    if not entry_is_the_sole_variant_read_again(dict(x), variant_id=n, stored=list(entries), seed_data=seed,
                                                 product_key=row.get("attached_product_key"),
                                                 product_id=product):
        return "sole_pair_read_rule_refuses"
    title = _text(x.get("title"))
    if title and not _is_product_name(title, names) and _norm(title) != _norm(kept.get("title")):
        return "sole_pair_title_differs"
    mine = size_tokens(title, x.get("variant_id"), x.get("sku"), x.get("sku_id"))
    theirs = size_tokens(kept.get("title"), kept.get("sku"), kept.get("sku_id"), *names)
    if mine - theirs:
        return "sole_pair_size_differs"
    return None


def plan_pairs(entries: List[Dict[str, Any]], row: Mapping[str, Any], names: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Kinds (ii) / (iv), or None when the list is not a [link, non-link] list."""
    classes = [key_class(e) for e in entries]
    links = {i: url_named_variant_id(entries[i].get("variant_id")) for i, c in enumerate(classes) if c == "link"}
    nonlinks = [i for i, c in enumerate(classes) if c in ("barcode", "sku", "digits", "numeric_id")]
    if not links or not nonlinks:
        return None
    ns = list(links.values())
    kinds = {"ii" if classes[i] == "barcode" or (classes[i] == "numeric_id" and len(
        _text(entries[i].get("variant_id"))) in _BARCODE_LENGTHS) else "iv" for i in nonlinks}
    kind = "ii+iv" if len(kinds) > 1 else next(iter(kinds))
    if any(c not in ("link", "barcode", "sku", "digits", "numeric_id") for c in classes):
        return _held(kind, "other_entry_shapes")
    if len(set(ns)) != len(ns):
        return _held(kind, "duplicate_link_entries")
    if any(canonical_variant_id(entries[i].get("variant_id")) in set(ns) for i in nonlinks):
        return _held(kind, "numeric_id_beside_its_link")
    if any(classes[i] == "numeric_id" for i in nonlinks):
        return _held(kind, "key_may_be_variant_id")

    seed_data = {"snapshot": {"variants": _snapshot_variants(row)}}
    snapshot = seed_data["snapshot"]["variants"]
    ties: Dict[int, str] = {}
    evidence: Set[str] = set()
    for i in nonlinks:
        tie, ev, why = _tie_of(entries[i], _text(entries[i].get("variant_id")), classes[i], links, entries, snapshot)
        if why:
            return _held(kind, why)
        if tie is not None:
            ties[i] = tie
            evidence.update(ev)
    link_of = {n: i for i, n in links.items()}
    untied = [i for i in nonlinks if i not in ties]
    sole: Set[int] = set()
    if untied:
        n = ns[0] if len(ns) == 1 else None
        if not (len(entries) == 2 and len(links) == 1 and n and snapshot_names_one_variant(seed_data, n)):
            return _held(kind, "no_positive_link", reread=True)
        why = _sole_pair_refusal(entries[link_of[n]], entries[untied[0]], n, entries, seed_data, row, names)
        if why:
            return _held(kind, why, reread=True)
        ties[untied[0]] = n
        sole.add(untied[0])
        evidence.add("sole_pair")

    patches: Dict[int, Dict[str, Any]] = {i: {} for i in links}
    folded: Set[int] = set()
    for i in nonlinks:
        n = ties[i]
        k = link_of[n]
        kept = {**entries[k], **patches[k]}
        x = entries[i]
        why = _fold_refusal(kept, x, names)
        if why:
            return _held(kind, why)
        key = _text(x.get("variant_id"))
        field = "barcode" if classes[i] == "barcode" else "sku"
        fields = _AGREE_FIELDS[field]
        other = "sku" if field == "barcode" else "barcode"
        # The kept entry, the folded entry's own fields and the snapshot's entry for N must all agree on the code
        # this fold adds, and on the other code wherever two of them state one.
        snap_n = [s for s in snapshot if n in _snapshot_claims(s)]
        for source in [kept, x] + snap_n:
            if _codes(source, fields) - {_code(key, barcode=field == "barcode")}:
                return _held(kind, f"{field}_differs")
        for source in [x] + snap_n:
            mine, theirs = _codes(kept, _AGREE_FIELDS[other]), _codes(source, _AGREE_FIELDS[other])
            if mine and theirs and mine != theirs:
                return _held(kind, f"{other}_differs")
        folded.add(i)
        if i in sole:
            # A sole pair is tied by the snapshot's variant COUNT alone, which says nothing about the code: the
            # duplicate is dropped, and nothing of it is written onto the kept entry (review of #2638, M1).
            continue
        copies: Dict[str, str] = {}
        if not _text(kept.get(field)):
            copies[field] = key
        for f in ("sku", "barcode"):
            if f != field and _text(x.get(f)) and not _text(kept.get(f)) and f not in patches[k]:
                copies[f] = _text(x.get(f))
        if any(_copied_code_may_be_variant_id(f, v, x, entries) for f, v in copies.items()):
            return _held(kind, "copied_code_may_be_variant_id")
        patches[k].update(copies)
        if not _text(kept.get("title")) and _text(x.get("title")):
            patches[k]["title"] = _text(x.get("title"))

    ops = [{"pos": pos, "i": i, "patch": patches.get(i, {})}
           for pos, i in enumerate(j for j in range(len(entries)) if j not in folded)]
    after = apply_ops(entries, ops)
    # The post-fold invariant every reader of the main list relies on (#2627 review): the entries naming a
    # variant cannot disagree about its price.
    if not all(namers_agree_on_price(after, n) for n in ns):
        return _held(kind, "namers_disagree_after_fold")
    return {"action": "write", "kind": kind, "ops": ops, "evidence": sorted(evidence), "dropped": sorted(folded)}


def apply_ops(entries: Sequence[Mapping[str, Any]], ops: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """What the SQL rebuild writes, in Python: kept elements by ordinal, in `pos` order, each `|| patch`."""
    return [{**entries[op["i"]], **op["patch"]} for op in sorted(ops, key=lambda o: o["pos"])]


def confirmation_shape(entries: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Shapes the controller verified re-read fine after #2634 (counted, never written)."""
    classes = {key_class(e) for e in entries}
    return {frozenset({"barcode"}): "i_gtin_only", frozenset({"sku"}): "v_sku_only",
            frozenset({"link"}): "vii_link_only", frozenset({"digits"}): "digits_only",
            frozenset({"numeric_id"}): "numeric_ids_only"}.get(frozenset(classes))


def _host(row: Mapping[str, Any]) -> str:
    host = _text(row.get("domain")).lower()
    if not host:
        try:
            host = (urlsplit(_text(row.get("canonical_url") or row.get("destination_url"))).hostname or "").lower()
        except ValueError:
            host = ""
    return host[4:] if host.startswith("www.") else host


def plan_seed(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The whole decision for one seed, pure."""
    raw = _json(row.get("variants"))
    base = {"seed_id": row["id"], "market": _text(row.get("market")).upper() or "?", "host": _host(row) or "?",
            "fp": row.get("fp")}
    if not isinstance(raw, list) or len(raw) < 2:
        return {**base, "action": "none"}
    if not all(isinstance(e, dict) for e in raw):
        return {**base, **_held("?", "non_object_entry")}
    entries: List[Dict[str, Any]] = raw
    names = product_names(row)
    decision = plan_plan_list(entries, names) or plan_pairs(entries, row, names)
    if decision is None:
        return {**base, "action": "none", "shape": confirmation_shape(entries)}
    out = {**base, **decision, "before": entries}
    if decision["action"] == "write":
        after = apply_ops(entries, decision["ops"])
        out.update(after=after, before_hash=canonical_hash(entries), after_hash=canonical_hash(after))
        if decision["kind"] == "iii":
            seed_price, kept_price = _price({"price_amount": row.get("seed_price")}), _price(after[0])
            out["seed_price_differs"] = seed_price is not None and seed_price != kept_price
    return out


def cap(seeds: List[Dict[str, Any]], *, kinds: Optional[Iterable[str]] = None,
        max_seeds: Optional[int] = None, include_sole_pair: bool = False) -> List[Dict[str, Any]]:
    """Narrow the writes BEFORE the digest: a capped dry run and its apply carry the same caps. A fold whose only
    evidence is `sole_pair` is written only with `include_sole_pair` (Peng approves that lane explicitly)."""
    want = set(kinds or ())
    kept = 0
    for s in sorted(seeds, key=lambda s: s["seed_id"]):
        if s["action"] != "write":
            continue
        if "sole_pair" in s.get("evidence", ()) and not include_sole_pair:
            s.update(action="held", reason="sole_pair_not_selected")
        elif want and s["kind"] not in want:
            s.update(action="held", reason="kind_not_selected")
        elif max_seeds is not None and kept >= max_seeds:
            s.update(action="held", reason="max_seeds")
        else:
            kept += 1
    return seeds


def plan_digest(seeds: Iterable[Mapping[str, Any]]) -> str:
    planned = sorted((s["seed_id"], s["before_hash"], s["after_hash"]) for s in seeds if s["action"] == "write")
    return hashlib.sha256(json.dumps(planned, separators=(",", ":")).encode()).hexdigest()


def _brief(entries: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    keys = ("variant_id", "title", "price_amount", "price_currency", "availability", "sku", "barcode")
    return [{k: e.get(k) for k in keys if k in e} for e in entries[:8]]


def report_lines(p: Mapping[str, Any], samples: int = SAMPLES) -> List[Dict[str, Any]]:
    seeds = p["seeds"]
    writes = [s for s in seeds if s["action"] == "write"]
    held = [s for s in seeds if s["action"] == "held"]
    kinds = Counter(s["kind"] for s in writes)
    held_reasons = Counter(s["reason"] for s in held)
    sole_w = [s for s in writes if "sole_pair" in s.get("evidence", ())]
    sole_h = [s for s in held if s["reason"] == "sole_pair_not_selected"]
    price_w = [s for s in writes if s.get("seed_price_differs")]
    by_market: Dict[str, Counter] = defaultdict(Counter)
    by_host: Dict[str, Counter] = defaultdict(Counter)
    for s in writes + held:
        label = s["kind"] if s["action"] == "write" else "held"
        for bucket, key in ((by_market, s["market"]), (by_host, s["host"])):
            bucket[key][label] += 1
            bucket[key]["total"] += 1

    def top(bucket: Dict[str, Counter]) -> List[Dict[str, Any]]:
        ranked = sorted(bucket.items(), key=lambda kv: (-kv[1]["total"], kv[0]))[:TOP]
        return [{"key": k, **dict(c)} for k, c in ranked]

    lines: List[Dict[str, Any]] = [
        {"line": "counts", "candidates": p["scanned"], "to_write": len(writes),
         "kinds": {k: kinds.get(k, 0) for k in KINDS},
         "evidence": dict(Counter(e for s in writes for e in s["evidence"])),
         "held": dict(held_reasons),
         "held_by_kind": {k: dict(Counter(s["reason"] for s in held if s["kind"] == k))
                          for k in sorted({s["kind"] for s in held})},
         "entries_dropped": sum(len(s["dropped"]) for s in writes)},
        {"line": "confirmation", **{k: v for k, v in sorted(Counter(
            s.get("shape") for s in seeds if s["action"] == "none" and s.get("shape")).items())},
         "snapshot_quality_summary_without_root": p.get("summary_gap")},
        # Its own line so the lane is approved explicitly (--include-sole-pair): its only evidence is a snapshot
        # holding one variant.
        {"line": "sole_pair", "to_write": len(sole_w), "not_selected": len(sole_h),
         "seed_ids": sorted(s["seed_id"] for s in sole_w + sole_h)[:200]},
        {"line": "iii_seed_price_differs", "seeds": len(price_w),
         "seed_ids": sorted(s["seed_id"] for s in price_w)[:50],
         "note": "seed-level price differs from the kept one-time entry's (read-only; the next refresh re-reads it)"},
        {"line": "notes",
         "plans": "Prepaid subscription entries (\"3 Month Subscription\", \"6 Month Subscription\" beside \"Single "
                  "Box\") and cadences (\"30 days\", \"60 days\") beside a one-time entry are DROPPED as plans, "
                  "consistent with #2634's extractor.",
         "sole_pair": f"{len(sole_w) + len(sole_h)} seed(s) fold on sole_pair evidence only; written only with "
                      "--include-sole-pair.",
         "no_positive_link": f"{held_reasons.get('no_positive_link', 0)} seed(s) held as no_positive_link need a "
                             "fresh page re-read (listed on the reread line)."},
        {"line": "by_market", "top": top(by_market)},
        {"line": "by_host", "top": top(by_host)},
        {"line": "digest", "digest": plan_digest(seeds), "seeds": len(writes)},
        {"line": "reread", "seed_ids": sorted(s["seed_id"] for s in held if s.get("reread"))},
    ]
    for kind in KINDS:
        for s in [s for s in writes if s["kind"] == kind][:samples]:
            lines.append({"line": "sample", "kind": kind, "seed_id": s["seed_id"], "host": s["host"],
                          "evidence": s["evidence"], "before": _brief(s["before"]), "after": _brief(s["after"])})
    for reason in sorted({s["reason"] for s in held}):
        for s in [s for s in held if s["reason"] == reason][:samples]:
            lines.append({"line": "held_sample", "kind": s["kind"], "reason": reason, "seed_id": s["seed_id"],
                          "host": s["host"], "before": _brief(s.get("before") or [])})
    return lines


# ── database ──────────────────────────────────────────────────────────────────────────────────────────────────

def _fp(sd: str, title: str) -> str:
    """md5 over everything the plan reads: the main list, the snapshot list, and the product-name fields. `sd` is
    an expression for seed_data (the column, or the once-extracted copy of its keys: same values, same text)."""
    return (f"md5(coalesce(({sd}->'variants')::text, '') || '|' || "
            f"coalesce(({sd}->'snapshot'->'variants')::text, '') || '|' || "
            f"coalesce({sd}->>'title', '') || '|' || coalesce({sd}->>'product_name', '') || '|' || "
            f"coalesce({title}, '') || '|' || coalesce({sd}->'snapshot'->>'title', ''))")


_ROW_FP = _fp("s.seed_data", "s.title")

# ONE detoast of seed_data per row (review of #2638, L3): its top-level keys this plan reads are extracted once
# (`strict $.keyvalue()`, silent, so a non-object seed_data yields nothing instead of failing the run); every
# other read works on that in-memory copy. Every active seed is paged (the summary-gap count needs them all);
# only a qualifying one (a main list of 2+) returns its lists.
_KEYS = ("variants", "snapshot", "title", "product_name", "product", "price_amount", "price",
         "pdp_field_quality_summary")
CANDIDATES_SQL = f"""
WITH page AS (
  SELECT s.id, s.market, s.domain, s.canonical_url, s.destination_url, s.title, s.attached_product_key,
         s.price_amount AS col_price,
         (SELECT jsonb_object_agg(kv->>'key', kv->'value')
            FROM jsonb_path_query(s.seed_data, 'strict $.keyvalue()', '{{}}', true) AS kv
           WHERE kv->>'key' IN ({", ".join(f"'{k}'" for k in _KEYS)})) AS sd
  FROM external_product_seeds s
  WHERE s.id > :after AND s.status = 'active'
  ORDER BY s.id
  LIMIT :page
), q AS (
  SELECT p.*, (jsonb_typeof(p.sd->'variants') = 'array' AND jsonb_array_length(p.sd->'variants') >= 2) AS qualifies
  FROM page p
)
SELECT q.id, q.qualifies, q.market, q.domain, q.canonical_url, q.destination_url, q.title, q.attached_product_key,
       CASE WHEN q.qualifies THEN q.sd->'variants' END AS variants,
       CASE WHEN q.qualifies THEN q.sd->'snapshot'->'variants' END AS snapshot_variants,
       q.sd->>'title' AS sd_title, q.sd->>'product_name' AS sd_product_name,
       q.sd->'snapshot'->>'title' AS snapshot_title,
       q.sd->'product'->>'platform_product_id' AS platform_product_id,
       coalesce(q.sd->>'price_amount', q.sd->>'price', q.col_price::text) AS seed_price,
       ((q.sd->'pdp_field_quality_summary') IS NULL AND jsonb_typeof(q.sd->'snapshot') = 'object'
        AND (q.sd->'snapshot'->'pdp_field_quality_summary') IS NOT NULL) AS summary_gap,
       CASE WHEN q.qualifies THEN {_fp("q.sd", "q.title")} END AS fp
FROM q
ORDER BY q.id
"""

# One statement per seed: the CAS write AND its revert record, so neither lands without the other.
APPLY_SQL = f"""
WITH cur AS (
  SELECT s.id, s.seed_data->'variants' AS before_variants
  FROM external_product_seeds s
  WHERE s.id = :id AND s.status = 'active' AND {_ROW_FP} = :fp
), upd AS (
  UPDATE external_product_seeds s
  SET seed_data = jsonb_set(s.seed_data, '{{variants}}', (
        SELECT jsonb_agg(e.v || op.patch ORDER BY op.pos)
        FROM jsonb_to_recordset(CAST(:ops AS jsonb)) AS op(pos int, i int, patch jsonb)
        JOIN jsonb_array_elements(s.seed_data->'variants') WITH ORDINALITY AS e(v, ord) ON e.ord = op.i + 1))
  FROM cur
  WHERE s.id = cur.id AND s.status = 'active' AND {_ROW_FP} = :fp
  RETURNING s.id, {_ROW_FP} AS fp_after, extract(epoch FROM s.updated_at)::text AS updated_epoch,
            s.seed_data->'snapshot'->>'extracted_at' AS extracted_at
)
INSERT INTO identity_resolution_events (proposal_id, action, run_id, detail)
SELECT NULL, CAST(:action AS text), CAST(:run_id AS text),
       jsonb_build_object('seed_id', u.id, 'kind', CAST(:kind AS text), 'fp_before', CAST(:fp AS text),
                          'fp_after', u.fp_after, 'updated_epoch', u.updated_epoch, 'extracted_at', u.extracted_at,
                          'ops', CAST(:ops AS jsonb), 'before_variants', c.before_variants)
FROM upd u JOIN cur c ON c.id = u.id
RETURNING id
"""

REVERT_SQL = f"""
UPDATE external_product_seeds s
SET seed_data = jsonb_set(s.seed_data, '{{variants}}', ev.detail->'before_variants')
FROM identity_resolution_events ev
WHERE ev.id = :event_id AND ev.action = CAST(:action AS text) AND s.id = ev.detail->>'seed_id'
  AND {_ROW_FP} = ev.detail->>'fp_after'
  -- Freshness too (review of #2638, L1): a refresh that re-stamped the seed since the apply owns it now.
  AND extract(epoch FROM s.updated_at)::text IS NOT DISTINCT FROM ev.detail->>'updated_epoch'
  AND (s.seed_data->'snapshot'->>'extracted_at') IS NOT DISTINCT FROM ev.detail->>'extracted_at'
RETURNING s.id
"""

# A seed an earlier, interrupted revert of this run already restored (review of #2638, L5).
ALREADY_REVERTED_SQL = f"""
SELECT s.id FROM external_product_seeds s JOIN identity_resolution_events ev
  ON ev.id = :event_id AND ev.action = CAST(:action AS text) AND s.id = ev.detail->>'seed_id'
WHERE {_ROW_FP} = ev.detail->>'fp_before'
"""

EVENT_SQL = """
INSERT INTO identity_resolution_events (proposal_id, action, run_id, detail)
VALUES (NULL, :action, :run_id, CAST(:detail AS jsonb))
RETURNING id
"""
AUDIT_SQL = """
INSERT INTO writer_audit_log (writer_name, batch_id, dry_run_report_hash, applied_rows, skipped_rows, reasons, actor)
VALUES (:writer, :batch, :digest, :applied, :skipped, CAST(:reasons AS jsonb), :actor)
"""
RUN_EVENTS_SQL = """
SELECT id, action, detail->>'seed_id' AS seed_id, detail FROM identity_resolution_events
WHERE run_id = :run_id AND action = ANY(:actions) ORDER BY id
"""
LATER_RUNS_SQL = """
SELECT DISTINCT e.run_id FROM identity_resolution_events e
WHERE e.action = :seed_action AND e.run_id <> :run_id AND e.id > :after_id AND e.detail->>'seed_id' = ANY(:seed_ids)
  AND NOT EXISTS (SELECT 1 FROM identity_resolution_events r WHERE r.run_id = e.run_id AND r.action = :reverted)
"""


async def plan(db: Any, *, page: int = PAGE, seed_ids: Optional[Iterable[str]] = None,
               kinds: Optional[Iterable[str]] = None, max_seeds: Optional[int] = None,
               include_sole_pair: bool = False) -> Dict[str, Any]:
    want = {i.strip() for i in seed_ids or [] if i and i.strip()}
    seeds: List[Dict[str, Any]] = []
    after, scanned, gap = "", 0, 0
    while True:
        rows = [dict(r) for r in await db.fetch_all(CANDIDATES_SQL, {"after": after, "page": page})]
        if not rows:
            break
        after = rows[-1]["id"]
        for r in rows:
            gap += bool(r["summary_gap"])
            if not r["qualifies"] or (want and r["id"] not in want):
                continue
            scanned += 1
            seeds.append(plan_seed(r))
        if len(rows) < page:
            break
    cap(seeds, kinds=kinds, max_seeds=max_seeds, include_sole_pair=include_sole_pair)
    return {"scanned": scanned, "seeds": seeds, "summary_gap": {"active": gap}}


async def dry_run(db: Any, **caps: Any) -> Dict[str, Any]:
    """The plan inside ONE read-only transaction with a 60s statement timeout: a dry run cannot write."""
    async with db.transaction():
        await db.execute("SET TRANSACTION READ ONLY")
        await db.execute(f"SET LOCAL statement_timeout = '{READ_TIMEOUT}'")
        return await plan(db, **caps)


async def _bounded(db: Any) -> None:
    await db.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    await db.execute(f"SET LOCAL statement_timeout = '{WRITE_TIMEOUT}'")


async def _record(db: Any, action: str, run_id: str, detail: Mapping[str, Any]) -> None:
    row = await db.fetch_one(EVENT_SQL, {"action": action, "run_id": run_id, "detail": json.dumps(detail, default=str)})
    if not row:
        raise RuntimeError(f"{action} for {run_id} was not recorded")


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


async def apply(db: Any, *, expect_digest: str, max_seeds: int, seed_ids: Optional[Iterable[str]] = None,
                kinds: Optional[Iterable[str]] = None, include_sole_pair: bool = False, batch_size: int = BATCH_SIZE,
                sleep_s: float = SLEEP_S, actor: str = WRITER) -> Dict[str, Any]:
    """Re-plan (same process, same caps), refuse on any digest but the reviewed one, then write in CAS batches."""
    p = await dry_run(db, seed_ids=seed_ids, kinds=kinds, max_seeds=max_seeds, include_sole_pair=include_sole_pair)
    digest = plan_digest(p["seeds"])
    if not expect_digest or expect_digest != digest:
        raise SystemExit(f"refused: --expect-digest {expect_digest!r} is not this plan's {digest}; dry-run again with "
                         "the same caps and review it. Nothing written.")
    writes = sorted((s for s in p["seeds"] if s["action"] == "write"), key=lambda s: s["seed_id"])
    if not writes:
        raise SystemExit("nothing to write")
    if any(not s["ops"] for s in writes):  # an empty rebuild would null seed_data: the planner never emits one
        raise RuntimeError("a planned write keeps no entry; nothing written")
    run_id = f"mainlist_{uuid.uuid4().hex[:12]}"
    await _record(db, RUN_ACTION, run_id, {"run_id": run_id, "digest": digest, "seeds": len(writes),
                                            "kinds": dict(Counter(s["kind"] for s in writes)),
                                            "caps": {"max_seeds": max_seeds, "kinds": sorted(kinds or []),
                                                     "seed_ids": sorted(seed_ids or []),
                                                     "include_sole_pair": include_sole_pair}})
    applied: List[str] = []
    skipped: List[str] = []
    for n, batch in enumerate(_chunks(writes, max(1, batch_size))):
        mine_applied: List[str] = []
        mine_skipped: List[str] = []
        async with db.transaction():
            await _bounded(db)
            for s in batch:
                row = await db.fetch_one(APPLY_SQL, {"id": s["seed_id"], "fp": s["fp"], "ops": json.dumps(s["ops"]),
                                                     "kind": s["kind"], "action": SEED_ACTION, "run_id": run_id})
                (mine_applied if row else mine_skipped).append(s["seed_id"])
            await db.execute(AUDIT_SQL, {
                "writer": WRITER, "batch": f"{run_id}:{n}", "digest": digest, "applied": len(mine_applied),
                "skipped": len(mine_skipped), "actor": actor,
                "reasons": json.dumps({"run_id": run_id, "cas_skipped": mine_skipped,
                                       "kinds": dict(Counter(s["kind"] for s in batch
                                                             if s["seed_id"] in set(mine_applied)))})})
        applied += mine_applied
        skipped += mine_skipped
        print(json.dumps({"line": "batch", "run_id": run_id, "batch": n, "applied": len(mine_applied),
                          "cas_skipped": len(mine_skipped)}), flush=True)
        if sleep_s and n * batch_size + len(batch) < len(writes):
            await asyncio.sleep(sleep_s)
    done = {"run_id": run_id, "digest": digest, "applied": len(applied), "cas_skipped": len(skipped),
            "cas_skipped_ids": skipped}
    await _record(db, APPLIED_ACTION, run_id, done)
    return done


async def revert(db: Any, run_id: str, *, batch_size: int = BATCH_SIZE, sleep_s: float = SLEEP_S,
                 actor: str = WRITER) -> Dict[str, Any]:
    rows = [dict(r) for r in await db.fetch_all(RUN_EVENTS_SQL, {
        "run_id": run_id, "actions": [RUN_ACTION, SEED_ACTION, REVERTED_ACTION]})]
    if any(r["action"] == REVERTED_ACTION for r in rows):
        raise SystemExit(f"{run_id} is already reverted")
    manifest = next((_json(r["detail"]) for r in rows if r["action"] == RUN_ACTION), None)
    seeds = [r for r in rows if r["action"] == SEED_ACTION]
    if not seeds:
        raise SystemExit(f"{run_id} wrote no seed: nothing to revert")
    later = await db.fetch_all(LATER_RUNS_SQL, {"seed_action": SEED_ACTION, "run_id": run_id,
                                                "after_id": seeds[0]["id"], "reverted": REVERTED_ACTION,
                                                "seed_ids": [r["seed_id"] for r in seeds]})
    if later:
        raise SystemExit(f"refused: later run(s) {sorted(r['run_id'] for r in later)} rewrote some of these seeds; "
                         "revert them first")
    digest = (manifest or {}).get("digest")
    reverted: List[str] = []
    changed: List[str] = []
    already: List[str] = []
    for n, batch in enumerate(_chunks(seeds, max(1, batch_size))):
        mine_reverted: List[str] = []
        mine_changed: List[str] = []
        async with db.transaction():
            await _bounded(db)
            for r in batch:
                params = {"event_id": r["id"], "action": SEED_ACTION}
                if await db.fetch_one(REVERT_SQL, params):
                    mine_reverted.append(r["seed_id"])
                elif await db.fetch_one(ALREADY_REVERTED_SQL, params):
                    already.append(r["seed_id"])
                else:
                    mine_changed.append(r["seed_id"])
            await db.execute(AUDIT_SQL, {
                "writer": WRITER, "batch": f"{run_id}:revert:{n}", "digest": digest, "applied": len(mine_reverted),
                "skipped": len(mine_changed), "actor": actor,
                "reasons": json.dumps({"run_id": run_id, "revert": True, "changed_since_apply": mine_changed})})
        reverted += mine_reverted
        changed += mine_changed
        if sleep_s and n * batch_size + len(batch) < len(seeds):
            await asyncio.sleep(sleep_s)
    done = {"run_id": run_id, "reverted": len(reverted), "already_reverted": len(already),
            "changed_since_apply": len(changed), "changed_ids": changed}
    await _record(db, REVERTED_ACTION, run_id, done)
    return done


async def run(args: argparse.Namespace) -> int:
    from db.database import database

    await database.connect()
    try:
        if args.revert:
            out = await revert(database, args.revert, batch_size=args.batch_size, sleep_s=args.sleep)
            print(json.dumps({"line": "reverted", **out}, default=str), flush=True)
            return 0
        caps = {"seed_ids": args.seed_ids, "kinds": args.kinds, "max_seeds": args.max_seeds,
                "include_sole_pair": args.include_sole_pair}
        if args.apply:
            out = await apply(database, expect_digest=args.expect_digest, batch_size=args.batch_size,
                              sleep_s=args.sleep, **caps)
            print(json.dumps({"line": "applied", **out}, default=str), flush=True)
            return 0
        p = await dry_run(database, page=args.page, **caps)
        for line in report_lines(p, samples=args.samples):
            print(json.dumps(line, default=str, ensure_ascii=False), flush=True)
        print(json.dumps({"line": "dry_run", "written": 0}), flush=True)
        return 0
    finally:
        await database.disconnect()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="(default) plan and report; writes nothing")
    mode.add_argument("--apply", action="store_true", help="write exactly the reviewed plan")
    mode.add_argument("--revert", metavar="RUN_ID", help="restore the before-variants a run wrote")
    p.add_argument("--expect-digest", dest="expect_digest", help="apply: the reviewed dry run's digest")
    p.add_argument("--max-seeds", dest="max_seeds", type=int, help="write at most this many seeds (seed id order)")
    p.add_argument("--seed-ids", dest="seed_ids", nargs="+", help="only these seeds")
    p.add_argument("--kinds", nargs="+", choices=KINDS, help="only these kinds")
    p.add_argument("--include-sole-pair", dest="include_sole_pair", action="store_true",
                   help="also write folds whose only evidence is sole_pair (needs Peng's explicit approval)")
    p.add_argument("--batch-size", dest="batch_size", type=int, default=BATCH_SIZE)
    p.add_argument("--sleep", type=float, default=SLEEP_S, help="seconds between write batches")
    p.add_argument("--page", type=int, default=PAGE)
    p.add_argument("--samples", type=int, default=SAMPLES)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply and (not args.expect_digest or args.max_seeds is None):
        parser.error("--apply needs --expect-digest (the reviewed dry run's digest) and --max-seeds")
    if args.max_seeds is not None and args.max_seeds < 1:
        parser.error("--max-seeds must be >= 1")
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
