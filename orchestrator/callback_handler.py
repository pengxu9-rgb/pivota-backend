from db.database import database, transactions
from utils.logger import logger

async def handle_psp_webhook(payment_intent_id, status, psp, psp_txn_id):
    # find transaction by metadata (in prototype we may store mapping)
    query = transactions.select().where(transactions.c.meta["payment_intent_id"].as_string() == payment_intent_id)
    row = await database.fetch_one(query)
    if not row:
        logger.warning("transaction not found for intent %s", payment_intent_id)
        return None
    await database.execute(transactions.update().where(transactions.c.order_id == row["order_id"]).values(
        status=status, psp=psp, psp_txn_id=psp_txn_id
    ))
    logger.info("psp callback handled for %s -> %s", row["order_id"], status)
    return row["order_id"]
