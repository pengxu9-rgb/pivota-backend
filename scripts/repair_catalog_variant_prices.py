"""Dry-run first; apply only the exact reviewed manifest against a pinned catalog database."""
import argparse
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.repair_missing_variant_offers import TARGETS,validate_target
CONTRACT='repair-native-variant-money-v1'

async def run(environment,apply=False,expected_hash=None):
    validate_target(environment)
    from db.database import database
    from services.catalog_variant_price_repair import repair_variant_prices
    await database.connect()
    try:
        identity=await database.fetch_one('SELECT current_database() AS db,host(inet_server_addr()) AS addr')
        if (identity['addr'],identity['db'])!=TARGETS[environment]:
            raise RuntimeError('catalog_server_target_refused')
        async with database.transaction():
            await database.execute("SET LOCAL statement_timeout='10000'")
            await database.execute("SET LOCAL lock_timeout='2000'")
            return {'environment':environment,'contract':CONTRACT,**await repair_variant_prices(database,apply=apply,expected_hash=expected_hash)}
    finally:await database.disconnect()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--environment',choices=sorted(TARGETS),default='staging')
    p.add_argument('--apply',action='store_true')
    p.add_argument('--expect-contract')
    p.add_argument('--expect-plan-sha256')
    a=p.parse_args()
    if a.apply and (a.expect_contract!=CONTRACT or not a.expect_plan_sha256):
        p.error('apply requires the contract and reviewed plan SHA256')
    result=asyncio.run(run(a.environment,a.apply,a.expect_plan_sha256))
    # Keep each manifest row below the log record size limit.
    for row in result.pop('manifest'):print(json.dumps({'kind':'catalog_variant_price_repair_manifest','row':row}),flush=True)
    print(json.dumps(result,sort_keys=True),flush=True)

if __name__=='__main__':main()
