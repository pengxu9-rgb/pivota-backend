"""Name the MERCHANT's own variant at hand-over, out of `catalog_skus`, or refuse.

WHY THIS EXISTS. The gateway's cart prefill and the checkout preflight both key on one value,
`cart_variant_id`, and until this module the only producer of that value for a crawl seed was
`shopify_variant_identity.sole_stamped_variant_id`, which reads
`seed_data.snapshot.variants[].shopify_variant_id`. Measured on prod 2026-09-08: **0 of 11,834
active seeds carry that key**, and 0 carry `snapshot.storefront_platform` either — the producer
(`scripts/backfill_shopify_variant_ids.py`) has never run at scale. So the prefill fired on zero
offers, the preflight gate behind it fired on zero offers, and the shadow report it feeds had an
empty denominator BY CONSTRUCTION. A gate that applies to nothing measures nothing.

The identity is not missing. It is in `catalog_skus.source_variant_id`, put there by
`scripts/backfill_variant_identity_skus.py` (6,087 SKU+offer pairs across 3,279 products on
2026-09-08) and by `catalog_enrichment_agent/ingestion.py`. One layer holds the evidence and a
different layer reads a field nobody writes. This module closes that seam and nothing else.

WHAT IT WILL AND WILL NOT SAY.

  * It accepts a row ONLY when `services.variant_identity.variant_id_provenance` calls the id
    MERCHANT_ISSUED. PRODUCT_DERIVED and UNVERIFIABLE are refused — they are exactly the shapes
    the safety kernel's `isRestatedProductId` throws away, and 39.4% + 13.3% of the SKU table is
    made of them.
  * The `sku_payload.variant_id_provenance` stamp may only VETO, never authorise. Read the order
    carefully: the classifier is run on every row, and the stamp is then required to agree.
    "Prefer the stamp" would let a stamp written by a future careless writer promote an id the
    classifier refuses, and this column is the one checkout reads as identity. (Measured on prod
    2026-09-08 over 20,898 live external SKUs: the stamp is present on 7,174 and disagrees with
    the classifier on **0**, so requiring agreement costs nothing today and bounds tomorrow.)
  * When a product has MORE THAN ONE live merchant-issued SKU it refuses, unless the hand-over
    itself names one of them EXACTLY. The redirect is built at product grain — the buyer has not
    chosen a variant — so picking one would be the `defaultVariant` guess that
    [[reap_variant_ids_are_reap_side_and_availability_ordered]] records as a live wrong-size
    hazard on a real storefront. An exact string equality against a stored merchant-issued id is
    not a guess; a preference order over candidates would be.
  * The seed-stamp path is kept, but ONLY where `catalog_skus` said nothing at all. If catalog
    holds candidates and refused them, the snapshot disagreeing with catalog is a reason to stay
    silent, not a second opinion to fall back on. ("A conditional fallback is still a fallback" —
    the round-5 P0 in `agent_shop_gateway._external_seed_redirect_identity`.)
  * A lookup that fails or was never primed answers `lookup_failed` / `not_primed`, which carry
    no id. Every failure here degrades the hand-over to an honest referral; none of them deletes
    an offer.

WHAT IT DOES NOT DECIDE. Whether a Shopify cart permalink can be BUILT at all — that is
`storefront_is_shopify` plus `resolve_cart_permalink`, and it is a separate piece of evidence
about the STOREFRONT rather than about the variant. Naming the variant is necessary and not
sufficient; see the note in `shopify_variant_identity.storefront_is_shopify`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from services.outbound_links_service import extract_shopify_numeric_variant_id
from services.variant_identity import (
    MERCHANT_ISSUED,
    is_merchant_issued_variant_id,
    variant_id_provenance,
)

logger = logging.getLogger("handover_variant_identity")

#: Where a resolved id came from. Emitted so a wrong cart can be traced to a layer.
SOURCE_CATALOG_SKU = "catalog_sku"
SOURCE_SEED_STAMP = "seed_stamp"

#: Outcomes. A CLOSED set, because they are counted per request and grouped in the coverage
#: line; a free-text reason makes that line unaggregatable (same rule as
#: `checkout_preflight`'s reason vocabulary).
R_SOLE = "sole_merchant_issued_sku"
R_EXACT = "exact_merchant_issued_sku"
R_SEED_STAMP = "seed_stamped_sole_variant"
R_AMBIGUOUS = "multiple_merchant_issued_skus"
#: The hand-over names a merchant-issued id, and NO live `catalog_skus` row carries it. Two
#: merchant-issued ids disagreeing about one hand-over is a contradiction, and naming either is
#: a guess — so this refuses even when there is exactly ONE candidate. Review of the first cut
#: found the sole-candidate path returning BEFORE any comparison with the name, so a lone
#: catalog row silently overrode an operator-attached variant: a $95 Mini prefilled for the
#: $140 Standard the offer actually named, which is the wrong-size hazard this module exists
#: to refuse, reached through the one door it had left open.
R_CONTRADICTED = "hand_over_names_a_different_merchant_issued_id"
R_NO_IDENTITY = "no_merchant_issued_sku"
R_NO_PRODUCT_KEY = "no_attached_product_key"
R_NOT_PRIMED = "not_primed"
R_LOOKUP_FAILED = "sku_lookup_failed"

#: Why a `catalog_skus` row was not admitted as a candidate. Counted, never silent.
X_NOT_MERCHANT_ISSUED = "not_merchant_issued"
X_STAMP_VETO = "stamp_vetoed"
X_PAYLOAD_DISAGREES = "stored_id_disagrees_with_payload"

_DEFAULT_TIMEOUT_S = 0.5
#: A resolve can touch hundreds of seed rows; the lookup is ONE statement over an indexed
#: column, but the IN-list still has to be bounded or a wide request writes an unbounded query.
#: Keys past the cap are simply never primed, and `choose` then answers `not_primed`, which
#: carries no id — the cap degrades hand-overs, it cannot publish a wrong one.
_DEFAULT_MAX_KEYS = 400

_SELECT = """
    SELECT s.sku_key, s.product_key, s.source_product_id, s.source_variant_id, s.sku_payload
      FROM catalog_skus s
      JOIN catalog_products cp ON cp.product_key = s.product_key
     WHERE s.suppressed_at IS NULL
       AND cp.suppressed_at IS NULL
       AND s.product_key IN ({placeholders})
"""


@dataclass(frozen=True)
class Candidate:
    """One live `catalog_skus` row whose id we can positively place as the merchant's.

    TWO SPELLINGS, deliberately. `stored_variant_id` is the column's own value; `variant_id` is
    the one CANONICAL form every consumer gets. They differ only for
    `gid://shopify/ProductVariant/<n>`, which `services.variant_identity` admits as identity and
    which every other reader in this lane has always normalised away
    (`outbound_links_service.extract_shopify_numeric_variant_id`, `shopify_variant_identity._numeric_id`).
    Review of the first cut found the raw gid reaching `offer_spec["variant_id"]` — documented
    one line above itself as "the NUMERIC storefront variant id" — and reaching the preflight,
    where `live_offer_verification._check_one` string-compares it against `parse_product_js`'s
    bare-numeric ids and so can NEVER match. Normalising once, here, is the only place that
    cannot drift.
    """

    sku_key: str
    product_key: str
    variant_id: str
    stamped: bool
    stored_variant_id: str = ""

    def answers_to(self, name: str) -> bool:
        """True when `name` is this variant, in either spelling. Equality, never a preference."""
        if not name:
            return False
        return name in (self.variant_id, self.stored_variant_id) or (
            canonical_variant_id(name) == self.variant_id
        )


@dataclass(frozen=True)
class HandoverVariant:
    """The answer, with its reason attached so a refusal is never indistinguishable from a miss."""

    variant_id: Optional[str]
    reason: str
    source: Optional[str] = None
    sku_key: Optional[str] = None
    candidates: int = 0

    @property
    def resolved(self) -> bool:
        return bool(self.variant_id)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def canonical_variant_id(value: Any) -> str:
    """ONE spelling for a variant id that two writers may spell two ways.

    `gid://shopify/ProductVariant/41234567890123` and `41234567890123` are the same variant, and
    `services.variant_identity` calls both MERCHANT_ISSUED. Everything downstream of this module
    — the cart permalink builder, the preflight's storefront comparison, the `variant_id` we
    publish on the offer spec — wants the bare numeric, so the fold happens once here rather
    than at each of them.
    """
    raw = _norm(value)
    if not raw:
        return ""
    return extract_shopify_numeric_variant_id(raw) or raw


def _payload(raw: Any) -> Dict[str, Any]:
    """`sku_payload` reaches this reader as a JSON STRING on BOTH dialects — accept either.

    Measured, because the obvious assumption is wrong: the column is `jsonb` on Postgres, but a
    raw-SQL read through `databases` + asyncpg does not apply the SQLAlchemy type, so the value
    arrives as text there exactly as it does on SQLite
    (`tests/test_handover_variant_identity_postgres.py` pins both facts). A reader written for
    a dict would find no `variant_id_provenance` in production, and a veto that cannot find its
    key does not veto — a fail-OPEN no SQLite test could see. The dict branch is kept because
    an ORM-typed caller does get one.

    Anything that is not an object reads as an empty one: a payload we cannot parse must not
    be able to veto (it carries no stamp) NOR to authorise (it carries no id to agree with).
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def candidate_from_row(row: Any, rejects: Optional[Dict[str, int]] = None) -> Optional[Candidate]:
    """Admit one `catalog_skus` row as a hand-over candidate, or say why not.

    THE ORDER IS THE POLICY, so it is spelled out rather than left to the reader:

      1. the CLASSIFIER runs first and is necessary — an id it does not call MERCHANT_ISSUED is
         out, whatever any stamp says;
      2. the stamp may then VETO — a row explicitly marked as something else is out even if the
         classifier would have admitted it;
      3. `sku_payload.variant_id` (the FULL id, which #2148 keeps beside the 128-bounded
         `source_variant_id`) must AGREE with the stored id. They differ exactly when the stored
         one was truncated at the column bound, and a truncated variant id names a variant the
         merchant does not have. Refusing is the only safe reading; the classifier structurally
         cannot see this, because it is handed one string.
    """
    d = dict(row) if not isinstance(row, dict) else row
    svid = _norm(d.get("source_variant_id"))
    if not svid:
        if rejects is not None:
            rejects[X_NOT_MERCHANT_ISSUED] = rejects.get(X_NOT_MERCHANT_ISSUED, 0) + 1
        return None

    product_key = _norm(d.get("product_key"))
    if variant_id_provenance(
        svid, product_key=product_key, product_id=d.get("source_product_id")
    ) != MERCHANT_ISSUED:
        if rejects is not None:
            rejects[X_NOT_MERCHANT_ISSUED] = rejects.get(X_NOT_MERCHANT_ISSUED, 0) + 1
        return None

    payload = _payload(d.get("sku_payload"))
    stamp = _norm(payload.get("variant_id_provenance"))
    if stamp and stamp != MERCHANT_ISSUED:
        if rejects is not None:
            rejects[X_STAMP_VETO] = rejects.get(X_STAMP_VETO, 0) + 1
        return None

    full = _norm(payload.get("variant_id"))
    if full and full != svid:
        if rejects is not None:
            rejects[X_PAYLOAD_DISAGREES] = rejects.get(X_PAYLOAD_DISAGREES, 0) + 1
        return None

    return Candidate(
        sku_key=_norm(d.get("sku_key")),
        product_key=product_key,
        variant_id=canonical_variant_id(svid),
        stamped=bool(stamp),
        stored_variant_id=svid,
    )


def choose_handover_variant(
    candidates: Optional[Sequence[Candidate]],
    *,
    seed_data: Any = None,
    offer_variant_id: Any = None,
    primed: bool = True,
    lookup_ok: bool = True,
) -> HandoverVariant:
    """The whole decision, pure — so it can be tested without a database.

    `candidates` are the rows `candidate_from_row` already admitted for ONE product.
    """
    if not lookup_ok:
        return HandoverVariant(None, R_LOOKUP_FAILED)
    if not primed:
        return HandoverVariant(None, R_NOT_PRIMED)

    live = list(candidates or [])
    wanted = _norm(offer_variant_id)

    if live:
        # THE NAME IS CHECKED BEFORE THE COUNT, and that order is the fix for the first cut's
        # one real hole. A `len(live) == 1` shortcut placed above this returned the lone
        # candidate without ever comparing it to the name the hand-over carries, so a single
        # catalog row overrode a DIFFERENT merchant-issued id the caller had explicitly
        # attached. Only a name that is ITSELF merchant-issued can contradict: the seed lane
        # routinely passes a SKU string here (`_seed_offer_variant_id` reads
        # variant_id | variantId | sku | sku_id | id), and a SKU naming no variant is an
        # absence of information, not a disagreement.
        hits = [c for c in live if c.answers_to(wanted)]
        if len(hits) == 1:
            return HandoverVariant(
                hits[0].variant_id,
                R_EXACT if len(live) > 1 else R_SOLE,
                SOURCE_CATALOG_SKU, hits[0].sku_key, len(live),
            )
        if not hits and wanted and is_merchant_issued_variant_id(wanted):
            return HandoverVariant(None, R_CONTRADICTED, None, None, len(live))

    if len(live) == 1:
        one = live[0]
        return HandoverVariant(one.variant_id, R_SOLE, SOURCE_CATALOG_SKU, one.sku_key, 1)

    if len(live) > 1:
        # Two or more live merchant-issued SKUs and the hand-over names none of them. There is
        # no ordering over these that is anything but a guess — see the module docstring.
        return HandoverVariant(None, R_AMBIGUOUS, None, None, len(live))

    # ZERO candidates. Only here may the seed's own stamp speak: nothing in catalog contradicts
    # it. `sole_stamped_variant_id` carries its own refusal (it declines on any product with
    # more than one snapshot variant entry) and is imported lazily so this module stays free of
    # the serving-path import graph for callers that only want the pure decision.
    from services.shopify_variant_identity import sole_stamped_variant_id

    stamped = _norm(sole_stamped_variant_id(seed_data))
    # THE SAME BAR THE CATALOG ROWS CLEAR. `sole_stamped_variant_id` gates on
    # `shopify_variant_identity._numeric_id`, which accepts ANY digit string, while
    # `variant_identity` requires 8+ digits — so a snapshot stamped `"12345"` used to become a
    # hand-over id that `checkout_preflight` would then refuse as
    # `no_merchant_issued_variant_id`, a refusal this resolver had caused. One rule for both
    # sources, or the module's own guarantee is only true of half its answers.
    if stamped and is_merchant_issued_variant_id(stamped):
        return HandoverVariant(
            canonical_variant_id(stamped), R_SEED_STAMP, SOURCE_SEED_STAMP, None, 0
        )
    return HandoverVariant(None, R_NO_IDENTITY, None, None, 0)


class HandoverVariantResolver:
    """ONE lookup and ONE memo for a whole request.

    Built per request, exactly like `_PreflightBudget`: the four hand-over lanes each walk a
    list of seed rows, and a per-row query would be a query per card. `prime` takes every
    product key a lane is about to hand over and loads them in one statement; `choose` is then
    pure and can be called inside the per-variant loop for free.

    Every failure mode answers with an EMPTY id: a timeout, a driver error, an unprimed key and
    an over-cap key are all "we could not name the variant", which degrades the hand-over to a
    referral. None of them can publish an id.
    """

    def __init__(self, *, timeout_s: Optional[float] = None, max_keys: Optional[int] = None) -> None:
        self._by_key: Dict[str, List[Candidate]] = {}
        self._primed: set = set()
        #: Keys whose batch RAISED. Per key rather than one latch: a latch made a single slow
        #: statement suppress the seed-stamp path for standalone seeds in the same request,
        #: which never needed the lookup at all — a failure about one product key is not
        #: evidence about another.
        self._failed: set = set()
        self.stats: Dict[str, int] = {
            "handover_considered": 0,
            "handover_resolved": 0,
            "handover_resolved_catalog": 0,
            "handover_resolved_seed_stamp": 0,
            "handover_refused_ambiguous": 0,
            "handover_contradicted": 0,
            "handover_no_identity": 0,
            "handover_no_product_key": 0,
            "handover_not_primed": 0,
            "handover_lookup_failed": 0,
            "handover_rows_scanned": 0,
            "handover_rows_rejected": 0,
        }
        if timeout_s is None:
            timeout_s = _env_float("HANDOVER_VARIANT_LOOKUP_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_S)
        if max_keys is None:
            max_keys = _env_int("HANDOVER_VARIANT_LOOKUP_MAX_KEYS", _DEFAULT_MAX_KEYS)
        self._timeout_s = max(0.0, float(timeout_s))
        self._max_keys = max(0, int(max_keys))

    async def prime(self, product_keys: Iterable[Any]) -> None:
        """Load every not-yet-loaded key, in one statement, under one timeout.

        Fail-soft on the DB call ONLY, and the softness is a refusal, not a pass: the keys in
        the failed batch are recorded in `_failed`, and `choose` answers `sku_lookup_failed`
        with no id for each of them until a later `prime` loads them. PER KEY, never one latch
        — a failure about one product key is not evidence about another, and a latch made a
        single slow statement silence standalone seeds that needed no lookup at all. An earlier
        shape of this in the same route caught the DERIVATION too, which turned a programming
        error into a feature that silently never ran.
        """
        wanted: List[str] = []
        for key in product_keys or ():
            k = _norm(key)
            if not k or k in self._primed or k in wanted:
                continue
            if len(self._primed) + len(wanted) >= self._max_keys:
                break
            wanted.append(k)
        if not wanted:
            return

        try:
            rows = await asyncio.wait_for(self._fetch(wanted), timeout=self._timeout_s or None)
        except Exception as exc:  # noqa: BLE001
            self._failed.update(wanted)
            logger.warning(
                "handover variant lookup failed for %d product key(s): %s",
                len(wanted), repr(exc)[:200],
            )
            return

        # Mark primed BEFORE filling, so a key with no rows at all is "primed and empty"
        # (-> no_merchant_issued_sku) rather than "never asked" (-> not_primed). The two are
        # different facts and the coverage line has to be able to tell them apart.
        self._primed.update(wanted)
        # AND CLEAR THE FAILURE. A key that timed out in one lane and loaded in the next kept
        # answering `sku_lookup_failed` with its candidates sitting in `_by_key` — fail-closed,
        # so never a wrong cart, but it threw away a hand-over the second lookup had paid for.
        self._failed.difference_update(wanted)
        rejects: Dict[str, int] = {}
        for row in rows or ():
            self.stats["handover_rows_scanned"] += 1
            cand = candidate_from_row(row, rejects)
            if cand is None:
                continue
            self._by_key.setdefault(cand.product_key, []).append(cand)
        self.stats["handover_rows_rejected"] += sum(rejects.values())
        for reason, n in rejects.items():
            self.stats["handover_reject_" + reason] = self.stats.get(
                "handover_reject_" + reason, 0
            ) + n

    async def _fetch(self, keys: Sequence[str]) -> Any:
        """Separated so a test can drive the decision without a database.

        The IN-list is built from GENERATED parameter names, never from the keys themselves —
        a product key is caller-influenced data (`external_product_seeds.attached_product_key`)
        and interpolating it would be an injection site on a serving path.
        """
        from db.database import database

        names = [f"pk{i}" for i in range(len(keys))]
        sql = _SELECT.format(placeholders=", ".join(":" + n for n in names))
        return await database.fetch_all(sql, dict(zip(names, keys)))

    def candidates_for(self, product_key: Any) -> List[Candidate]:
        return list(self._by_key.get(_norm(product_key)) or [])

    def choose(
        self,
        *,
        product_key: Any,
        seed_data: Any = None,
        offer_variant_id: Any = None,
    ) -> HandoverVariant:
        """The per-hand-over answer. Pure — `prime` already did every await."""
        self.stats["handover_considered"] += 1
        key = _norm(product_key)
        if not key:
            # A STANDALONE seed — no attached product key, so there is no `catalog_skus` row to
            # look up and nothing that could contradict the seed's own stamp. It takes the
            # zero-candidate path deliberately: refusing here instead would have silently
            # removed the seed-stamp hand-over that already worked for this cohort, which is a
            # regression dressed as caution. Only the REASON is relabelled, and only when
            # nothing resolved, so the coverage line can still tell this apart from a product
            # that simply has no merchant-issued SKU.
            answer = choose_handover_variant(
                (), seed_data=seed_data, offer_variant_id=offer_variant_id
            )
            if not answer.resolved:
                answer = HandoverVariant(None, R_NO_PRODUCT_KEY)
                self.stats["handover_no_product_key"] += 1
                return answer
            self._count(answer)
            return answer

        answer = choose_handover_variant(
            self._by_key.get(key),
            seed_data=seed_data,
            offer_variant_id=offer_variant_id,
            primed=key in self._primed,
            lookup_ok=key not in self._failed,
        )
        self._count(answer)
        return answer

    def _count(self, answer: HandoverVariant) -> None:
        if answer.resolved:
            self.stats["handover_resolved"] += 1
            if answer.source == SOURCE_CATALOG_SKU:
                self.stats["handover_resolved_catalog"] += 1
            else:
                self.stats["handover_resolved_seed_stamp"] += 1
            return
        if answer.reason == R_AMBIGUOUS:
            self.stats["handover_refused_ambiguous"] += 1
        elif answer.reason == R_CONTRADICTED:
            self.stats["handover_contradicted"] += 1
        elif answer.reason == R_LOOKUP_FAILED:
            self.stats["handover_lookup_failed"] += 1
        elif answer.reason == R_NOT_PRIMED:
            self.stats["handover_not_primed"] += 1
        else:
            self.stats["handover_no_identity"] += 1


def handover_coverage_fields(stats: Optional[Dict[str, int]]) -> Dict[str, Any]:
    """The counters, or {} when no hand-over was considered at all.

    DELIBERATELY NOT keyed on `resolved > 0`, which is the trap
    `preflight_coverage_fields` had to be corrected for in the other direction: the whole point
    of this lane is to see the REFUSALS, and a request where every hand-over was refused is the
    single most informative one to look at. It disappears from the log only when the request
    handed nothing over.
    """
    if not stats or not stats.get("handover_considered"):
        return {}
    considered = stats["handover_considered"]
    out: Dict[str, Any] = {
        "handover_considered": considered,
        # READ, not merely incremented. Review found these written in `prime` and present in no
        # emitted field — the defect the neighbouring comment in `_handle_offers_resolve` was
        # written about ("a counter nobody reads is indistinguishable from one that is always
        # zero"). They also carry the only production evidence for the two guards justified as
        # bounds on future writers: a stamp veto or a payload disagreement showing up in prod is
        # how anyone would ever learn a writer had started emitting one.
        "handover_rows_scanned": stats.get("handover_rows_scanned", 0),
        "handover_rows_rejected": stats.get("handover_rows_rejected", 0),
        "handover_rows_stamp_vetoed": stats.get("handover_reject_" + X_STAMP_VETO, 0),
        "handover_rows_payload_disagreed": stats.get("handover_reject_" + X_PAYLOAD_DISAGREES, 0),
        "handover_contradicted": stats.get("handover_contradicted", 0),
        "handover_resolved": stats.get("handover_resolved", 0),
        "handover_resolved_catalog": stats.get("handover_resolved_catalog", 0),
        "handover_resolved_seed_stamp": stats.get("handover_resolved_seed_stamp", 0),
        "handover_refused_ambiguous": stats.get("handover_refused_ambiguous", 0),
        "handover_no_identity": stats.get("handover_no_identity", 0),
        "handover_no_product_key": stats.get("handover_no_product_key", 0),
        "handover_lookup_failed": stats.get("handover_lookup_failed", 0),
        "handover_not_primed": stats.get("handover_not_primed", 0),
        "handover_resolved_fraction": round(stats.get("handover_resolved", 0) / considered, 3),
    }
    return out


def handover_coverage_message(fields: Dict[str, Any]) -> str:
    """The same numbers as MESSAGE TEXT.

    No formatter in this repo renders `extra`, and in production the root logger sits at
    WARNING with `setup_structured_logging()` never called from main — so a counter that lives
    only in `extra` is not merely unformatted, it is not emitted. Anything worth counting here
    has to be in the string.
    """
    if not fields:
        return ""
    return (
        " handover considered=%d resolved=%d catalog=%d seed_stamp=%d ambiguous=%d"
        " contradicted=%d no_identity=%d no_product_key=%d lookup_failed=%d not_primed=%d"
        " rows_scanned=%d rows_rejected=%d stamp_vetoed=%d payload_disagreed=%d"
        " resolved_fraction=%.3f"
    ) % (
        fields["handover_considered"], fields["handover_resolved"],
        fields["handover_resolved_catalog"], fields["handover_resolved_seed_stamp"],
        fields["handover_refused_ambiguous"], fields["handover_contradicted"],
        fields["handover_no_identity"], fields["handover_no_product_key"],
        fields["handover_lookup_failed"], fields["handover_not_primed"],
        fields["handover_rows_scanned"], fields["handover_rows_rejected"],
        fields["handover_rows_stamp_vetoed"], fields["handover_rows_payload_disagreed"],
        fields["handover_resolved_fraction"],
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default
