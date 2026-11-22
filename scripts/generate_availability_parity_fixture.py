"""Regenerate tests/fixtures/availability_spellings_parity.json from utils.availability_vocabulary.

The gateway (PIVOTA-Agent) carries a byte-identical copy of the fixture; regenerate here, copy it there, and
change both in one go. Never hand-edit the fixture.

    python scripts/generate_availability_parity_fixture.py > tests/fixtures/availability_spellings_parity.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.availability_vocabulary import normalize_availability
base = ["in_stock","in stock","In Stock","in-stock","InStock","instock","IN_STOCK","available","Available",
 "in stock now","limited availability","LimitedAvailability","limited stock","limited_stock",
 "out_of_stock","out of stock","Out of Stock","out-of-stock","OutOfStock","OUT_OF_STOCK","outofstock",
 "sold out","sold_out","Sold Out","SoldOut","soldout","unavailable","Unavailable","not available","notavailable",
 "oos","OOS","reserved","Reserved","discontinued","Discontinued",
 "Temporarily out of stock","Out Of Stock - notify me","no longer available","no longer in stock","not in stock",
 "Sold out of the old model, but in stock now",
 "backorder","back order","backordered","on backorder","onbackorder","BackOrder","preorder","pre-order","PreOrder",
 "presale","made to order","MadeToOrder","online only","OnlineOnly","in store only","InStoreOnly",
 "unknown","Unknown","","  ","n/a","active","true","yes","1","0","choose","preserved",
 "https://example.com/?x=schema.org/InStock"]
iris = []
for t in ["InStock","OutOfStock","SoldOut","Discontinued","LimitedAvailability","PreOrder","BackOrder",
          "InStoreOnly","OnlineOnly","Reserved","PreSale","MadeToOrder"]:
    for host in ["https://schema.org/","http://schema.org/","https://www.schema.org/"]:
        iris.append(host+t)
cases = [{"input": s, "expected": normalize_availability(s)} for s in base+iris]
cases += [{"input": True, "expected": normalize_availability(True)}, {"input": False, "expected": normalize_availability(False)},
          {"input": None, "expected": normalize_availability(None)}]
out = {"_comment": "Generated from pivota-backend utils/availability_vocabulary.normalize_availability. Expected: in_stock | out_of_stock | null (unknown). The gateway must classify every input identically; regenerate here, never hand-edit.",
       "cases": cases}
print(json.dumps(out, indent=2, ensure_ascii=True))
