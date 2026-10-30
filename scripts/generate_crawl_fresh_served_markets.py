"""Write the CRAWL_FRESH served (market, currency) pairs the gateway twin pins against.

    python scripts/generate_crawl_fresh_served_markets.py config/crawl_fresh_served_markets.json
    python scripts/generate_crawl_fresh_served_markets.py --check config/crawl_fresh_served_markets.json

The output is services.catalog_row_trust_upserter.CRAWL_SERVED_MARKET_CURRENCY_PAIRS (the registry's
served markets x region_pricing's currencies) as stable JSON. The committed copy is checked by
tests/test_catalog_row_trust_upserter_crawl_fresh.py, so a change to the registry's served set fails
here first; the gateway copies the file and pins its own CRAWL_FRESH statement to it. Never hand-edit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def served_markets_json() -> str:
    from services.catalog_row_trust_upserter import CRAWL_SERVED_MARKET_CURRENCY_PAIRS

    return json.dumps(
        {
            "version": 1,
            "generated_from": "pivota-backend services.catalog_row_trust_upserter.CRAWL_SERVED_MARKET_CURRENCY_PAIRS",
            "served_market_currency": [list(pair) for pair in CRAWL_SERVED_MARKET_CURRENCY_PAIRS],
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


def main(argv) -> int:
    check = "--check" in argv
    paths = [a for a in argv if a != "--check"]
    if len(paths) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(paths[0])
    want = served_markets_json()
    if check:
        have = target.read_text(encoding="utf-8") if target.exists() else ""
        if have != want:
            print(f"{target} is stale: regenerate with scripts/generate_crawl_fresh_served_markets.py",
                  file=sys.stderr)
            return 1
        print(f"{target} matches")
        return 0
    target.write_text(want, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
