"""Build an immutable US discovery input from the partner tracker and its evidence."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .scan import LOCALE, US_LOCALES


DATA_PATH = Path(__file__).resolve().parents[3] / "data" / "catalog_enrichment" / "us_discovery_storefronts.json"
_DATA = json.loads(DATA_PATH.read_text(encoding="utf-8"))["sources"]
NAMES = tuple(_DATA["evidence_files"])
HISTORICAL_OR_UNVERIFIED = set(_DATA["historical_or_unverified"])
EXCLUDED_HOSTS = frozenset(_DATA["excluded_hosts"])
BRAND_ALIASES = dict(_DATA["brand_aliases"])
HOST_PATH_PATTERNS = {host: re.compile(pattern) for host, pattern in _DATA["host_path_patterns"].items()}
EXTRA_CONTROL_URLS = tuple(_DATA["extra_control_urls"])
PINNED_HOST_COUNT = _DATA["pinned_host_count"]
SELECTION_PARAMS = {'color', 'd', 'id', 'select', 'sku', 'skuid', 'swatchcolor', 'swatchsize', 'variant'}
SHOPIFY_EVIDENCE = set(_DATA["shopify_evidence"])
PRODUCT_MARKERS = re.compile(
    r'/(?:products?|p|shop|s|o|dp|ip|product-lippenstift|aisles-online/p|en-US|en-us|usa|en)/',
    re.IGNORECASE,
)
VERIFIED_ROOT_PRODUCTS = {host: set(paths) for host, paths in _DATA["verified_root_products"].items()}


def load_evidence(directory):
    files = {name: (directory / name).read_bytes() for name in NAMES}
    return ({name: json.loads(raw) for name, raw in files.items()},
            {name: sha256(raw).hexdigest() for name, raw in files.items()})


def eligible_url(raw):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        url = urlsplit(raw)
    except ValueError:
        return None
    host = url.hostname or ''
    bare = host.removeprefix('www.')
    locale = LOCALE.match(url.path)
    product_path = PRODUCT_MARKERS.search(url.path) or url.path in VERIFIED_ROOT_PRODUCTS.get(bare, ())
    if (url.scheme != 'https' or url.netloc != host or not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', host)
            or bare in HISTORICAL_OR_UNVERIFIED or host in EXCLUDED_HOSTS
            or not product_path
            or (locale and locale[1].casefold() not in US_LOCALES)):
        return None
    path = url.path.removesuffix('.js')
    query = sorted((key, val) for key, val in parse_qsl(url.query)
                   if key.casefold() in SELECTION_PARAMS or key.startswith('dwvar_'))
    if host in HOST_PATH_PATTERNS and not HOST_PATH_PATTERNS[host].fullmatch(path):
        return None
    return urlunsplit(('https', BRAND_ALIASES.get(host, host), path, urlencode(query), ''))


def unique_add(sequence, value):
    if value not in sequence:
        sequence.append(value)


def build(evidence_directory, intake=None):
    data, hashes = load_evidence(evidence_directory)
    tracker, initial, expansion, retailer_data, pilot, original_44 = (data[name] for name in NAMES)
    anchors = {row[0]: {'meitu_line': row[0], 'meitu_id': row[1], 'meitu_brand': row[2],
                        'anchor_product': row[3], 'original_index_state': row[4]}
               for row in tracker['lines']}
    shopify = set(SHOPIFY_EVIDENCE)
    shopify.update(row[1] for row in tracker['channels'] if row[0] == 'US' and row[12] == 'Shopify')
    shopify.update(
        urlsplit(url).hostname for row in pilot['channels']
        if row.get('market') == 'US' and 'Shopify' in row.get('stock', '') and (url := eligible_url(row.get('pdp')))
    )
    shopify = {name.removeprefix('www.') for name in shopify}
    rows = {}

    def record(raw, source, status, **fields):
        if intake is not None:
            intake.append({'source': source, 'input_url': raw or None, 'status': status, **fields})

    def add(raw, *, source, relation, line=None, **fields):
        url = eligible_url(raw)
        if not url:
            return None
        addr = urlsplit(url)
        host = addr.hostname
        kind = 'public_pdp'
        if host.removeprefix('www.') in shopify and re.fullmatch(r'/(?:[a-z]{2}-[a-z]{2}/)?products/[^/]+/?', addr.path):
            kind = 'shopify_ajax'
            url = urlunsplit(addr._replace(query=''))
        item_id = 'pdp-' + sha256(url.encode()).hexdigest()[:20]
        row = rows.setdefault(url, {'item_id': item_id, 'url': url, 'host': host, 'source_kind': kind,
                                    'associations': [], 'source_urls': []})
        unique_add(row['source_urls'], raw)
        details = {'source': source, 'relation': relation, **anchors.get(line, {}),
                   **{key: value for key, value in fields.items() if value is not None and value != ''}}
        unique_add(row['associations'], details)
        return row

    def tracked(raw, *, source, relation, line=None, **fields):
        row = add(raw, source=source, relation=relation, line=line, **fields)
        status = 'queued_for_source_capture' if row else 'source_url_held' if raw else 'needs_product_url'
        record(raw, source, status, market='US', meitu_line=line, relation=relation, **fields,
               item_id=row['item_id'] if row else None, normalized_url=row['url'] if row else None)

    for row in tracker['lines']:
        tracked(row[18], source='tracker_original', relation='original_listing_candidate', line=row[0])
    for row in tracker['candidates']:
        line, _, market, channel, key, product, relation, dupe, _, shade, _, _, _, pivota_id, _, url, _ = row
        if market == 'US' and relation != '历史价格参考':
            tracked(url, source='tracker_relationship', relation=relation, line=line, relationship_channel=channel,
                    relationship_key=key, candidate_product=product, dupe_candidate=bool(dupe), shade_candidate=shade,
                    historic_pivota_id=pivota_id)
        else:
            record(url, 'tracker_relationship', 'historical_reference' if market == 'US' else 'out_of_market',
                   market=market, meitu_line=line, relation=relation, relationship_key=key,
                   candidate_product=product, dupe_candidate=bool(dupe))
    for row in tracker['channels']:
        market, host, seller, kind = row[:4]
        if market == 'US' and kind != 'Marketplace':
            tracked(row[15], source='tracker_channel', relation='channel_listing_candidate', seller=seller,
                    channel_type=kind, historic_channel_note=row[14], channel_host=host)
        else:
            record(row[15], 'tracker_channel', 'marketplace_held' if market == 'US' else 'out_of_market',
                   market=market, seller=seller, channel_type=kind, channel_host=host)
    for row in initial['evidence']:
        if row.get('match') not in {'marketplace', 'non_us'}:
            add(row.get('url'), source='anchor_evidence', relation=row.get('match') or 'original_listing_candidate',
                line=row['seq'], seller=row.get('seller'), historic_status=row.get('status'))
    for row in expansion['offers']:
        if row.get('currency', 'USD') == 'USD':
            add(row.get('url'), source='price_coverage_evidence', relation=row.get('match') or 'original_listing_candidate',
                line=row['seq'], seller=row.get('seller'), shade_candidate=row.get('shade'), gtin_candidate=row.get('gtin'),
                native_variant_candidate=row.get('variant_id'), shipping_source=row.get('shipping_url'),
                seller_review_source=row.get('shop_review_url'))
    for row in retailer_data['retailers']:
        if row.get('type') == 'direct retailer':
            add(row.get('example_url'), source='retailer_research', relation='retailer_listing_candidate',
                seller=row.get('seller'), seller_identity_source=row.get('identity_cross_url') or row.get('identity_url'),
                shipping_source=row.get('shipping_url'), seller_review_source=row.get('review_url'),
                prior_observed_merchant_id=row.get('observed_id'))
    for row in pilot['channels']:
        if row.get('market') == 'US':
            for line in row['seq']:
                add(row['pdp'], source='pilot_channel_research', relation='pilot_alternative_listing', line=line,
                    seller=row['seller'], seller_identity_source=row.get('identity_url'), shipping_source=row.get('shipping_url'))

    # Existing work is a control; these five families are not counted as new candidates.
    control_urls = {eligible_url(item['pdp']) for item in original_44['items']}
    control_urls.update(eligible_url(url) for url in EXTRA_CONTROL_URLS)
    for item in original_44['items']:
        add(item['pdp'], source='existing_cloud44_control', relation='existing_cloud44_listing_control',
            native_variant_candidate=item['native_variant_id'], gtin_candidate=item['gtin'],
            shipping_source=item.get('seller_sources', {}).get('shipping_candidate'))
    for row in rows.values():
        row['repeat_control'] = row['url'] in control_urls
        row['source_urls'].sort()
    items = sorted(rows.values(), key=lambda item: (item['repeat_control'], item['source_kind'], item['url']))
    count_by_adapter = Counter(item['source_kind'] for item in items)
    hosts = sorted({item['host'] for item in items})
    meitu_ids = {association['meitu_id'] for item in items for association in item['associations']
                 if 'meitu_id' in association}
    assert len(items) == len({item['item_id'] for item in items}) and len(items) > 100
    assert sum(item['host'] == PINNED_HOST_COUNT['host'] for item in items) == PINNED_HOST_COUNT['count']
    assert sum(item['repeat_control'] for item in items) >= 5
    return {'schema_version': 1, 'market': 'US', 'baseline_sha256': hashes, 'approved_hosts': hosts,
            'counts': {'candidate_urls': len(items) - sum(item['repeat_control'] for item in items),
                       'control_urls': sum(item['repeat_control'] for item in items), 'source_urls': len(items),
                       'storefront_hosts': len(hosts), 'meitu_lines_with_sources': len(meitu_ids),
                       PINNED_HOST_COUNT['count_key']: PINNED_HOST_COUNT['count'], 'per_source_kind': dict(sorted(count_by_adapter.items()))},
            'items': items}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--intake-ledger', type=Path)
    args = parser.parse_args()
    intake = [] if args.intake_ledger else None
    manifest = build(args.evidence_dir, intake)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n')
    if intake is not None:
        args.intake_ledger.write_text(json.dumps(intake, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(manifest['counts'], sort_keys=True))
