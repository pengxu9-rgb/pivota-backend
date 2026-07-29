"""Guard-rail + behavioural tests for the domain-less offer currency audit.

The script's safety IS its predicates and its operator controls, so both are
covered: string assertions pin the SQL guards (fill-only backfill, seed-source
scope), and a fake-DB drive proves the controls (dry-run inert, confirm token,
caps, confidence rules) actually gate the writes. The Postgres-only surface
(= ANY(:sources), ->>, LATERAL, jsonb_to_recordset) is executed for real by
tests/test_audit_domainless_offer_currency_postgres.py.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

mod = importlib.import_module("scripts.audit_domainless_offer_currency")
sibling = importlib.import_module("scripts.backfill_offer_market_currency")


# ---- scope + SQL guards -------------------------------------------------------


def test_seed_sources_match_the_weekly_backfill():
    """Both tools claim to cover 'the external-seed cohort'; if the tuples drift
    apart, an offer can be backfilled here yet invisible to the weekly audit."""
    assert tuple(mod._SEED_SOURCES) == tuple(sibling._SEED_SOURCES)


def test_scan_targets_only_domainless_seed_offers():
    sql = " ".join(mod._SCAN_SQL.split()).lower()
    assert "coalesce(btrim(o.source_domain), '') = ''" in sql
    assert "o.source_system = any(:sources)" in sql


def test_scan_includes_suppressed_rows_but_reports_the_split():
    """Rows suppressed FOR a currency defect must not be invisible to currency
    tooling (the 2026-07-27 lesson). The scan READS suppressed_at for the split
    but must not FILTER on it."""
    sql = mod._SCAN_SQL.lower()
    assert "o.suppressed_at is null" not in sql
    assert "is_suppressed" in sql


def test_backfill_is_fill_only_and_scoped():
    """The write must be structurally unable to overwrite a present
    source_domain, and must never reach a real merchant's sync rows."""
    sql = " ".join(mod._BACKFILL_SQL.split()).lower()
    assert "coalesce(btrim(o.source_domain), '') = ''" in sql
    assert "o.source_system = any(:sources)" in sql
    assert "returning o.offer_id" in sql
    assert "o.offer_id = m.offer_id" in sql


def test_backfill_writes_only_the_reviewed_mapping():
    """The UPDATE is driven by the offer_id->domain mapping, not by re-deriving
    inside SQL — what the operator reviewed is what lands."""
    assert "jsonb_to_recordset" in mod._BACKFILL_SQL


# ---- derivation ---------------------------------------------------------------


def _row(**kw):
    base = {
        "offer_id": "o1", "product_key": "pk1", "currency": "USD",
        "list_price": 10.0, "is_suppressed": False,
        "payload_domain": None, "payload_canonical_url": None,
        "payload_destination_url": None,
        "offer_seed_domain": None, "offer_seed_canonical_url": None,
        "offer_seed_destination_url": None,
        "product_seed_domain": None, "product_seed_canonical_url": None,
        "product_seed_destination_url": None,
        "attached_domains": None,
    }
    base.update(kw)
    return base


def test_own_seed_beats_payload_snapshot():
    domain, rule = mod.derive_offer_domain(_row(
        offer_seed_domain="fresh.example", payload_domain="stale.example"))
    assert (domain, rule) == ("fresh.example", "offer_seed")


def test_payload_beats_product_seed():
    domain, rule = mod.derive_offer_domain(_row(
        payload_domain="snap.example", product_seed_domain="prod.example"))
    assert (domain, rule) == ("snap.example", "offer_payload")


def test_url_hosts_are_normalized_to_bare_domains():
    domain, rule = mod.derive_offer_domain(_row(
        offer_seed_canonical_url="https://www.oiad.com/products/oddy-bunny?x=1"))
    assert (domain, rule) == ("oiad.com", "offer_seed")


def test_domain_column_beats_url_within_a_rule():
    domain, _ = mod.derive_offer_domain(_row(
        offer_seed_domain="oiad.com",
        offer_seed_canonical_url="https://redirect.example/p"))
    assert domain == "oiad.com"


def test_unanimous_attached_seeds_resolve():
    domain, rule = mod.derive_offer_domain(_row(
        attached_domains=["oiad.com", "www.oiad.com"]))
    assert (domain, rule) == ("oiad.com", "attached_seed_unique")


def test_disagreeing_attached_seeds_are_ambiguous_never_guessed():
    domain, rule = mod.derive_offer_domain(_row(
        attached_domains=["a.example", "b.example"]))
    assert domain is None
    assert rule == "attached_seed_ambiguous"


def test_no_provenance_is_unresolved_never_guessed():
    domain, rule = mod.derive_offer_domain(_row())
    assert domain is None
    assert rule == "unresolved"


def test_attached_is_not_confident_by_default():
    assert mod._confident("offer_seed", False)
    assert mod._confident("offer_payload", False)
    assert mod._confident("product_seed", False)
    assert not mod._confident("attached_seed_unique", False)
    assert mod._confident("attached_seed_unique", True)
    assert not mod._confident("attached_seed_ambiguous", True)
    assert not mod._confident("unresolved", True)


# ---- behavioural: the operator controls must gate the writes ------------------


class _FakeDB:
    is_connected = True

    def __init__(self, scan_rows):
        self._scan_rows = scan_rows
        self.backfills = []   # captured (mappings_json, sources)

    async def fetch_all(self, sql, params=None):
        params = params or {}
        if "UPDATE catalog_offers" in sql:
            import json

            mapping = json.loads(params["mappings"])
            self.backfills.append((mapping, list(params["sources"])))
            return [{"offer_id": m["offer_id"]} for m in mapping]
        if "OUT OF SCOPE" in sql or "NOT (source_system" in sql:
            return []
        return list(self._scan_rows)


_META = {
    "oiad.com": {"currency": "KRW", "country": "KR"},
    "honest-us.example": {"currency": "USD", "country": "US"},
}


def _drive(monkeypatch, argv, scan_rows, meta=None):
    fake = _FakeDB(scan_rows)
    monkeypatch.setattr(mod, "database", fake)
    quarantined = []

    async def fake_meta(domain, **kwargs):
        return (meta if meta is not None else _META).get(
            str(domain or "").strip().lower())

    async def fake_quarantine(**kwargs):
        quarantined.append(kwargs["match_value"])

        class _Q:
            quarantine_id = len(quarantined)

        return _Q()

    monkeypatch.setattr(mod, "fetch_storefront_meta", fake_meta)
    monkeypatch.setattr(mod, "create_quarantine", fake_quarantine)
    rc = mod.main(argv)
    return rc, fake, quarantined


_OIAD = _row(offer_id="oiad-offer", offer_seed_domain="oiad.com",
             currency="USD", list_price=400000.0)
_ATTACHED_ONLY = _row(offer_id="attached-offer",
                      attached_domains=["honest-us.example"])


def test_dry_run_writes_and_quarantines_nothing(monkeypatch):
    rc, fake, quarantined = _drive(monkeypatch, [], [_OIAD])
    assert rc == 0
    assert fake.backfills == []
    assert quarantined == []


def test_apply_without_confirm_token_is_refused(monkeypatch):
    rc, fake, _ = _drive(monkeypatch, ["--apply"], [_OIAD])
    assert rc == 2
    assert fake.backfills == []


def test_apply_backfills_the_confident_mapping(monkeypatch):
    rc, fake, quarantined = _drive(
        monkeypatch, ["--apply", "--confirm", mod.CONFIRM_TOKEN], [_OIAD])
    assert rc == 0
    assert len(fake.backfills) == 1
    mapping, sources = fake.backfills[0]
    assert mapping == [{"offer_id": "oiad-offer", "source_domain": "oiad.com"}]
    assert sources == list(mod._SEED_SOURCES)
    # backfill alone never quarantines
    assert quarantined == []


def test_attached_rows_are_excluded_unless_opted_in(monkeypatch):
    rc, fake, _ = _drive(
        monkeypatch, ["--apply", "--confirm", mod.CONFIRM_TOKEN],
        [_OIAD, _ATTACHED_ONLY])
    assert rc == 0
    (mapping, _), = fake.backfills
    assert [m["offer_id"] for m in mapping] == ["oiad-offer"]

    rc, fake, _ = _drive(
        monkeypatch,
        ["--apply", "--confirm", mod.CONFIRM_TOKEN, "--include-attached"],
        [_OIAD, _ATTACHED_ONLY])
    assert rc == 0
    (mapping, _), = fake.backfills
    assert sorted(m["offer_id"] for m in mapping) == ["attached-offer", "oiad-offer"]


def test_max_offers_refuses_and_writes_nothing(monkeypatch):
    rc, fake, _ = _drive(
        monkeypatch,
        ["--apply", "--confirm", mod.CONFIRM_TOKEN, "--max-offers", "1"],
        [_OIAD, _row(offer_id="second", offer_seed_domain="oiad.com")])
    assert rc == 2
    assert fake.backfills == []


def test_quarantine_requires_the_flag_and_hits_only_proven_mismatches(monkeypatch):
    rows = [
        _OIAD,                                                    # KRW-as-USD
        _row(offer_id="ok", offer_seed_domain="honest-us.example"),  # really USD
        _row(offer_id="dark", offer_seed_domain="unreachable.example"),
    ]
    rc, _fake, quarantined = _drive(
        monkeypatch,
        ["--apply", "--confirm", mod.CONFIRM_TOKEN, "--quarantine-mismatched"],
        rows)
    assert rc == 0
    # only the PROVEN mismatch: honest USD store and unresolvable store are not
    # offenders (never guess from silence)
    assert quarantined == ["oiad.com"]


def test_wholesale_derived_domain_is_an_offender(monkeypatch):
    rc, _fake, quarantined = _drive(
        monkeypatch,
        ["--apply", "--confirm", mod.CONFIRM_TOKEN, "--quarantine-mismatched"],
        [_row(offer_id="w1", offer_seed_domain="wholesale.publicgoods.com")])
    assert rc == 0
    assert quarantined == ["wholesale.publicgoods.com"]


def test_max_quarantine_refuses(monkeypatch):
    rows = [_row(offer_id=f"o{i}", offer_seed_domain=f"store{i}.example")
            for i in range(3)]
    meta = {f"store{i}.example": {"currency": "GBP", "country": "GB"}
            for i in range(3)}
    rc, _fake, quarantined = _drive(
        monkeypatch,
        ["--apply", "--confirm", mod.CONFIRM_TOKEN, "--quarantine-mismatched",
         "--max-quarantine", "2"],
        rows, meta=meta)
    assert rc == 2
    assert quarantined == []


def test_unresolvable_storefront_still_gets_the_domain_backfill(monkeypatch):
    """/meta.json silence blocks the MISMATCH verdict, not the provenance-derived
    domain write — the domain comes from our own seed, not from the network."""
    rc, fake, quarantined = _drive(
        monkeypatch, ["--apply", "--confirm", mod.CONFIRM_TOKEN],
        [_row(offer_id="dark", offer_seed_domain="unreachable.example")])
    assert rc == 0
    (mapping, _), = fake.backfills
    assert mapping == [{"offer_id": "dark", "source_domain": "unreachable.example"}]
    assert quarantined == []


def test_flags_are_wired():
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()) as buf:
        with pytest.raises(SystemExit):
            mod.main(["--help"])
    help_txt = buf.getvalue()
    for flag in ("--apply", "--confirm", "--max-offers", "--include-attached",
                 "--quarantine-mismatched", "--max-quarantine"):
        assert flag in help_txt


def test_price_alert_lists_the_oiad_class_without_acting(monkeypatch, capsys):
    """A KR Markets store with a USD base defeats the currency_mismatch check
    (label == base currency), so the ₩400,000-as-USD row must still surface —
    on the observe-only review list, and never in the backfill-blocking or
    quarantine paths."""
    rc, fake, quarantined = _drive(
        monkeypatch,
        ["--apply", "--confirm", mod.CONFIRM_TOKEN, "--quarantine-mismatched"],
        [_row(offer_id="oiad-offer", offer_seed_domain="oiad-markets.example",
              currency="USD", list_price=400000.0)],
        meta={"oiad-markets.example": {"currency": "USD", "country": "KR"}})
    out = capsys.readouterr().out
    assert rc == 0
    assert "IMPLAUSIBLE-PRICE REVIEW LIST: 1" in out
    assert "400000.00 USD" in out
    assert quarantined == []                      # label matches base: no offender
    (mapping, _), = fake.backfills                # domain backfill still lands
    assert mapping == [{"offer_id": "oiad-offer",
                        "source_domain": "oiad-markets.example"}]


def test_price_alert_ignores_plausible_and_non_usd_rows(monkeypatch, capsys):
    _drive(monkeypatch, [],
           [_row(offer_id="cheap", offer_seed_domain="honest-us.example",
                 currency="USD", list_price=29.0),
            _row(offer_id="jpy", offer_seed_domain="honest-us.example",
                 currency="JPY", list_price=22400.0)])
    out = capsys.readouterr().out
    assert "IMPLAUSIBLE-PRICE REVIEW LIST: 0" in out
