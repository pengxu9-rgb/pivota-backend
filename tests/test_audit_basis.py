"""A3 — the run-level audit basis: what a run was measured WITH.

Two properties carry the whole feature, and each has a way of failing that a
happy-path test would not see:

  IMMUTABILITY — a basis that can be rewritten is not a basis. It would let a
  later deploy retroactively make two runs look comparable, which is exactly
  the claim the table exists to prevent.

  CONSERVATIVE COMPARABILITY — `bases_are_comparable` may only answer True on a
  positive match of every component. A wrong True tells a merchant that a model
  swap was their own movement; a wrong False only says "we changed how we
  measure". So every False assertion below is paired with the True that proves
  the function is not simply refusing everything.

No hand-written fixture DDL: the table is built through the accessor's own
`ensure_*` backstop — the same statements migration 208 applies — so the UNIQUE
constraint under test is production's.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import db.audit_basis as mod
from db.database import database

RUN = "run_a3_basis"
OTHER_RUN = "run_a3_basis_other"
MERCHANT = "merch_a3"


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    mod.reset_ddl_ready_for_tests()
    await mod.ensure_audit_basis_table()
    await database.execute(
        "DELETE FROM audit_basis WHERE merchant_id = :m", {"m": MERCHANT}
    )
    yield


def _payload(**overrides):
    base = dict(
        audit_run_id=RUN,
        merchant_id=MERCHANT,
        providers_and_models={
            "gemini": {"model_id": "gemini-2.5-flash", "temperature": None},
        },
        prompt_set_id="ps_aaaa",
        selected_set_id="sel_bbbb",
        tier_mix={"category_head": 8, "navigational": 3},
        official_domains=["Anua.com", "anua.us"],
        primary_destination_version=1,
        market="US",
        language="en",
        currency=None,
    )
    base.update(overrides)
    return base


# =====================================================================
# What a run was measured with — recorded, and read back whole
# =====================================================================


async def test_record_then_get_round_trips_every_component():
    written = await mod.record_basis(**_payload())
    assert written is not None
    read = await mod.get_basis_for_run(RUN)
    assert read is not None

    assert read["merchant_id"] == MERCHANT
    assert read["methodology_version"] == mod.METHODOLOGY_VERSION
    assert read["providers_and_models"] == {
        "gemini": {"model_id": "gemini-2.5-flash", "temperature": None}
    }
    assert read["prompt_set_id"] == "ps_aaaa"
    assert read["selected_set_id"] == "sel_bbbb"
    assert read["tier_mix"] == {"category_head": 8, "navigational": 3}
    # Normalized to lowercase + sorted on the way in: the set is compared
    # between runs, so "Anua.com" and "anua.com" must not read as a change.
    assert read["official_domains"] == ["anua.com", "anua.us"]
    assert read["primary_destination_version"] == 1
    assert read["market"] == "US"
    assert read["language"] == "en"
    assert read["currency"] is None


async def test_get_basis_for_an_unrecorded_run_is_none():
    """Paired with its positive counterpart in the same test so a
    get_basis_for_run that always returned None cannot pass."""
    assert await mod.get_basis_for_run(OTHER_RUN) is None
    await mod.record_basis(**_payload(audit_run_id=OTHER_RUN))
    assert await mod.get_basis_for_run(OTHER_RUN) is not None


async def test_a_run_id_or_merchant_id_that_is_empty_records_nothing():
    assert await mod.record_basis(**_payload(audit_run_id="")) is None
    assert await mod.record_basis(**_payload(merchant_id="")) is None
    # Positive counterpart: the same payload with both present DOES record.
    assert await mod.record_basis(**_payload()) is not None


# =====================================================================
# Immutability
# =====================================================================


async def test_a_second_record_basis_is_a_no_op_returning_the_stored_row():
    first = await mod.record_basis(**_payload())
    assert first is not None

    second = await mod.record_basis(**_payload(
        prompt_set_id="ps_DIFFERENT",
        providers_and_models={"gemini": {"model_id": "swapped", "temperature": 0.7}},
        primary_destination_version=99,
    ))
    assert second is not None
    # The STORED row comes back, not the payload just passed.
    assert second["prompt_set_id"] == "ps_aaaa"
    assert second["providers_and_models"] == {
        "gemini": {"model_id": "gemini-2.5-flash", "temperature": None}
    }
    assert second["primary_destination_version"] == 1
    assert second["basis_id"] == first["basis_id"]

    rows = await database.fetch_all(
        "SELECT basis_id FROM audit_basis WHERE audit_run_id = :r", {"r": RUN}
    )
    assert len(rows) == 1


async def test_the_unique_constraint_and_not_only_the_read_check_enforces_one_basis():
    """The read-then-insert in record_basis is the fast path; the database is
    the guarantee. Two callers that both read "absent" must not both write —
    proved by inserting DIRECTLY, bypassing the accessor's check entirely."""
    await mod.record_basis(**_payload())
    with pytest.raises(Exception):
        await database.execute(
            mod.INSERT_BASIS_SQL.replace("ON CONFLICT (audit_run_id) DO NOTHING", ""),
            {
                "basis_id": "second_id", "audit_run_id": RUN,
                "merchant_id": MERCHANT, "methodology_version": "1",
                "providers_and_models": "{}", "prompt_set_id": None,
                "selected_set_id": None, "tier_mix": "{}",
                "official_domains": "[]", "primary_destination_version": None,
                "market": None, "language": None, "currency": None,
                "created_at": datetime.now(timezone.utc),
            },
        )
    # Positive counterpart: WITH the ON CONFLICT clause the same insert is a
    # silent no-op rather than an error, which is what makes record_basis safe
    # to call on a worker reclaim.
    await database.execute(
        mod.INSERT_BASIS_SQL,
        {
            "basis_id": "third_id", "audit_run_id": RUN,
            "merchant_id": MERCHANT, "methodology_version": "1",
            "providers_and_models": "{}", "prompt_set_id": None,
            "selected_set_id": None, "tier_mix": "{}",
            "official_domains": "[]", "primary_destination_version": None,
            "market": None, "language": None, "currency": None,
            "created_at": datetime.now(timezone.utc),
        },
    )
    rows = await database.fetch_all(
        "SELECT basis_id FROM audit_basis WHERE audit_run_id = :r", {"r": RUN}
    )
    assert len(rows) == 1


async def test_no_update_statement_exists_in_the_module():
    """A structural guard: immutability is a property of the module, not of the
    one function a test happens to call. Paired with the positive assertion that
    the INSERT the module DOES have is present and conflict-guarded."""
    statements = [
        v.upper() for k, v in vars(mod).items()
        if k.isupper() and isinstance(v, str) and "AUDIT_BASIS" in v.upper()
    ]
    # Floor: an empty collection makes the loop below vacuous, which is how a
    # guard like this stops guarding after a rename.
    assert len(statements) >= 2, statements
    for sql in statements:
        assert "UPDATE AUDIT_BASIS" not in sql
        assert "DO UPDATE" not in sql
    assert "ON CONFLICT (audit_run_id) DO NOTHING" in mod.INSERT_BASIS_SQL


# =====================================================================
# Comparability — the question a before/after diff must ask first
# =====================================================================


def _basis(**overrides):
    base = {
        "methodology_version": "1",
        "providers_and_models": {
            "gemini": {"model_id": "gemini-2.5-flash", "temperature": None}
        },
        "primary_destination_version": 1,
        "prompt_set_id": "ps_aaaa",
        "selected_set_id": "sel_bbbb",
    }
    base.update(overrides)
    return base


def test_identical_bases_are_comparable():
    assert mod.bases_are_comparable(_basis(), _basis()) is True


def test_every_comparability_field_is_actually_checked():
    """Counted, not reasoned about: each field in COMPARABILITY_FIELDS is
    perturbed in turn and must flip the verdict to False. A conjunct that was
    silently dropped from the predicate fails here by name."""
    perturbations = {
        "methodology_version": "2",
        "providers_and_models": {
            "gemini": {"model_id": "gemini-3.0-pro", "temperature": None}
        },
        "primary_destination_version": 2,
        "prompt_set_id": "ps_zzzz",
        "selected_set_id": "sel_zzzz",
        # Review: recorded but never consulted until now. official_domains
        # decides first_party on every cited host; tier_mix changes what was
        # asked; market/language change which SERP answered.
        "official_domains": ["brand.com", "brand.co.uk"],
        "tier_mix": {"trust": 10},
        "market": "GB",
        "language": "fr",
    }
    assert set(perturbations) == set(mod.COMPARABILITY_FIELDS)
    for field, value in perturbations.items():
        assert mod.bases_are_comparable(_basis(), _basis(**{field: value})) is False, field
    # Positive counterpart: unchanged, they compare.
    assert mod.bases_are_comparable(_basis(), _basis()) is True


def test_a_temperature_change_alone_makes_two_runs_non_comparable():
    """Temperature is recorded as null today because the probe path pins none.
    If it ever starts pinning one, that is a measurement change — asserted here
    so the null is a deliberate value rather than an ignored field."""
    pinned = _basis(providers_and_models={
        "gemini": {"model_id": "gemini-2.5-flash", "temperature": 0.2}
    })
    assert mod.bases_are_comparable(_basis(), pinned) is False
    assert mod.bases_are_comparable(pinned, pinned) is True


def test_a_dropped_or_added_provider_makes_two_runs_non_comparable():
    two = _basis(providers_and_models={
        "gemini": {"model_id": "gemini-2.5-flash", "temperature": None},
        "chatgpt": {"model_id": "gpt-5", "temperature": None},
    })
    assert mod.bases_are_comparable(_basis(), two) is False
    assert mod.bases_are_comparable(two, two) is True


def test_a_missing_basis_is_never_comparable():
    assert mod.bases_are_comparable(None, _basis()) is False
    assert mod.bases_are_comparable(_basis(), None) is False
    assert mod.bases_are_comparable(None, None) is False
    assert mod.bases_are_comparable(_basis(), _basis()) is True


def test_a_row_read_from_the_database_compares_against_an_in_memory_basis():
    """The stored form carries providers_and_models as a JSON STRING; the
    in-memory form carries a dict. Comparability must not depend on which side
    it was handed, or the diff would call every stored pair non-comparable."""
    stored = _basis(providers_and_models=
                    '{"gemini": {"model_id": "gemini-2.5-flash", "temperature": null}}')
    assert mod.bases_are_comparable(stored, _basis()) is True
    swapped = _basis(providers_and_models=
                     '{"gemini": {"model_id": "other", "temperature": null}}')
    assert mod.bases_are_comparable(swapped, _basis()) is False


def test_extra_provider_metadata_does_not_break_comparability():
    """Only model_id + temperature define the measurement. A caller that
    stapled on a default_model or a display name must not make two identical
    runs look different."""
    decorated = _basis(providers_and_models={
        "gemini": {"model_id": "gemini-2.5-flash", "temperature": None,
                   "default_model": "gemini-2.5-flash", "label": "Gemini"}
    })
    assert mod.bases_are_comparable(decorated, _basis()) is True
    # Negative counterpart: the model_id inside that same decorated shape still
    # governs.
    assert mod.bases_are_comparable(
        decorated,
        _basis(providers_and_models={"gemini": {"model_id": "x", "temperature": None}}),
    ) is False


# =====================================================================
# The wiring: what the audit's own report contributes to the basis
# =====================================================================


def _brand_report():
    return {
        "provider_models": {
            "Gemini": {"model": "gemini-2.5-flash", "default_model": "gemini-2.5-flash",
                       "model_is_override": False},
            "chatgpt": {"model": "gpt-5"},
            "broken": {"model": ""},
        },
        "per_sku_reports": [
            {
                "sku_key": "sku-1",
                "prompt_basis": {
                    "prompt_set_id": "ps_1111",
                    "selected_set_id": "sel_2222",
                    "selected_specs": [
                        {"query": "best vitamin c serum", "axis": "category"},
                        {"query": "best serum for dullness", "axis": "category"},
                        {"query": "is TestBrand legit", "axis": "review"},
                        {"query": "buy TestBrand serum", "axis": "intent"},
                    ],
                },
            },
        ],
    }


def test_providers_and_models_reads_the_run_and_records_no_invented_temperature():
    from services.audit_evidence_builder import build_providers_and_models

    out = build_providers_and_models(_brand_report())
    # Provider ids normalized; a provider with no resolved model is omitted
    # rather than recorded as an empty measurement.
    assert set(out) == {"gemini", "chatgpt"}
    assert out["gemini"] == {"model_id": "gemini-2.5-flash", "temperature": None}
    assert out["chatgpt"]["model_id"] == "gpt-5"
    # The probe path pins no temperature for these providers, so null is the
    # recorded fact. Asserted explicitly: a future default of 0.0 here would be
    # a fabricated value inside an immutable record.
    assert all(v["temperature"] is None for v in out.values())


def test_providers_and_models_is_empty_when_the_report_carries_none():
    from services.audit_evidence_builder import build_providers_and_models

    assert build_providers_and_models({}) == {}
    # Positive counterpart so an always-empty implementation cannot pass.
    assert build_providers_and_models(_brand_report()) != {}


def test_tier_mix_uses_the_current_intent_axis_vocabulary():
    """Counts must come from services.audit_facts.intent_axis_for, not a
    parallel taxonomy — so the expected values are computed WITH it here."""
    from services.audit_evidence_builder import build_tier_mix
    from services.audit_facts import INTENT_AXES, intent_axis_for

    mix = build_tier_mix(_brand_report())
    assert mix == {
        intent_axis_for("best vitamin c serum", "category"): 1,
        intent_axis_for("best serum for dullness", "category"): 1,
        intent_axis_for("is TestBrand legit", "review"): 1,
        intent_axis_for("buy TestBrand serum", "intent"): 1,
    }
    assert sum(mix.values()) == 4
    assert set(mix) <= set(INTENT_AXES)
    # Negative counterpart: no specs, no mix — and it does not invent a bucket.
    assert build_tier_mix({"per_sku_reports": [{"prompt_basis": {}}]}) == {}


async def test_record_audit_basis_snapshots_the_live_official_domain_set():
    """The official-domain set decides first_party on every cited host, so the
    basis must record the set AS IT STOOD. A `dead` domain is excluded — the set
    recorded must be the set used — and a live one is kept."""
    import db.merchant_official_domains as dom
    from services.audit_evidence_builder import record_audit_basis

    dom.reset_ddl_ready_for_tests()
    await dom.ensure_merchant_official_domains_table()
    await database.execute(
        "DELETE FROM merchant_official_domains WHERE merchant_id = :m",
        {"m": MERCHANT},
    )
    await dom.upsert_official_domain(
        merchant_id=MERCHANT, domain="anua.com", source=dom.SOURCE_ASSERTED,
        liveness_status=dom.LIVENESS_LIVE,
    )
    await dom.upsert_official_domain(
        merchant_id=MERCHANT, domain="judydoll.shop", source=dom.SOURCE_INFERRED,
        liveness_status=dom.LIVENESS_DEAD,
    )
    await dom.upsert_official_domain(
        merchant_id=MERCHANT, domain="anua.us", source=dom.SOURCE_ASSERTED,
        liveness_status=dom.LIVENESS_UNVERIFIABLE,
    )

    row = await record_audit_basis(
        audit_run_id=RUN, brand_report=_brand_report(), merchant_id=MERCHANT,
    )
    assert row is not None
    # `unverifiable` stays in (only `dead` excludes) — the same rule the report
    # applies, so the snapshot matches the set that produced the numbers.
    assert row["official_domains"] == ["anua.com", "anua.us"]
    assert row["prompt_set_id"] == "ps_1111"
    assert row["selected_set_id"] == "sel_2222"
    assert row["providers_and_models"]["gemini"]["model_id"] == "gemini-2.5-flash"
    assert sum(row["tier_mix"].values()) == 4

    from services.primary_destination import PRIMARY_DESTINATION_VERSION

    assert row["primary_destination_version"] == PRIMARY_DESTINATION_VERSION
    # The audit pins no currency anywhere on this path; recording a guessed
    # "USD" in an immutable record would be a fabrication.
    assert row["currency"] is None
    assert row["market"] and row["language"]

    await database.execute(
        "DELETE FROM merchant_official_domains WHERE merchant_id = :m",
        {"m": MERCHANT},
    )


async def test_record_audit_basis_needs_a_merchant():
    from services.audit_evidence_builder import record_audit_basis

    assert await record_audit_basis(
        audit_run_id=RUN, brand_report=_brand_report(), merchant_id=None,
    ) is None
    # Positive counterpart in the same test.
    assert await record_audit_basis(
        audit_run_id=RUN, brand_report=_brand_report(), merchant_id=MERCHANT,
    ) is not None


async def test_two_runs_recorded_from_the_same_inputs_are_comparable_end_to_end():
    """The round trip that matters: record two runs, read both back, and ask.
    Negative counterpart: a third run recorded with a swapped model is not
    comparable to the first."""
    await mod.record_basis(**_payload(audit_run_id=RUN))
    await mod.record_basis(**_payload(audit_run_id=OTHER_RUN))
    a = await mod.get_basis_for_run(RUN)
    b = await mod.get_basis_for_run(OTHER_RUN)
    assert mod.bases_are_comparable(a, b) is True

    await mod.record_basis(**_payload(
        audit_run_id="run_a3_basis_third",
        providers_and_models={"gemini": {"model_id": "swapped", "temperature": None}},
    ))
    c = await mod.get_basis_for_run("run_a3_basis_third")
    assert mod.bases_are_comparable(a, c) is False
    await database.execute(
        "DELETE FROM audit_basis WHERE audit_run_id = :r",
        {"r": "run_a3_basis_third"},
    )
