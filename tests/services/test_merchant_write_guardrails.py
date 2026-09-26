"""The pure guardrail logic in services/merchant_write_guardrails.py.

Every refusal here has a should-apply twin in the same test, so a rule that starts
refusing everything fails just as loudly as one that stops refusing anything.
"""

from __future__ import annotations

import pytest

from services.merchant_write_guardrails import (
    ACTOR_HUMAN,
    ACTOR_MODEL,
    ACTOR_SYSTEM,
    CONTENT_KINDS,
    DEFAULT_CONFIG,
    KIND_INVENTORY_ACTION,
    KIND_PDP_MODULE_CONTENT,
    KIND_PRICE_UPDATE,
    KIND_PRODUCT_ENRICHMENT,
    KIND_STORE_CONTENT_WRITEBACK,
    STORE_WRITEBACK_COPY_FIELDS,
    WRITE_KINDS,
    GuardrailViolation,
    MerchantWriteGuardrailConfig,
    WriteItem,
    check_guardrails,
    check_host_approval,
    current_config,
    guardrail_block_message,
    items_from_payload,
    requires_host_approval,
)


def _items(n: int, target: str = "pdp-1:copy") -> list:
    return [WriteItem(target=target, field=f"f{i}", after="x") for i in range(n)]


# ---------------------------------------------------------------- items-per-change


def test_items_cap_refuses_over_the_limit_and_allows_a_change_at_the_limit():
    cfg = MerchantWriteGuardrailConfig(max_items_per_change=3)

    at_limit = check_guardrails(KIND_PDP_MODULE_CONTENT, _items(3), cfg)
    assert at_limit == [], at_limit

    over = check_guardrails(KIND_PDP_MODULE_CONTENT, _items(4), cfg)
    assert len(over) == 1
    assert "touches 4 fields" in over[0]
    assert "limit is 3" in over[0]


# ------------------------------------------------------- one line per target+field


def test_a_repeated_target_and_field_is_refused_but_two_fields_on_one_target_are_not():
    # The positive twin matters here: every cap below is per item, so if this rule
    # over-fired on distinct fields it would refuse ordinary multi-field copy edits.
    two_fields = [
        WriteItem(target="pdp-1:copy", field="title", after="a"),
        WriteItem(target="pdp-1:copy", field="description", after="b"),
    ]
    assert check_guardrails(KIND_PDP_MODULE_CONTENT, two_fields, DEFAULT_CONFIG) == []

    repeated = [
        WriteItem(target="pdp-1:copy", field="title", after="a"),
        WriteItem(target="pdp-1:copy", field="title", after="b"),
    ]
    out = check_guardrails(KIND_PDP_MODULE_CONTENT, repeated, DEFAULT_CONFIG)
    assert len(out) == 1
    assert "appears more than once" in out[0]


def test_the_repeat_rule_is_case_insensitive_on_the_field_name():
    repeated = [
        WriteItem(target="pdp-1:copy", field="Title", after="a"),
        WriteItem(target="pdp-1:copy", field="title", after="b"),
    ]
    out = check_guardrails(KIND_PDP_MODULE_CONTENT, repeated, DEFAULT_CONFIG)
    assert any("appears more than once" in m for m in out)

    # Twin: the same two names on DIFFERENT targets are two legitimate lines.
    distinct = [
        WriteItem(target="pdp-1:copy", field="Title", after="a"),
        WriteItem(target="pdp-2:copy", field="title", after="b"),
    ]
    assert check_guardrails(KIND_PDP_MODULE_CONTENT, distinct, DEFAULT_CONFIG) == []


# --------------------------------------------------------------- protected fields


@pytest.mark.parametrize(
    "protected_field",
    ["product_key", "merchant_id", "sku", "canonical_url", "currency", "body_html"],
)
def test_a_protected_field_is_refused_on_every_kind(protected_field):
    for kind in WRITE_KINDS:
        out = check_guardrails(
            kind, [WriteItem(target="t", field=protected_field, after="x")], DEFAULT_CONFIG
        )
        assert any("is protected" in m for m in out), (kind, protected_field, out)


def test_a_protected_field_is_matched_case_insensitively_and_an_ordinary_field_is_not():
    out = check_guardrails(
        KIND_PDP_MODULE_CONTENT,
        [WriteItem(target="t", field="Merchant_ID", after="other-merchant")],
        DEFAULT_CONFIG,
    )
    assert any("is protected" in m for m in out)

    # Twin: the copy fields these lanes really write are not protected.
    ok = check_guardrails(
        KIND_PDP_MODULE_CONTENT,
        [
            WriteItem(target="t", field="title", after="A tee"),
            WriteItem(target="t", field="description", after="Soft."),
            WriteItem(target="t", field="summary", after="Soft tee."),
        ],
        DEFAULT_CONFIG,
    )
    assert ok == []


# ------------------------------------------- price or stock riding in a copy edit


@pytest.mark.parametrize("kind", sorted(CONTENT_KINDS))
@pytest.mark.parametrize(
    "field_name", ["price", "compare_at_price", "list_price", "inventory_quantity", "stock"]
)
def test_a_content_update_may_not_carry_a_price_or_stock_field(kind, field_name):
    out = check_guardrails(
        kind, [WriteItem(target="t", field=field_name, before=10, after=1)], DEFAULT_CONFIG
    )
    assert any("cannot be changed through a content update" in m for m in out), out


def test_the_same_price_field_under_a_price_update_is_not_refused_by_the_content_rule():
    # The should-apply twin: the rule redirects a price move to its own kind, so under
    # that kind a compliant move passes. If it refused here too, the message it prints
    # ("stage it as a price update") would be a lie.
    out = check_guardrails(
        KIND_PRICE_UPDATE,
        [WriteItem(target="t", field="price", before=10.0, after=10.5)],
        DEFAULT_CONFIG,
    )
    assert out == []


# ------------------------------------------------------------- field size ceiling


def test_field_size_ceiling_refuses_a_runaway_value_and_allows_one_at_the_limit():
    cfg = MerchantWriteGuardrailConfig(max_field_chars=100)

    at_limit = check_guardrails(
        KIND_STORE_CONTENT_WRITEBACK,
        [WriteItem(target="t", field="description", after="x" * 100)],
        cfg,
    )
    assert at_limit == []

    over = check_guardrails(
        KIND_STORE_CONTENT_WRITEBACK,
        [WriteItem(target="t", field="description", after="x" * 101)],
        cfg,
    )
    assert len(over) == 1
    assert "101 characters" in over[0]


def test_a_non_string_value_is_measured_as_the_json_that_would_be_persisted():
    cfg = MerchantWriteGuardrailConfig(max_field_chars=20)
    out = check_guardrails(
        KIND_STORE_CONTENT_WRITEBACK,
        [WriteItem(target="t", field="bullets", after=["aaaaaaaaaa", "bbbbbbbbbb"])],
        cfg,
    )
    assert any("characters and the limit is 20" in m for m in out), out

    # Twin: a short list is not refused.
    assert (
        check_guardrails(
            KIND_STORE_CONTENT_WRITEBACK,
            [WriteItem(target="t", field="bullets", after=["a"])],
            cfg,
        )
        == []
    )


# ------------------------------------------------------ price movement (dormant)


def test_price_movement_cap_refuses_a_big_move_and_allows_one_inside_the_cap():
    cfg = MerchantWriteGuardrailConfig(max_price_delta_pct=10.0)

    inside = check_guardrails(
        KIND_PRICE_UPDATE,
        [WriteItem(target="t", field="price", before=100.0, after=109.0)],
        cfg,
    )
    assert inside == []

    outside = check_guardrails(
        KIND_PRICE_UPDATE,
        [WriteItem(target="t", field="price", before=100.0, after=130.0)],
        cfg,
    )
    assert len(outside) == 1
    assert "exceeds the 10% per-change limit" in outside[0]


def test_a_price_move_without_a_grounded_before_is_refused_rather_than_assumed():
    out = check_guardrails(
        KIND_PRICE_UPDATE,
        [WriteItem(target="t", field="price", before=None, after=50.0)],
        DEFAULT_CONFIG,
    )
    assert any("no grounded current price" in m for m in out), out


def test_a_non_positive_price_is_refused():
    out = check_guardrails(
        KIND_PRICE_UPDATE,
        [WriteItem(target="t", field="price", before=10.0, after=0)],
        DEFAULT_CONFIG,
    )
    assert any("must be a positive amount" in m for m in out), out


# --------------------------------------------------------- restock cap (dormant)


def test_restock_cap_refuses_a_big_raise_and_allows_one_at_the_cap():
    cfg = MerchantWriteGuardrailConfig(max_restock_quantity=50)

    at_cap = check_guardrails(
        KIND_INVENTORY_ACTION,
        [WriteItem(target="t", field="on_hand", before=10, after=60)],
        cfg,
    )
    assert at_cap == []

    over = check_guardrails(
        KIND_INVENTORY_ACTION,
        [WriteItem(target="t", field="on_hand", before=10, after=61)],
        cfg,
    )
    assert len(over) == 1
    assert "restock of 51 units" in over[0]


def test_lowering_stock_is_not_a_restock():
    out = check_guardrails(
        KIND_INVENTORY_ACTION,
        [WriteItem(target="t", field="on_hand", before=500, after=1)],
        MerchantWriteGuardrailConfig(max_restock_quantity=5),
    )
    assert out == []


# ------------------------------------------------------------------ host approval


def test_a_model_can_never_satisfy_host_approval_but_a_human_can():
    cfg = MerchantWriteGuardrailConfig(require_host_approval_store_writeback=True)

    refused = check_host_approval(
        KIND_STORE_CONTENT_WRITEBACK, actor_kind=ACTOR_MODEL, config=cfg
    )
    assert len(refused) == 1
    assert "a model verdict does not approve a merchant write" in refused[0]

    assert check_host_approval(KIND_STORE_CONTENT_WRITEBACK, actor_kind=ACTOR_HUMAN, config=cfg) == []
    assert check_host_approval(KIND_STORE_CONTENT_WRITEBACK, actor_kind=ACTOR_SYSTEM, config=cfg) == []


def test_host_approval_is_off_for_a_lane_whose_switch_is_off():
    cfg = MerchantWriteGuardrailConfig(require_host_approval_pdp_module_publish=False)
    assert check_host_approval(KIND_PDP_MODULE_CONTENT, actor_kind=ACTOR_MODEL, config=cfg) == []

    # Twin: the same model actor IS refused once an operator flips the switch on.
    on = MerchantWriteGuardrailConfig(require_host_approval_pdp_module_publish=True)
    assert check_host_approval(KIND_PDP_MODULE_CONTENT, actor_kind=ACTOR_MODEL, config=on) != []


def test_the_shipped_defaults_are_the_documented_ones():
    # The store-writeback lane reaches the merchant's LIVE store, so it defaults to
    # requiring host approval. The overlay publish lane defaults OFF because that is
    # today's production behaviour (the designed machine-publish lane); changing this
    # default is a deliberate product decision, not a refactor.
    assert requires_host_approval(KIND_STORE_CONTENT_WRITEBACK, DEFAULT_CONFIG) is True
    assert requires_host_approval(KIND_PDP_MODULE_CONTENT, DEFAULT_CONFIG) is False
    assert requires_host_approval(KIND_PRODUCT_ENRICHMENT, DEFAULT_CONFIG) is False


# ------------------------------------------------- config in force AT APPLY TIME


def test_current_config_re_reads_the_environment_on_every_call(monkeypatch):
    # The blueprint's contract: apply re-checks against the config in force at apply
    # time. A config cached at import would make a tightened limit take a deploy.
    before = current_config().max_items_per_change
    monkeypatch.setenv("MERCHANT_WRITE_MAX_ITEMS", "2")
    assert current_config().max_items_per_change == 2
    monkeypatch.delenv("MERCHANT_WRITE_MAX_ITEMS")
    assert current_config().max_items_per_change == before


def test_a_malformed_or_non_positive_env_override_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("MERCHANT_WRITE_MAX_ITEMS", "not-a-number")
    assert current_config().max_items_per_change == DEFAULT_CONFIG.max_items_per_change
    monkeypatch.setenv("MERCHANT_WRITE_MAX_ITEMS", "0")
    assert current_config().max_items_per_change == DEFAULT_CONFIG.max_items_per_change


def test_the_host_approval_switches_can_be_flipped_by_environment(monkeypatch):
    monkeypatch.setenv("MERCHANT_WRITE_REQUIRE_HOST_APPROVAL_PDP_PUBLISH", "true")
    assert current_config().require_host_approval_pdp_module_publish is True
    monkeypatch.setenv("MERCHANT_WRITE_REQUIRE_HOST_APPROVAL_STORE_WRITEBACK", "false")
    assert current_config().require_host_approval_store_writeback is False


# ------------------------------------------------------------------ items builder


def test_items_from_payload_makes_one_line_per_top_level_key_and_carries_the_before():
    items = items_from_payload(
        "t", {"title": "new", "description": "d"}, before={"title": "old"}
    )
    assert {(i.field, i.before, i.after) for i in items} == {
        ("title", "old", "new"),
        ("description", None, "d"),
    }


def test_items_from_payload_ignores_a_non_mapping_payload():
    assert items_from_payload("t", None) == []
    assert items_from_payload("t", ["not", "a", "mapping"]) == []


def test_items_from_payload_does_not_descend_into_nested_values():
    # Deliberate: the unit an operator approves is a top-level field. A nested
    # "variant_id" inside a gallery image dict is data, not a field being changed, and
    # treating it as one would refuse every legitimate gallery upload.
    items = items_from_payload("t", {"images": [{"variant_id": "v1"}]})
    assert [i.field for i in items] == ["images"]
    assert check_guardrails(KIND_PDP_MODULE_CONTENT, items, DEFAULT_CONFIG) == []


# ------------------------------------------------------------------- the exception


def test_guardrail_violation_keeps_every_message_and_reads_as_one_line():
    exc = GuardrailViolation(["a is protected", "b is too long"])
    assert exc.violations == ["a is protected", "b is too long"]
    assert "a is protected" in str(exc) and "b is too long" in str(exc)
    assert guardrail_block_message(exc.violations).startswith("Refused by the merchant write guardrails:")


# --------------------------------------------------------- allowlist completeness


def test_store_writeback_copy_fields_still_names_exactly_what_the_writeback_builds():
    """Completeness claim for STORE_WRITEBACK_COPY_FIELDS.

    The guardrail lane bounds the blob `_build_metafield_value` produces. If someone
    widens that blob — a new copy key, or a native Shopify field — this test goes red
    rather than the new key riding to a merchant's live store unbounded.
    """
    from services.shopify_content_writeback import _build_metafield_value

    built = _build_metafield_value(
        {
            "title_override": "T",
            "summary_short": "S",
            "description_markdown": "D",
            "bullet_points": ["b"],
            "usage_scenarios": ["u"],
        }
    )
    assert built is not None
    assert set(built.keys()) == set(STORE_WRITEBACK_COPY_FIELDS)


def test_no_copy_field_the_writeback_lane_writes_is_itself_protected_or_blocked():
    """The other half of the completeness claim: the guardrail must not refuse the
    lane's own legitimate fields. A name added to protected_fields that collides with
    a copy key would make every store publish fail closed, silently."""
    protected = {n.casefold() for n in DEFAULT_CONFIG.protected_fields}
    blocked = {n.casefold() for n in DEFAULT_CONFIG.content_update_blocked_fields}
    for name in STORE_WRITEBACK_COPY_FIELDS:
        assert name.casefold() not in protected, name
        assert name.casefold() not in blocked, name


def test_every_price_bearing_field_is_also_blocked_from_a_content_update():
    # A price-bearing name that a content edit may still carry would be a hole: the
    # movement cap only runs under KIND_PRICE_UPDATE.
    blocked = {n.casefold() for n in DEFAULT_CONFIG.content_update_blocked_fields}
    for name in DEFAULT_CONFIG.price_bearing_fields:
        assert name.casefold() in blocked, name


def test_content_kinds_names_only_kinds_that_exist():
    assert CONTENT_KINDS <= set(WRITE_KINDS)
