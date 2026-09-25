"""The hand-over variant decision, and the wire that carries it into the four cart lanes.

WHY THIS FILE EXISTS. `routes/agent_shop_gateway` derived the cart variant from
`shopify_variant_identity.sole_stamped_variant_id`, which reads
`seed_data.snapshot.variants[].shopify_variant_id`. Measured on prod 2026-09-08: **0 of 11,834
active seeds carry that key** — so every test that pinned the cart lane pinned a path that
never fires in production, and the checkout preflight behind it had an empty denominator by
construction. `services/handover_variant_identity` reads the identity where it actually lives,
`catalog_skus.source_variant_id`, and this file pins the DECISION (pure, no database) plus the
threading into the route. The write path against a real `catalog_skus` is
`tests/test_handover_variant_identity_postgres.py`; SQLite cannot express the shapes that
matter there and a decision test must not need a database to say what it means.
"""

import json
import os

import pytest

from services.handover_variant_identity import (
    Candidate,
    HandoverVariant,
    HandoverVariantResolver,
    R_AMBIGUOUS,
    R_CONTRADICTED,
    R_EXACT,
    R_LOOKUP_FAILED,
    R_NOT_PRIMED,
    R_NO_IDENTITY,
    R_NO_PRODUCT_KEY,
    R_SEED_STAMP,
    R_SOLE,
    SOURCE_CATALOG_SKU,
    SOURCE_SEED_STAMP,
    X_NOT_MERCHANT_ISSUED,
    X_PAYLOAD_DISAGREES,
    X_STAMP_VETO,
    candidate_from_row,
    choose_handover_variant,
    handover_coverage_fields,
    handover_coverage_message,
)

PK = "prod::m_brand::external_seed::brand-serum"
SPID = "brand-serum"
#: A real Shopify variant id shape: 8+ digits, and not derivable from the product key.
VID = "41234567890123"
VID_B = "41234567890999"


def _row(**kw):
    row = {
        "sku_key": PK + "::v:" + VID,
        "product_key": PK,
        "source_product_id": SPID,
        "source_variant_id": VID,
        "sku_payload": json.dumps({"variant_id": VID, "variant_id_provenance": "merchant_issued"}),
    }
    row.update(kw)
    return row


def _seed(*variants):
    return {"snapshot": {"variants": list(variants)}}


# ---------------------------------------------------------------------------------------------
# Which rows may become a candidate at all
# ---------------------------------------------------------------------------------------------

def test_a_product_derived_id_is_never_a_candidate():
    """MUTANT: drop the classifier call from `candidate_from_row`.

    39.4% of `catalog_skus` (11,926 rows, prod 2026-09-08) carry a `source_variant_id` that is
    the product key restated — `ingestion.py:841` writes it as a STORAGE token to satisfy the
    identity index, and the gateway's own `isRestatedProductId` throws exactly that shape away.
    Admitting it would build a cart URL from a value Shopify never issued.
    """
    rejects = {}
    assert candidate_from_row(_row(source_variant_id=PK, sku_payload=None), rejects) is None
    assert rejects == {X_NOT_MERCHANT_ISSUED: 1}


def test_an_unverifiable_id_is_never_a_candidate():
    """`ABCD-RED-30ML` is a merchant SKU string, not a merchant VARIANT id.

    UNVERIFIABLE is 13.3% of the table. It is not an accusation — we simply cannot place it —
    and `variant_identity`'s docstring is explicit that money callers must treat it as they
    treat a missing id.
    """
    rejects = {}
    assert candidate_from_row(
        _row(source_variant_id="ABCD-RED-30ML", sku_payload=None), rejects) is None
    assert rejects == {X_NOT_MERCHANT_ISSUED: 1}


def test_the_gid_form_is_admitted():
    """`gid://shopify/ProductVariant/<n>` is the same identity in Shopify's newer spelling.

    Admitted, and FOLDED — see `test_a_gid_is_folded_to_the_one_spelling_every_consumer_reads`
    for why the fold is not cosmetic.
    """
    gid = "gid://shopify/ProductVariant/41234567890123"
    cand = candidate_from_row(_row(source_variant_id=gid, sku_payload=None))
    assert cand is not None and cand.stored_variant_id == gid


def test_the_stamp_may_veto_but_may_not_authorise():
    """MUTANT: make the stamp authoritative ("prefer the stamp") instead of a veto.

    Both halves are pinned here because they fail in opposite directions and a test for one
    survives the other's mutation. A stamp saying `product_derived` refuses a row the classifier
    would have taken; a stamp saying `merchant_issued` does NOT rescue a row the classifier
    refuses. Measured on prod 2026-09-08 the two never disagree (0 of 7,174 stamped rows), so
    this costs nothing today and bounds a careless future writer.
    """
    vetoed = {}
    assert candidate_from_row(
        _row(sku_payload=json.dumps({"variant_id_provenance": "product_derived"})), vetoed) is None
    assert vetoed == {X_STAMP_VETO: 1}

    forged = {}
    assert candidate_from_row(
        _row(source_variant_id=PK,
             sku_payload=json.dumps({"variant_id_provenance": "merchant_issued"})), forged) is None
    assert forged == {X_NOT_MERCHANT_ISSUED: 1}, (
        "the classifier runs FIRST and is necessary; a stamp cannot promote a restated id")


def test_a_truncated_stored_id_is_refused_rather_than_handed_over():
    """`source_variant_id` is `String(128)`; `sku_payload.variant_id` keeps the FULL id (#2148).

    They differ exactly when the id was cut at the column bound, and a cut variant id names a
    variant the merchant does not have. The classifier structurally cannot see this — it is
    handed one string — so the agreement check is the only place it can be caught.
    """
    rejects = {}
    long_id = "8" * 130
    assert candidate_from_row(
        _row(source_variant_id=long_id[:128],
             sku_payload=json.dumps({"variant_id": long_id})), rejects) is None
    assert rejects == {X_PAYLOAD_DISAGREES: 1}


def test_an_unparseable_payload_neither_vetoes_nor_authorises():
    """A jsonb column read back as junk must not be able to swing the decision either way."""
    cand = candidate_from_row(_row(sku_payload="{not json"))
    assert cand is not None and cand.variant_id == VID and cand.stamped is False


def test_a_dict_payload_is_read_the_same_as_a_json_string():
    """Postgres hands back a dict, SQLite a string. One decision, both dialects."""
    as_dict = candidate_from_row(_row(sku_payload={"variant_id": VID,
                                                   "variant_id_provenance": "merchant_issued"}))
    as_text = candidate_from_row(_row())
    assert as_dict is not None and as_text is not None
    assert as_dict.variant_id == as_text.variant_id == VID
    assert as_dict.stamped is True and as_text.stamped is True


# ---------------------------------------------------------------------------------------------
# The decision over the candidates
# ---------------------------------------------------------------------------------------------

def _cand(vid, sku_key=None):
    return Candidate(sku_key=sku_key or (PK + "::v:" + vid), product_key=PK,
                     variant_id=vid, stamped=True)


def test_one_live_merchant_issued_sku_is_the_answer():
    got = choose_handover_variant([_cand(VID)])
    assert got.variant_id == VID
    assert got.reason == R_SOLE and got.source == SOURCE_CATALOG_SKU
    assert got.sku_key == PK + "::v:" + VID


def test_two_candidates_and_no_name_refuses_rather_than_picking_one():
    """MUTANT: `return live[0]` when the exact match misses.

    This is the `defaultVariant` hazard, observed live: Reap's availability-ordered default
    resolved a $95 Mini for a $140 Standard with `ok=True`. The redirect is built at PRODUCT
    grain — the buyer has not chosen — so any ordering over these candidates is a guess, and a
    wrong variant id is worse than none.
    """
    got = choose_handover_variant([_cand(VID), _cand(VID_B)])
    assert got.variant_id is None
    assert got.reason == R_AMBIGUOUS
    assert got.candidates == 2


def test_two_candidates_and_an_exact_name_resolves_that_one():
    """String equality against a STORED merchant-issued id is identity, not inference.

    1,011 of the 3,871 resolvable seeds (prod, 2026-09-08) land here — they are multi-variant
    products whose hand-over names one variant — so refusing this case would throw away a
    quarter of the recovered identity.
    """
    got = choose_handover_variant([_cand(VID), _cand(VID_B)], offer_variant_id=VID_B)
    assert got.variant_id == VID_B
    assert got.reason == R_EXACT and got.candidates == 2


def test_a_name_that_matches_no_candidate_still_refuses():
    """The name narrows; it never authorises on its own. `_seed_offer_variant_id` resolves from
    variant_id | variantId | sku | sku_id | id, so the value reaching here is routinely a SKU
    string — and a SKU that matches nothing must not fall back to a candidate."""
    got = choose_handover_variant([_cand(VID), _cand(VID_B)], offer_variant_id="SKU-30ML")
    assert got.variant_id is None and got.reason == R_AMBIGUOUS


def test_the_seed_stamp_speaks_only_where_catalog_said_nothing():
    """MUTANT: fall back to the seed stamp after an AMBIGUOUS refusal.

    A snapshot claiming one variant while catalog holds two live merchant-issued SKUs is a
    contradiction, and the resolution of a contradiction is silence, not the other opinion.
    "A conditional fallback is still a fallback" — the round-5 P0 in the same lane.
    """
    seed = _seed({"shopify_variant_id": VID})
    empty = choose_handover_variant([], seed_data=seed)
    assert empty.variant_id == VID and empty.reason == R_SEED_STAMP
    assert empty.source == SOURCE_SEED_STAMP

    contradicted = choose_handover_variant([_cand("41111111111111"), _cand(VID_B)],
                                           seed_data=seed)
    assert contradicted.variant_id is None and contradicted.reason == R_AMBIGUOUS


def test_no_candidates_and_no_stamp_is_an_honest_absence():
    got = choose_handover_variant([], seed_data=_seed({"variant_id": "80072940"}))
    assert got.variant_id is None and got.reason == R_NO_IDENTITY


def test_a_failed_or_unprimed_lookup_carries_no_id():
    """Both are "we could not name the variant", and both must degrade to a referral. Neither
    may reach the seed stamp: a lookup we could not complete is not evidence that catalog holds
    nothing."""
    seed = _seed({"shopify_variant_id": VID})
    assert choose_handover_variant([], seed_data=seed, lookup_ok=False) == HandoverVariant(
        None, R_LOOKUP_FAILED)
    assert choose_handover_variant([], seed_data=seed, primed=False) == HandoverVariant(
        None, R_NOT_PRIMED)


# ---------------------------------------------------------------------------------------------
# The resolver: batching, failure isolation, counters
# ---------------------------------------------------------------------------------------------

class _FakeResolver(HandoverVariantResolver):
    def __init__(self, rows_by_key=None, raises=False, **kw):
        super().__init__(**kw)
        self._rows_by_key = rows_by_key or {}
        self._raises = raises
        self.fetch_calls = []

    async def _fetch(self, keys):
        self.fetch_calls.append(list(keys))
        if self._raises:
            raise RuntimeError("statement timeout")
        out = []
        for k in keys:
            out.extend(self._rows_by_key.get(k) or [])
        return out


async def test_the_whole_batch_is_one_statement_and_a_second_prime_asks_only_for_new_keys():
    """MUTANT: prime inside the per-card loop.

    `_append_external_offers_from_seed_rows` is called from three sites and the search lane
    builds cards under a wall-clock budget; a query per card spends that budget on round trips.
    """
    r = _FakeResolver({PK: [_row()]})
    await r.prime([PK, PK, "prod::x::external_seed::y", None, ""])
    assert r.fetch_calls == [[PK, "prod::x::external_seed::y"]]
    await r.prime([PK, "prod::z::external_seed::w"])
    assert r.fetch_calls[1] == ["prod::z::external_seed::w"], "already-primed keys are not re-asked"


async def test_a_key_with_no_rows_is_primed_and_empty_not_unknown():
    """"Asked, and the answer was nothing" and "never asked" are different facts, and the
    coverage line has to be able to tell them apart — otherwise a missing prime reads as a
    catalog with no identity in it."""
    r = _FakeResolver({})
    await r.prime([PK])
    assert r.choose(product_key=PK).reason == R_NO_IDENTITY
    assert r.choose(product_key="prod::never::external_seed::asked").reason == R_NOT_PRIMED


async def test_a_failed_batch_does_not_silence_seeds_that_needed_no_lookup():
    """A standalone seed carries no `attached_product_key`, so no lookup was ever made on its
    behalf, and a failed batch must not silence its seed-stamp hand-over.

    NOT a mutant pin, and the docstring used to say it was. Review falsified that: `choose`
    takes the no-product-key branch BEFORE it consults `_failed`, so this case is identical
    under a latch and under the per-key set. The latch is pinned by
    `test_one_failed_batch_does_not_condemn_a_key_that_loaded_fine`, which uses two keys.
    """
    r = _FakeResolver({}, raises=True)
    await r.prime([PK])
    assert r.choose(product_key=PK).reason == R_LOOKUP_FAILED
    standalone = r.choose(product_key=None, seed_data=_seed({"shopify_variant_id": VID}))
    assert standalone.variant_id == VID and standalone.reason == R_SEED_STAMP


async def test_a_standalone_seed_without_a_stamp_reports_the_missing_key():
    r = _FakeResolver({})
    got = r.choose(product_key="   ", seed_data=_seed({"variant_id": "80072940"}))
    assert got.variant_id is None and got.reason == R_NO_PRODUCT_KEY
    assert r.stats["handover_no_product_key"] == 1


async def test_the_key_cap_degrades_hand_overs_and_cannot_publish_one():
    r = _FakeResolver({PK: [_row()]}, max_keys=1)
    await r.prime([PK, "prod::b::external_seed::b", "prod::c::external_seed::c"])
    assert r.fetch_calls == [[PK]]
    assert r.choose(product_key=PK).variant_id == VID
    assert r.choose(product_key="prod::b::external_seed::b").reason == R_NOT_PRIMED


async def test_the_in_list_is_built_from_generated_names_never_from_the_keys(monkeypatch):
    """`attached_product_key` is caller-influenced data. A key interpolated into the SQL would
    be an injection site on a serving path, so the placeholders are positional names and the
    keys only ever travel as bound values.

    The FIRST version of this test rebuilt the statement from `_SELECT` itself and asserted on
    its own string; review replaced `_fetch`'s body with literal interpolation and it stayed
    green. It has to drive the real `_fetch` and read what the database was actually handed.
    """
    from db import database as db_module

    seen = {}

    async def recording_fetch_all(query, values=None):
        seen["query"] = str(query)
        seen["values"] = values
        return []

    monkeypatch.setattr(db_module.database, "fetch_all", recording_fetch_all)
    hostile = "prod::m::external_seed::x'); DROP TABLE catalog_skus; --"
    r = HandoverVariantResolver()
    await r.prime([PK, hostile])

    assert hostile not in seen["query"], "the key must never appear in the statement text"
    assert "DROP TABLE" not in seen["query"]
    assert ":pk0" in seen["query"] and ":pk1" in seen["query"]
    assert set(seen["values"].values()) == {PK, hostile}, "keys travel only as bound values"


async def test_a_key_that_failed_then_loaded_answers_from_the_load(monkeypatch):
    """MUTANT: drop `self._failed.difference_update(wanted)` from `prime`.

    Three lanes prime the same resolver. A key whose first batch timed out kept answering
    `sku_lookup_failed` even with its candidates sitting in `_by_key` — fail-closed, so never a
    wrong cart, but it threw away a hand-over the second lookup had already paid for.
    """
    r = _FakeResolver({PK: [_row()]}, raises=True)
    await r.prime([PK])
    assert r.choose(product_key=PK).reason == R_LOOKUP_FAILED
    r._raises = False
    await r.prime([PK])
    assert r.choose(product_key=PK).variant_id == VID


async def test_one_failed_batch_does_not_condemn_a_key_that_loaded_fine(monkeypatch):
    """MUTANT: latch the failure resolver-wide (`self._latched = True`).

    Review falsified the first version of this pin: its second assertion used
    `choose(product_key=None)`, which takes the no-key early branch and never consults
    `_failed` under EITHER design, so the mutant survived 250/250. The distinguishing case is
    two DIFFERENT keys — one whose batch raised, one whose batch succeeded.
    """
    r = _FakeResolver({"prod::ok::external_seed::k": [_row(product_key="prod::ok::external_seed::k")]},
                      raises=True)
    await r.prime([PK])
    r._raises = False
    await r.prime(["prod::ok::external_seed::k"])

    assert r.choose(product_key=PK).reason == R_LOOKUP_FAILED, "the failed key stays refused"
    assert r.choose(product_key="prod::ok::external_seed::k").variant_id == VID, (
        "a failure about one product key is not evidence about another")


async def test_the_lookup_is_bounded_by_a_wall_clock_timeout(monkeypatch):
    """MUTANT: remove `asyncio.wait_for` from `prime`.

    Review found this entirely unpinned while a gate-file comment claimed it was pinned. This
    lane is an agent-facing serving path; without the bound a slow statement holds offer
    resolution for the full statement timeout, for a read that only decorates.
    """
    import asyncio as _asyncio

    class _SlowResolver(HandoverVariantResolver):
        async def _fetch(self, keys):
            await _asyncio.sleep(5)
            return []

    r = _SlowResolver(timeout_s=0.05)
    await r.prime([PK])
    assert r.choose(product_key=PK).reason == R_LOOKUP_FAILED, (
        "an unbounded lookup would have blocked here for five seconds and then answered")


async def test_two_candidates_sharing_one_id_are_not_treated_as_an_exact_hit():
    """MUTANT: `len(hits) >= 1`.

    Two rows spelling the same identity is a data defect (`::v:` vs `::v::`, the two lanes'
    infixes), and picking one makes the reported `sku_key` arbitrary. Refusing keeps the
    ambiguity visible.
    """
    got = choose_handover_variant(
        [_cand(VID, sku_key="a"), _cand(VID, sku_key="b")], offer_variant_id=VID)
    assert got.variant_id is None and got.reason == R_AMBIGUOUS


def test_a_gid_is_folded_to_the_one_spelling_every_consumer_reads():
    """MUTANT: return `svid` unnormalised.

    `variant_identity` admits `gid://shopify/ProductVariant/<n>` as identity, and every other
    reader in this lane folds it away. Unfolded it reaches `offer_spec["variant_id"]`, which is
    documented one line above itself as "the NUMERIC storefront variant id", and it reaches
    `live_offer_verification._check_one`, which string-compares against `parse_product_js`'s
    bare-numeric ids and so can never match — turning a live variant into `variant_absent`.
    """
    gid = "gid://shopify/ProductVariant/" + VID
    cand = candidate_from_row(_row(source_variant_id=gid, sku_payload=None))
    assert cand is not None
    assert cand.variant_id == VID, "the canonical form"
    assert cand.stored_variant_id == gid, (
        "the column's own spelling is kept for a debugger, not for matching — see Candidate")
    assert choose_handover_variant([cand]).variant_id == VID
    assert choose_handover_variant([cand], offer_variant_id=gid).variant_id == VID, (
        "named in either spelling, it is still the same variant")


def test_a_hand_over_naming_a_different_merchant_issued_id_refuses_even_against_one_candidate():
    """MUTANT: return the sole candidate before comparing it to the name.

    THIS IS THE ROUND-1 P1. An operator attached the $140 Standard; catalog held exactly one
    merchant-issued row, for the $95 Mini; the first cut prefilled the Mini. One catalog row
    being alone is not evidence that the name is wrong — it is two merchant-issued ids
    disagreeing, and naming either is the guess this module refuses everywhere else.
    """
    got = choose_handover_variant([_cand(VID)], offer_variant_id=VID_B)
    assert got.variant_id is None
    assert got.reason == R_CONTRADICTED and got.candidates == 1


def test_a_name_that_is_not_itself_merchant_issued_cannot_contradict():
    """The seed lane passes `_seed_offer_variant_id`, which reads
    variant_id | variantId | sku | sku_id | id — routinely a SKU string. A SKU naming no
    variant is an ABSENCE of information, not a disagreement, and treating it as one would
    have thrown away most of the 2,864 sole-candidate seeds."""
    got = choose_handover_variant([_cand(VID)], offer_variant_id="SKU-30ML")
    assert got.variant_id == VID and got.reason == R_SOLE


def test_a_seed_stamp_too_short_for_the_classifier_is_not_handed_over():
    """`sole_stamped_variant_id` gates on any digit string; `variant_identity` requires 8+.

    A snapshot stamped "12345" used to become a hand-over id that `checkout_preflight` then
    refused as `no_merchant_issued_variant_id` — a refusal this resolver had caused. One rule
    for both sources, or the module's guarantee is only true of half its answers.
    """
    short = choose_handover_variant([], seed_data=_seed({"shopify_variant_id": "12345"}))
    assert short.variant_id is None and short.reason == R_NO_IDENTITY
    good = choose_handover_variant([], seed_data=_seed({"shopify_variant_id": VID}))
    assert good.variant_id == VID and good.reason == R_SEED_STAMP


async def test_the_counters_count_what_happened():
    r = _FakeResolver({
        PK: [_row()],
        "prod::m::external_seed::multi": [
            _row(product_key="prod::m::external_seed::multi",
                 sku_key="a", source_variant_id=VID, sku_payload=None),
            _row(product_key="prod::m::external_seed::multi",
                 sku_key="b", source_variant_id=VID_B, sku_payload=None),
        ],
        "prod::m::external_seed::derived": [
            _row(product_key="prod::m::external_seed::derived",
                 source_variant_id="prod::m::external_seed::derived", sku_payload=None),
        ],
    })
    await r.prime([PK, "prod::m::external_seed::multi", "prod::m::external_seed::derived"])
    r.choose(product_key=PK)
    r.choose(product_key="prod::m::external_seed::multi")
    r.choose(product_key="prod::m::external_seed::derived")
    r.choose(product_key=None, seed_data=_seed({"shopify_variant_id": VID}))

    f = handover_coverage_fields(r.stats)
    assert f["handover_considered"] == 4
    assert f["handover_resolved"] == 2
    assert f["handover_resolved_catalog"] == 1
    assert f["handover_resolved_seed_stamp"] == 1
    assert f["handover_refused_ambiguous"] == 1
    assert f["handover_no_identity"] == 1
    assert f["handover_resolved_fraction"] == 0.5
    assert r.stats["handover_reject_" + X_NOT_MERCHANT_ISSUED] == 1


def test_coverage_is_reported_even_when_nothing_resolved():
    """MUTANT: key the early return on `handover_resolved` the way
    `preflight_coverage_fields` keys its own on coverage.

    That is the right rule there and the wrong rule here: a request where every hand-over was
    refused is the single most informative one to read, and on today's corpus it is also the
    common one. The line disappears only when nothing was handed over at all.
    """
    assert handover_coverage_fields({}) == {}
    assert handover_coverage_fields({"handover_considered": 0}) == {}
    f = handover_coverage_fields({"handover_considered": 5, "handover_no_identity": 5})
    assert f["handover_considered"] == 5 and f["handover_resolved_fraction"] == 0.0


def test_the_numbers_are_in_the_message_text_not_only_in_extra():
    """`setup_structured_logging()` is never called from main and the root logger sits at
    WARNING in prod, so a counter that lives only in `extra` does not leave the process. Every
    field emitted has to appear in the string."""
    f = handover_coverage_fields({
        "handover_considered": 9, "handover_resolved": 4, "handover_resolved_catalog": 3,
        "handover_resolved_seed_stamp": 1, "handover_refused_ambiguous": 2,
        "handover_contradicted": 1, "handover_no_identity": 2, "handover_no_product_key": 1,
        "handover_lookup_failed": 0, "handover_not_primed": 0,
        "handover_rows_scanned": 20, "handover_rows_rejected": 11,
        "handover_reject_" + X_STAMP_VETO: 3,
        "handover_reject_" + X_PAYLOAD_DISAGREES: 2,
    })
    msg = handover_coverage_message(f)
    for key, value in f.items():
        if key == "handover_resolved_fraction":
            continue
        assert "=%d" % value in msg or ("%s=%d" % (key.replace("handover_", ""), value)) in msg
    assert "considered=9" in msg and "catalog=3" in msg and "ambiguous=2" in msg
    assert "resolved_fraction=0.444" in msg
    # The row counters were incremented in `prime` and emitted NOWHERE — the exact "a counter
    # nobody reads" defect the route comment warns about, and the only production evidence that
    # the two future-writer guards ever fire.
    assert "rows_scanned=20" in msg and "rows_rejected=11" in msg
    assert "stamp_vetoed=3" in msg and "payload_disagreed=2" in msg
    assert "contradicted=1" in msg
    assert handover_coverage_message({}) == ""


# ---------------------------------------------------------------------------------------------
# THE WIRE. The decision above is worthless if the route does not read it, and every existing
# cart-lane test in this repo drives the seed-stamp path — a path measured DEAD on prod. These
# drive the catalog path end to end, through the real route, to the real redirect builder.
# ---------------------------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

_VERIFIED_DESTINATION = {
    "destination_checked_at": datetime.now(timezone.utc).isoformat(),
    "destination_http_status": 200,
    "destination_verdict": "live",
    "destination_failure_streak": 0,
}
_VERIFIED_CONTENT = {"extracted_at": datetime.now(timezone.utc).isoformat()}

CATALOG_VID = "43062643884185"
OTHER_VID = "43062643884999"


@pytest.fixture
def client():
    return TestClient(app)


def _seed_row(*, snapshot_extra=None, variants=None, attached_product_key=PK):
    snapshot = {**_VERIFIED_CONTENT, "variants": []}
    snapshot.update(snapshot_extra or {})
    return {
        "id": "eps_handover",
        "external_product_id": SPID,
        "market": "US",
        "tool": "*",
        "destination_url": "https://brand.com/products/serum",
        "canonical_url": "https://brand.com/products/serum",
        "domain": "brand.com",
        "title": "Serum",
        "price_amount": 19.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "utm_template": None,
        "attached_product_key": attached_product_key,
        "seed_data": {
            "brand": "Brand",
            "snapshot": snapshot,
            "variants": variants if variants is not None else [
                {"variant_id": "SKU-30ML", "title": "30ml", "price_amount": 19.0,
                 "price_currency": "USD", "availability": "in_stock"},
            ],
        },
        "status": "active",
        **_VERIFIED_DESTINATION,
    }


def _wire(monkeypatch, *, seed_row, sku_rows):
    """offers.resolve with ONE seed row and a controlled `catalog_skus` answer.

    The redirect builder is recorded rather than mocked away wholesale, because the argument
    between identity and builder is exactly what round 6 found unverified at three of four
    production call sites.
    """
    import routes.agent_shop_gateway as gateway

    seen = {}

    async def fake_fetch_all(query, values=None):
        q = str(query)
        if "FROM catalog_skus" in q:
            return list(sku_rows)
        if "FROM external_product_seeds" in q:
            return [seed_row]
        return []

    async def recording_builder(**kwargs):
        seen.update(kwargs)
        return "https://example.com/r?token=test"

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", recording_builder)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    return seen


def _resolve(client, sku_id="SKU-30ML", product_id=None):
    """`sku_id` filters `matched_variants` to the one candidate naming it; `product_id` instead
    lets the whole variant list through, which is how a multi-variant hand-over is exercised."""
    product = {"product_id": product_id} if product_id else {"sku_id": sku_id}
    return client.post(
        "/agent/shop/v1/invoke",
        json={"operation": "offers.resolve",
              "payload": {"product": product, "limit": 10, "market": "US", "tool": "*"},
              "metadata": {"source": "creator-agent-ui"}},
    )


def test_the_cart_variant_comes_from_catalog_skus_when_the_seed_field_is_empty(
    monkeypatch, client
):
    """THE SEAM, end to end. FAILS ON MAIN with `cart_variant_id=None`.

    The seed carries storefront evidence but NO `shopify_variant_id` — which is the shape of
    every one of the 11,834 active seeds on prod — while `catalog_skus` holds the merchant's
    own id. Before #2151 the route read only the seed and handed the builder nothing.
    """
    seen = _wire(
        monkeypatch,
        seed_row=_seed_row(snapshot_extra={"storefront_platform": "shopify",
                                           "storefront_platform_source": "products_js_v1"}),
        sku_rows=[_row(source_variant_id=CATALOG_VID,
                       sku_payload=json.dumps({"variant_id": CATALOG_VID,
                                               "variant_id_provenance": "merchant_issued"}))],
    )
    assert _resolve(client).status_code == 200
    assert seen, "the redirect builder was never reached — this test would prove nothing"
    assert seen["cart_variant_id"] == CATALOG_VID
    assert seen["variant_id"] != CATALOG_VID, (
        "attribution keeps its own value; the recovered id rides cart_variant_id only")


def test_a_product_derived_catalog_row_never_reaches_the_cart(monkeypatch, client):
    """MUTANT: remove the MERCHANT_ISSUED filter from `candidate_from_row`.

    This is the 39.4% of the SKU table whose `source_variant_id` is the product key restated.
    Handing one to the cart builder is the wrong-cart hazard the whole module exists to refuse.
    """
    seen = _wire(
        monkeypatch,
        seed_row=_seed_row(snapshot_extra={"storefront_platform": "shopify"}),
        sku_rows=[_row(source_variant_id=PK, sku_payload=None)],
    )
    assert _resolve(client).status_code == 200
    assert seen and seen["cart_variant_id"] is None


def test_two_live_merchant_issued_skus_and_no_name_hand_over_no_cart(monkeypatch, client):
    """MUTANT: take the first candidate. The buyer has not chosen a variant here."""
    seen = _wire(
        monkeypatch,
        seed_row=_seed_row(snapshot_extra={"storefront_platform": "shopify"}),
        sku_rows=[
            _row(sku_key="a", source_variant_id=CATALOG_VID, sku_payload=None),
            _row(sku_key="b", source_variant_id=OTHER_VID, sku_payload=None),
        ],
    )
    assert _resolve(client).status_code == 200
    assert seen and seen["cart_variant_id"] is None


def test_a_hand_over_naming_one_of_several_skus_resolves_that_one(monkeypatch, client):
    """The 1,011-seed case: a multi-variant product whose candidate names one stored id."""
    seen = _wire(
        monkeypatch,
        seed_row=_seed_row(
            snapshot_extra={"storefront_platform": "shopify"},
            variants=[{"variant_id": OTHER_VID, "title": "50ml", "price_amount": 29.0,
                       "price_currency": "USD", "availability": "in_stock"}],
        ),
        sku_rows=[
            _row(sku_key="a", source_variant_id=CATALOG_VID, sku_payload=None),
            _row(sku_key="b", source_variant_id=OTHER_VID, sku_payload=None),
        ],
    )
    assert _resolve(client, sku_id=OTHER_VID).status_code == 200
    assert seen and seen["cart_variant_id"] == OTHER_VID


def test_a_stale_seed_stamp_never_outranks_the_catalog_row(monkeypatch, client):
    """MUTANT: `sole_stamped_variant_id(seed_data) or handover.variant_id`.

    The stamp is a snapshot of a crawl; the catalog row is the identity the backfill wrote and
    audited. Where both exist the catalog row is the one checkout reads, and the two disagreeing
    is precisely when preferring the stale one does damage.
    """
    seen = _wire(
        monkeypatch,
        seed_row=_seed_row(snapshot_extra={
            "storefront_platform": "shopify",
            "variants": [{"shopify_variant_id": OTHER_VID, "title": "30ml"}],
        }),
        sku_rows=[_row(source_variant_id=CATALOG_VID, sku_payload=None)],
    )
    assert _resolve(client).status_code == 200
    assert seen and seen["cart_variant_id"] == CATALOG_VID


def test_the_preflight_fires_where_the_variant_is_named_even_with_no_cart(monkeypatch, client):
    """THE DENOMINATOR PIN. FAILS ON MAIN, where the gate asks about nothing.

    No storefront evidence, so no cart can be built and `cart_variant_id` stays None — the
    state of ALL 11,834 active seeds. The merchant question ("is this variant still real")
    needs only the identity, and #2151 moved the gate onto it. Keyed on `cart_variant_id` the
    shadow report's denominator is empty by construction, which is what the 2026-09-08 sample
    measured.
    """
    import routes.agent_shop_gateway as gateway

    asked = []

    async def counting_preflight(offer):
        asked.append(offer)
        return True

    seen = _wire(
        monkeypatch,
        seed_row=_seed_row(),
        sku_rows=[_row(source_variant_id=CATALOG_VID, sku_payload=None)],
    )
    monkeypatch.setattr(gateway, "_preflight_allows_external_offer", counting_preflight)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    assert _resolve(client).status_code == 200

    assert seen["cart_variant_id"] is None, (
        "no storefront evidence, so no cart — that half of the seam is NOT fixed here")
    assert len(asked) == 1, "the gate must still apply to this hand-over"
    assert asked[0]["execution_spec"]["variant_id"] == CATALOG_VID, (
        "and it must ask about the id we resolved, not about None")


def test_budget_exhaustion_fails_closed_under_enforce_and_open_under_shadow(monkeypatch, client):
    """MUTANT: restore the unconditional `_allowed = True`.

    Exhausting the budget is "we could not ask the merchant", which is `unverifiable` — and
    `checkout_preflight`'s contract is that enforce refuses that. The old unconditional True
    was a fail-open the mode could not override. It stays mode-respecting rather than a hard
    False because shadow's one guarantee is that it never changes what the buyer is handed.
    """
    import routes.agent_shop_gateway as gateway

    async def never_asked(offer):
        raise AssertionError("the budget was supposed to be exhausted before any ask")

    for mode, expect_cart in (("enforce", False), ("shadow", True)):
        seen = _wire(
            monkeypatch,
            seed_row=_seed_row(snapshot_extra={"storefront_platform": "shopify"}),
            sku_rows=[_row(source_variant_id=CATALOG_VID, sku_payload=None)],
        )
        monkeypatch.setattr(gateway, "_preflight_allows_external_offer", never_asked)
        monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", mode)
        monkeypatch.setenv("CHECKOUT_PREFLIGHT_MAX_PER_REQUEST", "0")
        assert _resolve(client).status_code == 200
        assert bool(seen["cart_variant_id"]) is expect_cart, (
            f"mode={mode}: exhaustion must degrade the cart under enforce and only there")


def test_a_failed_sku_lookup_degrades_the_hand_over_and_serves_the_offer(monkeypatch, client):
    """A gate that DELETES SUPPLY is the shape this repo has been bitten by. A lookup failure
    costs the cart shortcut and nothing else — the offer still ships as an honest referral."""
    import routes.agent_shop_gateway as gateway

    async def exploding_fetch_all(query, values=None):
        q = str(query)
        if "FROM catalog_skus" in q:
            raise RuntimeError("statement timeout")
        if "FROM external_product_seeds" in q:
            return [_seed_row(snapshot_extra={"storefront_platform": "shopify"})]
        return []

    seen = _wire(monkeypatch, seed_row=_seed_row(), sku_rows=[])
    monkeypatch.setattr(gateway.database, "fetch_all", exploding_fetch_all)
    res = _resolve(client)
    assert res.status_code == 200
    assert seen and seen["cart_variant_id"] is None
    assert res.json()["offers_count"] >= 1, "the offer survives; only the cart is withdrawn"


def test_the_shopify_attach_branch_has_three_answers_and_the_middle_one_is_silence(
    monkeypatch, client
):
    """MUTANT (the round-1 P1): `handover.variant_id or extract_shopify_numeric_variant_id(...)`.

    `attached_variant_id` is whatever an operator pasted into an attach form — the one input on
    this path with no catalog lookup behind it. The first cut let a catalog row win
    unconditionally, so an operator who attached the $140 Standard got the $95 Mini prefilled
    the moment catalog held one merchant-issued row for it, and the test that pinned that
    override would have kept it forever. Three cases, one test, so no two can drift:

      agree      -> the id;
      disagree   -> NOTHING, because two merchant-issued ids disagreeing about one hand-over
                    means we do not know which physical thing the buyer would receive;
      no catalog -> the operator value, exactly as before this PR.
    """
    shopify_pk = "prod::m_brand::shopify::brand-serum"
    row = _seed_row(attached_product_key=shopify_pk)
    row["attached_variant_id"] = CATALOG_VID
    agree = _wire(
        monkeypatch, seed_row=row,
        sku_rows=[_row(product_key=shopify_pk, source_variant_id=CATALOG_VID, sku_payload=None)])
    assert _resolve(client).status_code == 200
    assert agree["cart_variant_id"] == CATALOG_VID

    row_disagreeing = dict(row, attached_variant_id="99999999999999")
    disagree = _wire(
        monkeypatch, seed_row=row_disagreeing,
        sku_rows=[_row(product_key=shopify_pk, source_variant_id=CATALOG_VID, sku_payload=None)])
    assert _resolve(client).status_code == 200
    assert disagree["cart_variant_id"] is None, (
        "the operator named one variant and catalog holds another — naming either is a guess")

    none_stored = _wire(monkeypatch, seed_row=row_disagreeing, sku_rows=[])
    assert _resolve(client).status_code == 200
    assert none_stored["cart_variant_id"] == "99999999999999", (
        "no catalog row: unchanged behaviour")


def test_a_key_carried_only_inside_seed_data_is_still_looked_up(monkeypatch, client):
    """MUTANT: read `attached_product_key` from the row only.

    `_external_seed_redirect_identity` has always taken it from the row OR from `seed_data`, so
    a resolver primed from the row alone gives such a seed a parsed merchant and platform and
    NO catalog lookup — a half-wiring that reads exactly like "this product has no identity"
    and would never be noticed, because the honest-absence answer looks identical.
    """
    row = _seed_row(snapshot_extra={"storefront_platform": "shopify"},
                    attached_product_key=None)
    row.pop("attached_product_key")
    row["seed_data"]["attached_product_key"] = PK

    seen = _wire(
        monkeypatch, seed_row=row,
        sku_rows=[_row(source_variant_id=CATALOG_VID, sku_payload=None)])
    assert _resolve(client).status_code == 200
    assert seen and seen["cart_variant_id"] == CATALOG_VID


def test_the_lookup_is_one_statement_for_the_whole_seed_batch(monkeypatch, client):
    """MUTANT: prime inside the per-card loop. Counted at the route, not at the resolver, so
    the pin survives a refactor that moves the priming."""
    import routes.agent_shop_gateway as gateway

    sku_queries = []

    async def counting_fetch_all(query, values=None):
        q = str(query)
        if "FROM catalog_skus" in q:
            sku_queries.append(values)
            return []
        if "FROM external_product_seeds" in q:
            # DISTINCT product keys, or per-row priming would still make one call (the second
            # key already primed) and the mutant would survive — the same masking that let a
            # single-seed memo hide the preflight budget.
            return [
                _seed_row(),
                {**_seed_row(attached_product_key=PK + "-two"), "id": "eps_2",
                 "external_product_id": SPID + "_2",
                 "destination_url": "https://brand.com/products/serum-2",
                 "canonical_url": "https://brand.com/products/serum-2"},
            ]
        return []

    _wire(monkeypatch, seed_row=_seed_row(), sku_rows=[])
    monkeypatch.setattr(gateway.database, "fetch_all", counting_fetch_all)
    assert _resolve(client).status_code == 200
    assert len(sku_queries) == 1, f"one statement for the batch, got {len(sku_queries)}"
    assert set(sku_queries[0].values()) == {PK, PK + "-two"}, (
        "both keys must travel in the SAME statement, as bound values")


# ---------------------------------------------------------------------------------------------
# Round 2. Three spellings of one question became one; these hold it that way.
# ---------------------------------------------------------------------------------------------

def test_the_seed_stamp_path_obeys_the_contradiction_rule_too():
    """MUTANT: keep the name check inside `if live:`.

    THIS IS THE ROUND-2 P1. `sole_stamped_variant_id` reads `shopify_variant_id` while
    `_seed_offer_variant_id` reads `variant_id | variantId | sku | sku_id | id` — two different
    functions over the SAME variant dict — so one snapshot entry can carry two merchant-issued
    ids that disagree, with no data corruption at all. With the rule on the catalog path only,
    the disagreement refused one branch up was resolved in the stamp's favour, and the buyer got
    a cart for the variant they did not name.

    Nothing takes this path on today's corpus (0 of 11,834 seeds are stamped). It goes live the
    moment `scripts/backfill_shopify_variant_ids.py` runs at scale, which is the stated plan.
    """
    seed = _seed({"shopify_variant_id": VID, "variant_id": VID_B})
    got = choose_handover_variant([], seed_data=seed, offer_variant_id=VID_B)
    assert got.variant_id is None and got.reason == R_CONTRADICTED

    agreeing = choose_handover_variant([], seed_data=seed, offer_variant_id=VID)
    assert agreeing.variant_id == VID and agreeing.reason == R_SEED_STAMP

    unnamed = choose_handover_variant([], seed_data=seed)
    assert unnamed.variant_id == VID, "no name is not a disagreement"


def test_the_contradiction_predicate_is_parent_aware_like_the_admission_one():
    """MUTANT: `is_merchant_issued_variant_id(wanted)` with no parents.

    `variant_identity` checks DERIVATION BEFORE SHAPE, so the same string is `product_derived`
    with a parent and `merchant_issued` without one. Admission passed the parent; the
    contradiction guard did not — so a numeric product id restated as the snapshot variant's
    `sku` (the shape `ingestion.py` and `onboard_external_brand_from_crawl` both mint) refused
    a perfectly good candidate as "contradicted" by a name the module itself calls a forgery.
    """
    numeric_pk = "prod::m_brand::external_seed::80072940"
    cand = Candidate(sku_key=numeric_pk + "::v:" + VID, product_key=numeric_pk,
                     variant_id=VID, stamped=True, stored_variant_id=VID,
                     source_product_id="80072940")
    got = choose_handover_variant([cand], offer_variant_id="80072940")
    assert got.variant_id == VID and got.reason == R_SOLE, (
        "the name restates the product's own id, so it is not identity and cannot contradict")

    # ...and the SAME string, with no product to be relative to, IS identity — which is the
    # whole reason the two predicates could disagree.
    assert choose_handover_variant(
        [Candidate(sku_key="k", product_key="prod::m::x::handle", variant_id=VID,
                   stamped=True, stored_variant_id=VID, source_product_id="handle")],
        offer_variant_id="80072940",
    ).reason == R_CONTRADICTED


def test_one_predicate_answers_the_identity_question_everywhere():
    """The three call sites agree because there is one function, and it is parent-aware.

    Pinned as a property rather than through its callers, because the failure mode round 2
    found was two call sites disagreeing about ONE string, which no single-caller test can see.
    """
    from services.handover_variant_identity import names_a_merchant_issued_variant

    assert names_a_merchant_issued_variant(VID)
    assert names_a_merchant_issued_variant("gid://shopify/ProductVariant/" + VID), (
        "canonicalised before classification, so the two spellings cannot get two answers")
    assert not names_a_merchant_issued_variant("12345"), "5 digits is not a Shopify id"
    assert not names_a_merchant_issued_variant("SKU-30ML")
    assert not names_a_merchant_issued_variant(VID, product_key=VID), (
        "an id that restates the product it belongs to is a forgery, whatever its shape")
    assert not names_a_merchant_issued_variant("80072940", product_id="80072940")
    assert not names_a_merchant_issued_variant("")


def test_a_short_operator_sku_cannot_veto_a_real_catalog_id(monkeypatch, client):
    """MUTANT: `extract_shopify_numeric_variant_id(attached_variant_id)` as the veto predicate.

    That accepts ANY digit string, so a 5-digit operator SKU withdrew a cart the catalog row
    could have built — while a comment claimed the branch applied the resolver's rule. A value
    that is not identity cannot disagree with identity.
    """
    shopify_pk = "prod::m_brand::shopify::brand-serum"
    row = _seed_row(attached_product_key=shopify_pk)
    row["attached_variant_id"] = "12345"
    seen = _wire(
        monkeypatch, seed_row=row,
        sku_rows=[_row(product_key=shopify_pk, source_variant_id=CATALOG_VID, sku_payload=None)])
    assert _resolve(client).status_code == 200
    assert seen["cart_variant_id"] == CATALOG_VID


async def test_the_row_counters_count_rows():
    """MUTANT: `self.stats["handover_rows_scanned"] += 0` (and the same for rejected).

    Both survived round 2's mutation because the only test touching them fed
    `handover_coverage_fields` a hand-built dict — it pinned the formatter, not the counter.
    These two are the only production evidence that the stamp veto and the truncation guard
    ever fire, which is the whole justification for keeping them.
    """
    r = _FakeResolver({PK: [
        _row(sku_key="a"),
        _row(sku_key="b", source_variant_id=PK, sku_payload=None),
        _row(sku_key="c", sku_payload=json.dumps({"variant_id_provenance": "product_derived"})),
    ]})
    await r.prime([PK])
    assert r.stats["handover_rows_scanned"] == 3
    assert r.stats["handover_rows_rejected"] == 2
    f = handover_coverage_fields({**r.stats, "handover_considered": 1})
    assert f["handover_rows_scanned"] == 3 and f["handover_rows_rejected"] == 2
    assert f["handover_rows_stamp_vetoed"] == 1


async def test_a_configured_zero_timeout_is_not_an_unbounded_await():
    """MUTANT: `timeout=self._timeout_s or None`.

    `asyncio.wait_for(..., timeout=None)` waits forever, so the most natural spelling of "do
    not wait" removed the only wall-clock bound on a serving path — the inverse of what the
    operator asked for, silently.
    """
    import asyncio as _asyncio

    class _SlowResolver(HandoverVariantResolver):
        async def _fetch(self, keys):
            await _asyncio.sleep(5)
            return []

    r = _SlowResolver(timeout_s=0)
    await r.prime([PK])
    assert r.choose(product_key=PK).reason == R_LOOKUP_FAILED, (
        "a configured 0 must fall back to the default bound, never to no bound")


def test_the_cart_counter_does_not_count_a_cart_the_gate_withdrew(monkeypatch, client, caplog):
    """MUTANT: increment `cart_prefilled` at `_cart_vid`, before the gate runs.

    Its comment defines it as "how many hand-overs actually got a cart". Counted before the
    gate it also counts the ones this very request then degraded to a referral, so the number
    contradicts its own definition by exactly `degraded_to_referral` — and that is the number
    someone reads to decide whether arming `enforce` cost anything.
    """
    import logging

    import routes.agent_shop_gateway as gateway

    async def refusing(offer):
        return False

    _wire(
        monkeypatch,
        seed_row=_seed_row(snapshot_extra={"storefront_platform": "shopify"}),
        sku_rows=[_row(source_variant_id=CATALOG_VID, sku_payload=None)],
    )
    monkeypatch.setattr(gateway, "_preflight_allows_external_offer", refusing)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    with caplog.at_level(logging.INFO):
        assert _resolve(client).status_code == 200

    rec = next(r for r in caplog.records
               if r.getMessage().startswith("offers.resolve.summary"))
    assert rec.preflight_gated == 1
    assert rec.preflight_degraded_to_referral == 1
    assert rec.preflight_carts_built == 0, (
        "the gate withdrew the only cart on this request; counting it is a false claim")


def test_the_row_column_wins_over_a_stale_snapshot_copy_of_the_key(monkeypatch, client):
    """MUTANT: `seed_data.get(...) or row.get(...)` in `_handover_product_key`.

    `_external_seed_redirect_identity` parses merchant, platform and product from the ROW first.
    If the resolver preferred the snapshot, a row whose column holds key A while a stale
    snapshot holds key B would have merchant and product parsed for A and the variant looked up
    for B — B's variant id published as A's cart. The two reads have to agree, and nothing but
    a test holds them together.
    """
    other_pk = "prod::m_other::external_seed::other-serum"
    row = _seed_row(snapshot_extra={"storefront_platform": "shopify"})
    row["seed_data"]["attached_product_key"] = other_pk

    seen = _wire(
        monkeypatch, seed_row=row,
        sku_rows=[
            _row(source_variant_id=CATALOG_VID, sku_payload=None),
            _row(product_key=other_pk, sku_key="other", source_variant_id=OTHER_VID,
                 sku_payload=None),
        ])
    assert _resolve(client).status_code == 200
    assert seen["cart_variant_id"] == CATALOG_VID, (
        "the row's own column is the key the identity parse used, so it is the key we look up")


# ---------------------------------------------------------------------------------------------
# Round 3.
# ---------------------------------------------------------------------------------------------

def test_an_operator_cart_with_no_catalog_row_is_still_gated(monkeypatch, client):
    """MUTANT: gate on `_handover_id` alone instead of the union.

    THIS IS THE ROUND-3 P1, and it is a SWAP rather than a widening. The two values are not
    nested populations: an attach-lane seed with an operator-typed `attached_variant_id` and no
    merchant-issued `catalog_skus` row — 67.1% of the corpus has no such row — still builds a
    real Shopify cart permalink, and keying the gate on the resolved id alone stopped gating it.
    Under `enforce` the merchant saying "that variant is gone" no longer withdrew that cart,
    which is a regression against the merge base on the ONE cohort that ships prefilled carts
    today.
    """
    import routes.agent_shop_gateway as gateway

    asked = []

    async def counting_preflight(offer):
        asked.append(offer)
        return True

    shopify_pk = "prod::m_brand::shopify::brand-serum"
    row = _seed_row(attached_product_key=shopify_pk)
    row["attached_variant_id"] = CATALOG_VID

    seen = _wire(monkeypatch, seed_row=row, sku_rows=[])
    monkeypatch.setattr(gateway, "_preflight_allows_external_offer", counting_preflight)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    assert _resolve(client).status_code == 200

    assert seen["cart_variant_id"] == CATALOG_VID, "a real cart is built from the operator id"
    assert len(asked) == 1, "and the gate must be asked about it"
    assert asked[0]["execution_spec"]["variant_id"] == CATALOG_VID


def test_a_barcode_sku_may_match_a_variant_but_may_not_refuse_one(monkeypatch, client):
    """MUTANT: use the broad `_seed_offer_variant_id` chain for the contradiction check.

    `_seed_offer_variant_id` reads `variant_id | variantId | sku | sku_id | id`. On this corpus
    an 8+-digit `sku` is routinely an EAN-13 or UPC-12 barcode, which `variant_identity` calls
    merchant-issued by shape alone. `stamp_variant_ids` writes only `shopify_variant_id` and
    leaves `sku` untouched, so after `backfill_shopify_variant_ids.py` runs the two sit side by
    side in ONE snapshot entry — and the broad chain would have made the barcode veto the very
    stamp the backfill was run to produce.
    """
    seen = _wire(
        monkeypatch,
        seed_row=_seed_row(
            snapshot_extra={"storefront_platform": "shopify"},
            variants=[{"sku": "4901234567894", "title": "30ml", "price_amount": 19.0,
                       "price_currency": "USD", "availability": "in_stock"}],
        ),
        sku_rows=[_row(source_variant_id=CATALOG_VID, sku_payload=None)],
    )
    assert _resolve(client, sku_id="4901234567894").status_code == 200
    assert seen["cart_variant_id"] == CATALOG_VID, (
        "a barcode names no variant; it is an absence of information, not a disagreement")


def test_a_barcode_sku_on_the_seed_stamp_path_does_not_veto_the_stamp():
    """The same rule on the other path — the one that goes live when the storefront backfill
    runs, which is exactly when a stamp and a barcode start sharing one snapshot entry."""
    seed = _seed({"shopify_variant_id": VID, "sku": "4901234567894"})
    got = choose_handover_variant(
        [], seed_data=seed, offer_variant_id="4901234567894", named_variant_id="")
    assert got.variant_id == VID and got.reason == R_SEED_STAMP

    # ...and a value that DOES claim to be a variant id still contradicts.
    claimed = choose_handover_variant(
        [], seed_data=seed, offer_variant_id=VID_B, named_variant_id=VID_B)
    assert claimed.variant_id is None and claimed.reason == R_CONTRADICTED


def test_the_zero_candidate_path_knows_the_products_own_id_too():
    """MUTANT: pass `product_id=None` when there are no candidates.

    Round 3 found one string getting two verdicts depending on whether catalog happened to hold
    a row: `product_id` came from the candidate and from nowhere else, so the seed-stamp path
    called a restated `source_product_id` merchant-issued while the catalog path called it a
    forgery. Same product, same string, opposite answers.
    """
    seed = _seed({"shopify_variant_id": VID})
    got = choose_handover_variant(
        [], seed_data=seed, offer_variant_id="80072940", named_variant_id="80072940",
        product_key="prod::m::external_seed::80072940", product_id="80072940")
    assert got.variant_id == VID and got.reason == R_SEED_STAMP, (
        "the name restates the product's own id, so it is not identity and cannot contradict")


def test_the_predicate_canonicalises_before_it_classifies():
    """MUTANT: classify the raw string.

    `_RE_GID` accepts any gid, so a gid wrapping a FIVE-digit number reads as merchant-issued
    raw and is refused once canonicalised — and the canonical form is the value that actually
    ships, which `checkout_preflight` then classifies itself. Admitting the raw form produces a
    hand-over the preflight refuses as `no_merchant_issued_variant_id`: a refusal this resolver
    would have caused. Round 3 found this property asserted in a test MESSAGE and pinned by no
    assertion.
    """
    from services.handover_variant_identity import names_a_merchant_issued_variant

    short_gid = "gid://shopify/ProductVariant/12345"
    assert not names_a_merchant_issued_variant(short_gid), (
        "the id that ships is '12345', and five digits is not a Shopify variant id")
    assert candidate_from_row(_row(source_variant_id=short_gid, sku_payload=None)) is None

    derived_gid = "gid://shopify/ProductVariant/" + VID
    assert not names_a_merchant_issued_variant(derived_gid, product_id=VID), (
        "canonicalised, it restates the product's own id — raw, the gid prefix hides that")


def test_the_admitted_candidate_carries_the_products_own_id(monkeypatch):
    """MUTANT: `source_product_id=""` in `candidate_from_row`.

    The read side is pinned by the parent-awareness test, which hand-builds its Candidates — so
    the WRITE was unpinned, and the field's whole justification (`onboard_external_brand_from_crawl`
    restates `source_product_id` while `ingestion.py` restates `product_key`) rested on a line
    no test executed.
    """
    cand = candidate_from_row(_row(source_product_id="brand-serum-77"))
    assert cand is not None and cand.source_product_id == "brand-serum-77"

    # ...and it reaches the contradiction guard, which is the only thing it is for.
    restating = candidate_from_row(_row(source_product_id="80072940"))
    assert restating is not None
    assert choose_handover_variant(
        [restating], offer_variant_id="80072940").reason == R_SOLE


async def test_the_resolver_threads_the_products_own_id_into_the_decision():
    """MUTANT: drop `product_id=product_id` from `HandoverVariantResolver.choose`.

    The decision function's parent-awareness is pinned directly, and it survived a resolver
    that never passed the parent — which is the same "pinned the read, not the write" gap
    round 3 found on `Candidate.source_product_id`. The route reads
    `row_dict["external_product_id"]` and hands it here; nothing else can.
    """
    r = _FakeResolver({})
    await r.prime([PK])
    got = r.choose(
        product_key=PK,
        product_id="80072940",
        seed_data=_seed({"shopify_variant_id": VID}),
        offer_variant_id="80072940",
        named_variant_id="80072940",
    )
    assert got.variant_id == VID and got.reason == R_SEED_STAMP, (
        "the name restates the product's own id, so it cannot contradict the stamp")


async def test_an_operator_cart_from_a_non_identity_value_is_refused_without_asking_anyone():
    """The `or` half of `_gate_vid` can be a value this module would not call identity — the
    attach branch ships any digit string when catalog is silent. That must cost NO merchant
    request: `checkout_preflight.preflight` classifies the id before any egress and blocks.

    Pinned here rather than trusted, because the union was added in round 3 and the obvious
    worry about it is that it sends `_check_one` off to compare a 5-digit number against a
    storefront that will never match it.
    """
    from services import checkout_preflight, live_offer_verification

    asked = []

    async def never(*a, **kw):
        asked.append(kw)
        raise AssertionError("no merchant request may be made for a non-identity id")

    original = live_offer_verification._check_one
    live_offer_verification._check_one = never
    prior = os.environ.get("CHECKOUT_PREFLIGHT_MODE")
    os.environ["CHECKOUT_PREFLIGHT_MODE"] = "enforce"
    try:
        verdict = await checkout_preflight.preflight({
            "offer_id": "of:test:1",
            "product_key": PK,
            "source_product_id": SPID,
            "execution_spec": {"pdp_url": "https://brand.com/products/serum",
                               "variant_id": "12345"},
        })
    finally:
        live_offer_verification._check_one = original
        if prior is None:
            os.environ.pop("CHECKOUT_PREFLIGHT_MODE", None)
        else:
            os.environ["CHECKOUT_PREFLIGHT_MODE"] = prior

    assert asked == [], "the id was classified first; no merchant was contacted"
    assert verdict.outcome == checkout_preflight.BLOCK
    assert verdict.reason == checkout_preflight.R_NO_MERCHANT_VARIANT
    assert not verdict.allows_checkout


def test_the_attach_branch_is_given_the_bare_product_id_not_the_key_twice(monkeypatch, client):
    """MUTANT: `product_id=canonical_product_id` (which IS `attached_key`).

    `variant_identity` compares a variant id to its parent with `startswith`, so the full
    `prod::m::platform::<spid>` key never matches a bare `<spid>` — passing the key as BOTH
    parents is a pair that catches half of what it looks like it catches. Round 2 added
    `Candidate.source_product_id` to the resolver for exactly this reason; round 4 found the
    attach branch still doing it, so the one shared predicate gave one string two verdicts.

    Concretely: an operator pastes the PRODUCT id into the attach form. The resolver, given the
    bare id, calls it a restatement and resolves the catalog row; the attach branch, given the
    key twice, called it identity and withdrew the cart. Fail-closed, but it loses a hand-over
    we had resolved.
    """
    # NUMERIC, because that is the only shape where the two parent pairs disagree: a
    # non-numeric operator value is UNVERIFIABLE under both and never vetoes anything, so a
    # test built on one would pass under the mutant. The merchant's own numeric product id
    # pasted into the attach form is a real shape — `ingestion.py` restates exactly it.
    spid = "80072940"
    shopify_pk = f"prod::m_brand::shopify::{spid}"
    row = _seed_row(attached_product_key=shopify_pk)
    row["attached_variant_id"] = spid
    row["external_product_id"] = spid

    seen = _wire(
        monkeypatch, seed_row=row,
        sku_rows=[_row(product_key=shopify_pk, source_product_id=spid,
                       source_variant_id=CATALOG_VID, sku_payload=None)])
    assert _resolve(client).status_code == 200
    assert seen["cart_variant_id"] == CATALOG_VID, (
        "the operator value restates the product's own id, so it is not identity and may not "
        "veto the catalog row")


def test_the_preflight_memo_is_keyed_on_the_variant_as_well_as_the_page(monkeypatch, client):
    """MUTANT: `_q = (pdp_url,)`, dropping the variant.

    Pre-existing shape, but the union puts more distinct ids through this key than before: two
    candidates on one `canonical_url` naming different variants are two different questions, and
    collapsing them makes one merchant answer stand for both — under `enforce`, one variant's
    `gone` withdraws the other's working cart.
    """
    import routes.agent_shop_gateway as gateway

    asked = []

    async def counting_preflight(offer):
        asked.append(offer["execution_spec"]["variant_id"])
        return True

    _wire(
        monkeypatch,
        seed_row=_seed_row(
            snapshot_extra={"storefront_platform": "shopify"},
            variants=[
                {"variant_id": CATALOG_VID, "title": "30ml", "price_amount": 19.0,
                 "price_currency": "USD", "availability": "in_stock"},
                {"variant_id": OTHER_VID, "title": "50ml", "price_amount": 29.0,
                 "price_currency": "USD", "availability": "in_stock"},
            ],
        ),
        sku_rows=[
            _row(sku_key="a", source_variant_id=CATALOG_VID, sku_payload=None),
            _row(sku_key="b", source_variant_id=OTHER_VID, sku_payload=None),
        ],
    )
    monkeypatch.setattr(gateway, "_preflight_allows_external_offer", counting_preflight)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MAX_PER_REQUEST", "8")
    assert _resolve(client, product_id="sig_handover_memo").status_code == 200

    assert sorted(asked) == sorted([CATALOG_VID, OTHER_VID]), (
        "one page, two variants, two questions — the memo may not collapse them")


async def test_the_prefetch_lane_reads_the_product_id_its_own_payloads_carry(monkeypatch):
    """MUTANT: `product_id=candidate.get("external_product_id")` alone.

    `_build_prefetched_external_seed_wrappers` takes caller-supplied dicts, and this lane's own
    convention is a three-key chain — `_normalize_prefetched_external_seed_candidates` reads
    `product_id | id | external_product_id`, and the row the loop builds spells the same
    fallback. Reading only the one key handed the resolver `None` here while lanes 1, 3 and 4
    passed a real id, so one seed got two answers depending on which lane resolved it: the
    zero-candidate path admits a stamp restating the product id, because `product_key` alone
    cannot catch a restatement (`startswith` never matches a full key against a bare id).

    Driven through the real lane, because the value is decided in the wiring and nowhere else.
    """
    import routes.agent_shop_gateway as gateway

    seen = []
    original = gateway.HandoverVariantResolver.choose

    def recording_choose(self, **kwargs):
        seen.append(kwargs)
        return original(self, **kwargs)

    async def no_rows(query, values=None):
        return []

    monkeypatch.setattr(gateway.HandoverVariantResolver, "choose", recording_choose)
    monkeypatch.setattr(gateway.database, "fetch_all", no_rows)
    monkeypatch.setattr(
        gateway, "_make_external_redirect_url",
        lambda **kw: _completed("https://example.com/r?token=x"))

    await gateway._build_prefetched_external_seed_wrappers({
        "external_seed_candidates": [{
            # The canonical prefetch payload: `product_id`, no `external_product_id`.
            "product_id": "80072940",
            "external_seed_id": "eps_prefetch",
            "attached_product_key": PK,
            "destination_url": "https://brand.com/products/serum",
            "canonical_url": "https://brand.com/products/serum",
            "seed_data": {"snapshot": {"variants": [{"shopify_variant_id": VID}]}},
        }],
    })

    assert seen, "the lane never reached the resolver — this test would prove nothing"
    assert seen[0]["product_id"] == "80072940", (
        "the lane must read the product id its own payloads carry, not only the one key")


def _completed(value):
    import asyncio

    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut
