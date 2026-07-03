"""ADR-008 #2 — deposit-unresolved telemetry: an unresolved/missing identity
drops a SKU's citations from deposit, and that drop is now counted."""
import services.audit_evidence_builder as aeb
from services.audit_evidence_builder import (
    _count_depositable_observations,
    extract_citation_observations,
)
from services.catalog_identity import (
    DEPOSIT_BASIS_GTIN,
    DEPOSIT_BASIS_UNRESOLVED,
    ResolvedDepositKey,
)


def _sku(product_key, content_key, n_obs=2):
    obs = [
        {"query": f"q{i}", "query_class": "category", "provider": "gemini"}
        for i in range(n_obs)
    ]
    return {
        "sku_key": product_key,
        "product_key": product_key,
        "content_key": content_key,
        "authority_hosts": [
            {
                "host": "reddit.com",
                "evidence_urls": ["https://reddit.com/x"],
                "query_observations": obs,
            }
        ],
    }


def _report(*skus):
    return {"authority_map": {"skus": list(skus)}}


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aeb, "record_deposit_dropped",
        lambda *, basis, observations: calls.append((basis, observations)),
    )
    return calls


def test_counts_observations_helper():
    assert _count_depositable_observations(_sku("p", "ck", n_obs=3)) == 3
    # rows missing a query or provider are not depositable and not counted
    sku = _sku("p", "ck", n_obs=1)
    sku["authority_hosts"][0]["query_observations"].append({"query": "", "provider": "x"})
    sku["authority_hosts"][0]["query_observations"].append({"query": "y", "provider": ""})
    assert _count_depositable_observations(sku) == 1


def test_unresolved_identity_records_drop_and_excludes(monkeypatch):
    calls = _capture(monkeypatch)
    report = _report(_sku("m1|shopify|s1", "ck_seed", n_obs=2))
    out = extract_citation_observations(
        report,
        content_key_map={
            "m1|shopify|s1": ResolvedDepositKey("ck_seed", DEPOSIT_BASIS_UNRESOLVED, 0.0),
        },
    )
    assert out == []                       # citations dropped from deposit
    assert calls == [("unresolved", 2)]    # ...and the drop is counted


def test_missing_map_entry_records_missing_basis(monkeypatch):
    calls = _capture(monkeypatch)
    report = _report(_sku("m1|shopify|s1", "ck_seed", n_obs=3))
    out = extract_citation_observations(report, content_key_map={})  # no entry
    assert out == []
    assert calls == [("missing", 3)]


def test_depositable_sku_not_counted_as_dropped(monkeypatch):
    calls = _capture(monkeypatch)
    report = _report(_sku("m1|shopify|s1", "ck_seed", n_obs=2))
    out = extract_citation_observations(
        report,
        content_key_map={
            "m1|shopify|s1": ResolvedDepositKey("ck_" + "b" * 32, DEPOSIT_BASIS_GTIN, 1.0),
        },
    )
    assert len(out) == 2
    assert calls == []                     # nothing dropped


def test_mixed_batch_counts_only_the_dropped(monkeypatch):
    calls = _capture(monkeypatch)
    report = _report(
        _sku("good", "ck_good", n_obs=2),
        _sku("bad", "ck_bad", n_obs=1),
    )
    out = extract_citation_observations(
        report,
        content_key_map={
            "good": ResolvedDepositKey("ck_" + "c" * 32, DEPOSIT_BASIS_GTIN, 1.0),
            "bad": ResolvedDepositKey("ck_bad", DEPOSIT_BASIS_UNRESOLVED, 0.0),
        },
    )
    assert len(out) == 2                    # only the good SKU's rows
    assert all(o["product_key"] == "good" for o in out)
    assert calls == [("unresolved", 1)]


def test_no_map_does_not_record(monkeypatch):
    """The pure/no-map path (tests + legacy) must not emit drop telemetry."""
    calls = _capture(monkeypatch)
    out = extract_citation_observations(_report(_sku("p", "ck", n_obs=2)))
    assert len(out) == 2
    assert calls == []


def test_record_deposit_dropped_is_noop_safe():
    """The real record fn must never raise, even with odd inputs."""
    from observability.citation_deposit_metrics import record_deposit_dropped

    record_deposit_dropped(basis="unresolved", observations=0)
    record_deposit_dropped(basis="", observations=5)
    record_deposit_dropped(basis=None, observations=-1)  # type: ignore[arg-type]
