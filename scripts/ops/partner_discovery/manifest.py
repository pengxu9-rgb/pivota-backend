"""Build US first-party rediscovery queries for the tracked partner originals."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


# US storefront approval is independent of whether a historic product still exists. The
# approved storefronts, the search clues and the first-party anchors live in the data file below.
# Search clues can justify a discovery candidate; claims about original product identity need
# review. The tracker sometimes carries a third-party URL even when the US brand has a known
# first-party product URL; only that approved origin may be seeded.
DATA_PATH = Path(__file__).resolve().parents[3] / "data" / "catalog_enrichment" / "us_discovery_storefronts.json"
_DATA = json.loads(DATA_PATH.read_text(encoding="utf-8"))["manifest"]
US_STOREFRONTS = {brand: tuple(value) for brand, value in _DATA["us_storefronts"].items()}
SEARCH_NAMES = {int(line): tuple(names) for line, names in _DATA["search_names"].items()}
FIRST_PARTY_ANCHORS = {int(line): url for line, url in _DATA["first_party_anchors"].items()}
ANCHOR_PATH_PREFIXES = _DATA["anchor_path_prefixes"]
EXPECTED_LINES = _DATA["expected_lines"]
EXPECTED_BRANDS = _DATA["expected_brands"]


def clean(text):
    return ' '.join(text.split())


def first_party_anchor(value, host):
    if not value or not host:
        return None
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.netloc != parts.hostname
            or (parts.hostname or '').removeprefix('www.') != host.removeprefix('www.')
            or parts.path.startswith(('/en-ca/', '/en-gb/', '/en-au/'))):
        return None
    if host in ANCHOR_PATH_PREFIXES and not parts.path.startswith(ANCHOR_PATH_PREFIXES[host]):
        return None
    return urlunsplit(('https', host, parts.path, '', ''))


def search_names(line, name):
    names = {}
    for label in SEARCH_NAMES.get(line, ()):
        label = clean(label)
        if label.casefold() != name.casefold():
            names.setdefault(label.casefold(), label)
    return list(names.values())


def build(tracker):
    lines = tracker['lines']
    numbers = [row[0] for row in lines]
    ids = [str(row[1]).strip() for row in lines]
    if (len(lines) != EXPECTED_LINES or set(numbers) != set(range(1, EXPECTED_LINES + 1))
            or len(set(ids)) != EXPECTED_LINES or not all(ids) or len({row[2] for row in lines}) != EXPECTED_BRANDS):
        raise ValueError(f'expected {EXPECTED_LINES} distinct original partner lines across {EXPECTED_BRANDS} brands')
    queries = []
    for row in sorted(lines, key=lambda row: row[0]):
        line, meitu_id, brand, name = row[:4]
        host, platform = US_STOREFRONTS.get(brand, (None, 'unknown'))
        name = clean(name)
        if not name:
            raise ValueError(f'empty original product name on line {line}')
        queries.append({
            'line': line, 'meitu_id': str(meitu_id).strip(), 'brand': brand, 'name': name,
            'host': host, 'platform': platform, 'search_names': search_names(line, name),
            'anchor_url': first_party_anchor(FIRST_PARTY_ANCHORS.get(line, row[18]), host),
        })
    return {'schema_version': 1, 'market': 'US', 'queries': queries}


def resume(previous, receipts, batch_id):
    from .scan import checked_queries

    original = {row['line']: row for row in checked_queries(previous)}
    if not batch_id or batch_id == previous['batch_id']:
        raise ValueError('a continuation needs a new batch ID')
    seen, queries = set(), []
    for receipt in receipts:
        line = receipt['meitu_line']
        row = original.get(line)
        if (row is None or line in seen or receipt.get('batch_id') != previous['batch_id']
                or receipt.get('scope') != 'meitu_official_rediscovery_query' or receipt.get('market') != 'US'
                or any(receipt[key] != row[key] for key in ('meitu_id', 'brand', 'name', 'host'))):
            raise ValueError('continuation receipt does not match the launched query')
        seen.add(line)
        coverage = receipt['coverage']
        if row['host'] is None:
            continue
        if coverage['page_cursor'] != row.get('cursor', {}):
            raise ValueError('continuation receipt belongs to a different candidate page')
        current_variants = row.get('variant_cursors') or {}
        next_variants, checked_variants = {}, set()
        for candidate in receipt['candidates']:
            url = candidate['url']
            if current_variants and url not in current_variants:
                continue
            pagination = candidate['evidence'].get('variant_coverage')
            if not pagination:
                continue
            checked_variants.add(url)
            offset = 0 if pagination['status'] == 'restart_required' else pagination['next_offset']
            if offset is not None:
                next_variants[url] = {'offset': offset, 'sha256': pagination['sha256']}
        # A temporarily inaccessible PDP does not exhaust its previous cursor.
        next_variants.update({url: cursor for url, cursor in current_variants.items() if url not in checked_variants})

        next_query = {k: v for k, v in row.items() if k not in {'cursor', 'variant_cursors', 'retry_pdp_urls'}}
        if retry_urls := coverage.get('retry_pdp_urls'):
            next_query['retry_pdp_urls'] = retry_urls
        if coverage.get('candidate_restart_required', coverage['status'] == 'restart_required' and not next_variants):
            queries.append(next_query)
        elif next_variants:
            if page := coverage['page_cursor']:
                next_query['cursor'] = page
            next_query['variant_cursors'] = next_variants
            queries.append(next_query)
        elif next_cursor := coverage['next_cursor']:
            queries.append({**next_query, 'cursor': next_cursor})
        elif coverage.get('search_retry_required'):
            queries.append(next_query)
        elif coverage.get('failed_source_count') or retry_urls:
            queries.append({**next_query, **({'cursor': row['cursor']} if row.get('cursor') else {})})
    if seen != original.keys():
        raise ValueError('continuation needs a receipt for every launched query')
    output = {'schema_version': 1, 'market': 'US', 'batch_id': batch_id,
              'continues_batch_id': previous['batch_id'], 'queries': sorted(queries, key=lambda row: row['line'])}
    if queries:
        checked_queries(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--tracker', type=Path, help='path to the operator-supplied partner tracker JSON')
    source.add_argument('--resume', type=Path, help='previous launched manifest')
    parser.add_argument('--query-receipts', type=Path, help='JSON Lines receipts from that run')
    parser.add_argument('--batch-id', help='distinct ID for the output batch')
    args = parser.parse_args(argv)
    if args.resume:
        if not args.query_receipts or not args.batch_id:
            parser.error('--resume requires --query-receipts and --batch-id')
        receipts = [json.loads(line) for line in args.query_receipts.read_text().splitlines() if line]
        payload = resume(json.loads(args.resume.read_bytes()), receipts, args.batch_id)
    else:
        if args.query_receipts:
            parser.error('--query-receipts requires --resume')
        payload = build(json.loads(args.tracker.read_text()))
        if args.batch_id:
            payload['batch_id'] = args.batch_id
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
