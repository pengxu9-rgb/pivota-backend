"""Account for US tracker inputs, official discovery, and prior storefront captures."""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path


def previous_captures(manifest_bytes, receipts):
    manifest = json.loads(manifest_bytes)
    if manifest.get("market") != "US":
        raise ValueError("expected a US capture manifest")
    expected = {row["item_id"]: row for row in manifest["items"]}
    if len(expected) != len(manifest["items"]):
        raise ValueError("duplicate previous capture item")
    digest = sha256(manifest_bytes).hexdigest()
    captures = {}
    for receipt in receipts:
        source = receipt["tracked_source"]
        identity = source["item_id"]
        if (identity in captures or expected.get(identity) != source or receipt["market"] != "US"
                or receipt["manifest_sha256"] != digest):
            raise ValueError("capture does not match exactly one previous source and manifest")
        captures[identity] = receipt
    return captures


def reconcile(intake, sources, official, captures):
    if sources.get("market") != "US":
        raise ValueError("expected US discovery sources")
    originals = [row["meitu_line"] for row in intake if row["source"] == "tracker_original"]
    line_keys = set(originals)
    if not originals or len(line_keys) != len(originals):
        raise ValueError("expected unique original lines in source intake")
    results = {}
    for row in official:
        line = row["meitu_line"]
        if (line in results or line not in line_keys or row["market"] != "US"
                or row["scope"] != "meitu_official_rediscovery_query"):
            raise ValueError("receipt is not a unique US original line")
        results[line] = row
    batch_ids = {row["batch_id"] for row in results.values()}
    if set(results) != line_keys or len(batch_ids) != 1 or not next(iter(batch_ids)):
        raise ValueError("expected one official result batch with every original present")

    sources_by_id = {}
    by_line = defaultdict(list)
    expected_meitu_ids = defaultdict(set)
    for item in sources["items"]:
        identity = item["item_id"]
        if identity in sources_by_id:
            raise ValueError("duplicate source item")
        sources_by_id[identity] = item
        receipt = captures.get(identity)
        if receipt and receipt["tracked_source"]["url"] != item["url"]:
            raise ValueError("capture source URL differs from the queued source")
        status = receipt["evidence"]["status"] if receipt else "awaiting_capture"
        for association in item["associations"]:
            if line := association.get("meitu_line"):
                by_line[line].append({
                    "item_id": identity, "url": item["url"], "source": association["source"],
                    "relation": association["relation"], "capture_status": status,
                })
                if meitu_id := association.get("meitu_id"):
                    expected_meitu_ids[line].add(str(meitu_id))

    rows, summary = [], defaultdict(Counter)
    for src in intake:
        identity = src.get("item_id")
        if identity is not None and (identity not in sources_by_id or sources_by_id[identity]["url"] != src["normalized_url"]):
            raise ValueError("intake row is not bound to its queued source")
        row = dict(src)
        if identity is not None:
            receipt = captures.get(identity)
            row["capture_status"] = receipt["evidence"]["status"] if receipt else "awaiting_capture"
        if src["source"] == "tracker_original":
            line = src["meitu_line"]
            prior = results[line]
            if expected_meitu_ids[line] and expected_meitu_ids[line] != {str(prior.get("meitu_id"))}:
                raise ValueError("official receipt does not match the original Meitu product")
            row["official"] = {key: prior[key] for key in ("status", "host", "candidate_count")}
            row["official"]["pending"] = prior.get("coverage") or {}
            row["source_associations"] = by_line[line]
            summary["official_results"][prior["status"]] += 1
        summary[src["source"]][src["status"]] += 1
        rows.append(row)
    return {
        "market": "US", "official_batch_id": next(iter(batch_ids)), "rows": rows,
        "summary": {kind: dict(counts) for kind, counts in summary.items()},
        "previous_source_receipts": len(captures),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake", type=Path, required=True, help="source intake ledger as JSON")
    parser.add_argument("--sources", type=Path, required=True, help="current capture source manifest as JSON")
    parser.add_argument("--official", type=Path, required=True, help="complete official query receipts as JSON Lines")
    parser.add_argument("--previous", type=Path, required=True, help="exact previous capture launch manifest")
    parser.add_argument("--receipts", type=Path, required=True, help="directory of previous pdp-*.json capture receipts")
    args = parser.parse_args()
    results = [json.loads(line) for line in args.official.read_text().splitlines() if line.strip()]
    if not args.receipts.is_dir():
        parser.error("--receipts must be an existing receipt directory")
    receipts = [json.loads(path.read_bytes()) for path in sorted(args.receipts.glob("pdp-*.json"))]
    captures = previous_captures(args.previous.read_bytes(), receipts)
    ledger = reconcile(json.loads(args.intake.read_bytes()), json.loads(args.sources.read_bytes()), results, captures)
    print(json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
