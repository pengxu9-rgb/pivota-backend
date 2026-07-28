"""Classification matrix for the ADR-018 connection-layer taxonomy.

These are SEMANTICS tests and run on any engine (they touch no database). The
dialect half — proving Postgres will PREPARE the SQL twin inside a real
serving-shaped statement — lives in ``tests/test_connection_layer_postgres.py``,
because this repo has twice shipped a statement Postgres refused while the
SQLite suite stayed green.
"""

from __future__ import annotations

import pytest

from services.connection_layer import (
    CONNECTION_LAYER_SLUGS,
    EXECUTION_ATTRIBUTED_REDIRECT,
    EXECUTION_DELEGATED_CHECKOUT,
    EXECUTION_PIVOTA_PSP_CHECKOUT,
    EXECUTION_WARM_HANDOFF,
    LAYER_CRAWLED,
    LAYER_SYNCED,
    LAYER_SYNCED_PSP,
    classify_connection_layer,
    connection_layer_field_enabled,
    connection_layer_slug,
    connection_layer_sql,
    resolve_execution_paths,
)


# ---- layer classification ---------------------------------------------------


def test_external_referral_is_layer_1():
    assert classify_connection_layer(catalog_track="external_referral") == LAYER_CRAWLED


def test_external_referral_stays_layer_1_even_for_a_fully_connected_merchant():
    """ADR-001: the ROW's provenance is the row's, not the merchant's.

    A crawled row does not become "synced" because its observed seller also
    happens to have a live store and a PSP — nothing re-fetched that row from an
    API, so its freshness guarantees are the crawl's.
    """
    assert (
        classify_connection_layer(
            catalog_track="external_referral",
            merchant_known=True,
            has_active_store=True,
            psp_connected=True,
            has_native_payments=True,
        )
        == LAYER_CRAWLED
    )


def test_unknown_merchant_is_layer_1_by_construction():
    """F3: crawled sellers are absent from merchant_onboarding entirely.

    ``merchant_known=False`` must land on layer 1 on its own — not by falling
    through to a ``COALESCE(psp_connected, false)`` that happens to be false.
    """
    assert (
        classify_connection_layer(
            catalog_track="internal_merchant",
            merchant_known=False,
            has_active_store=True,
            psp_connected=True,
        )
        == LAYER_CRAWLED
    )


def test_internal_merchant_with_active_store_is_layer_2():
    assert (
        classify_connection_layer(
            catalog_track="internal_merchant",
            merchant_known=True,
            has_active_store=True,
        )
        == LAYER_SYNCED
    )


def test_internal_merchant_without_active_store_falls_back_to_layer_1():
    """A sync whose store is disconnected is stale by definition."""
    assert (
        classify_connection_layer(
            catalog_track="internal_merchant",
            merchant_known=True,
            has_active_store=False,
            psp_connected=True,
        )
        == LAYER_CRAWLED
    )


def test_psp_connected_lifts_to_layer_3():
    assert (
        classify_connection_layer(
            catalog_track="internal_merchant",
            merchant_known=True,
            has_active_store=True,
            psp_connected=True,
        )
        == LAYER_SYNCED_PSP
    )


def test_native_payments_alone_does_NOT_lift_to_layer_3():
    """FOUNDER RULING 2026-07-28: "PSP integrated" means `psp_connected` — the
    Pivota merchant-portal flag — and nothing else.

    They are facts about DIFFERENT PARTIES, which is what made the old OR
    tempting: `psp_connected` means PIVOTA can orchestrate the charge;
    `has_shopify_payments` means the MERCHANT'S OWN checkout can settle. The
    three-layer model is about Pivota's connection depth, so only the former is
    a layer input. A verified merchant checkout, on its own, is layer 2.
    """
    assert (
        classify_connection_layer(
            catalog_track="internal_merchant",
            merchant_known=True,
            has_active_store=True,
            psp_connected=None,
            has_native_payments=True,
        )
        == LAYER_SYNCED
    )


def test_psp_connected_is_the_ONLY_arm_that_reaches_layer_3():
    """Mutation guard: removing the `psp_connected` leg must turn this red.

    A predicate whose only arm is untested is the shape that has cost this
    project repeatedly, so the positive case is pinned explicitly alongside the
    negative one above.
    """
    assert (
        classify_connection_layer(
            catalog_track="internal_merchant",
            merchant_known=True,
            has_active_store=True,
            psp_connected=True,
            has_native_payments=False,
        )
        == LAYER_SYNCED_PSP
    )


@pytest.mark.parametrize("unknown", [True, None, False, 0, "", "true", 1])
def test_only_a_strict_true_psp_fact_lifts_the_layer(unknown):
    """Unknown (None) and truthy-non-True are NOT yes; only ``True`` is.

    Mirrors ``get_platform_settlement_rails``' identity check: an unverified fact
    must never light a rail, and ``"true"``/``1`` are the shapes a JSON round-trip
    produces from a source that was never actually verified. (``1 is True`` is
    ``False`` in Python, so the ``1`` case genuinely exercises the negative arm —
    ``True`` is in the list so the positive arm is reachable at all.)
    """
    expected = LAYER_SYNCED_PSP if unknown is True else LAYER_SYNCED
    assert (
        classify_connection_layer(
            catalog_track="internal_merchant",
            merchant_known=True,
            has_active_store=True,
            psp_connected=unknown,
        )
        == expected
    )


@pytest.mark.parametrize("track", [None, "", "  ", "referral", "unknown_track"])
def test_unrecognised_track_falls_to_the_honest_floor(track):
    assert (
        classify_connection_layer(
            catalog_track=track,
            merchant_known=True,
            has_active_store=True,
            psp_connected=True,
        )
        == LAYER_CRAWLED
    )


def test_track_matching_is_case_and_whitespace_insensitive():
    assert (
        classify_connection_layer(catalog_track="  External_Referral ") == LAYER_CRAWLED
    )
    assert (
        classify_connection_layer(
            catalog_track=" INTERNAL_MERCHANT ",
            merchant_known=True,
            has_active_store=True,
        )
        == LAYER_SYNCED
    )


def test_slugs_cover_every_layer_and_unknown_falls_to_crawled():
    assert set(CONNECTION_LAYER_SLUGS) == {LAYER_CRAWLED, LAYER_SYNCED, LAYER_SYNCED_PSP}
    assert connection_layer_slug(LAYER_SYNCED_PSP) == "product_synced_psp"
    assert connection_layer_slug(99) == "crawled"
    assert connection_layer_slug(None) == "crawled"


# ---- execution paths (the orthogonal axis, F2) ------------------------------


def test_layer_1_allowlisted_brand_outranks_layer_2_that_is_not():
    """F2 stated as an executable assertion.

    If this ever fails because someone derived the path from the layer, the
    no-execution-layer-fallback rule has been broken: a redirect-only item would
    start advertising the better path.
    """
    layer1 = resolve_execution_paths(
        layer=LAYER_CRAWLED, has_destination_url=True, warm_handoff_eligible=True
    )
    layer2 = resolve_execution_paths(
        layer=LAYER_SYNCED, has_destination_url=True, warm_handoff_eligible=False
    )
    assert layer1[0] == EXECUTION_WARM_HANDOFF
    assert layer2 == [EXECUTION_ATTRIBUTED_REDIRECT]


def test_no_destination_url_means_no_redirect_floor():
    """Honest empty beats advertising a path that dead-ends."""
    assert resolve_execution_paths(layer=LAYER_CRAWLED, has_destination_url=False) == []


def test_warm_handoff_requires_a_destination():
    assert (
        resolve_execution_paths(
            layer=LAYER_CRAWLED, has_destination_url=False, warm_handoff_eligible=True
        )
        == []
    )


def test_dark_acp_doors_are_never_advertised():
    """A live PSP behind a closed door is not a path an agent can take."""
    paths = resolve_execution_paths(
        layer=LAYER_SYNCED_PSP,
        has_destination_url=True,
        pivota_psp_checkout_open=False,
    )
    assert EXECUTION_PIVOTA_PSP_CHECKOUT not in paths


def test_paths_are_ordered_best_first():
    paths = resolve_execution_paths(
        layer=LAYER_SYNCED_PSP,
        has_destination_url=True,
        warm_handoff_eligible=True,
        native_checkout_available=True,
        pivota_psp_checkout_open=True,
    )
    assert paths == [
        EXECUTION_PIVOTA_PSP_CHECKOUT,
        EXECUTION_WARM_HANDOFF,
        EXECUTION_DELEGATED_CHECKOUT,
        EXECUTION_ATTRIBUTED_REDIRECT,
    ]


def test_layer_alone_never_produces_a_path():
    """Every layer with no live facts yields nothing. The layer is a label."""
    for layer in (LAYER_CRAWLED, LAYER_SYNCED, LAYER_SYNCED_PSP):
        assert resolve_execution_paths(layer=layer) == []


# ---- outward-emission flag --------------------------------------------------


def test_field_emission_is_default_off():
    assert connection_layer_field_enabled({}) is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_field_emission_flag_accepts_the_usual_truthy_spellings(value):
    assert connection_layer_field_enabled({"CONNECTION_LAYER_FIELD_ENABLED": value}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_field_emission_flag_rejects_everything_else(value):
    assert connection_layer_field_enabled({"CONNECTION_LAYER_FIELD_ENABLED": value}) is False


# ---- SQL twin shape ---------------------------------------------------------


@pytest.mark.parametrize(
    "bad_alias",
    [
        "cp; DROP TABLE catalog_products; --",
        "cp'--",
        "cp)",
        "1cp",
        "",
        "   ",
        None,
        0,
    ],
)
def test_sql_alias_is_identifier_validated(bad_alias):
    """The alias is the only caller-supplied token in the expression.

    Asserting the absence of ``DROP TABLE`` alone would pass for ANY fallback,
    so this asserts the emitted alias is exactly the default.
    """
    sql = connection_layer_sql(bad_alias)
    assert "DROP TABLE" not in sql
    assert "cp.catalog_track" in sql
    assert "cp.merchant_id" in sql


@pytest.mark.parametrize("reserved", ["ms_cl", "mo_cl", "mo_psp", "MS_CL"])
def test_reserved_internal_aliases_cannot_shadow_the_subquery_scope(reserved):
    """A caller alias equal to an internal one decorrelates the subquery.

    ``connection_layer_sql('ms_cl')`` would emit
    ``WHERE ms_cl.merchant_id = ms_cl.merchant_id`` — always true, so the store
    leg silently becomes "does ANY live store exist anywhere" and the expression
    returns a wrong layer with no error and no injection. Measured before the
    guard existed.
    """
    # BOTH alias forms — the joined variant emits the ms_cl subquery too, so
    # checking only the correlated form left half the surface unasserted.
    for sql in (connection_layer_sql(reserved),
                connection_layer_sql(reserved, onboarding_alias="mo")):
        assert "ms_cl.merchant_id = ms_cl.merchant_id" not in sql
    assert "mo_cl.merchant_id = mo_cl.merchant_id" not in sql
    assert "mo_psp.merchant_id = mo_psp.merchant_id" not in sql
    assert "pmc_cl.merchant_id = pmc_cl.merchant_id" not in sql
    assert "cp.catalog_track" in sql


def test_sql_trims_the_track_exactly_as_python_does():
    """The Python twin normalises with ``.strip()``. Without ``btrim`` in the
    SQL, ``' internal_merchant '`` is layer 2 in Python and layer 1 in SQL —
    executed against real Postgres before this assertion existed.

    The character set matters as much as the call: **single-argument
    ``btrim(x)`` strips SPACES ONLY**, so a tab/newline-padded value still
    diverged. Asserting on the second argument rather than on an exact
    expression string keeps this from being reformat-brittle.
    """
    sql = connection_layer_sql("cp")
    assert sql.count("btrim(COALESCE(cp.catalog_track, '')") == 2
    for whitespace in ("\\t", "\\n", "\\r", "\\f", "\\v"):
        assert whitespace in sql, whitespace
    assert sql.count("E' ") >= 2  # track + store status


def test_sql_accepts_every_live_store_status():
    """A narrower set here than ``merchant_store_service``'s canonical
    ``status IN ('active','connected')`` makes the twins disagree."""
    sql = connection_layer_sql("cp")
    assert "'active'" in sql
    assert "'connected'" in sql


def test_slug_never_raises_on_a_stray_value():
    """This feeds a protocol payload; a ValueError would take the response with it."""
    assert connection_layer_slug("abc") == "crawled"
    assert connection_layer_slug([]) == "crawled"
    assert connection_layer_slug(None) == "crawled"


@pytest.mark.parametrize("not_a_layer", [2.7, 3.0, "2", True, False])
def test_slug_does_not_round_a_non_integer_into_a_layer(not_a_layer):
    """``int(2.7)`` is 2, so a truncating conversion would answer
    "product_synced" for a value that is not a layer. This function's job is
    expressing the layer honestly — a non-integral input gets the floor, not a
    rounded lie. ``True == 1`` is excluded for the same reason."""
    assert connection_layer_slug(not_a_layer) == "crawled"


def test_sql_no_longer_references_the_capabilities_table():
    """Founder ruling: has_shopify_payments is not a layer input, so the SQL twin
    must not join pcs_merchant_capabilities. Dropping it also removes a
    correlated subquery from the hot path."""
    sql = connection_layer_sql("cp")
    assert "pcs_merchant_capabilities" not in sql
    assert "has_shopify_payments" not in sql
    assert "psp_connected" in sql, "the one remaining layer-3 arm must still be there"


def test_sql_states_the_missing_merchant_leg_explicitly():
    """F3 must be visible in the SQL, not left to COALESCE."""
    sql = connection_layer_sql("cp")
    assert "NOT EXISTS" in sql
    assert "merchant_onboarding" in sql


def test_sql_joined_form_uses_the_supplied_onboarding_alias():
    sql = connection_layer_sql("cp", onboarding_alias="mo")
    assert "mo.merchant_id IS NULL" in sql
    assert "mo.psp_connected" in sql


def test_sql_is_a_single_parenthesised_expression():
    """It is dropped into SELECT lists and ORDER BY; it must not need wrapping.

    A compiled fragment that still needs parens is exactly how #1593 produced
    ``syntax error at or near "AS"`` with 142 SQLite tests green.
    """
    sql = connection_layer_sql("cp").strip()
    assert sql.startswith("(CASE")
    assert sql.endswith("END)")


def test_sql_emits_no_bind_parameters():
    """Every literal is a taxonomy constant; an untyped bind is what broke #1588."""
    assert ":" not in connection_layer_sql("cp")
    assert "%s" not in connection_layer_sql("cp")
    assert "$" not in connection_layer_sql("cp")
