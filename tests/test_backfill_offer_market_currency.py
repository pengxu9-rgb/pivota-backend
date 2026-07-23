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
