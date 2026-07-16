"""Multi-brand-retailer intake — crawl a retailer's catalog, then for each SKU
either propose attaching a retailer offer to an existing canonical product or
flag it as new-to-us.

See `services/retailer_ingest/sitemap_crawler.py` (generic sitemap fetch +
deterministic JSON-LD extraction) and per-retailer adapters (e.g.
`stylekorean.py`). The match decision uses
`services/pdp_matcher/retailer_match.py` (propose-then-attach). Offer writes go
through the sanctioned `scripts/attach_retailer_offer.py` path.
"""
