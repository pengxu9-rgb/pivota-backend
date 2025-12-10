"""
Similarity Service

Provides a pluggable interface to fetch similar product IDs. The handler
is responsible for loading full product objects and applying any ranking
boosts (e.g., same_merchant_first).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from db.database import database
from models.standard_product import StandardProduct, ProductStatus

SimilarityStrategy = str  # "content_embedding" | "co_view" | "same_merchant_first"


@dataclass
class SimilarCandidate:
    productId: str
    score: Optional[float] = None  # normalized 0..1 if available


class SimilarityService:
    """Basic, replaceable similarity service with multiple strategies."""

    def __init__(self, max_pool_size: int = 200):
        self.max_pool_size = max_pool_size

    async def hasCoViewData(self, product_id: str) -> bool:
        """Placeholder: no co-view data yet."""
        return False

    async def findSimilar(
        self,
        input: dict,
    ) -> List[SimilarCandidate]:
        """
        Dispatch by strategy. Expects:
        {
          baseProductId: string,
          limit: number,
          strategy: SimilarityStrategy,
          userId?: string
        }
        """
        base_product = await self._load_base_product(input.get("baseProductId"))
        if not base_product:
            return []

        strategy: SimilarityStrategy = input.get("strategy") or "content_embedding"
        if strategy == "co_view":
            return await self._find_similar_by_co_view(input, base_product)
        # same_merchant_first uses content similarity; handler will apply merchant boost.
        return await self._find_similar_by_content(input, base_product)

    async def _find_similar_by_content(
        self,
        input: dict,
        base_product: StandardProduct,
    ) -> List[SimilarCandidate]:
        """
        Three-level recall:
        1) Same category + price band
        2) Same category, no price band
        3) Broad text fallback
        """
        limit = int(input.get("limit") or 10)
        fetch_size = min(limit * 5, self.max_pool_size)
        base_pid = base_product.product_id or base_product.id
        base_price = base_product.price or 0
        base_type = (base_product.product_type or "").lower()

        base_tokens = self._tokenize(f"{base_product.title} {base_product.product_type or ''}")
        text_query = f"{base_product.title or ''} {base_product.product_type or ''}".strip()

        def score_candidate(sp: StandardProduct) -> float:
            tokens = self._tokenize(f"{sp.title} {sp.product_type or ''}")
            overlap = len(base_tokens & tokens)
            denom = max(len(base_tokens | tokens), 1)
            token_score = overlap / denom
            bonus = 0.0
            if base_type and sp.product_type and base_type == sp.product_type.lower():
                bonus += 0.1
            return round(min(1.0, token_score + bonus), 3)

        def build_candidates(pool: List[StandardProduct]) -> List[SimilarCandidate]:
            out: List[SimilarCandidate] = []
            seen = set()
            for sp in pool:
                pid = sp.product_id or sp.id
                if not pid or pid == base_pid or pid in seen:
                    continue
                if sp.status and sp.status != ProductStatus.ACTIVE:
                    continue
                out.append(SimilarCandidate(productId=pid, score=score_candidate(sp)))
                seen.add(pid)
                if len(out) >= fetch_size:
                    break
            return out

        # Level 1: same category + price band
        level1_pool = await self._search_candidates_content(
            base_product,
            fetch_size,
            require_same_category=True,
            price_band=(0.7 * base_price if base_price else None, 1.4 * base_price if base_price else None),
            text_query=text_query,
        )
        level1_candidates = build_candidates(level1_pool)
        if len(level1_candidates) >= limit:
            return level1_candidates

        # Level 2: same category, no price band
        level2_pool = await self._search_candidates_content(
            base_product,
            fetch_size,
            require_same_category=True,
            price_band=None,
            text_query=text_query,
        )
        level2_candidates = build_candidates(level2_pool)
        if len(level2_candidates) >= limit:
            return level2_candidates

        # Level 3: broad fallback (no category filter)
        level3_pool = await self._search_candidates_content(
            base_product,
            fetch_size,
            require_same_category=False,
            price_band=None,
            text_query=text_query,
        )
        level3_candidates = build_candidates(level3_pool)
        return level3_candidates[:fetch_size]

    async def _find_similar_by_co_view(
        self,
        input: dict,
        base_product: StandardProduct,
    ) -> List[SimilarCandidate]:
        # TODO: replace with real co-view/co-purchase signals.
        return await self._find_similar_by_content(input, base_product)

    async def _search_candidates_content(
        self,
        base_product: StandardProduct,
        limit: int,
        require_same_category: bool,
        price_band: Optional[Tuple[Optional[float], Optional[float]]],
        text_query: str,
    ) -> List[StandardProduct]:
        """
        Fetch candidate products from cache with optional category/price filters.
        """
        params = {
            "limit": limit,
        }
        where_clauses = ["1=1"]
        if require_same_category:
            params["ptype"] = (base_product.product_type or "").lower()
            where_clauses.append("LOWER(product_data->>'product_type') = :ptype")
        if price_band and price_band[0] is not None and price_band[1] is not None:
            params["pmin"] = price_band[0]
            params["pmax"] = price_band[1]
            where_clauses.append(
                "(CAST(product_data->>'price' AS FLOAT) BETWEEN :pmin AND :pmax)"
            )

        where_sql = " AND ".join(where_clauses)
        query = f"""
        SELECT product_data
        FROM products_cache
        WHERE {where_sql}
        ORDER BY cached_at DESC
        LIMIT :limit
        """
        rows = []
        try:
            rows = await database.fetch_all(query, params)
        except Exception:
            rows = []

        products: List[StandardProduct] = []
        for row in rows:
            try:
                pdata = row.get("product_data") or row
                sp = StandardProduct.parse_obj(pdata)
                products.append(sp)
            except Exception:
                continue

        # If nothing found and category was required, try without category as a fallback within this level
        if not products and require_same_category:
            try:
                rows = await database.fetch_all(
                    """
                    SELECT product_data FROM products_cache
                    ORDER BY cached_at DESC
                    LIMIT :limit
                    """,
                    {"limit": limit},
                )
                for row in rows:
                    try:
                        pdata = row.get("product_data") or row
                        sp = StandardProduct.parse_obj(pdata)
                        products.append(sp)
                    except Exception:
                        continue
            except Exception:
                return []

        return products

    async def _load_base_product(self, product_id: Optional[str]) -> Optional[StandardProduct]:
        if not product_id:
            return None
        queries = [
            """
            SELECT product_data
            FROM products_cache
            WHERE product_data->>'product_id' = :pid
            LIMIT 1
            """,
            """
            SELECT product_data
            FROM products_cache
            WHERE platform_product_id = :pid
            LIMIT 1
            """,
        ]
        for q in queries:
            try:
                row = await database.fetch_one(q, {"pid": product_id})
                if row and "product_data" in row:
                    return StandardProduct.parse_obj(row["product_data"])
            except Exception:
                continue
        return None

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


# Singleton instance
similarity_service = SimilarityService()
