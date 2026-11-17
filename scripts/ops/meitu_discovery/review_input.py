"""Plan exact source-review samples and reuse authenticated completed samples."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from .capture import _gtin


REVIEW_POLICY = "meitu-official-source-v2"
REVIEW_SCOPE = "meitu_official_candidate_source_review"
READY = "ready_for_readonly_catalog_preflight"
APPROVE = "catalog_review_candidate"
STAGES = {"primary", "secondary"}
SAMPLE_SIZE = 4
NATIVE_ID = re.compile(r"[1-9][0-9]{0,23}\Z")
EXECUTION = re.compile(r"(meitu-us-official-model-review-[a-f0-9]{12})-[a-z0-9]+\Z")
CANDIDATE_STATES = {"original_title_candidate", "related_title_candidate"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(value).hexdigest()


def reviewed_successfully(models):
    if not isinstance(models, list) or len(models) not in {2, 3} or not all(isinstance(row, dict) for row in models):
        return False
    stages = {row.get("stage"): row for row in models}
    ids = {row.get("response_id") for row in models}
    if (len(stages) != len(models) or not STAGES <= stages.keys() or len(ids) != len(models) or not all(ids)
            or any(not row.get("model") or row.get("decision") not in {APPROVE, "hold"} for row in models)
            or len({row["model"] for row in models}) != len(models)):
        return False
    primary, secondary = (stages[stage]["decision"] for stage in ("primary", "secondary"))
    if primary == secondary:
        return primary == APPROVE and len(models) == 2
    return stages.get("tie_break", {}).get("decision") == APPROVE


def eligible_quotes(candidate, host):
    evidence = candidate["evidence"]
    if (candidate.get("source_kind"), evidence.get("status"), evidence.get("currency"),
            evidence.get("currency_proof"), evidence.get("identity_status")) != (
        "shopify_ajax", "shopify_us_quote_observed", "USD", "product_cart_currency_cookie", "verified",
    ):
        return None
    if evidence.get("storefront_country_proof") not in {None, "US"}:
        return None
    url = urlsplit(candidate["url"])
    source = urlsplit(evidence.get("source_url") or "")
    if (url.scheme != "https" or not host or url.netloc != host or url.query or url.fragment
            or source.geturl() != url._replace(path=url.path.rstrip("/") + ".js", query="country=US").geturl()
            or not NATIVE_ID.fullmatch(str(evidence.get("product_id") or ""))
            or not evidence.get("vendor") or not evidence.get("title")):
        raise ValueError("native source evidence does not match the US product")
    variants = {}
    for variant in evidence.get("variants", []):
        native = str(variant.get("variant_id") or "")
        code, sku = _gtin(variant.get("gtin")), variant.get("sku")
        price = variant.get("price_minor")
        if (not NATIVE_ID.fullmatch(native) or not code or not isinstance(sku, str) or not sku.strip()
                or type(price) is not int or price <= 0 or variant.get("in_stock") is not True):
            continue
        if native in variants:
            raise ValueError("duplicate native variant in source evidence")
        variants[native] = {"variant_id": native, "gtin": code, "sku": sku.strip(), "title": variant.get("title"),
                            "price_minor": price, "in_stock": True}
    if not variants:
        return None
    return {**{key: evidence[key] for key in ("status", "currency", "currency_proof", "identity_status",
                                            "source_url", "product_id", "title", "vendor")},
            "storefront_country_proof": evidence.get("storefront_country_proof"),
            "variants": sorted(variants.values(), key=lambda item: int(item["variant_id"]))}


def sample_key(row):
    source, evidence = row["tracked_source"], row["evidence"]
    associations = source["associations"]
    if len(associations) != 1 or associations[0].get("source") != "official_rediscovery":
        raise ValueError("review sample must bind exactly one Meitu association")
    association = associations[0]
    context = {key: association[key] for key in ("meitu_line", "meitu_id", "meitu_brand", "anchor_product", "relation")}
    if (type(context["meitu_line"]) is not int or context["meitu_line"] < 1
            or not all(isinstance(context[key], str) and context[key] for key in
                       ("meitu_id", "meitu_brand", "anchor_product"))
            or context["relation"] not in CANDIDATE_STATES):
        raise ValueError("invalid source review association")
    sample = eligible_quotes({"url": source["url"], "source_kind": source["source_kind"], "evidence": evidence},
                             source["host"])
    if sample is None or not 1 <= len(sample["variants"]) <= SAMPLE_SIZE or sample != evidence:
        raise ValueError("review sample must contain the exact eligible variants supplied to the models")
    return digest(canonical({"policy": REVIEW_POLICY, "market": "US", "url": source["url"],
                             "association": context, "evidence": sample}))


def completion_decisions(input_bytes, result):
    rows = json.loads(input_bytes)
    if (result.get("scope"), result.get("market"), result.get("review_policy"),
            result.get("review_input_sha256"), result.get("candidate_count")) != (
        REVIEW_SCOPE, "US", REVIEW_POLICY, digest(input_bytes), len(rows),
    ) or any(
        type(result.get(key)) is not int or result[key] != 0
        for key in ("commerce_index_writes", "merchant_db_writes", "offer_db_writes")
    ):
        raise ValueError("review completion does not match immutable source input")
    by_id = {row["tracked_source"]["item_id"]: row for row in rows}
    reviews = result["reviews"]
    if (not isinstance(reviews, list) or len(by_id) != len(rows) or len(reviews) != len(rows)
            or {row["candidate_id"] for row in reviews} != set(by_id)):
        raise ValueError("review completion does not cover the submitted sample IDs")
    covered, held = set(), set()
    for result_row in reviews:
        row = by_id[result_row["candidate_id"]]
        if (result_row["url"] != row["tracked_source"]["url"]
                or result_row.get("model_sample_variant_ids") != [v["variant_id"] for v in row["evidence"]["variants"]]):
            raise ValueError("review completion covers different storefront evidence")
        key = sample_key(row)
        if row["tracked_source"]["item_id"] != f"pdp-{key[:24]}":
            raise ValueError("review sample identity is inconsistent")
        if result_row.get("disposition") == READY and reviewed_successfully(result_row.get("models")):
            covered.add(key)
        elif result_row.get("disposition") == "held_for_source_or_mapping_review":
            held.add(key)
    return covered, held


def reuse_from_completion(input_bytes, result):
    covered, held = completion_decisions(input_bytes, result)
    return covered - held


def load_history(root, verify, exclude=None):
    """Re-query completed Cloud executions. Local files provide only locators and input bytes."""
    if not root.is_dir():
        raise ValueError("review history root must be a directory")
    runs = [path for path in root.iterdir() if path.is_dir()]
    partitions = [part for run in runs for part in run.glob("part-*") if part.is_dir()]
    archives = []
    for path in sorted([*runs, *partitions]):
        resolved = path.resolve()
        if (resolved == exclude or exclude in resolved.parents
                or not all((path / name).is_file() for name in ("review-summary.json", "review-input.sha256", "review-execution.json"))):
            continue
        archives.append(path)

    def load(path):
        locator = json.loads((path / "review-execution.json").read_bytes())
        if not {"job", "execution", "review_input_sha256"} <= locator.keys():
            return set(), set()
        data = (path / "review-input.json").read_bytes()
        input_sha = (path / "review-input.sha256").read_text().strip()
        execution = locator["execution"]
        match = EXECUTION.fullmatch(execution)
        if digest(data) != input_sha or match is None or (locator["job"], locator["review_input_sha256"]) != (match[1], input_sha):
            raise ValueError(f"invalid archived review locator in {path.name}")
        result = verify(match[1], execution, input_sha)
        if result is None:
            return set(), set()
        return completion_decisions(data, result)

    covered, held = set(), set()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for approved, blocked in pool.map(load, archives):
            covered.update(approved)
            held.update(blocked)
    return covered - held, held


def plan(queries, covered=(), held=()):
    if (not queries or len({row.get("batch_id") for row in queries}) != 1
            or not queries[0].get("batch_id") or len({row["meitu_line"] for row in queries}) != len(queries)):
        raise ValueError("expected one batch of unique Meitu query receipts")
    covered = set(covered)
    held = set(held)
    pending, reused, blocked = {}, set(), set()
    for query in queries:
        if (query.get("scope"), query.get("market")) != ("meitu_official_rediscovery_query", "US"):
            raise ValueError("expected only US official rediscovery receipts")
        for candidate in query["candidates"]:
            if candidate.get("status") not in CANDIDATE_STATES:
                continue
            evidence = eligible_quotes(candidate, query.get("host"))
            if evidence is None:
                continue
            url = candidate["url"]
            association = {"source": "official_rediscovery", "meitu_line": query["meitu_line"],
                           "meitu_id": query["meitu_id"], "meitu_brand": query["brand"],
                           "anchor_product": query["name"], "relation": candidate["status"]}
            variants = evidence["variants"]
            for start in range(0, len(variants), SAMPLE_SIZE):
                source = {"url": url, "host": query["host"], "source_kind": "shopify_ajax",
                          "associations": [association]}
                row = {"tracked_source": source, "evidence": {**evidence, "variants": variants[start:start + SAMPLE_SIZE]}}
                key = sample_key(row)
                if key in held:
                    blocked.add(key)
                    continue
                if key in covered:
                    reused.add(key)
                    continue
                source["item_id"] = f"pdp-{key[:24]}"
                pending.setdefault(key, row)
    return [pending[key] for key in sorted(pending)], len(reused), len(blocked)


def partition(rows, max_rows, fits):
    if not rows:
        return [canonical([]) + b"\n"]
    payloads, offset = [], 0
    while offset < len(rows):
        limit = min(max_rows, len(rows) - offset)
        while limit:
            payload = canonical(rows[offset:offset + limit]) + b"\n"
            if fits(payload):
                break
            limit //= 2
        if not limit:
            raise ValueError("one source-review sample exceeds the worker input limit")
        payloads.append(payload)
        offset += limit
    return payloads


def write_immutable(path, payload):
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(payload)
    try:
        try:
            os.link(temporary.name, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError("review input already exists with different content; use a new run directory") from None
    finally:
        Path(temporary.name).unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queries", type=Path, help="collected US rediscovery query JSONL")
    parser.add_argument("--run-dir", type=Path, required=True, help="new review run directory")
    parser.add_argument("--history-root", type=Path, help="shared parent directory of completed review runs")
    args = parser.parse_args()
    root = args.history_root or args.run_dir.parent
    queries = [json.loads(line) for line in args.queries.read_text().splitlines() if line.strip()]
    from .model_worker import MAX_INPUT_BYTES, MAX_INPUT_ROWS
    from .review_cloud import _program, read_completed_review

    if not args.history_root:
        root.mkdir(parents=True, exist_ok=True)

    covered, held = load_history(root, read_completed_review, exclude=args.run_dir.resolve())
    rows, reused, blocked = plan(queries, covered, held)

    def fits(payload):
        if len(payload) > MAX_INPUT_BYTES:
            return False
        try:
            _program(payload)
        except ValueError:
            return False
        return True

    payloads = partition(rows, MAX_INPUT_ROWS, fits)
    runs = [args.run_dir / f"part-{idx:03d}" for idx in range(1, len(payloads) + 1)] if len(payloads) > 1 else [args.run_dir]
    args.run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"pending_samples": len(rows), "reused_samples": reused, "held_samples": blocked,
                "runs": [str(run) for run in runs],
                "input_sha256": [digest(payload) for payload in payloads]}
    write_immutable(args.run_dir / "review-plan.json", canonical(manifest) + b"\n")
    for run, payload in zip(runs, payloads, strict=True):
        run.mkdir(parents=True, exist_ok=True)
        write_immutable(run / "review-input.json", payload)
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
