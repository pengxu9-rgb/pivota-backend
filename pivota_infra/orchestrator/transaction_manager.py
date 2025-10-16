import uuid
from pivota_infra.db.database import database, transactions
from pivota_infra.utils.logger import logger
from datetime import datetime

async def create_transaction(merchant_id, amount, currency, meta=None):
    order_id = f"ORD_{uuid.uuid4().hex[:10]}"
    query = transactions.insert().values(
        order_id=order_id,
        merchant_id=merchant_id,
        amount=amount,
        currency=currency,
        status="pending",
        created_at=datetime.utcnow(),
        meta=meta or {}
    )
    tx_id = await database.execute(query)
    logger.info(f"created tx {order_id}")
    return order_id

async def update_transaction(order_id, **fields):
    q = transactions.update().where(transactions.c.order_id == order_id).values(**fields)
    await database.execute(q)
    logger.info(f"updated tx {order_id} fields {fields}")
