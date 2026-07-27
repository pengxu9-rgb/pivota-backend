"""Guard-rail tests for the currency relabel backfill.

The safety of this prod-writing script IS its SQL predicates, so assert they are
present rather than let a refactor silently drop one. (End-to-end behaviour is
covered by the dry-run against prod + the tested storefront_currency primitives.)
"""
import importlib

mod = importlib.import_module("scripts.backfill_offer_market_currency")


def test_only_external_seed_sources():
    # a real merchant's own sync must never be in scope
    for banned in ("shopify_products_sync", "universal_product_sync"):
        assert banned not in mod._SEED_SOURCES
    assert "external_product_seeds_mirror_v1" in mod._SEED_SOURCES


def test_update_only_touches_usd_stamped_rows():
    # the currency='USD' guard is what makes it idempotent + mixed-currency-safe:
    # a row already bearing a real currency must be structurally untouchable.
    sql = mod._UPDATE_OFFERS_SQL.lower()
    assert "upper(trim(coalesce(o.currency,''))) = 'usd'" in sql
    assert "set currency = :cur" in sql
    assert "source_system = any(:sources)" in sql


def test_domain_scan_only_counts_usd_rows():
    sql = mod._DOMAINS_SQL.lower()
    assert "upper(trim(coalesce(o.currency, ''))) = 'usd'" in sql
    assert "source_system = any(:sources)" in sql


# ---- 2026-07-27: the self-defeating-scope fix -------------------------------
# Rows suppressed FOR a currency defect used to be invisible to the tool built to
# correct currency defects (`suppressed_at IS NULL` in BOTH statements). Mintree
# (213 offers) and RED DANE (71) sat mislabelled `currency='USD'` for months
# behind that filter — hidden, never corrected.


def test_default_scope_includes_suppressed_rows():
    """The default rendering must not FILTER on suppressed_at. This is the bug.

    Asserting on the filter specifically, not on the word: the scan legitimately
    READS `suppressed_at` to report the live/suppressed split. Excluding rows and
    counting them are opposite things and the test has to tell them apart.
    """
    for sql in (mod.domains_sql(), mod.update_offers_sql()):
        assert "o.suppressed_at is null" not in sql.lower(), (
            "default scope re-excluded suppressed rows — the rows suppressed FOR "
            "a currency defect are exactly the ones that must be correctable"
        )
    # and the UPDATE must not mention it at all — it has no split to report
    assert "suppressed_at" not in mod.update_offers_sql().lower()


def test_live_only_escape_hatch_restores_the_old_filter():
    for sql in (mod.domains_sql(True), mod.update_offers_sql(True)):
        assert "o.suppressed_at is null" in sql.lower()


def test_correct_only_guard_survives_both_scopes():
    """Widening which rows are VISIBLE must never widen which rows are WRITABLE.

    The `currency='USD'` predicate is the whole safety argument for this script:
    a row already carrying a real currency is structurally untouchable. It must
    hold identically with suppressed rows in or out of scope.
    """
    for live_only in (False, True):
        sql = mod.update_offers_sql(live_only).lower()
        assert "upper(trim(coalesce(o.currency,''))) = 'usd'" in sql
        assert "source_system = any(:sources)" in sql
        assert "o.list_price > 0" in sql


def test_scope_switch_is_a_literal_not_a_bind_parameter():
    """No `:live_only`-style bind. An untyped boolean bind is the #1588 class:
    Postgres cannot infer the type, SQLite never notices, prod 500s."""
    for sql in (mod.domains_sql(), mod.domains_sql(True),
                mod.update_offers_sql(), mod.update_offers_sql(True)):
        assert ":live_only" not in sql
        assert ":suppressed" not in sql


def test_domain_scan_reports_the_live_suppressed_split():
    """The operator must be able to see that a domain is entirely suppressed
    before approving --apply; that is what makes the write reviewable."""
    sql = mod.domains_sql().lower()
    assert "live_offers" in sql
    assert "suppressed_offers" in sql


def test_live_only_and_only_domain_flags_are_wired():
    import contextlib, io

    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            mod.main(["--help"])
        except SystemExit:
            pass
    help_txt = buf.getvalue()
    assert "--live-only" in help_txt
    assert "--only-domain" in help_txt


def test_max_domains_and_apply_flags_exist():
    ns = mod.main.__wrapped__ if hasattr(mod.main, "__wrapped__") else None
    # parse args to confirm the guard flags are wired
    import argparse, contextlib, io
    p = argparse.ArgumentParser()
    # re-parse via the module's own parser by invoking with --help capture
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            mod.main(["--help"])
        except SystemExit:
            pass
    help_txt = buf.getvalue()
    assert "--max-domains" in help_txt
    assert "--apply" in help_txt


# ---- BEHAVIOURAL tests --------------------------------------------------------
# The tests above assert on SQL strings, which is enough for the predicates but
# NOT for the operator controls. An adversarial review mutated `--only-domain`
# to its exact INVERSE (write every domain EXCEPT the ones named), made
# `--live-only` dead at runtime, disabled `--max-domains`, and swapped the
# live/suppressed report columns — and the FULL suite stayed green through every
# one of them. These drive `_run` end-to-end against a fake DB so that the
# controls on a prod-writing script actually have coverage.

import asyncio

import pytest


class _FakeDB:
    """Minimal stand-in: answers the domain scan, records every UPDATE."""

    is_connected = True

    def __init__(self, scan_rows):
        self._scan_rows = scan_rows
        self.updates = []          # [(domain, currency, market, sql)]
        self.scan_sql = None

    async def connect(self):  # pragma: no cover - never called (is_connected)
        pass

    async def disconnect(self):  # pragma: no cover
        pass

    async def fetch_all(self, sql, params=None):
        params = params or {}
        if "UPDATE catalog_offers" in sql:
            self.updates.append(
                (params.get("domain"), params.get("cur"), params.get("mkt"), sql)
            )
            # RETURNING one row per relabelled offer; count is what the script prints
            return [{"offer_id": "o1"}]
        self.scan_sql = sql
        return list(self._scan_rows)


def _scan_row(domain, usd=10, live=0, suppressed=10):
    return {
        "domain": domain,
        "usd_offers": usd,
        "live_offers": live,
        "suppressed_offers": suppressed,
    }


_META = {
    "mintree.us": {"currency": "INR", "country": "IN"},
    "reddane.co.za": {"currency": "ZAR", "country": "ZA"},
    "lasavonneriedupilonduroy.com": {"currency": "EUR", "country": "FR"},
}


def _drive(monkeypatch, argv, scan_rows):
    fake = _FakeDB(scan_rows)
    monkeypatch.setattr(mod, "database", fake)

    async def fake_meta(domain, **kwargs):
        return _META.get(domain)

    monkeypatch.setattr(mod, "fetch_storefront_meta", fake_meta)
    rc = mod.main(argv)
    return rc, fake


def test_only_domain_writes_exactly_the_named_domains(monkeypatch):
    """The inverse mutation (write everything EXCEPT the named domains) must fail
    here. It survived the entire suite before this test existed."""
    rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "5", "--only-domain", "mintree.us"],
        [_scan_row("mintree.us"), _scan_row("reddane.co.za"),
         _scan_row("lasavonneriedupilonduroy.com", live=57, suppressed=0)],
    )
    assert rc == 0
    written = sorted(d for d, _c, _m, _s in fake.updates)
    assert written == ["mintree.us"]


def test_only_domain_is_case_insensitive(monkeypatch):
    rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "5", "--only-domain", "MinTree.US"],
        [_scan_row("mintree.us"), _scan_row("reddane.co.za")],
    )
    assert rc == 0
    assert [d for d, *_ in fake.updates] == ["mintree.us"]


def test_only_domain_repeats_accumulate(monkeypatch):
    rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "5",
         "--only-domain", "mintree.us", "--only-domain", "reddane.co.za"],
        [_scan_row("mintree.us"), _scan_row("reddane.co.za"),
         _scan_row("lasavonneriedupilonduroy.com", live=57, suppressed=0)],
    )
    assert rc == 0
    assert sorted(d for d, *_ in fake.updates) == ["mintree.us", "reddane.co.za"]


def test_only_domain_writes_the_currency_the_storefront_reported(monkeypatch):
    """Correct-only is about WHICH rows; this is about WHAT value lands."""
    _rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "5", "--only-domain", "reddane.co.za"],
        [_scan_row("reddane.co.za")],
    )
    assert fake.updates[0][1] == "ZAR"
    assert fake.updates[0][2] == "ZA"


def test_dry_run_writes_nothing(monkeypatch):
    _rc, fake = _drive(monkeypatch, [], [_scan_row("mintree.us")])
    assert fake.updates == []


def test_max_domains_refuses_and_writes_nothing(monkeypatch):
    """The circuit breaker must actually break the circuit, not just print."""
    rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "1"],
        [_scan_row("mintree.us"), _scan_row("reddane.co.za")],
    )
    assert rc == 2
    assert fake.updates == []


def test_live_only_reaches_the_UPDATE_not_just_the_scan(monkeypatch):
    """A `--live-only` honoured in the scan but dropped from the UPDATE survived
    the whole suite. Assert the flag on the SQL that actually writes."""
    _rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "5", "--live-only", "--only-domain", "mintree.us"],
        [_scan_row("mintree.us")],
    )
    assert "o.suppressed_at IS NULL" in fake.scan_sql
    assert "o.suppressed_at IS NULL" in fake.updates[0][3]


def test_default_scope_reaches_the_UPDATE_without_the_filter(monkeypatch):
    _rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "5", "--only-domain", "mintree.us"],
        [_scan_row("mintree.us")],
    )
    assert "suppressed_at" not in fake.scan_sql or "IS NULL" not in fake.scan_sql
    assert "suppressed_at" not in fake.updates[0][3]


def test_unresolvable_storefront_is_never_written(monkeypatch):
    """`fetch_storefront_meta` returning None means UNKNOWN. Never assume USD,
    and never fabricate a currency — that is the bug this whole lane exists for."""
    _rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "5"],
        [_scan_row("loaskin.com")],   # absent from _META -> unresolved
    )
    assert fake.updates == []


def test_usd_storefront_is_never_written(monkeypatch):
    """A store that really IS USD is not a correction."""
    monkeypatch.setitem(_META, "genuinely-us.com", {"currency": "USD", "country": "US"})
    try:
        _rc, fake = _drive(
            monkeypatch, ["--apply", "--max-domains", "5"],
            [_scan_row("genuinely-us.com")],
        )
        assert fake.updates == []
    finally:
        _META.pop("genuinely-us.com", None)


def test_report_maps_live_and_suppressed_to_the_right_columns(monkeypatch, capsys):
    """Swapping these two columns survived the suite. The 'you can see the domain
    is entirely suppressed' safety argument rests entirely on this mapping."""
    _drive(
        monkeypatch, [],
        [_scan_row("lasavonneriedupilonduroy.com", usd=57, live=57, suppressed=0)],
    )
    out = capsys.readouterr().out
    assert "live=57 suppressed=0" in out


def test_only_domain_typo_warns_loudly(monkeypatch, capsys):
    """The silent failure mode is 'operator typed a domain, saw 0 written, read it
    as already-corrected'."""
    _rc, fake = _drive(
        monkeypatch,
        ["--apply", "--max-domains", "5", "--only-domain", "mintree.co"],
        [_scan_row("mintree.us")],
    )
    assert fake.updates == []
    assert "WARNING" in capsys.readouterr().out
