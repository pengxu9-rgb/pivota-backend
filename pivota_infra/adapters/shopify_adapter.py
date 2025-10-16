# Minimal demo adapter for Shopify - for prototype only
# In real world: implement OAuth, webhooks, order fetch, inventory checks

from pivota_infra.utils.logger import logger

async def get_product(merchant_id, sku):
    # MOCK: for prototype, return static product info
    return {"sku": sku, "title": "Demo Product", "price": 49.9, "stock": 10}

async def create_order(merchant_id, order_data):
    # Return mock order id
    order_id = f"ord_{merchant_id}_{order_data.get('items')[0].get('sku')}"
    logger.info(f"shopify mock create order {order_id}")
    return {"order_id": order_id, "status": "created"}
