from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mvp.ops import InMemoryOpsPackStore
from mvp.schemas import (
    EvidencePointer,
    Geo,
    RecommendationClaim,
    RecommendationContext,
    RecommendationItem,
    RecommendationPack,
)


def _now():
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_ops_pack_validate_fails_when_required_fields_missing():
    store = InMemoryOpsPackStore()
    pack = RecommendationPack(
        pack_id="",
        created_at=_now(),
        context=RecommendationContext(session_id="s1", geo=Geo(country="US"), surface="gateway"),
        recommendations=[
            RecommendationItem(
                offer_id="offer_1",
                title="Title",
                narrative="",
                claims=[],
                authorized_signals=[],
            )
        ],
    )

    rec = await store.create_draft(merchant_id="merch_1", pack=pack)
    assert rec.status == "draft"
    with pytest.raises(ValueError):
        await store.transition(pack_id=rec.pack_id, target_status="validated")


@pytest.mark.asyncio
async def test_ops_pack_lifecycle_validated_then_published():
    store = InMemoryOpsPackStore()
    pack = RecommendationPack(
        pack_id="",
        created_at=_now(),
        context=RecommendationContext(session_id="s1", geo=Geo(country="US"), surface="gateway"),
        recommendations=[
            RecommendationItem(
                offer_id="offer_1",
                title="Title",
                narrative="Short narrative",
                claims=[
                    RecommendationClaim(
                        claim="Claim",
                        confidence=0.5,
                        evidence=[EvidencePointer(type="product_cache", ref={"platform_product_id": "p1"})],
                    )
                ],
                authorized_signals=[],
            )
        ],
    )

    rec = await store.create_draft(merchant_id="merch_1", pack=pack)
    v = await store.transition(pack_id=rec.pack_id, target_status="validated")
    assert v.status == "validated"
    p = await store.transition(pack_id=rec.pack_id, target_status="published")
    assert p.status == "published"

