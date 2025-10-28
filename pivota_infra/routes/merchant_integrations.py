"""
Merchant Integrations API
Handles PSP and E-commerce platform integrations
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel, Field
import time

from utils.auth import get_current_user
from utils.encryption import encrypt_data, decrypt_data, mask_credentials
from db.database import database
from db.merchant_integrations import (
    get_merchant_integrations,
    create_merchant_integration,
    update_integration_status,
    log_integration_activity
)
from adapters.psp_factory import PSPFactory
from adapters.ecommerce_factory import EcommerceFactory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/merchant/integrations", tags=["Merchant Integrations"])


class IntegrationCreateRequest(BaseModel):
    integration_type: str = Field(..., pattern="^(psp|platform)$")
    provider: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1, max_length=100)
    credentials: Dict[str, Any]
    settings: Optional[Dict[str, Any]] = None


class IntegrationUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    credentials: Optional[Dict[str, Any]] = None
    settings: Optional[Dict[str, Any]] = None
    is_primary: Optional[bool] = None


class IntegrationTestRequest(BaseModel):
    credentials: Optional[Dict[str, Any]] = None  # Optional, use stored if not provided


@router.get("")
async def list_integrations(
    integration_type: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """List all integrations for the merchant"""
    try:
        merchant_id = current_user.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found")
        
        integrations = await get_merchant_integrations(merchant_id, integration_type)
        
        # Convert to list of dicts and mask credentials
        formatted_integrations = []
        for integration in integrations:
            integration_dict = dict(integration)
            
            # Remove raw credentials from response
            integration_dict.pop('encrypted_credentials', None)
            
            formatted_integrations.append({
                'id': integration_dict['id'],
                'integration_type': integration_dict['integration_type'],
                'provider': integration_dict['provider'],
                'display_name': integration_dict['display_name'],
                'status': integration_dict['status'],
                'is_primary': integration_dict['is_primary'],
                'created_at': str(integration_dict['created_at']),
                'updated_at': str(integration_dict['updated_at']),
                'last_tested_at': str(integration_dict['last_tested_at']) if integration_dict.get('last_tested_at') else None,
                'last_test_result': integration_dict.get('last_test_result'),
                'settings': integration_dict.get('settings', {}),
                'environment': integration_dict.get('environment', 'production'),
                'webhook_count': integration_dict.get('webhook_count', 0),
                'active_webhook_count': integration_dict.get('active_webhook_count', 0)
            })
        
        return {
            'success': True,
            'integrations': formatted_integrations
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing integrations: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas")
async def get_integration_schemas(
    integration_type: Optional[str] = None,
    provider: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get configuration schemas for integrations"""
    try:
        schemas = {}
        
        # Get PSP schemas
        if not integration_type or integration_type == 'psp':
            if provider and provider in ['square', 'mollie', 'braintree']:
                schemas['psp'] = {
                    provider: PSPFactory.get_psp_config_schema(provider)
                }
            else:
                # Get all new PSP schemas (not the old ones)
                new_psps = ['square', 'mollie', 'braintree']
                schemas['psp'] = {
                    p: PSPFactory.get_psp_config_schema(p)
                    for p in new_psps
                }
        
        # Get platform schemas
        if not integration_type or integration_type == 'platform':
            if provider:
                schemas['platform'] = {
                    provider: EcommerceFactory.get_platform_config_schema(provider)
                }
            else:
                # Get all platform schemas
                platforms = EcommerceFactory.get_supported_platforms()
                schemas['platform'] = {
                    p: EcommerceFactory.get_platform_config_schema(p)
                    for p in platforms
                }
        
        return {
            'success': True,
            'schemas': schemas
        }
        
    except Exception as e:
        logger.error(f"Error getting schemas: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_integration(
    data: IntegrationCreateRequest,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Create a new integration"""
    try:
        merchant_id = current_user.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found")
        
        # Validate provider
        if data.integration_type == 'psp':
            if data.provider not in ['square', 'mollie', 'braintree']:
                raise HTTPException(status_code=400, detail=f"PSP provider {data.provider} not supported")
        elif data.integration_type == 'platform':
            if data.provider not in EcommerceFactory.get_supported_platforms():
                raise HTTPException(status_code=400, detail=f"Platform {data.provider} not supported")
        
        # Create integration
        integration_id = await create_merchant_integration(
            merchant_id=merchant_id,
            integration_type=data.integration_type,
            provider=data.provider,
            display_name=data.display_name,
            credentials=data.credentials,
            settings=data.settings
        )
        
        if not integration_id:
            raise HTTPException(status_code=500, detail="Failed to create integration")
        
        # Log activity
        await log_integration_activity(
            integration_id=integration_id,
            log_type='config_change',
            request_data={'action': 'create'},
            response_data={'success': True}
        )
        
        return {
            'success': True,
            'integration_id': integration_id,
            'message': f'{data.provider} integration created successfully'
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating integration: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{integration_id}/test")
async def test_integration(
    integration_id: int,
    data: IntegrationTestRequest,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Test an integration connection"""
    try:
        merchant_id = current_user.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found")
        
        start_time = time.time()
        
        # Get integration details
        integration = await database.fetch_one("""
            SELECT mi.*, mis.encrypted_credentials, mis2.settings
            FROM merchant_integrations mi
            JOIN merchant_integration_secrets mis ON mi.id = mis.integration_id
            LEFT JOIN merchant_integration_settings mis2 ON mi.id = mis2.integration_id
            WHERE mi.id = :integration_id AND mi.merchant_id = :merchant_id
        """, {
            "integration_id": integration_id,
            "merchant_id": merchant_id
        })
        
        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")
        
        # Use provided credentials or decrypt stored ones
        if data.credentials:
            credentials = data.credentials
        else:
            credentials = decrypt_data(integration['encrypted_credentials'])
        
        test_result = {
            'success': False,
            'message': 'Test failed',
            'details': {}
        }
        
        try:
            if integration['integration_type'] == 'psp':
                # Test PSP connection
                adapter = PSPFactory.create_adapter(integration['provider'], credentials)
                if adapter:
                    # Validate configuration
                    is_valid, error_msg = adapter.validate_config()
                    if is_valid:
                        # For new PSPs, add specific test methods
                        if integration['provider'] == 'square':
                            # Test Square by listing locations
                            async with httpx.AsyncClient() as client:
                                response = await client.get(
                                    f"{adapter.base_url}/locations",
                                    headers=adapter.headers,
                                    timeout=10.0
                                )
                                if response.status_code == 200:
                                    test_result = {
                                        'success': True,
                                        'message': 'Square connection successful',
                                        'details': {
                                            'locations': len(response.json().get('locations', []))
                                        }
                                    }
                                else:
                                    test_result['message'] = f'Square API error: {response.status_code}'
                                    
                        elif integration['provider'] == 'mollie':
                            # Test Mollie by getting payment methods
                            methods_result = await adapter.list_payment_methods()
                            if methods_result.get('success'):
                                test_result = {
                                    'success': True,
                                    'message': 'Mollie connection successful',
                                    'details': {
                                        'payment_methods': len(methods_result.get('methods', []))
                                    }
                                }
                            else:
                                test_result['message'] = methods_result.get('error', 'Mollie test failed')
                                
                        elif integration['provider'] == 'braintree':
                            # Test Braintree by generating client token
                            token_result = await adapter.generate_client_token()
                            if token_result.get('success'):
                                test_result = {
                                    'success': True,
                                    'message': 'Braintree connection successful',
                                    'details': {
                                        'environment': adapter.environment
                                    }
                                }
                            else:
                                test_result['message'] = token_result.get('error', 'Braintree test failed')
                    else:
                        test_result['message'] = error_msg or 'Invalid configuration'
                else:
                    test_result['message'] = 'Failed to create adapter'
                    
            elif integration['integration_type'] == 'platform':
                # Test e-commerce platform connection
                adapter = EcommerceFactory.create_adapter(integration['provider'], credentials)
                if adapter:
                    # Test connection
                    connection_result = await adapter.test_connection()
                    if connection_result.get('success'):
                        test_result = {
                            'success': True,
                            'message': f"{integration['provider']} connection successful",
                            'details': connection_result
                        }
                    else:
                        test_result['message'] = connection_result.get('error', 'Connection test failed')
                else:
                    test_result['message'] = 'Failed to create adapter'
            
        except Exception as test_error:
            test_result['message'] = str(test_error)
            test_result['details']['error_type'] = type(test_error).__name__
        
        duration_ms = int((time.time() - start_time) * 1000)
        
        # Update integration status based on test result
        new_status = 'active' if test_result['success'] else 'error'
        await update_integration_status(integration_id, new_status, test_result)
        
        # Log activity
        await log_integration_activity(
            integration_id=integration_id,
            log_type='api_call',
            request_data={'action': 'test_connection'},
            response_data=test_result,
            status_code=200 if test_result['success'] else 400,
            error_message=None if test_result['success'] else test_result['message'],
            duration_ms=duration_ms
        )
        
        return {
            'success': True,
            'test_result': test_result,
            'duration_ms': duration_ms
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing integration: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{integration_id}")
async def update_integration(
    integration_id: int,
    data: IntegrationUpdateRequest,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Update an integration"""
    try:
        merchant_id = current_user.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found")
        
        # Verify ownership
        integration = await database.fetch_one("""
            SELECT * FROM merchant_integrations 
            WHERE id = :integration_id AND merchant_id = :merchant_id
        """, {
            "integration_id": integration_id,
            "merchant_id": merchant_id
        })
        
        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")
        
        # Update main record
        update_fields = []
        params = {"integration_id": integration_id}
        
        if data.display_name is not None:
            update_fields.append("display_name = :display_name")
            params["display_name"] = data.display_name
            
        if data.is_primary is not None:
            update_fields.append("is_primary = :is_primary")
            params["is_primary"] = data.is_primary
            
            # If setting as primary, unset other primaries
            if data.is_primary:
                await database.execute("""
                    UPDATE merchant_integrations 
                    SET is_primary = FALSE 
                    WHERE merchant_id = :merchant_id 
                    AND integration_type = :integration_type 
                    AND id != :integration_id
                """, {
                    "merchant_id": merchant_id,
                    "integration_type": integration['integration_type'],
                    "integration_id": integration_id
                })
        
        update_fields.append("updated_at = CURRENT_TIMESTAMP")
        
        if update_fields:
            await database.execute(
                f"UPDATE merchant_integrations SET {', '.join(update_fields)} WHERE id = :integration_id",
                params
            )
        
        # Update credentials if provided
        if data.credentials:
            encrypted_creds = encrypt_data(data.credentials)
            await database.execute("""
                UPDATE merchant_integration_secrets 
                SET encrypted_credentials = :encrypted_credentials,
                    rotated_at = CURRENT_TIMESTAMP
                WHERE integration_id = :integration_id
            """, {
                "integration_id": integration_id,
                "encrypted_credentials": encrypted_creds
            })
        
        # Update settings if provided
        if data.settings is not None:
            await database.execute("""
                UPDATE merchant_integration_settings 
                SET settings = :settings,
                    updated_at = CURRENT_TIMESTAMP
                WHERE integration_id = :integration_id
            """, {
                "integration_id": integration_id,
                "settings": data.settings
            })
        
        # Log activity
        await log_integration_activity(
            integration_id=integration_id,
            log_type='config_change',
            request_data={'action': 'update', 'fields_updated': list(update_fields)},
            response_data={'success': True}
        )
        
        return {
            'success': True,
            'message': 'Integration updated successfully'
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating integration: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{integration_id}")
async def delete_integration(
    integration_id: int,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Delete an integration"""
    try:
        merchant_id = current_user.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found")
        
        # Verify ownership
        result = await database.execute("""
            DELETE FROM merchant_integrations 
            WHERE id = :integration_id AND merchant_id = :merchant_id
            RETURNING id
        """, {
            "integration_id": integration_id,
            "merchant_id": merchant_id
        })
        
        if not result:
            raise HTTPException(status_code=404, detail="Integration not found")
        
        return {
            'success': True,
            'message': 'Integration deleted successfully'
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting integration: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{integration_id}/webhooks/sync")
async def sync_webhooks(
    integration_id: int,
    webhook_url: str = Body(...),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Sync webhooks for an integration"""
    try:
        merchant_id = current_user.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found")
        
        # Get integration details
        integration = await database.fetch_one("""
            SELECT mi.*, mis.encrypted_credentials
            FROM merchant_integrations mi
            JOIN merchant_integration_secrets mis ON mi.id = mis.integration_id
            WHERE mi.id = :integration_id AND mi.merchant_id = :merchant_id
        """, {
            "integration_id": integration_id,
            "merchant_id": merchant_id
        })
        
        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")
        
        # Decrypt credentials
        credentials = decrypt_data(integration['encrypted_credentials'])
        
        webhook_result = {
            'success': False,
            'message': 'Webhook sync not implemented',
            'webhooks': []
        }
        
        if integration['integration_type'] == 'psp':
            # Create PSP adapter
            adapter = PSPFactory.create_adapter(integration['provider'], credentials)
            if adapter:
                # Register webhooks
                events = ['payment.completed', 'payment.failed', 'refund.completed']
                result = await adapter.create_webhook(webhook_url, events)
                
                if result.get('success'):
                    webhook_result = {
                        'success': True,
                        'message': 'Webhooks registered successfully',
                        'webhooks': result.get('webhooks', [])
                    }
                    
                    # Store webhook info
                    await database.execute("""
                        UPDATE merchant_integration_settings
                        SET webhook_url = :webhook_url,
                            webhook_secret = :webhook_secret,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE integration_id = :integration_id
                    """, {
                        "integration_id": integration_id,
                        "webhook_url": webhook_url,
                        "webhook_secret": result.get('webhook_secret', '')
                    })
                else:
                    webhook_result['message'] = result.get('error', 'Failed to register webhooks')
                    
        elif integration['integration_type'] == 'platform':
            # Create platform adapter
            adapter = EcommerceFactory.create_adapter(integration['provider'], credentials)
            if adapter and hasattr(adapter, 'register_webhooks'):
                result = await adapter.register_webhooks(webhook_url)
                
                if result.get('success'):
                    webhook_result = {
                        'success': True,
                        'message': 'Webhooks registered successfully',
                        'webhooks': result.get('webhooks', [])
                    }
                    
                    # Store webhook registrations
                    for webhook in result.get('webhooks', []):
                        await database.execute("""
                            INSERT INTO merchant_integration_webhooks
                            (integration_id, event_type, external_webhook_id, endpoint_url)
                            VALUES (:integration_id, :event_type, :webhook_id, :endpoint_url)
                            ON CONFLICT (integration_id, event_type) 
                            DO UPDATE SET 
                                external_webhook_id = EXCLUDED.external_webhook_id,
                                endpoint_url = EXCLUDED.endpoint_url,
                                updated_at = CURRENT_TIMESTAMP
                        """, {
                            "integration_id": integration_id,
                            "event_type": webhook.get('topic', webhook.get('name', '')),
                            "webhook_id": webhook.get('id', ''),
                            "endpoint_url": webhook.get('destination', webhook.get('delivery_url', ''))
                        })
                else:
                    webhook_result['message'] = result.get('error', 'Failed to register webhooks')
            else:
                webhook_result['message'] = f"Webhook sync not supported for {integration['provider']}"
        
        # Log activity
        await log_integration_activity(
            integration_id=integration_id,
            log_type='webhook',
            request_data={'action': 'sync_webhooks', 'webhook_url': webhook_url},
            response_data=webhook_result,
            status_code=200 if webhook_result['success'] else 400
        )
        
        return {
            'success': True,
            'sync_result': webhook_result
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing webhooks: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{integration_id}/logs")
async def get_integration_logs(
    integration_id: int,
    log_type: Optional[str] = None,
    limit: int = 100,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get integration activity logs"""
    try:
        merchant_id = current_user.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found")
        
        # Verify ownership
        integration = await database.fetch_one("""
            SELECT * FROM merchant_integrations 
            WHERE id = :integration_id AND merchant_id = :merchant_id
        """, {
            "integration_id": integration_id,
            "merchant_id": merchant_id
        })
        
        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")
        
        # Get logs
        query = """
            SELECT * FROM merchant_integration_logs 
            WHERE integration_id = :integration_id
        """
        params = {"integration_id": integration_id}
        
        if log_type:
            query += " AND log_type = :log_type"
            params["log_type"] = log_type
        
        query += " ORDER BY created_at DESC LIMIT :limit"
        params["limit"] = limit
        
        logs = await database.fetch_all(query, params)
        
        # Format logs
        formatted_logs = []
        for log in logs:
            formatted_logs.append({
                'id': log['id'],
                'log_type': log['log_type'],
                'status_code': log['status_code'],
                'error_message': log['error_message'],
                'duration_ms': log['duration_ms'],
                'created_at': str(log['created_at']),
                'request_summary': {
                    'action': log.get('request_data', {}).get('action') if log.get('request_data') else None
                },
                'response_summary': {
                    'success': log.get('response_data', {}).get('success') if log.get('response_data') else None,
                    'message': log.get('response_data', {}).get('message') if log.get('response_data') else None
                }
            })
        
        return {
            'success': True,
            'logs': formatted_logs,
            'total': len(formatted_logs)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting integration logs: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
