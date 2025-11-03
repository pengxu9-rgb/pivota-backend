from fastapi import APIRouter
from adapters.shopify_adapter import get_product, create_order
from models.schemas import Item
from utils.logger import logger

router = APIRouter(prefix="/merchant", tags=["merchant"])

@router.get("/product/{merchant_id}/{sku}")
async def product_info(merchant_id: str, sku: str):
    product = await get_product(merchant_id, sku)
    return product

@router.post("/order/{merchant_id}")
async def create_merchant_order(merchant_id: str, items: list[Item]):
    # create order in merchant system - prototype: mock
    order = await create_order(merchant_id, {"items": [i.dict() for i in items]})
    return order
