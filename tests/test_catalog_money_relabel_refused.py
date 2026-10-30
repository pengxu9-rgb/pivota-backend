from argparse import Namespace
import pytest
from scripts import backfill_offer_market_currency as script

@pytest.mark.asyncio
async def test_domain_base_currency_cannot_relabel_captured_money(monkeypatch):
 async def forbidden(*args,**kwargs):raise AssertionError('must refuse before DB or merchant access')
 monkeypatch.setattr(script.database,'connect',forbidden)
 monkeypatch.setattr(script,'fetch_storefront_meta',forbidden)
 with pytest.raises(ValueError,match='domain_currency_is_not_price_evidence'):
  await script._run(Namespace(apply=True))
