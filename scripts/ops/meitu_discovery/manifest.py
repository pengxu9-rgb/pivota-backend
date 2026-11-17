"""Build US first-party rediscovery queries for the 57 tracked Meitu originals."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


# US storefront approval is independent of whether a historic product still exists.
# The local official-platform probes accepted native product reads from the five
# Shopify hosts below. The Lime Crime site independently exposes its US/USD
# Shopify store, although these three lip lines may only be historical. Clarins
# and Charlotte have verified US pages, and Lord & Berry has a first-party /usa/ shop.
US_STOREFRONTS = {
    'M.A.C': ('www.maccosmetics.com', 'shopify'),
    'LORD&BERRY': ('www.lordandberry.com', 'other'),
    'Tarte Cosmetics': ('tartecosmetics.com', 'shopify'),
    'Flower Beauty': ('www.flowerbeauty.com', 'shopify'),
    'Lime Crime': ('limecrime.com', 'shopify'),
    'Stila': ('www.stilacosmetics.com', 'shopify'),
    'Clarins': ('www.clarinsusa.com', 'other'),
    'Surratt Beauty': ('www.surrattbeauty.com', 'shopify'),
    'Charlotte Tilbury': ('www.charlottetilbury.com', 'other'),
}

# These are search clues. A modern family or similar appearance can justify a
# discovery candidate, while claims about original product identity need review.
SEARCH_NAMES = {
    1: ('Amplified Crème Lipstick', 'Amplified Cream Lipstick', 'Amplified Lipstick'),
    2: ('Retro Matte Lipstick', 'Retro Matte'),
    3: ('Liptensity Lipstick', 'Lip Tensity Lipstick'),
    4: ('Matte Lipstick', 'M·A·Cximal Silky Matte Lipstick', 'MACximal Silky Matte Lipstick'),
    5: ('Cremesheen Lipstick', 'Creme Sheen Lipstick', 'M·A·Cximal Sleek Satin Lipstick'),
    6: ('Retro Matte Liquid Lipcolour', 'Retro Matte Liquid Lipcolor', 'Retro Matte Liquid Lip Color'),
    10: ('SEA H2O Lip Gloss', 'Rainforest of the Sea H2O Lip Gloss', 'maracuja juicy lip plump gloss'),
    11: ('Color Splash Hydrating Lipstick', 'Rainforest of the Sea Color Splash Lipstick'),
    12: ('Tarteist Lip Bling', 'Tarteist Lip Bling Liquid Lipstick'),
    13: ('Color Splash Shade Shifting Lipstick', 'lime coded maracuja juicy lip shift', 'maracuja juicy lip shift'),
    14: ('Lippie Lingerie Matte Tint', 'Lippie Lingerie Matte Lip Tint'),
    15: ('Perfect Pout Soft Matte Lip Color',),
    18: ('Tarteist Quick Dry Matte Lip Paint', 'Tarteist Quick Dry Lip Paint'),
    19: ('The Lip Sculptor Lipstick & Lipgloss', 'Lip Sculptor Double Ended Lipstick and Lip Gloss',
         'maracuja juicy lip sculptor lipstick & lip gloss'),
    20: ('The Lip Architect Lipstick & Liner', 'Lip Architect Double Ended Lipstick and Lip Liner'),
    41: ('Lip Perfector Peptide-Plumping + Hydrating Lip Gloss',),
    43: ('Joli Rouge Shine',),
}

# The frozen tracker sometimes carries a third-party URL even when the US brand
# has a known first-party product URL. Only that approved origin may be seeded.
FIRST_PARTY_ANCHORS = {
    44: 'https://www.clarinsusa.com/en/lip-comfort-oil-hydrating-and-plumping-lip-oil/80100468.html',
    45: 'https://www.surrattbeauty.com/products/lipslique',
    46: 'https://www.surrattbeauty.com/products/automatique-lip-crayon',
    48: 'https://www.charlottetilbury.com/us/product/hot-lips-lipstick-kim-k-w',
    49: 'https://www.charlottetilbury.com/us/product/matte-revolution-lipstick-pillowtalk',
}


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
    if host == 'www.charlottetilbury.com' and not parts.path.startswith('/us/product/'):
        return None
    if host == 'www.lordandberry.com' and not parts.path.startswith('/usa/'):
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
    if (len(lines) != 57 or set(numbers) != set(range(1, 58)) or len(set(ids)) != 57
            or not all(ids) or len({row[2] for row in lines}) != 17):
        raise ValueError('expected 57 distinct original Meitu lines across 17 brands')
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
    source.add_argument('--tracker', type=Path, help='path to the operator-supplied Meitu tracker JSON')
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
