"""
Amazon Feeds - Track Amazon SP-API Feed submissions for order fulfillment and other updates.
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, JSON, ARRAY, Text
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, Any, Optional, List
from datetime import datetime


amazon_feeds = Table(
    "amazon_feeds",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("feed_type", String(100), nullable=False),
    Column("feed_id", String(100), unique=True),
    Column("feed_document_id", String(100)),
    
    # Feed submission details
    Column("submission_data", JSON, nullable=False),
    Column("submission_result", JSON),
    
    # Status tracking
    Column("status", String(50), nullable=False, server_default="pending"),
    Column("processing_status", String(50)),
    
    # Related entities
    Column("related_orders", ARRAY(Text)),
    
    # Timestamps
    Column("submitted_at", DateTime(timezone=True)),
    Column("started_processing_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


async def create_amazon_feed(
    merchant_id: str,
    feed_type: str,
    submission_data: Dict[str, Any],
    related_orders: Optional[List[str]] = None,
) -> int:
    """
    Create a new Amazon feed record.
    
    Args:
        merchant_id: Merchant identifier
        feed_type: Type of feed (e.g., 'POST_ORDER_FULFILLMENT_DATA')
        submission_data: Feed payload data
        related_orders: List of order IDs affected by this feed
        
    Returns:
        ID of created feed record
    """
    query = amazon_feeds.insert().values(
        merchant_id=merchant_id,
        feed_type=feed_type,
        submission_data=submission_data,
        related_orders=related_orders or [],
        status="pending",
    )
    
    feed_id = await database.execute(query)
    return int(feed_id)


async def update_amazon_feed(
    feed_id: int,
    amazon_feed_id: Optional[str] = None,
    feed_document_id: Optional[str] = None,
    status: Optional[str] = None,
    processing_status: Optional[str] = None,
    submission_result: Optional[Dict[str, Any]] = None,
    submitted_at: Optional[datetime] = None,
    started_processing_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
) -> bool:
    """
    Update an Amazon feed record.
    
    Returns:
        True if updated successfully
    """
    values = {}
    
    if amazon_feed_id is not None:
        values["feed_id"] = amazon_feed_id
    if feed_document_id is not None:
        values["feed_document_id"] = feed_document_id
    if status is not None:
        values["status"] = status
    if processing_status is not None:
        values["processing_status"] = processing_status
    if submission_result is not None:
        values["submission_result"] = submission_result
    if submitted_at is not None:
        values["submitted_at"] = submitted_at
    if started_processing_at is not None:
        values["started_processing_at"] = started_processing_at
    if completed_at is not None:
        values["completed_at"] = completed_at
    
    if not values:
        return False
    
    values["updated_at"] = func.now()
    
    query = amazon_feeds.update().where(
        amazon_feeds.c.id == feed_id
    ).values(**values)
    
    result = await database.execute(query)
    return result > 0


async def get_amazon_feed(feed_id: int) -> Optional[Dict[str, Any]]:
    """
    Get a feed by ID.
    
    Returns:
        Feed record or None if not found
    """
    query = amazon_feeds.select().where(
        amazon_feeds.c.id == feed_id
    )
    
    result = await database.fetch_one(query)
    return dict(result) if result else None


async def get_amazon_feed_by_amazon_id(amazon_feed_id: str) -> Optional[Dict[str, Any]]:
    """
    Get a feed by Amazon's feed ID.
    
    Returns:
        Feed record or None if not found
    """
    query = amazon_feeds.select().where(
        amazon_feeds.c.feed_id == amazon_feed_id
    )
    
    result = await database.fetch_one(query)
    return dict(result) if result else None


async def list_amazon_feeds(
    merchant_id: str,
    feed_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    List feeds for a merchant with optional filters.
    
    Returns:
        List of feed records
    """
    query = amazon_feeds.select().where(
        amazon_feeds.c.merchant_id == merchant_id
    )
    
    if feed_type:
        query = query.where(amazon_feeds.c.feed_type == feed_type)
    
    if status:
        query = query.where(amazon_feeds.c.status == status)
    
    query = query.order_by(amazon_feeds.c.created_at.desc())
    query = query.limit(limit).offset(offset)
    
    results = await database.fetch_all(query)
    return [dict(row) for row in results]


async def get_feed_by_order(
    merchant_id: str,
    order_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Get the most recent feed related to a specific order.
    
    Returns:
        Feed record or None if not found
    """
    query = amazon_feeds.select().where(
        (amazon_feeds.c.merchant_id == merchant_id) &
        (amazon_feeds.c.related_orders.contains([order_id]))
    ).order_by(amazon_feeds.c.created_at.desc()).limit(1)
    
    result = await database.fetch_one(query)
    return dict(result) if result else None
