"""
Amazon Feed XML Builder for various feed types.

This module provides builders for creating XML feeds required by Amazon SP-API.
"""

from typing import List, Dict, Optional, Any
from datetime import datetime
from xml.etree import ElementTree as ET
import xml.dom.minidom
from zoneinfo import ZoneInfo


class FeedBuilder:
    """Base class for Amazon feed builders."""
    
    def __init__(self):
        self.ns = {
            'xsi': 'http://www.w3.org/2001/XMLSchema-instance',
        }
    
    def prettify_xml(self, elem: ET.Element) -> str:
        """Return a pretty-printed XML string."""
        rough_string = ET.tostring(elem, encoding='unicode')
        reparsed = xml.dom.minidom.parseString(rough_string)
        return reparsed.toprettyxml(indent="  ", encoding='UTF-8').decode('utf-8')


class OrderFulfillmentFeedBuilder(FeedBuilder):
    """
    Build POST_ORDER_FULFILLMENT_DATA XML feed.
    
    This feed is used to update Amazon with shipment tracking information.
    """
    
    def build_fulfillment_feed(
        self,
        merchant_id: str,
        fulfillments: List[Dict[str, Any]],
    ) -> str:
        """
        Build order fulfillment XML feed.
        
        Args:
            merchant_id: Merchant identifier for the feed
            fulfillments: List of fulfillment data, each containing:
                - amazon_order_id: Amazon order ID
                - amazon_order_item_id: Amazon order item ID (optional, for partial)
                - carrier_code: Carrier code (e.g., 'USPS', 'FedEx')
                - carrier_name: Carrier name (optional)
                - tracking_number: Shipment tracking number
                - ship_date: Shipment date (datetime or ISO string)
                - quantity: Quantity shipped (optional, defaults to all)
                
        Returns:
            XML string for the fulfillment feed
            
        Example fulfillment:
            {
                'amazon_order_id': '111-1234567-1234567',
                'carrier_code': 'USPS',
                'tracking_number': '9400100000000000000000',
                'ship_date': '2024-01-15T10:30:00Z',
            }
        """
        # Create root element
        root = ET.Element('AmazonEnvelope')
        root.set('xmlns:xsi', self.ns['xsi'])
        root.set('xsi:noNamespaceSchemaLocation', 'amzn-envelope.xsd')
        
        # Add header
        header = ET.SubElement(root, 'Header')
        ET.SubElement(header, 'DocumentVersion').text = '1.01'
        ET.SubElement(header, 'MerchantIdentifier').text = merchant_id
        
        # Add message type
        ET.SubElement(root, 'MessageType').text = 'OrderFulfillment'
        
        # Add messages
        for idx, fulfillment in enumerate(fulfillments, 1):
            message = ET.SubElement(root, 'Message')
            ET.SubElement(message, 'MessageID').text = str(idx)
            
            order_fulfillment = ET.SubElement(message, 'OrderFulfillment')
            ET.SubElement(order_fulfillment, 'AmazonOrderID').text = fulfillment['amazon_order_id']
            
            # Handle fulfillment date
            fulfill_date = fulfillment.get('ship_date')
            if isinstance(fulfill_date, str):
                # Assume it's already in ISO format
                fulfill_date_str = fulfill_date
            elif isinstance(fulfill_date, datetime):
                # Convert to UTC ISO format
                if fulfill_date.tzinfo is None:
                    # Assume UTC if no timezone
                    fulfill_date = fulfill_date.replace(tzinfo=ZoneInfo('UTC'))
                fulfill_date_str = fulfill_date.isoformat()
            else:
                # Use current time as fallback
                fulfill_date_str = datetime.now(ZoneInfo('UTC')).isoformat()
            
            ET.SubElement(order_fulfillment, 'FulfillmentDate').text = fulfill_date_str
            
            # Add fulfillment data
            fulfillment_data = ET.SubElement(order_fulfillment, 'FulfillmentData')
            
            # Carrier information
            carrier_code = fulfillment.get('carrier_code', 'Other')
            ET.SubElement(fulfillment_data, 'CarrierCode').text = carrier_code
            
            if fulfillment.get('carrier_name'):
                ET.SubElement(fulfillment_data, 'CarrierName').text = fulfillment['carrier_name']
            
            # Tracking number
            if fulfillment.get('tracking_number'):
                ET.SubElement(fulfillment_data, 'ShipperTrackingNumber').text = fulfillment['tracking_number']
            
            # Handle items (optional - if not specified, fulfills entire order)
            if fulfillment.get('amazon_order_item_id') or fulfillment.get('quantity'):
                item = ET.SubElement(order_fulfillment, 'Item')
                
                if fulfillment.get('amazon_order_item_id'):
                    ET.SubElement(item, 'AmazonOrderItemCode').text = fulfillment['amazon_order_item_id']
                
                if fulfillment.get('quantity'):
                    ET.SubElement(item, 'Quantity').text = str(fulfillment['quantity'])
        
        return self.prettify_xml(root)


class OrderAcknowledgmentFeedBuilder(FeedBuilder):
    """
    Build POST_ORDER_ACKNOWLEDGMENT_DATA XML feed.
    
    This feed is used to acknowledge order receipt and confirm items.
    """
    
    def build_acknowledgment_feed(
        self,
        merchant_id: str,
        acknowledgments: List[Dict[str, Any]],
    ) -> str:
        """
        Build order acknowledgment XML feed.
        
        Args:
            merchant_id: Merchant identifier
            acknowledgments: List of acknowledgment data containing:
                - amazon_order_id: Amazon order ID
                - status_code: 'Success' or 'Failure'
                - items: List of items to acknowledge (optional)
                
        Returns:
            XML string for the acknowledgment feed
        """
        root = ET.Element('AmazonEnvelope')
        root.set('xmlns:xsi', self.ns['xsi'])
        root.set('xsi:noNamespaceSchemaLocation', 'amzn-envelope.xsd')
        
        # Add header
        header = ET.SubElement(root, 'Header')
        ET.SubElement(header, 'DocumentVersion').text = '1.01'
        ET.SubElement(header, 'MerchantIdentifier').text = merchant_id
        
        # Add message type
        ET.SubElement(root, 'MessageType').text = 'OrderAcknowledgment'
        
        # Add messages
        for idx, ack in enumerate(acknowledgments, 1):
            message = ET.SubElement(root, 'Message')
            ET.SubElement(message, 'MessageID').text = str(idx)
            
            order_ack = ET.SubElement(message, 'OrderAcknowledgment')
            ET.SubElement(order_ack, 'AmazonOrderID').text = ack['amazon_order_id']
            ET.SubElement(order_ack, 'StatusCode').text = ack.get('status_code', 'Success')
            
            # Add items if specified
            if ack.get('items'):
                for item in ack['items']:
                    item_elem = ET.SubElement(order_ack, 'Item')
                    ET.SubElement(item_elem, 'AmazonOrderItemCode').text = item['amazon_order_item_id']
                    
                    if item.get('cancel_reason'):
                        ET.SubElement(item_elem, 'CancelReason').text = item['cancel_reason']
        
        return self.prettify_xml(root)


# Utility functions

def validate_carrier_code(carrier_code: str) -> bool:
    """
    Validate if carrier code is supported by Amazon.
    
    Common carrier codes:
    - USPS, UPS, FedEx, DHL, OnTrac, Lasership, 
    - CanadaPost, DHL_Global_Mail, Other
    """
    valid_carriers = {
        'USPS', 'UPS', 'FedEx', 'DHL', 'OnTrac', 'Lasership',
        'CanadaPost', 'DHL_Global_Mail', 'FBA', 'Other',
        # Add more as needed
    }
    return carrier_code in valid_carriers


def normalize_tracking_number(tracking_number: str) -> str:
    """Clean and normalize tracking number."""
    return tracking_number.strip().upper().replace(' ', '')


# Example usage and testing
if __name__ == "__main__":
    # Example: Build fulfillment feed
    builder = OrderFulfillmentFeedBuilder()
    
    fulfillments = [
        {
            'amazon_order_id': '111-1234567-1234567',
            'carrier_code': 'USPS',
            'tracking_number': '9400100000000000000000',
            'ship_date': datetime.now(ZoneInfo('UTC')),
        }
    ]
    
    xml_content = builder.build_fulfillment_feed('MERCHANT123', fulfillments)
    print(xml_content)
