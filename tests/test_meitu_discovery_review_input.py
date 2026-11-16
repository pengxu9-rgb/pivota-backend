import json

import pytest

from scripts.ops.meitu_discovery import review_input as review


URL = "https://brand.example/products/lip"
JOB = "meitu-us-official-model-review-0123456789ab"
EXECUTION = JOB + "-test"
GTIN = "00094800364448"


def query(line=1, *, start=101, count=5, amount=2500, relation="original_title_candidate"):
    variants = [{"variant_id": str(native), "gtin": GTIN, "sku": f"sku-{native}", "title": f"Shade {native}",
                 "price_minor": amount, "in_stock": True} for native in range(start, start + count)]
    return {"scope": "meitu_official_rediscovery_query", "market": "US", "batch_id": "meitu-test-input",
            "meitu_line": line, "meitu_id": f"id-{line}", "brand": "Test", "host": "brand.example", "name": "Lip",
            "candidates": [{"url": URL, "status": relation, "source_kind": "shopify_ajax",
                            "evidence": {"status": "shopify_us_quote_observed", "product_id": "3201", "title": "Lip",
                                         "vendor": "Test", "currency": "USD", "currency_proof": "product_cart_currency_cookie",
                                         "identity_status": "verified", "source_url": URL + ".js?country=US",
                                         "storefront_country_proof": "US", "variants": variants}}]}


def completion(rows, data):
    rounds = [{"stage": stage, "model": model, "decision": review.APPROVE, "response_id": str(index)}
              for index, (stage, model) in enumerate((("primary", "gemini-3.8-flash"), ("secondary", "gemini-3.5-flash-lite")))]
    return {"scope": review.REVIEW_SCOPE, "market": "US", "review_policy": review.REVIEW_POLICY,
            "review_input_sha256": review.digest(data), "candidate_count": len(rows),
            "commerce_index_writes": 0, "merchant_db_writes": 0, "offer_db_writes": 0,
            "reviews": [{"candidate_id": row["tracked_source"]["item_id"], "url": URL, "disposition": review.READY,
                         "models": rounds, "model_sample_variant_ids": [v["variant_id"] for v in row["evidence"]["variants"]]}
                        for row in rows]}


def test_samples_are_disjoint_and_bound_to_one_meitu_association():
    rows, reused = review.plan([query(), query(2)])
    assert reused == 0
    assert sorted(len(row["evidence"]["variants"]) for row in rows) == [1, 1, 4, 4]
    assert len({row["tracked_source"]["item_id"] for row in rows}) == 4
    assert {row["tracked_source"]["associations"][0]["meitu_line"] for row in rows} == {1, 2}
    assert all(len(row["tracked_source"]["associations"]) == 1 for row in rows)


def test_authenticated_history_reuses_only_exact_samples(tmp_path):
    rows, _ = review.plan([query()])
    data = review.canonical(rows) + b"\n"
    result = completion(rows, data)
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "review-input.json").write_bytes(data)
    (previous / "review-input.sha256").write_text(review.digest(data))
    (previous / "review-execution.json").write_text(json.dumps(
        {"job": JOB, "execution": EXECUTION, "review_input_sha256": review.digest(data)}))
    # Presence is required, but local summary contents cannot authorize reuse.
    (previous / "review-summary.json").write_text('{"forged":true}')
    calls = []

    def verify(job, execution, fingerprint):
        calls.append((job, execution, fingerprint))
        return result

    covered = review.load_history(tmp_path, verify)
    assert calls == [(JOB, EXECUTION, review.digest(data))]
    assert review.plan([query()], covered) == ([], 2)
    assert len(review.plan([query(2)], covered)[0]) == 2
    assert len(review.plan([query(amount=2600)], covered)[0]) == 2
    assert len(review.plan([query(relation="related_title_candidate")], covered)[0]) == 2
    assert len(review.plan([query(start=106)], covered)[0]) == 2
    assert review.load_history(tmp_path, verify, exclude=previous.resolve()) == set()
    assert review.load_history(tmp_path, lambda *_: None) == set()


def test_tampered_input_market_samples_and_policy_fail_closed():
    rows, _ = review.plan([query()])
    data = review.canonical(rows) + b"\n"
    result = completion(rows, data)
    for changed in ({"market": "CA"}, {"review_policy": "legacy"}, {"offer_db_writes": 1}):
        with pytest.raises(ValueError):
            review.reuse_from_completion(data, {**result, **changed})
    with pytest.raises(ValueError):
        review.reuse_from_completion(data + b" ", result)
    result["reviews"][0]["model_sample_variant_ids"] = ["999"]
    with pytest.raises(ValueError):
        review.reuse_from_completion(data, result)


def test_hold_missing_round_and_duplicate_response_do_not_authorize_reuse():
    rows, _ = review.plan([query(count=1)])
    data = review.canonical(rows)
    result = completion(rows, data)
    model_rounds = result["reviews"][0]["models"]
    primary, secondary = model_rounds
    for rounds in ([primary], [primary, primary], [primary, {**secondary, "response_id": primary["response_id"]}],
                   [primary, {**secondary, "decision": "hold"}]):
        result["reviews"][0]["models"] = rounds
        assert not review.reuse_from_completion(data, result)
    result["reviews"][0]["models"] = [primary, {**secondary, "decision": "hold"},
                                     {"stage": "tie_break", "model": "gemini-3.5-flash", "decision": review.APPROVE,
                                      "response_id": "tie"}]
    assert len(review.reuse_from_completion(data, result)) == 1
    result["reviews"][0]["disposition"] = "held_for_source_or_mapping_review"
    assert not review.reuse_from_completion(data, result)


def test_without_identity_or_with_foreign_country_quote_is_not_planned():
    source = query()
    evidence = source["candidates"][0]["evidence"]
    evidence["identity_status"] = "handle_mismatch"
    assert review.plan([source]) == ([], 0)
    evidence.update(identity_status="verified", storefront_country_proof="CA")
    assert review.plan([source]) == ([], 0)
