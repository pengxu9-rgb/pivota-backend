"""One vocabulary for availability, and the four call sites that must share it.

Context: four separate implementations of this mapping existed, and MEASURED 2026-08-25
they disagreed on 16 of 23 real spellings. The same defect class shipped in PIVOTA-Agent
(#2099): a predicate written against whichever producer its author happened to be reading,
so it could not fire on the other lane.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.availability_vocabulary import (
    IN_STOCK,
    OUT_OF_STOCK,
    is_in_stock,
    is_out_of_stock,
    normalize_availability,
)


# Every spelling the module classifies decisively. The call-site agreement test derives its
# corpus from this so the two can never drift apart — retyping a shorter list is exactly how
# the long tail stopped being covered.
_ALL_CLASSIFIED_SPELLINGS = [
    "out_of_stock", "out of stock", "Out of Stock", "out-of-stock", "outofstock", "OutOfStock",
    "sold_out", "sold out", "Sold Out", "soldout", "unavailable", "not available",
    "oos", "discontinued", "reserved",
    "https://schema.org/OutOfStock", "http://schema.org/OutOfStock",
    "https://schema.org/SoldOut", "https://schema.org/Discontinued", "https://schema.org/Reserved",
    "in_stock", "in stock", "In Stock", "in-stock", "instock", "InStock", "available",
    "limited availability", "limited stock",
    "https://schema.org/InStock", "http://schema.org/InStock",
    "https://schema.org/LimitedAvailability",
]


@pytest.mark.parametrize(
    "raw",
    [
        "out_of_stock", "out of stock", "Out of Stock", "OUT OF STOCK", "out-of-stock",
        "outofstock", "OutOfStock",
        "sold_out", "sold out", "Sold Out", "soldout", "SoldOut",
        "unavailable", "Unavailable", "not available", "notavailable",
        "oos", "OOS",
        "discontinued", "Discontinued",
        # JSON-LD carries the full IRI, and schema.org's own vocabulary includes SoldOut
        # and Discontinued — the reader that only matched OutOfStock called these unknown.
        "https://schema.org/OutOfStock", "http://schema.org/OutOfStock",
        "https://schema.org/SoldOut", "https://schema.org/Discontinued",
        # schema.org: "the item is reserved and therefore not available".
        "https://schema.org/Reserved", "reserved",
        False,  # the boolean counterpart of the Shopify `available` field
    ],
)
def test_every_out_of_stock_spelling_is_recognised(raw):
    assert normalize_availability(raw) == OUT_OF_STOCK
    assert is_out_of_stock(raw) is True
    assert is_in_stock(raw) is False


@pytest.mark.parametrize(
    "raw",
    [
        "in_stock", "in stock", "In Stock", "IN STOCK", "in-stock", "instock", "InStock",
        "available", "Available",
        "https://schema.org/InStock", "http://schema.org/InStock",
        # Stock remains, merely scarce — and BOTH spellings of that one concept must agree.
        "https://schema.org/LimitedAvailability", "limited availability", "limited stock",
        True,  # Shopify products.json carries a boolean `available`
    ],
)
def test_every_in_stock_spelling_is_recognised(raw):
    assert normalize_availability(raw) == IN_STOCK
    assert is_in_stock(raw) is True
    assert is_out_of_stock(raw) is False


@pytest.mark.parametrize(
    "raw",
    [
        None, "", "   ", "wibble", "???", 42, [], {},
        # Deliberately ambiguous: orderable-but-not-immediate, or channel-restricted.
        # Forcing these into a binary would be a guess.
        "backorder", "back order", "BackOrder", "https://schema.org/BackOrder",
        "preorder", "pre-order", "https://schema.org/PreOrder", "https://schema.org/PreSale",
        "onbackorder",  # WooCommerce's third stock_status value
        "https://schema.org/MadeToOrder",
        # CHANNEL statements carry no inventory information. Both sides of the axis are
        # unknown — treating one as in-stock while its mirror image was unknown gave one
        # axis two opposite treatments, and invented a positive from nothing.
        "https://schema.org/InStoreOnly", "in store only",
        "https://schema.org/OnlineOnly", "online only",
    ],
)
def test_unknown_is_never_a_false_negative(raw):
    # A wrong "out of stock" silently deletes a live, sellable product from a lane. Anything
    # we cannot classify must report as unknown and let the caller decide.
    assert normalize_availability(raw) is None
    assert is_out_of_stock(raw) is False
    assert is_in_stock(raw) is False


def test_every_call_site_agrees_on_every_spelling_the_module_classifies():
    """The regression that motivated this module: four readers, four different answers.

    The spelling list is DERIVED from the module's own parametrize corpora rather than
    retyped shorter — a curated list silently stopped covering the long tail, and three
    one-site regressions (hyphenated forms, http:// IRIs, "not available") survived it.

    Divergence on values the module does NOT classify is intentional and is pinned
    separately by test_the_readers_diverge_on_unrecognised_input_ON_PURPOSE.
    """
    from routes.employee_products import _normalize_seed_availability
    from services.external_offers_service import _availability_from_raw
    from services.external_seed_audit import normalize_seed_availability

    for raw in _ALL_CLASSIFIED_SPELLINGS:
        expected = normalize_availability(raw)
        assert expected is not None, f"corpus drift: {raw!r} no longer classifies"
        assert _normalize_seed_availability(raw) == expected, raw
        assert normalize_seed_availability(raw) == expected, raw
        assert _availability_from_raw(raw) == expected, raw

def test_no_previously_denied_value_stops_being_denied():
    """The swap must never make a value that WAS treated as out-of-stock read as in-stock.

    Named for what it actually checks. It deliberately does NOT claim full equivalence: the
    swap WIDENS the denylist, which is the point of the change — see the companion test
    below for the values that newly deny. readiness/scoring.py writes only canonical tokens,
    so nothing in the widened set can reach that call site today.
    """
    previous_denylist = {"out_of_stock", "outofstock", "sold_out", "soldout", "unavailable"}
    for value in previous_denylist:
        assert is_out_of_stock(value), value
    for value in ["in_stock", "", "unknown"]:
        assert not is_out_of_stock(value), value


def test_the_widened_denials_are_intended_new_behaviour():
    """Values the old denylist let through as IN STOCK that are now correctly denied."""
    for value in ["out of stock", "Out of Stock", "sold out", "oos", "not available",
                  "discontinued", "https://schema.org/SoldOut", "https://schema.org/Reserved"]:
        assert is_out_of_stock(value), value


def test_schema_org_sold_out_no_longer_reads_as_unknown():
    """The specific reachable defect: JSON-LD SoldOut resolved to 'unknown' before."""
    from services.external_offers_service import _availability_from_raw

    assert _availability_from_raw("https://schema.org/SoldOut") == OUT_OF_STOCK
    assert _availability_from_raw("https://schema.org/Discontinued") == OUT_OF_STOCK
    assert _availability_from_raw("sold out") == OUT_OF_STOCK
    assert _availability_from_raw("unavailable") == OUT_OF_STOCK
    # ...and an unreadable signal is still 'unknown', not a fabricated negative.
    assert _availability_from_raw("wibble") == "unknown"
    assert _availability_from_raw(None) == "unknown"


def test_unrecognised_values_still_pass_through_where_that_contract_exists():
    """employee_products/seed_audit callers read a passthrough as 'unknown'.

    Collapsing those to None here would instead make them read as AVAILABLE at
    routes/employee_products.py, so the passthrough is load-bearing, not cosmetic.
    """
    from routes.employee_products import _normalize_seed_availability
    from services.external_seed_audit import normalize_seed_availability

    assert _normalize_seed_availability("some vendor state") == "some_vendor_state"
    assert normalize_seed_availability("some vendor state") == "some_vendor_state"
    assert _normalize_seed_availability(None) is None
    assert _normalize_seed_availability("  ") is None


def test_ambiguous_tokens_stay_disjoint_from_the_decisive_sets():
    # MUTATION NOTE, so the next person does not chase two unkillable survivors:
    #   * removing a token from _AMBIGUOUS_TOKENS without adding it elsewhere is EQUIVALENT —
    #     an unclassified token already returns None. What is killable is MOVING one into a
    #     decisive set, and test_unknown_is_never_a_false_negative covers exactly that.
    #   * swapping _SCHEMA_ORG_IRI_RE.match for .search is EQUIVALENT because the pattern is
    #     anchored ^...$. Do not "fix" it by unanchoring the pattern.
    # Neither warrants a vacuous test row.
    """The ambiguity check is a PRECEDENCE guard, and only works while the sets are disjoint.

    Removing that check is an equivalent mutant today — the tokens appear nowhere else, so it
    has no observable effect and no test can kill it. What this pins instead is the invariant
    it depends on: if someone later adds "backorder" to the in-stock set, ambiguity must still
    win, and they should have to come here and say so deliberately.
    """
    from utils.availability_vocabulary import (
        _AMBIGUOUS_TOKENS,
        _IN_STOCK_TOKENS,
        _OUT_OF_STOCK_TOKENS,
    )

    assert not (_AMBIGUOUS_TOKENS & _IN_STOCK_TOKENS)
    assert not (_AMBIGUOUS_TOKENS & _OUT_OF_STOCK_TOKENS)
    # And the two decisive sets must never overlap each other either.
    assert not (_IN_STOCK_TOKENS & _OUT_OF_STOCK_TOKENS)


def test_a_self_contradictory_container_is_unknown_not_a_coin_flip():
    """JSON-LD allows a list, and the call sites stringify whatever they receive.

    Before this was handled, a value was classified by whichever IRI happened to be printed
    LAST in the repr, so ["OutOfStock", "InStock"] resolved to in_stock — a cart built on
    something explicitly marked out of stock.
    """
    assert normalize_availability(
        ["https://schema.org/OutOfStock", "https://schema.org/InStock"]
    ) is None
    assert normalize_availability(["in stock", "sold out"]) is None
    # Agreeing members still resolve.
    assert normalize_availability(["https://schema.org/OutOfStock"]) == OUT_OF_STOCK
    assert normalize_availability(["sold out", "out of stock"]) == OUT_OF_STOCK
    assert normalize_availability([]) is None


def test_a_jsonld_node_object_is_read_by_its_id():
    """JSON-LD writes an IRI as {"@id": ...}; the repr of that dict is not a signal."""
    assert normalize_availability({"@id": "https://schema.org/OutOfStock"}) == OUT_OF_STOCK
    assert normalize_availability({"@value": "in stock"}) == IN_STOCK
    # A key we do not treat as authoritative must not sway the verdict.
    assert normalize_availability(
        {"@id": "https://schema.org/OutOfStock", "sameAs": "https://schema.org/InStock"}
    ) == OUT_OF_STOCK
    assert normalize_availability({"unrelated": "in stock"}) is None


def test_only_a_WHOLE_schema_org_iri_is_unwrapped():
    """A loose substring split also fired on any string merely CONTAINING 'schema.org/'.

    The IN direction is what matters here and is exact-only, so a tracking URL can never
    fabricate availability. A URL that merely contains "OutOfStock" does still resolve to
    out_of_stock via the prose fallback — that is the safe direction (it declines to serve)
    and is the deliberate cost of catching prose like "Out Of Stock - notify me".
    """
    assert normalize_availability("https://example.com/?x=schema.org/InStock") is None
    assert normalize_availability("https://schema.org/InStock") == IN_STOCK
    assert normalize_availability("http://schema.org/InStock") == IN_STOCK
    assert normalize_availability("https://www.schema.org/SoldOut") == OUT_OF_STOCK


def test_channel_tokens_are_symmetric():
    """One axis must not get two opposite treatments."""
    assert normalize_availability("https://schema.org/OnlineOnly") is None
    assert normalize_availability("https://schema.org/InStoreOnly") is None


def test_both_spellings_of_limited_agree():
    """The split-by-spelling defect this module exists to remove, applied to itself."""
    assert normalize_availability("limited availability") == normalize_availability("limited stock")
    assert normalize_availability("limited_availability") == normalize_availability("limitedstock")


def test_a_phrase_never_fabricates_an_IN_STOCK_verdict():
    """The in-stock direction is exact-token only, so no phrase can invent availability.

    Negations are the reason: "not in stock" and "0 in stock" both contain "in stock".
    (The out-of-stock direction deliberately DOES match prose — see
    TestFreeTextOutOfStockIsNotLost for why the asymmetry is the safe way round.)
    """
    for phrase in ["back in stock", "0 in stock", "available soon", "only 2 left in stock",
                   "in stock soon", "not in stock", "out of stock soon"]:
        assert normalize_availability(phrase) != IN_STOCK, phrase


class TestFreeTextOutOfStockIsNotLost:
    """Availability arrives as human prose, and the two errors do NOT cost the same.

    Every serving gate in this repo is a DENYLIST on out_of_stock, so `unknown` is SERVABLE
    (see routes/employee_products.py "writing 'unknown' over a known out_of_stock serves it
    as purchasable"). Missing "Temporarily out of stock" therefore hands a shopper a dead
    product, while missing free-text in-stock costs nothing. Hence: generous matching for
    out-of-stock, strict exact-token matching for in-stock.

    Regression cover for a rewrite that swapped substring matching for exact-token matching
    and silently made every prose out-of-stock string servable again.
    """

    @pytest.mark.parametrize(
        "phrase",
        [
            "Temporarily out of stock", "This item is out of stock", "Out Of Stock - notify me",
            "Currently out of stock, back soon", "out-of-stock (restocking)", "SOLD OUT!",
            "sold out online", "temporarily unavailable", "unavailable online",
            "no longer available", "not in stock", "no longer in stock", "not available",
        ],
    )
    def test_prose_out_of_stock_is_caught(self, phrase):
        assert normalize_availability(phrase) == OUT_OF_STOCK

    @pytest.mark.parametrize(
        "phrase",
        ["back in stock", "0 in stock", "only 2 left in stock", "available soon",
         "currently available", "in stock soon"],
    )
    def test_in_stock_is_NEVER_inferred_from_prose(self, phrase):
        # A positive must never be fabricated from a phrase that might be negating it.
        # Unknown is already servable, so declining to guess costs nothing.
        assert normalize_availability(phrase) is None

    def test_a_phrase_saying_BOTH_things_is_unknown(self):
        # Unanchored substring matching reads this as out of stock; it is evidence of neither.
        assert normalize_availability("Sold out of the old model, but in stock now") is None

    @pytest.mark.parametrize("phrase", ["choose your size", "preserved formula", "oosphere"])
    def test_short_tokens_do_not_match_inside_unrelated_words(self, phrase):
        # "choose" contains "oos" and "preserved" contains "reserved" — which is why those two
        # stay exact-token-only and are not in the substring phrase list.
        assert normalize_availability(phrase) is None

    def test_no_prose_that_the_previous_substring_matcher_caught_is_lost(self):
        """Direct old-vs-new comparison in the dangerous direction, over a generated corpus."""
        import itertools

        def previous_implementation(raw):
            v = str(raw).lower()
            if "instock" in v or "in_stock" in v or "in stock" in v:
                return "in_stock"
            if "outofstock" in v or "out_of_stock" in v or "out of stock" in v:
                return "out_of_stock"
            return "unknown"

        prefixes = ["", "Temporarily ", "This item is ", "Currently ", "Sorry, "]
        cores = ["out of stock", "out-of-stock", "out_of_stock", "OUT OF STOCK", "sold out",
                 "unavailable", "in stock", "available"]
        suffixes = ["", " - notify me", ", back soon", " online", "!"]

        lost = [
            text
            for text in ("".join(parts) for parts in itertools.product(prefixes, cores, suffixes))
            if previous_implementation(text) == "out_of_stock"
            and normalize_availability(text) != OUT_OF_STOCK
        ]
        assert lost == [], f"these became servable again: {lost}"


class TestTheReadinessCallSitesThemselves:
    """Drive the REAL projection functions, not a re-implementation of their expression.

    The first version of this file asserted `not is_out_of_stock(value)` against a hand-copied
    denylist — which is the call site's expression retyped, so it could not detect the call
    site changing. Swapping `not is_out_of_stock(x)` for `is_in_stock(x)` at either readiness
    site survived every test, even though the two differ precisely for unknown/empty input:
    that swap is the exact flip the code comment promises will not happen ("Unknown stays IN
    STOCK here"). A test that simulates the caller leaves the caller's delivering line untested.
    """

    @staticmethod
    def _variant(availability, quantity):
        return SimpleNamespace(
            blockers={"discovery": [], "checkout": []},
            price={"amount": 10.0, "currency": "USD"},
            inventory={"quantity": quantity, "availability": availability},
        )

    @pytest.mark.parametrize("projection", ["summary", "remediation"])
    @pytest.mark.parametrize("availability", ["", "unknown", None, "in_stock"])
    def test_unknown_availability_with_stock_on_hand_is_NOT_flagged_out_of_stock(
        self, projection, availability
    ):
        from readiness.remediation import _variant_agent_push_projection
        from readiness.summary import _snapshot_variant_agent_push_projection

        fn = _snapshot_variant_agent_push_projection if projection == "summary" else _variant_agent_push_projection
        result = fn(self._variant(availability, 5))
        assert "out_of_stock" not in result["agent_push_reason_codes"], (projection, availability)

    @pytest.mark.parametrize("projection", ["summary", "remediation"])
    @pytest.mark.parametrize("availability", ["out_of_stock", "out of stock", "sold out", "unavailable"])
    def test_an_affirmative_out_of_stock_IS_flagged_even_with_stock_on_hand(
        self, projection, availability
    ):
        from readiness.remediation import _variant_agent_push_projection
        from readiness.summary import _snapshot_variant_agent_push_projection

        fn = _snapshot_variant_agent_push_projection if projection == "summary" else _variant_agent_push_projection
        result = fn(self._variant(availability, 5))
        assert "out_of_stock" in result["agent_push_reason_codes"], (projection, availability)

    @pytest.mark.parametrize("projection", ["summary", "remediation"])
    def test_zero_quantity_still_flags_regardless_of_the_availability_string(self, projection):
        # Pins the OTHER conjunct: readiness/scoring.py derives availability FROM quantity, so
        # production always correlates them and the availability predicate alone cannot be seen.
        from readiness.remediation import _variant_agent_push_projection
        from readiness.summary import _snapshot_variant_agent_push_projection

        fn = _snapshot_variant_agent_push_projection if projection == "summary" else _variant_agent_push_projection
        result = fn(self._variant("in_stock", 0))
        assert "out_of_stock" in result["agent_push_reason_codes"]


def test_the_canonical_tokens_are_the_exact_persisted_strings():
    """These values are PERSISTED and then read by literal comparison elsewhere.

    Every other assertion in this file compares against the constants themselves, which is
    self-referential: renaming IN_STOCK to "instock" would keep them all green while breaking
    services/offer_buyability.py, services/product_exposure_service.py and the
    Literal["unknown","in_stock","out_of_stock"] in mvp/schemas.py.
    """
    assert IN_STOCK == "in_stock"
    assert OUT_OF_STOCK == "out_of_stock"
    assert normalize_availability("sold out") == "out_of_stock"
    assert normalize_availability("in stock") == "in_stock"


def test_the_readers_diverge_on_unrecognised_input_ON_PURPOSE():
    """The agreement test above covers values that map to a canonical token.

    For everything else the three readers deliberately DIFFER — employee_products and
    seed_audit pass the value through (their callers read a passthrough as "unknown"), while
    offers_service answers the literal string "unknown". Pinning it here so a future
    "cleanup" that collapses the passthrough to None cannot land quietly: at
    routes/employee_products.py that would flip these rows from unknown to AVAILABLE.
    """
    from routes.employee_products import _normalize_seed_availability
    from services.external_offers_service import _availability_from_raw
    from services.external_seed_audit import normalize_seed_availability

    for raw in ["wibble", "backorder", "https://schema.org/InStoreOnly"]:
        assert _normalize_seed_availability(raw) not in (None, "in_stock", "out_of_stock"), raw
        assert normalize_seed_availability(raw) not in (None, "in_stock", "out_of_stock"), raw
        assert _availability_from_raw(raw) == "unknown", raw

    assert _normalize_seed_availability(None) is None
    assert normalize_seed_availability(None) is None
    assert _availability_from_raw(None) == "unknown"


def test_the_passthrough_normalises_case_and_separators_at_both_sites():
    from routes.employee_products import _normalize_seed_availability
    from services.external_seed_audit import normalize_seed_availability

    assert _normalize_seed_availability("Some Vendor State") == "some_vendor_state"
    assert normalize_seed_availability("Some Vendor State") == "some_vendor_state"
    assert normalize_seed_availability("   ") is None
    assert normalize_seed_availability("") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("in stock now", IN_STOCK),
        ("InStock2", None),          # digits survive canonicalisation, so this is not a token
        ("in stock (2)", None),
        ("none", None),              # str(None) would collapse here — must stay unknown
        ("null", None),
        ("Availability: In Stock", None),   # prose NEVER yields in-stock; see the class above
        ("http://schema.org/OutOfStock?x=1", OUT_OF_STOCK),  # not a whole IRI, caught as prose
    ],
)
def test_edge_tokens(raw, expected):
    assert normalize_availability(raw) == expected
