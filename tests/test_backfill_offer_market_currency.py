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
    assert "upper(coalesce(o.currency,'')) = 'usd'" in sql
    assert "set currency = :cur" in sql
    assert "source_system = any(:sources)" in sql


def test_domain_scan_only_counts_usd_rows():
    sql = mod._DOMAINS_SQL.lower()
    assert "upper(coalesce(o.currency, '')) = 'usd'" in sql
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
        assert "upper(coalesce(o.currency,'')) = 'usd'" in sql
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
