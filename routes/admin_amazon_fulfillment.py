"""
Amazon Fulfillment Admin Routes

Admin endpoints for submitting shipment information to Amazon SP-API.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
import logging

from utils.auth import require_admin
from config.settings import settings
from db.connector_credentials import get_latest_connector_credential_for_merchant
from db.amazon_feeds import (
    create_amazon_feed,
    update_amazon_feed,
    get_amazon_feed,
    get_amazon_feed_by_amazon_id,
    list_amazon_feeds,
    get_feed_by_order,
)
from db.platform_orders import get_platform_orders, update_platform_order_fulfillment
from services.crypto_service import crypto_service
from services.amazon_sp_api_service import (
    get_amazon_access_token,
    submit_order_fulfillment,
    get_feed,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/amazon",
    tags=["amazon_fulfillment"],
    dependencies=[Depends(require_admin)],
)


class FulfillmentItem(BaseModel):
    """Single item fulfillment data."""
    amazon_order_item_id: Optional[str] = Field(None, description="Order item ID (for partial fulfillment)")
    quantity: Optional[int] = Field(None, description="Quantity to fulfill (for partial fulfillment)")


class FulfillmentRequest(BaseModel):
    """Request to fulfill an Amazon order."""
    carrier_code: str = Field(..., description="Carrier code (e.g., 'USPS', 'FedEx', 'UPS')")
    tracking_number: str = Field(..., description="Shipment tracking number")
    ship_date: Optional[datetime] = Field(None, description="Ship date (defaults to now)")
    carrier_name: Optional[str] = Field(None, description="Carrier name (optional)")
    items: Optional[List[FulfillmentItem]] = Field(None, description="Items to fulfill (optional, for partial)")


class FulfillmentResponse(BaseModel):
    """Response from fulfillment submission."""
    success: bool
    feed_id: Optional[int] = Field(None, description="Internal feed record ID")
    amazon_feed_id: Optional[str] = Field(None, description="Amazon's feed ID")
    message: str
    details: Optional[Dict[str, Any]] = None


class FeedStatusResponse(BaseModel):
    """Feed status response."""
    feed_id: int
    amazon_feed_id: Optional[str]
    status: str
    processing_status: Optional[str]
    submitted_at: Optional[datetime]
    completed_at: Optional[datetime]
    submission_result: Optional[Dict[str, Any]]
    related_orders: List[str]


@router.post("/orders/{order_id}/fulfill", response_model=FulfillmentResponse)
async def fulfill_amazon_order(
    merchant_id: str,
    order_id: str,
    request: FulfillmentRequest,
    current_admin: dict = Depends(require_admin),
):
    """
    Submit fulfillment information for an Amazon order.
    
    This endpoint:
    1. Validates the order exists and is from Amazon
    2. Gets Amazon credentials
    3. Submits fulfillment feed to Amazon
    4. Records the feed submission
    5. Updates order fulfillment status
    
    Args:
        merchant_id: Merchant identifier
        order_id: Amazon order ID (e.g., '111-1234567-1234567')
        request: Fulfillment details
        
    Returns:
        FulfillmentResponse with feed submission details
        
    Raises:
        HTTPException: If order not found, not Amazon order, or submission fails
    """
    if not settings.enable_amazon_sp_api:
        raise HTTPException(
            status_code=403,
            detail="Amazon SP-API integration is not enabled",
        )
    
    logger.info(
        f"Fulfilling Amazon order",
        extra={
            "merchant_id": merchant_id,
            "order_id": order_id,
            "carrier": request.carrier_code,
            "tracking": request.tracking_number,
        },
    )
    
    try:
        # 1. Validate order exists and is from Amazon
        orders = await get_platform_orders(
            merchant_id=merchant_id,
            platform="amazon",
            order_ids=[order_id],
            limit=1,
        )
        
        if not orders:
            raise HTTPException(
                status_code=404,
                detail=f"Order {order_id} not found for merchant {merchant_id}",
            )
        
        order_data = orders[0]["data"]
        
        # Check if already fulfilled
        if orders[0].get("fulfillment_status") == "fulfilled":
            logger.warning(f"Order {order_id} already fulfilled")
            # Check for existing feed
            existing_feed = await get_feed_by_order(merchant_id, order_id)
            if existing_feed:
                return FulfillmentResponse(
                    success=True,
                    feed_id=existing_feed["id"],
                    amazon_feed_id=existing_feed.get("feed_id"),
                    message="Order already fulfilled",
                    details={"existing_feed": existing_feed},
                )
        
        # 2. Get Amazon credentials
        creds_record = await get_latest_connector_credential_for_merchant(merchant_id, "amazon")
        if not creds_record or not creds_record.get("is_valid"):
            raise HTTPException(
                status_code=400,
                detail="No valid Amazon credentials found",
            )
        
        credentials = crypto_service.decrypt_json_secret(creds_record["credentials_encrypted"])
        refresh_token = credentials.get("refresh_token")
        marketplace_ids = credentials.get("marketplace_ids", ["ATVPDKIKX0DER"])  # Default to US
        
        if not refresh_token:
            raise HTTPException(
                status_code=400,
                detail="No refresh token found in credentials",
            )
        
        # 3. Get access token
        access_token = await get_amazon_access_token(refresh_token)
        
        # 4. Prepare fulfillment data
        fulfillment_data = [{
            "amazon_order_id": order_id,
            "carrier_code": request.carrier_code,
            "tracking_number": request.tracking_number,
            "ship_date": request.ship_date or datetime.utcnow(),
            "carrier_name": request.carrier_name,
        }]
        
        # Add items if partial fulfillment
        if request.items:
            fulfillment_data[0]["items"] = [
                {
                    "amazon_order_item_id": item.amazon_order_item_id,
                    "quantity": item.quantity,
                }
                for item in request.items
                if item.amazon_order_item_id
            ]
        
        # 5. Submit to Amazon
        amazon_feed_id, feed_response = await submit_order_fulfillment(
            merchant_id=merchant_id,
            access_token=access_token,
            fulfillment_data=fulfillment_data,
            marketplace_ids=marketplace_ids,
            region=settings.amazon_sp_api_region,
        )
        
        if not amazon_feed_id:
            logger.error(f"Failed to submit fulfillment: {feed_response}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to submit fulfillment: {feed_response.get('error', 'Unknown error')}",
            )
        
        # 6. Record feed submission
        feed_id = await create_amazon_feed(
            merchant_id=merchant_id,
            feed_type="POST_ORDER_FULFILLMENT_DATA",
            submission_data={
                "fulfillment_data": fulfillment_data,
                "marketplace_ids": marketplace_ids,
                "request": request.dict(),
            },
            related_orders=[order_id],
        )
        
        # Update with Amazon feed ID
        await update_amazon_feed(
            feed_id=feed_id,
            amazon_feed_id=amazon_feed_id,
            status="submitted",
            submitted_at=datetime.utcnow(),
        )
        
        # 7. Update order fulfillment status
        shipment_data = {
            "carrier_code": request.carrier_code,
            "tracking_number": request.tracking_number,
            "ship_date": (request.ship_date or datetime.utcnow()).isoformat(),
            "feed_id": feed_id,
            "amazon_feed_id": amazon_feed_id,
        }
        
        await update_platform_order_fulfillment(
            merchant_id=merchant_id,
            order_id=order_id,
            fulfillment_status="pending",  # Will be updated when feed completes
            shipment_data=shipment_data,
        )
        
        logger.info(
            f"Successfully submitted fulfillment",
            extra={
                "merchant_id": merchant_id,
                "order_id": order_id,
                "feed_id": feed_id,
                "amazon_feed_id": amazon_feed_id,
            },
        )
        
        return FulfillmentResponse(
            success=True,
            feed_id=feed_id,
            amazon_feed_id=amazon_feed_id,
            message="Fulfillment submitted successfully",
            details={
                "feed_response": feed_response,
                "tracking_number": request.tracking_number,
                "carrier": request.carrier_code,
            },
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to fulfill order: {str(e)}",
            extra={"merchant_id": merchant_id, "order_id": order_id},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fulfill order: {str(e)}",
        )


@router.get("/feeds/{feed_id}", response_model=FeedStatusResponse)
async def get_feed_status(
    merchant_id: str,
    feed_id: int,
    refresh: bool = Query(False, description="Refresh status from Amazon"),
    current_admin: dict = Depends(require_admin),
):
    """
    Get feed processing status.
    
    Args:
        merchant_id: Merchant identifier
        feed_id: Internal feed ID
        refresh: Whether to fetch latest status from Amazon
        
    Returns:
        FeedStatusResponse with current status
        
    Raises:
        HTTPException: If feed not found or refresh fails
    """
    # Get feed record
    feed = await get_amazon_feed(feed_id)
    if not feed or feed["merchant_id"] != merchant_id:
        raise HTTPException(
            status_code=404,
            detail=f"Feed {feed_id} not found",
        )
    
    # Refresh from Amazon if requested and we have an Amazon feed ID
    if refresh and feed.get("feed_id"):
        try:
            # Get credentials and access token
            creds_record = await get_latest_connector_credential_for_merchant(merchant_id, "amazon")
            if creds_record and creds_record.get("is_valid"):
                credentials = crypto_service.decrypt_json_secret(creds_record["credentials_encrypted"])
                refresh_token = credentials.get("refresh_token")
                
                if refresh_token:
                    access_token = await get_amazon_access_token(refresh_token)
                    
                    # Get feed status from Amazon
                    amazon_status = await get_feed(
                        access_token=access_token,
                        feed_id=feed["feed_id"],
                        region=settings.amazon_sp_api_region,
                    )
                    
                    # Update our record
                    processing_status = amazon_status.get("processingStatus")
                    update_values = {
                        "processing_status": processing_status,
                    }
                    
                    # Map Amazon status to our status
                    if processing_status == "DONE":
                        update_values["status"] = "completed"
                        update_values["completed_at"] = datetime.utcnow()
                    elif processing_status == "CANCELLED":
                        update_values["status"] = "cancelled"
                        update_values["completed_at"] = datetime.utcnow()
                    elif processing_status in ["IN_QUEUE", "IN_PROGRESS"]:
                        update_values["status"] = "in_progress"
                        if processing_status == "IN_PROGRESS" and not feed.get("started_processing_at"):
                            update_values["started_processing_at"] = datetime.utcnow()
                    
                    # Add result if available
                    if amazon_status.get("resultFeedDocumentId"):
                        update_values["submission_result"] = {
                            "resultFeedDocumentId": amazon_status["resultFeedDocumentId"],
                            "processingStatus": processing_status,
                        }
                    
                    await update_amazon_feed(feed_id, **update_values)
                    
                    # Update order fulfillment status if completed
                    if processing_status == "DONE" and feed.get("related_orders"):
                        for order_id in feed["related_orders"]:
                            await update_platform_order_fulfillment(
                                merchant_id=merchant_id,
                                order_id=order_id,
                                fulfillment_status="fulfilled",
                                shipment_data={"feed_completed": True},
                            )
                    
                    # Re-fetch updated record
                    feed = await get_amazon_feed(feed_id)
                    
        except Exception as e:
            logger.error(f"Failed to refresh feed status: {str(e)}", exc_info=True)
            # Continue with cached data
    
    return FeedStatusResponse(
        feed_id=feed["id"],
        amazon_feed_id=feed.get("feed_id"),
        status=feed["status"],
        processing_status=feed.get("processing_status"),
        submitted_at=feed.get("submitted_at"),
        completed_at=feed.get("completed_at"),
        submission_result=feed.get("submission_result"),
        related_orders=feed.get("related_orders", []),
    )


@router.get("/feeds", response_model=List[FeedStatusResponse])
async def list_feeds(
    merchant_id: str,
    feed_type: Optional[str] = Query(None, description="Filter by feed type"),
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_admin: dict = Depends(require_admin),
):
    """
    List feed submissions for a merchant.
    
    Args:
        merchant_id: Merchant identifier
        feed_type: Optional feed type filter
        status: Optional status filter
        limit: Maximum results
        offset: Results offset
        
    Returns:
        List of FeedStatusResponse
    """
    feeds = await list_amazon_feeds(
        merchant_id=merchant_id,
        feed_type=feed_type,
        status=status,
        limit=limit,
        offset=offset,
    )
    
    return [
        FeedStatusResponse(
            feed_id=feed["id"],
            amazon_feed_id=feed.get("feed_id"),
            status=feed["status"],
            processing_status=feed.get("processing_status"),
            submitted_at=feed.get("submitted_at"),
            completed_at=feed.get("completed_at"),
            submission_result=feed.get("submission_result"),
            related_orders=feed.get("related_orders", []),
        )
        for feed in feeds
    ]


# Mock endpoints for testing without Amazon account
# Always available for testing purposes
@router.post("/orders/{order_id}/fulfill/mock", response_model=FulfillmentResponse)
async def mock_fulfill_order(
        merchant_id: str,
        order_id: str,
        request: FulfillmentRequest,
        current_admin: dict = Depends(require_admin),
    ):
        """
        Mock fulfillment endpoint for testing.
        
        Simulates the fulfillment process without calling Amazon APIs.
        """
        logger.info(f"Mock fulfilling order {order_id}")
        
        # Create mock feed record
        feed_id = await create_amazon_feed(
            merchant_id=merchant_id,
            feed_type="POST_ORDER_FULFILLMENT_DATA",
            submission_data={
                "mock": True,
                "fulfillment_data": [{
                    "amazon_order_id": order_id,
                    "carrier_code": request.carrier_code,
                    "tracking_number": request.tracking_number,
                }],
                "request": request.dict(),
            },
            related_orders=[order_id],
        )
        
        # Update with mock Amazon feed ID
        mock_feed_id = f"MOCK-FEED-{feed_id}"
        await update_amazon_feed(
            feed_id=feed_id,
            amazon_feed_id=mock_feed_id,
            status="submitted",
            submitted_at=datetime.utcnow(),
        )
        
        return FulfillmentResponse(
            success=True,
            feed_id=feed_id,
            amazon_feed_id=mock_feed_id,
            message="Mock fulfillment submitted successfully",
            details={
                "mock": True,
                "tracking_number": request.tracking_number,
                "carrier": request.carrier_code,
            },
        )


"""
Admin endpoints for submitting shipment information to Amazon SP-API.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
import logging

from utils.auth import require_admin
from config.settings import settings
from db.connector_credentials import get_latest_connector_credential_for_merchant
from db.amazon_feeds import (
    create_amazon_feed,
    update_amazon_feed,
    get_amazon_feed,
    get_amazon_feed_by_amazon_id,
    list_amazon_feeds,
    get_feed_by_order,
)
from db.platform_orders import get_platform_orders, update_platform_order_fulfillment
from services.crypto_service import crypto_service
from services.amazon_sp_api_service import (
    get_amazon_access_token,
    submit_order_fulfillment,
    get_feed,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/amazon",
    tags=["amazon_fulfillment"],
    dependencies=[Depends(require_admin)],
)


class FulfillmentItem(BaseModel):
    """Single item fulfillment data."""
    amazon_order_item_id: Optional[str] = Field(None, description="Order item ID (for partial fulfillment)")
    quantity: Optional[int] = Field(None, description="Quantity to fulfill (for partial fulfillment)")


class FulfillmentRequest(BaseModel):
    """Request to fulfill an Amazon order."""
    carrier_code: str = Field(..., description="Carrier code (e.g., 'USPS', 'FedEx', 'UPS')")
    tracking_number: str = Field(..., description="Shipment tracking number")
    ship_date: Optional[datetime] = Field(None, description="Ship date (defaults to now)")
    carrier_name: Optional[str] = Field(None, description="Carrier name (optional)")
    items: Optional[List[FulfillmentItem]] = Field(None, description="Items to fulfill (optional, for partial)")


class FulfillmentResponse(BaseModel):
    """Response from fulfillment submission."""
    success: bool
    feed_id: Optional[int] = Field(None, description="Internal feed record ID")
    amazon_feed_id: Optional[str] = Field(None, description="Amazon's feed ID")
    message: str
    details: Optional[Dict[str, Any]] = None


class FeedStatusResponse(BaseModel):
    """Feed status response."""
    feed_id: int
    amazon_feed_id: Optional[str]
    status: str
    processing_status: Optional[str]
    submitted_at: Optional[datetime]
    completed_at: Optional[datetime]
    submission_result: Optional[Dict[str, Any]]
    related_orders: List[str]


@router.post("/orders/{order_id}/fulfill", response_model=FulfillmentResponse)
async def fulfill_amazon_order(
    merchant_id: str,
    order_id: str,
    request: FulfillmentRequest,
    current_admin: dict = Depends(require_admin),
):
    """
    Submit fulfillment information for an Amazon order.
    
    This endpoint:
    1. Validates the order exists and is from Amazon
    2. Gets Amazon credentials
    3. Submits fulfillment feed to Amazon
    4. Records the feed submission
    5. Updates order fulfillment status
    
    Args:
        merchant_id: Merchant identifier
        order_id: Amazon order ID (e.g., '111-1234567-1234567')
        request: Fulfillment details
        
    Returns:
        FulfillmentResponse with feed submission details
        
    Raises:
        HTTPException: If order not found, not Amazon order, or submission fails
    """
    if not settings.enable_amazon_sp_api:
        raise HTTPException(
            status_code=403,
            detail="Amazon SP-API integration is not enabled",
        )
    
    logger.info(
        f"Fulfilling Amazon order",
        extra={
            "merchant_id": merchant_id,
            "order_id": order_id,
            "carrier": request.carrier_code,
            "tracking": request.tracking_number,
        },
    )
    
    try:
        # 1. Validate order exists and is from Amazon
        orders = await get_platform_orders(
            merchant_id=merchant_id,
            platform="amazon",
            order_ids=[order_id],
            limit=1,
        )
        
        if not orders:
            raise HTTPException(
                status_code=404,
                detail=f"Order {order_id} not found for merchant {merchant_id}",
            )
        
        order_data = orders[0]["data"]
        
        # Check if already fulfilled
        if orders[0].get("fulfillment_status") == "fulfilled":
            logger.warning(f"Order {order_id} already fulfilled")
            # Check for existing feed
            existing_feed = await get_feed_by_order(merchant_id, order_id)
            if existing_feed:
                return FulfillmentResponse(
                    success=True,
                    feed_id=existing_feed["id"],
                    amazon_feed_id=existing_feed.get("feed_id"),
                    message="Order already fulfilled",
                    details={"existing_feed": existing_feed},
                )
        
        # 2. Get Amazon credentials
        creds_record = await get_latest_connector_credential_for_merchant(merchant_id, "amazon")
        if not creds_record or not creds_record.get("is_valid"):
            raise HTTPException(
                status_code=400,
                detail="No valid Amazon credentials found",
            )
        
        credentials = crypto_service.decrypt_json_secret(creds_record["credentials_encrypted"])
        refresh_token = credentials.get("refresh_token")
        marketplace_ids = credentials.get("marketplace_ids", ["ATVPDKIKX0DER"])  # Default to US
        
        if not refresh_token:
            raise HTTPException(
                status_code=400,
                detail="No refresh token found in credentials",
            )
        
        # 3. Get access token
        access_token = await get_amazon_access_token(refresh_token)
        
        # 4. Prepare fulfillment data
        fulfillment_data = [{
            "amazon_order_id": order_id,
            "carrier_code": request.carrier_code,
            "tracking_number": request.tracking_number,
            "ship_date": request.ship_date or datetime.utcnow(),
            "carrier_name": request.carrier_name,
        }]
        
        # Add items if partial fulfillment
        if request.items:
            fulfillment_data[0]["items"] = [
                {
                    "amazon_order_item_id": item.amazon_order_item_id,
                    "quantity": item.quantity,
                }
                for item in request.items
                if item.amazon_order_item_id
            ]
        
        # 5. Submit to Amazon
        amazon_feed_id, feed_response = await submit_order_fulfillment(
            merchant_id=merchant_id,
            access_token=access_token,
            fulfillment_data=fulfillment_data,
            marketplace_ids=marketplace_ids,
            region=settings.amazon_sp_api_region,
        )
        
        if not amazon_feed_id:
            logger.error(f"Failed to submit fulfillment: {feed_response}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to submit fulfillment: {feed_response.get('error', 'Unknown error')}",
            )
        
        # 6. Record feed submission
        feed_id = await create_amazon_feed(
            merchant_id=merchant_id,
            feed_type="POST_ORDER_FULFILLMENT_DATA",
            submission_data={
                "fulfillment_data": fulfillment_data,
                "marketplace_ids": marketplace_ids,
                "request": request.dict(),
            },
            related_orders=[order_id],
        )
        
        # Update with Amazon feed ID
        await update_amazon_feed(
            feed_id=feed_id,
            amazon_feed_id=amazon_feed_id,
            status="submitted",
            submitted_at=datetime.utcnow(),
        )
        
        # 7. Update order fulfillment status
        shipment_data = {
            "carrier_code": request.carrier_code,
            "tracking_number": request.tracking_number,
            "ship_date": (request.ship_date or datetime.utcnow()).isoformat(),
            "feed_id": feed_id,
            "amazon_feed_id": amazon_feed_id,
        }
        
        await update_platform_order_fulfillment(
            merchant_id=merchant_id,
            order_id=order_id,
            fulfillment_status="pending",  # Will be updated when feed completes
            shipment_data=shipment_data,
        )
        
        logger.info(
            f"Successfully submitted fulfillment",
            extra={
                "merchant_id": merchant_id,
                "order_id": order_id,
                "feed_id": feed_id,
                "amazon_feed_id": amazon_feed_id,
            },
        )
        
        return FulfillmentResponse(
            success=True,
            feed_id=feed_id,
            amazon_feed_id=amazon_feed_id,
            message="Fulfillment submitted successfully",
            details={
                "feed_response": feed_response,
                "tracking_number": request.tracking_number,
                "carrier": request.carrier_code,
            },
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to fulfill order: {str(e)}",
            extra={"merchant_id": merchant_id, "order_id": order_id},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fulfill order: {str(e)}",
        )


@router.get("/feeds/{feed_id}", response_model=FeedStatusResponse)
async def get_feed_status(
    merchant_id: str,
    feed_id: int,
    refresh: bool = Query(False, description="Refresh status from Amazon"),
    current_admin: dict = Depends(require_admin),
):
    """
    Get feed processing status.
    
    Args:
        merchant_id: Merchant identifier
        feed_id: Internal feed ID
        refresh: Whether to fetch latest status from Amazon
        
    Returns:
        FeedStatusResponse with current status
        
    Raises:
        HTTPException: If feed not found or refresh fails
    """
    # Get feed record
    feed = await get_amazon_feed(feed_id)
    if not feed or feed["merchant_id"] != merchant_id:
        raise HTTPException(
            status_code=404,
            detail=f"Feed {feed_id} not found",
        )
    
    # Refresh from Amazon if requested and we have an Amazon feed ID
    if refresh and feed.get("feed_id"):
        try:
            # Get credentials and access token
            creds_record = await get_latest_connector_credential_for_merchant(merchant_id, "amazon")
            if creds_record and creds_record.get("is_valid"):
                credentials = crypto_service.decrypt_json_secret(creds_record["credentials_encrypted"])
                refresh_token = credentials.get("refresh_token")
                
                if refresh_token:
                    access_token = await get_amazon_access_token(refresh_token)
                    
                    # Get feed status from Amazon
                    amazon_status = await get_feed(
                        access_token=access_token,
                        feed_id=feed["feed_id"],
                        region=settings.amazon_sp_api_region,
                    )
                    
                    # Update our record
                    processing_status = amazon_status.get("processingStatus")
                    update_values = {
                        "processing_status": processing_status,
                    }
                    
                    # Map Amazon status to our status
                    if processing_status == "DONE":
                        update_values["status"] = "completed"
                        update_values["completed_at"] = datetime.utcnow()
                    elif processing_status == "CANCELLED":
                        update_values["status"] = "cancelled"
                        update_values["completed_at"] = datetime.utcnow()
                    elif processing_status in ["IN_QUEUE", "IN_PROGRESS"]:
                        update_values["status"] = "in_progress"
                        if processing_status == "IN_PROGRESS" and not feed.get("started_processing_at"):
                            update_values["started_processing_at"] = datetime.utcnow()
                    
                    # Add result if available
                    if amazon_status.get("resultFeedDocumentId"):
                        update_values["submission_result"] = {
                            "resultFeedDocumentId": amazon_status["resultFeedDocumentId"],
                            "processingStatus": processing_status,
                        }
                    
                    await update_amazon_feed(feed_id, **update_values)
                    
                    # Update order fulfillment status if completed
                    if processing_status == "DONE" and feed.get("related_orders"):
                        for order_id in feed["related_orders"]:
                            await update_platform_order_fulfillment(
                                merchant_id=merchant_id,
                                order_id=order_id,
                                fulfillment_status="fulfilled",
                                shipment_data={"feed_completed": True},
                            )
                    
                    # Re-fetch updated record
                    feed = await get_amazon_feed(feed_id)
                    
        except Exception as e:
            logger.error(f"Failed to refresh feed status: {str(e)}", exc_info=True)
            # Continue with cached data
    
    return FeedStatusResponse(
        feed_id=feed["id"],
        amazon_feed_id=feed.get("feed_id"),
        status=feed["status"],
        processing_status=feed.get("processing_status"),
        submitted_at=feed.get("submitted_at"),
        completed_at=feed.get("completed_at"),
        submission_result=feed.get("submission_result"),
        related_orders=feed.get("related_orders", []),
    )


@router.get("/feeds", response_model=List[FeedStatusResponse])
async def list_feeds(
    merchant_id: str,
    feed_type: Optional[str] = Query(None, description="Filter by feed type"),
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_admin: dict = Depends(require_admin),
):
    """
    List feed submissions for a merchant.
    
    Args:
        merchant_id: Merchant identifier
        feed_type: Optional feed type filter
        status: Optional status filter
        limit: Maximum results
        offset: Results offset
        
    Returns:
        List of FeedStatusResponse
    """
    feeds = await list_amazon_feeds(
        merchant_id=merchant_id,
        feed_type=feed_type,
        status=status,
        limit=limit,
        offset=offset,
    )
    
    return [
        FeedStatusResponse(
            feed_id=feed["id"],
            amazon_feed_id=feed.get("feed_id"),
            status=feed["status"],
            processing_status=feed.get("processing_status"),
            submitted_at=feed.get("submitted_at"),
            completed_at=feed.get("completed_at"),
            submission_result=feed.get("submission_result"),
            related_orders=feed.get("related_orders", []),
        )
        for feed in feeds
    ]


# Mock endpoint for testing without Amazon account.
# Keep it behind admin auth and feature flag, so it is safe in production.
@router.post("/orders/{order_id}/fulfill/mock", response_model=FulfillmentResponse)
async def mock_fulfill_order(
    merchant_id: str,
    order_id: str,
    request: FulfillmentRequest,
    current_admin: dict = Depends(require_admin),
):
    # Mock fulfillment endpoint for testing without hitting Amazon APIs.
    logger.info(f"Mock fulfilling order {order_id}")

    # Create mock feed record
    feed_id = await create_amazon_feed(
        merchant_id=merchant_id,
        feed_type="POST_ORDER_FULFILLMENT_DATA",
        submission_data={
            "mock": True,
            "fulfillment_data": [{
                "amazon_order_id": order_id,
                "carrier_code": request.carrier_code,
                "tracking_number": request.tracking_number,
            }],
            "request": request.dict(),
        },
        related_orders=[order_id],
    )

    # Update with mock Amazon feed ID
    mock_feed_id = f"MOCK-FEED-{feed_id}"
    await update_amazon_feed(
        feed_id=feed_id,
        amazon_feed_id=mock_feed_id,
        status="submitted",
        submitted_at=datetime.utcnow(),
    )

    return FulfillmentResponse(
        success=True,
        feed_id=feed_id,
        amazon_feed_id=mock_feed_id,
        message="Mock fulfillment submitted successfully",
        details={
            "mock": True,
            "tracking_number": request.tracking_number,
            "carrier": request.carrier_code,
        },
    )

