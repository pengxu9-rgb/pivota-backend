"""
Amazon SP-API Service

Core service layer for Amazon Selling Partner API integration.
Handles OAuth token management, orders fetching, and API communication.
"""

import httpx
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import asyncio

from config.settings import settings

logger = logging.getLogger(__name__)

# Amazon SP-API endpoints
AMAZON_LWA_URL = "https://api.amazon.com/auth/o2/token"
AMAZON_SP_API_BASE_NA = "https://sellingpartnerapi-na.amazon.com"
AMAZON_SP_API_BASE_EU = "https://sellingpartnerapi-eu.amazon.com"
AMAZON_SP_API_BASE_FE = "https://sellingpartnerapi-fe.amazon.com"

# Rate limiting constants
MAX_RETRIES = 3
BACKOFF_BASE = 5  # seconds

# Amazon API Error Codes
THROTTLED_ERROR = "QuotaExceeded"
INVALID_TOKEN_ERROR = "Unauthorized"


def get_sp_api_base_url(region: str = "na") -> str:
    """Get SP-API base URL for the given region."""
    region_map = {
        "na": AMAZON_SP_API_BASE_NA,
        "eu": AMAZON_SP_API_BASE_EU,
        "fe": AMAZON_SP_API_BASE_FE,
    }
    return region_map.get(region.lower(), AMAZON_SP_API_BASE_NA)


async def get_amazon_access_token(refresh_token: str) -> str:
    """
    Get Amazon SP-API access token from refresh token.
    
    Access tokens are valid for 1 hour.
    
    Args:
        refresh_token: LWA refresh token
        
    Returns:
        access_token: Valid access token
        
    Raises:
        Exception: If token refresh fails
    """
    if not settings.amazon_sp_api_client_id or not settings.amazon_sp_api_client_secret:
        raise ValueError("Amazon SP-API credentials not configured")
    
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.amazon_sp_api_client_id,
        "client_secret": settings.amazon_sp_api_client_secret,
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(AMAZON_LWA_URL, data=data)
            response.raise_for_status()
            
            result = response.json()
            access_token = result.get("access_token")
            
            if not access_token:
                raise ValueError("No access_token in response")
            
            logger.info("Successfully refreshed Amazon access token")
            return access_token
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to refresh Amazon token: {e.response.status_code} - {e.response.text}")
            raise Exception(f"Token refresh failed: {e.response.text}")
        except Exception as e:
            logger.error(f"Error refreshing Amazon token: {e}")
            raise


async def exchange_authorization_code(
    authorization_code: str,
) -> Dict[str, Any]:
    """
    Exchange authorization code for refresh token.
    
    Args:
        authorization_code: Authorization code from OAuth callback
        
    Returns:
        Token response containing refresh_token and access_token
    """
    if not settings.amazon_sp_api_client_id or not settings.amazon_sp_api_client_secret:
        raise ValueError("Amazon SP-API credentials not configured")
    
    data = {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "client_id": settings.amazon_sp_api_client_id,
        "client_secret": settings.amazon_sp_api_client_secret,
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(AMAZON_LWA_URL, data=data)
            response.raise_for_status()
            
            result = response.json()
            logger.info("Successfully exchanged authorization code for tokens")
            return result
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to exchange code: {e.response.status_code} - {e.response.text}")
            raise Exception(f"Code exchange failed: {e.response.text}")
        except Exception as e:
            logger.error(f"Error exchanging authorization code: {e}")
            raise


async def _make_sp_api_request(
    method: str,
    url: str,
    access_token: str,
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    retry_count: int = 0,
) -> Dict[str, Any]:
    """
    Make a request to Amazon SP-API with retry logic.
    
    Handles 429 rate limiting with exponential backoff.
    Retries 5xx errors.
    
    Args:
        method: HTTP method (GET, POST, etc.)
        url: Full API URL
        access_token: Valid access token
        params: Query parameters
        json_data: JSON body data
        retry_count: Current retry attempt
        
    Returns:
        API response as dict
    """
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json",
    }
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            if method.upper() == "GET":
                response = await client.get(url, headers=headers, params=params)
            elif method.upper() == "POST":
                response = await client.post(url, headers=headers, json=json_data)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            # Handle rate limiting (429)
            if response.status_code == 429:
                if retry_count < MAX_RETRIES:
                    backoff_time = BACKOFF_BASE * (3 ** retry_count)  # 5s, 15s, 45s
                    logger.warning(f"Rate limited (429), backing off for {backoff_time}s (attempt {retry_count + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(backoff_time)
                    return await _make_sp_api_request(method, url, access_token, params, json_data, retry_count + 1)
                else:
                    raise Exception(f"Rate limited after {MAX_RETRIES} retries")
            
            # Handle server errors (5xx)
            if 500 <= response.status_code < 600:
                if retry_count < MAX_RETRIES:
                    backoff_time = BACKOFF_BASE * (2 ** retry_count)  # 5s, 10s, 20s
                    logger.warning(f"Server error ({response.status_code}), retrying in {backoff_time}s")
                    await asyncio.sleep(backoff_time)
                    return await _make_sp_api_request(method, url, access_token, params, json_data, retry_count + 1)
                else:
                    raise Exception(f"Server error after {MAX_RETRIES} retries: {response.status_code}")
            
            response.raise_for_status()
            return response.json()
            
        except httpx.HTTPStatusError as e:
            logger.error(f"SP-API request failed: {e.response.status_code} - {e.response.text}")
            raise Exception(f"SP-API error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"Error making SP-API request: {e}")
            raise


async def fetch_amazon_orders(
    access_token: str,
    marketplace_id: str,
    created_after: datetime,
    created_before: Optional[datetime] = None,
    region: str = "na",
) -> List[Dict[str, Any]]:
    """
    Fetch orders from Amazon SP-API.
    
    Handles pagination automatically using NextToken.
    
    Args:
        access_token: Valid access token
        marketplace_id: Amazon marketplace ID (e.g., ATVPDKIKX0DER for US)
        created_after: Start date for orders
        created_before: End date for orders (default: now)
        region: Amazon region (na, eu, fe)
        
    Returns:
        List of order objects
    """
    if not created_before:
        created_before = datetime.utcnow()
    
    base_url = get_sp_api_base_url(region)
    url = f"{base_url}/orders/v0/orders"
    
    params = {
        "MarketplaceIds": marketplace_id,
        "CreatedAfter": created_after.isoformat(),
        "CreatedBefore": created_before.isoformat(),
        "MaxResultsPerPage": 100,
    }
    
    orders = []
    next_token = None
    page_count = 0
    
    while True:
        if next_token:
            params["NextToken"] = next_token
        
        page_count += 1
        logger.info(f"Fetching orders page {page_count} (created_after={created_after.isoformat()})")
        
        try:
            data = await _make_sp_api_request("GET", url, access_token, params=params)
            payload = data.get("payload", {})
            
            page_orders = payload.get("Orders", [])
            orders.extend(page_orders)
            
            logger.info(f"Page {page_count}: fetched {len(page_orders)} orders (total: {len(orders)})")
            
            next_token = payload.get("NextToken")
            if not next_token:
                break
                
        except Exception as e:
            logger.error(f"Error fetching orders page {page_count}: {e}")
            raise
    
    logger.info(f"Successfully fetched {len(orders)} total orders across {page_count} pages")
    return orders


async def fetch_order_items(
    access_token: str,
    order_id: str,
    region: str = "na",
) -> List[Dict[str, Any]]:
    """
    Fetch order items for a specific order.
    
    Args:
        access_token: Valid access token
        order_id: Amazon order ID
        region: Amazon region
        
    Returns:
        List of order item objects
    """
    base_url = get_sp_api_base_url(region)
    url = f"{base_url}/orders/v0/orders/{order_id}/orderItems"
    
    try:
        data = await _make_sp_api_request("GET", url, access_token)
        payload = data.get("payload", {})
        items = payload.get("OrderItems", [])
        
        logger.info(f"Fetched {len(items)} items for order {order_id}")
        return items
        
    except Exception as e:
        logger.error(f"Error fetching items for order {order_id}: {e}")
        raise


def convert_amazon_order_to_platform_format(
    merchant_id: str,
    order: Dict[str, Any],
    order_items: List[Dict[str, Any]],
    marketplace_id: str,
) -> List[Dict[str, Any]]:
    """
    Convert Amazon order and items to platform_orders format.
    
    Returns one record per order item (matching the CSV import behavior).
    
    Args:
        merchant_id: Merchant ID
        order: Amazon Order object
        order_items: Amazon OrderItems array
        marketplace_id: Marketplace ID
        
    Returns:
        List of platform_orders data objects (one per item)
    """
    amazon_order_id = order.get("AmazonOrderId")
    order_date = order.get("PurchaseDate")
    order_status = order.get("OrderStatus")
    
    # Buyer info
    buyer_info = order.get("BuyerInfo", {})
    buyer_email = buyer_info.get("BuyerEmail", "")
    buyer_name = buyer_info.get("BuyerName", "")
    
    # Shipping address
    shipping_address = order.get("ShippingAddress", {})
    ship_address_parts = []
    if shipping_address.get("AddressLine1"):
        ship_address_parts.append(shipping_address["AddressLine1"])
    if shipping_address.get("AddressLine2"):
        ship_address_parts.append(shipping_address["AddressLine2"])
    if shipping_address.get("City"):
        ship_address_parts.append(shipping_address["City"])
    if shipping_address.get("StateOrRegion"):
        ship_address_parts.append(shipping_address["StateOrRegion"])
    if shipping_address.get("PostalCode"):
        ship_address_parts.append(shipping_address["PostalCode"])
    ship_address = ", ".join(ship_address_parts) if ship_address_parts else "N/A"
    
    result = []
    
    for item in order_items:
        order_item_id = item.get("OrderItemId")
        sku = item.get("SellerSKU", "")
        asin = item.get("ASIN", "")
        title = item.get("Title", "")
        quantity = item.get("QuantityOrdered", 1)
        
        # Price info
        item_price = item.get("ItemPrice", {})
        price = float(item_price.get("Amount", 0))
        currency = item_price.get("CurrencyCode", "USD")
        
        platform_order_data = {
            "order_id": amazon_order_id,
            "source": "sp_api",
            "marketplace_id": marketplace_id,
            "order_date": order_date,
            "order_status": order_status,
            "buyer_email": buyer_email,
            "buyer_name": buyer_name,
            "ship_address": ship_address,
            "items": [{
                "order_item_id": order_item_id,
                "sku": sku,
                "asin": asin,
                "title": title,
                "quantity": quantity,
                "price": price,
                "currency": currency,
            }],
            "raw_order": order,
            "raw_items": [item],
            "sync_at": datetime.utcnow().isoformat(),
        }
        
        result.append({
            "merchant_id": merchant_id,
            "platform": "amazon",
            "order_id": amazon_order_id,
            "order_item_id": order_item_id,
            "data": platform_order_data,
        })
    
    return result


# ============================================================
# Feed API Functions
# ============================================================

async def call_sp_api_with_retry(
    func,
    *args,
    max_retries: int = MAX_RETRIES,
    **kwargs
) -> Any:
    """
    Call Amazon SP-API with automatic retry on rate limiting and server errors.
    
    Args:
        func: Async function to call
        *args: Function arguments
        max_retries: Maximum retry attempts
        **kwargs: Function keyword arguments
        
    Returns:
        Function response
        
    Raises:
        Last exception if all retries fail
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
            
        except httpx.HTTPStatusError as e:
            last_exception = e
            
            # Handle specific HTTP errors
            if e.response.status_code == 429:  # Rate limited
                retry_after = int(e.response.headers.get("Retry-After", BACKOFF_BASE * (2 ** attempt)))
                logger.warning(
                    f"Rate limited on attempt {attempt + 1}/{max_retries + 1}, "
                    f"waiting {retry_after}s",
                    extra={"status_code": 429, "attempt": attempt + 1}
                )
                
                if attempt < max_retries:
                    await asyncio.sleep(retry_after)
                    continue
                    
            elif e.response.status_code == 401:  # Unauthorized
                logger.error("Unauthorized - token may be expired or invalid")
                raise  # Don't retry auth errors
                
            elif e.response.status_code >= 500:  # Server errors
                wait_time = BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    f"Server error {e.response.status_code} on attempt {attempt + 1}/{max_retries + 1}, "
                    f"waiting {wait_time}s",
                    extra={"status_code": e.response.status_code, "attempt": attempt + 1}
                )
                
                if attempt < max_retries:
                    await asyncio.sleep(wait_time)
                    continue
            else:
                # Other errors - don't retry
                raise
                
        except Exception as e:
            last_exception = e
            logger.error(
                f"Unexpected error on attempt {attempt + 1}: {str(e)}",
                exc_info=True
            )
            
            # Only retry on network/connection errors
            if isinstance(e, (httpx.ConnectError, httpx.TimeoutException)):
                wait_time = BACKOFF_BASE * (2 ** attempt)
                if attempt < max_retries:
                    await asyncio.sleep(wait_time)
                    continue
            else:
                raise
    
    # All retries exhausted
    logger.error(f"All {max_retries + 1} attempts failed")
    raise last_exception


async def create_feed_document(
    access_token: str,
    content_type: str = "text/xml; charset=UTF-8",
    region: str = "na",
) -> Dict[str, Any]:
    """
    Create a feed document to get upload URL.
    
    Args:
        access_token: Valid SP-API access token
        content_type: Content type of the feed (default: XML)
        region: Amazon region
        
    Returns:
        Feed document response containing:
        - feedDocumentId: ID of the feed document
        - url: Pre-signed URL for upload
        
    Raises:
        httpx.HTTPStatusError: If API request fails
    """
    base_url = get_sp_api_base_url(region)
    url = f"{base_url}/feeds/2021-06-30/documents"
    
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json",
    }
    
    data = {
        "contentType": content_type,
    }
    
    async def _make_request():
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json=data,
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
    
    return await call_sp_api_with_retry(_make_request)


async def upload_feed_content(
    upload_url: str,
    feed_content: str,
    content_type: str = "text/xml; charset=UTF-8",
) -> None:
    """
    Upload feed content to the pre-signed URL.
    
    Args:
        upload_url: Pre-signed URL from create_feed_document
        feed_content: XML/JSON content to upload
        content_type: Content type of the feed
        
    Raises:
        httpx.HTTPStatusError: If upload fails
    """
    headers = {
        "Content-Type": content_type,
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.put(
            upload_url,
            content=feed_content.encode('utf-8'),
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()


async def create_feed(
    access_token: str,
    feed_type: str,
    feed_document_id: str,
    marketplace_ids: List[str],
    region: str = "na",
) -> Dict[str, Any]:
    """
    Create a feed submission.
    
    Args:
        access_token: Valid SP-API access token
        feed_type: Type of feed (e.g., 'POST_ORDER_FULFILLMENT_DATA')
        feed_document_id: ID from create_feed_document
        marketplace_ids: List of marketplace IDs
        region: Amazon region
        
    Returns:
        Feed creation response containing:
        - feedId: ID of the created feed
        
    Raises:
        httpx.HTTPStatusError: If API request fails
    """
    base_url = get_sp_api_base_url(region)
    url = f"{base_url}/feeds/2021-06-30/feeds"
    
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json",
    }
    
    data = {
        "feedType": feed_type,
        "marketplaceIds": marketplace_ids,
        "inputFeedDocumentId": feed_document_id,
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            headers=headers,
            json=data,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()


async def get_feed(
    access_token: str,
    feed_id: str,
    region: str = "na",
) -> Dict[str, Any]:
    """
    Get feed processing status.
    
    Args:
        access_token: Valid SP-API access token
        feed_id: Feed ID from create_feed
        region: Amazon region
        
    Returns:
        Feed status response containing:
        - feedId: Feed ID
        - feedType: Type of feed
        - createdTime: When feed was created
        - processingStatus: Current status
        - processingStartTime: When processing started (if applicable)
        - processingEndTime: When processing ended (if applicable)
        - resultFeedDocumentId: Result document ID (if completed)
        
    Raises:
        httpx.HTTPStatusError: If API request fails
    """
    base_url = get_sp_api_base_url(region)
    url = f"{base_url}/feeds/2021-06-30/feeds/{feed_id}"
    
    headers = {
        "x-amz-access-token": access_token,
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()


async def submit_order_fulfillment(
    merchant_id: str,
    access_token: str,
    fulfillment_data: List[Dict[str, Any]],
    marketplace_ids: List[str],
    region: str = "na",
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Submit order fulfillment feed to Amazon.
    
    This is a high-level function that:
    1. Builds the XML feed
    2. Creates a feed document
    3. Uploads the content
    4. Creates the feed submission
    
    Args:
        merchant_id: Merchant identifier
        access_token: Valid SP-API access token
        fulfillment_data: List of fulfillment records
        marketplace_ids: List of marketplace IDs
        region: Amazon region
        
    Returns:
        Tuple of (feed_id, feed_response) or (None, error_dict)
        
    Example fulfillment_data:
        [{
            'amazon_order_id': '111-1234567-1234567',
            'carrier_code': 'USPS',
            'tracking_number': '9400100000000000000000',
            'ship_date': '2024-01-15T10:30:00Z',
        }]
    """
    try:
        # 1. Build XML feed
        from services.amazon_feed_builder import OrderFulfillmentFeedBuilder
        builder = OrderFulfillmentFeedBuilder()
        xml_content = builder.build_fulfillment_feed(merchant_id, fulfillment_data)
        
        logger.info(f"Built fulfillment feed for {len(fulfillment_data)} orders")
        
        # 2. Create feed document
        doc_response = await create_feed_document(access_token, region=region)
        feed_document_id = doc_response['feedDocumentId']
        upload_url = doc_response['url']
        
        logger.info(f"Created feed document: {feed_document_id}")
        
        # 3. Upload feed content
        await upload_feed_content(upload_url, xml_content)
        logger.info("Uploaded feed content")
        
        # 4. Create feed submission
        feed_response = await create_feed(
            access_token=access_token,
            feed_type="POST_ORDER_FULFILLMENT_DATA",
            feed_document_id=feed_document_id,
            marketplace_ids=marketplace_ids,
            region=region,
        )
        
        feed_id = feed_response['feedId']
        logger.info(f"Created feed submission: {feed_id}")
        
        return feed_id, feed_response
        
    except Exception as e:
        logger.error(f"Failed to submit fulfillment: {str(e)}")
        return None, {
            "error": str(e),
            "error_type": type(e).__name__,
        }

