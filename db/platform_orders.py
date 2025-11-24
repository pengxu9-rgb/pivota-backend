"""
Platform Orders - side-car cache for imported platform orders (Amazon/Temu).
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, JSON
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, Any, Optional, List


platform_orders = Table(
    "platform_orders",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("platform", String(50), nullable=False, index=True),
    Column("order_id", String(100), nullable=False, index=True),
    Column("order_item_id", String(100), nullable=True),
    Column("data", JSON, nullable=False),
    Column("import_task_id", Integer, nullable=True, index=True),
    Column("created_at", DateTime, server_default=func.now(), index=True),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)


async def insert_platform_order(
    merchant_id: str,
    platform: str,
    order_id: str,
    order_item_id: Optional[str],
    data: Dict[str, Any],
    import_task_id: Optional[int] = None,
) -> Optional[int]:
    """
    Insert a platform order row. Returns inserted id or None on conflict.
    """
    query = platform_orders.insert().values(
        merchant_id=merchant_id,
        platform=platform,
        order_id=order_id,
        order_item_id=order_item_id,
        data=data,
        import_task_id=import_task_id,
    )
    try:
        inserted_id = await database.execute(query)
        return int(inserted_id)
    except Exception:
        # Ignore conflicts or other insert errors; caller should count failure.
        return None


async def list_orders_by_task(import_task_id: int) -> List[Dict[str, Any]]:
    """List all orders from a specific import task."""
    query = platform_orders.select().where(platform_orders.c.import_task_id == import_task_id)
    rows = await database.fetch_all(query)
    return [dict(r) for r in rows]


async def get_platform_order_by_id(order_id: int) -> Optional[Dict[str, Any]]:
    """Get a platform order by its internal ID."""
    query = platform_orders.select().where(platform_orders.c.id == order_id)
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def update_platform_order_data(
    order_id: int,
    updates: Dict[str, Any]
) -> bool:
    """
    Update data fields for a platform order.
    
    Args:
        order_id: Internal platform order ID
        updates: Dictionary of fields to update in data JSONB
    
    Returns:
        True if successful
    """
    import json
    
    # Fetch current data
    current = await get_platform_order_by_id(order_id)
    if not current:
        return False
    
    # Merge updates into existing data
    new_data = current["data"].copy()
    new_data.update(updates)
    
    # Update the record
    query = platform_orders.update().where(
        platform_orders.c.id == order_id
    ).values(
        data=new_data,
        updated_at=func.now()
    )
    
    result = await database.execute(query)
    return result > 0


async def update_platform_order_by_platform_id(
    platform_order_id: str,
    updates: Dict[str, Any]
) -> bool:
    """
    Update data fields for a platform order by platform's order ID.
    
    Args:
        platform_order_id: Platform's order ID (e.g., AMZ-001)
        updates: Dict of fields to update in data JSONB
    
    Returns:
        True if successful
    """
    import json
    
    for key, value in updates.items():
        query = f"""
            UPDATE platform_orders
            SET data = jsonb_set(data, '{{{key}}}', $1, true),
                updated_at = NOW()
            WHERE order_id = $2
        """
        await database.execute(query, [json.dumps(value), platform_order_id])
    
    return True


async def list_orders_for_merchant(
    merchant_id: str,
    platform: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    List orders for a merchant with optional platform filter.
    Uses SQLAlchemy Table API for better compatibility.
    """
    # Build base query
    query = platform_orders.select().where(platform_orders.c.merchant_id == merchant_id)
    
    if platform:
        query = query.where(platform_orders.c.platform == platform)
    
    # Get total count using a simpler approach
    all_rows = await database.fetch_all(query)
    total_count = len(all_rows)
    
    # Apply ordering and pagination
    paged_query = query.order_by(platform_orders.c.created_at.desc()).limit(limit).offset(offset)
    rows = await database.fetch_all(paged_query)
    orders = [dict(r) for r in rows]
    
    return {
        "orders": orders,
        "total": total_count,
        "limit": limit,
        "offset": offset,
        "platform": platform or "all",
    }


async def get_platform_orders(
    merchant_id: str,
    platform: Optional[str] = None,
    order_ids: Optional[List[str]] = None,
    limit: int = 100,
    offset: int = 0,
    since: Optional[DateTime] = None,
) -> List[Dict[str, Any]]:
    """
    Get platform orders with flexible filtering.
    
    Args:
        merchant_id: Merchant identifier
        platform: Optional platform filter (e.g., 'amazon')
        order_ids: Optional list of order IDs to filter
        limit: Maximum results
        offset: Result offset
        since: Optional datetime filter for created_at
        
    Returns:
        List of order records
    """
    # Build query
    query = platform_orders.select().where(
        platform_orders.c.merchant_id == merchant_id
    )
    
    if platform:
        query = query.where(platform_orders.c.platform == platform)
    
    if order_ids:
        query = query.where(platform_orders.c.order_id.in_(order_ids))
    
    if since:
        query = query.where(platform_orders.c.created_at >= since)

    query = query.order_by(platform_orders.c.created_at.desc())
    query = query.limit(limit).offset(offset)

    results = await database.fetch_all(query)
    return [dict(row) for row in results]


async def update_platform_order_fulfillment(
    merchant_id: str,
    order_id: str,
    fulfillment_status: str,
    shipment_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Update fulfillment status and shipment data for an order.
    
    Args:
        merchant_id: Merchant identifier
        order_id: Order ID
        fulfillment_status: New fulfillment status
        shipment_data: Optional shipment tracking data
        
    Returns:
        True if updated successfully
    """
    # Build update values
    values = {
        "fulfillment_status": fulfillment_status,
        "updated_at": func.now(),
    }
    
    if fulfillment_status == "fulfilled":
        values["fulfilled_at"] = func.now()
    
    # Merge shipment data if provided
    if shipment_data:
        # Get current order to merge shipment data
        query = platform_orders.select().where(
            (platform_orders.c.merchant_id == merchant_id) &
            (platform_orders.c.order_id == order_id)
        ).limit(1)
        
        result = await database.fetch_one(query)
        if result:
            current_shipment = result.get("shipment_data") or {}
            current_shipment.update(shipment_data)
            values["shipment_data"] = current_shipment
    
    # Update the order
    update_query = platform_orders.update().where(
        (platform_orders.c.merchant_id == merchant_id) &
        (platform_orders.c.order_id == order_id)
    ).values(**values)
    
    result = await database.execute(update_query)
    return result > 0