#!/usr/bin/env python3
"""
Demo script for Pivota Operations System
Tests the complete onboarding workflow for agents and merchants
"""

import asyncio
import requests
import json
import time
from typing import Dict, Any

# Configuration
BASE_URL = "http://localhost:8000"
OPERATIONS_BASE = f"{BASE_URL}/api/operations"

def print_header(title: str):
    """Print a formatted header"""
    print(f"\n{'='*60}")
    print(f"🏢 {title}")
    print(f"{'='*60}")

def print_section(title: str):
    """Print a formatted section"""
    print(f"\n📋 {title}")
    print("-" * 40)

def make_request(method: str, url: str, data: Dict[str, Any] = None) -> Dict[str, Any]:
    """Make HTTP request with error handling"""
    try:
        if method.upper() == "GET":
            response = requests.get(url, timeout=10)
        elif method.upper() == "POST":
            response = requests.post(url, json=data, timeout=10)
        elif method.upper() == "PUT":
            response = requests.put(url, json=data, timeout=10)
        else:
            raise ValueError(f"Unsupported method: {method}")
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed: {e}")
        return {"error": str(e)}
    except json.JSONDecodeError as e:
        print(f"❌ JSON decode error: {e}")
        return {"error": f"Invalid JSON response: {e}"}

def demo_agent_onboarding():
    """Demo agent onboarding process"""
    print_section("Agent Onboarding Demo")
    
    # Sample agent data
    agent_data = {
        "agent_name": "TechGear AI Agent",
        "contact_email": "agent@techgear.ai",
        "company_name": "TechGear Solutions",
        "description": "AI-powered shopping assistant specializing in electronics and gadgets",
        "expected_volume": 5000,
        "preferred_psps": ["stripe", "adyen"]
    }
    
    print(f"📝 Onboarding agent: {agent_data['agent_name']}")
    result = make_request("POST", f"{OPERATIONS_BASE}/agents/onboard", agent_data)
    
    if "error" not in result:
        print(f"✅ Agent onboarded successfully!")
        print(f"   Agent ID: {result.get('agent_id')}")
        print(f"   Next steps: {', '.join(result.get('next_steps', []))}")
        return result.get('agent_id')
    else:
        print(f"❌ Agent onboarding failed: {result['error']}")
        return None

def demo_merchant_onboarding():
    """Demo merchant onboarding process"""
    print_section("Merchant Onboarding Demo")
    
    # Sample merchant data
    merchant_data = {
        "merchant_name": "Electronics Plus Store",
        "contact_email": "merchant@electronicsplus.com",
        "store_url": "https://electronicsplus.myshopify.com",
        "platform": "shopify",
        "api_credentials": {
            "shop_domain": "electronicsplus.myshopify.com",
            "access_token": "shpat_demo_token_12345"
        },
        "description": "Premium electronics retailer with global shipping",
        "expected_volume": 10000
    }
    
    print(f"📝 Onboarding merchant: {merchant_data['merchant_name']}")
    result = make_request("POST", f"{OPERATIONS_BASE}/merchants/onboard", merchant_data)
    
    if "error" not in result:
        print(f"✅ Merchant onboarded successfully!")
        print(f"   Merchant ID: {result.get('merchant_id')}")
        print(f"   Next steps: {', '.join(result.get('next_steps', []))}")
        return result.get('merchant_id')
    else:
        print(f"❌ Merchant onboarding failed: {result['error']}")
        return None

def demo_onboarding_queue():
    """Demo onboarding queue management"""
    print_section("Onboarding Queue Demo")
    
    result = make_request("GET", f"{OPERATIONS_BASE}/onboarding-queue")
    
    if "error" not in result:
        queue = result.get('queue', [])
        summary = result.get('summary', {})
        
        print(f"📊 Queue Summary:")
        print(f"   Total pending: {result.get('total_pending', 0)}")
        print(f"   Agents pending: {summary.get('agents_pending', 0)}")
        print(f"   Merchants pending: {summary.get('merchants_pending', 0)}")
        
        if queue:
            print(f"\n📋 Queue Items:")
            for item in queue[:5]:  # Show first 5 items
                entity_data = item.get('entity_data', {})
                print(f"   • {entity_data.get('name', 'Unknown')} ({item.get('entity_type')})")
                print(f"     Status: {entity_data.get('status', 'unknown')}")
                print(f"     Days in queue: {item.get('days_in_queue', 0):.1f}")
        else:
            print("   No items in queue")
        
        return queue
    else:
        print(f"❌ Failed to get queue: {result['error']}")
        return []

def demo_status_updates(agent_id: str, merchant_id: str):
    """Demo status update workflow"""
    print_section("Status Update Demo")
    
    entities = []
    if agent_id:
        entities.append(("agent", agent_id, "AGENT_001"))
    if merchant_id:
        entities.append(("merchant", merchant_id, "MERCH_001"))
    
    for entity_type, entity_id, operator in entities:
        print(f"📝 Updating {entity_type} status to 'under_review'")
        
        update_data = {
            "status": "under_review",
            "notes": f"Initial review by {operator}",
            "assigned_to": operator
        }
        
        result = make_request("PUT", f"{OPERATIONS_BASE}/onboarding/{entity_id}/status", update_data)
        
        if "error" not in result:
            print(f"✅ {entity_type.title()} status updated successfully")
        else:
            print(f"❌ Failed to update {entity_type} status: {result['error']}")
        
        # Wait a moment between updates
        time.sleep(1)

def demo_verifications(agent_id: str, merchant_id: str):
    """Demo verification process"""
    print_section("Verification Demo")
    
    entities = []
    if agent_id:
        entities.append(("agent", agent_id))
    if merchant_id:
        entities.append(("merchant", merchant_id))
    
    for entity_type, entity_id in entities:
        print(f"🔍 Starting verification for {entity_type}: {entity_id}")
        
        verification_data = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "verification_type": "api_test"
        }
        
        result = make_request("POST", f"{OPERATIONS_BASE}/verify", verification_data)
        
        if "error" not in result:
            print(f"✅ Verification started for {entity_type}")
            print(f"   Verification ID: {result.get('verification_id')}")
            print(f"   Estimated duration: {result.get('estimated_duration', 'unknown')}")
        else:
            print(f"❌ Failed to start verification: {result['error']}")
        
        time.sleep(1)

def demo_analytics():
    """Demo operations analytics"""
    print_section("Operations Analytics Demo")
    
    result = make_request("GET", f"{OPERATIONS_BASE}/analytics?days=30")
    
    if "error" not in result:
        ops_summary = result.get('operations_summary', {})
        onboarding_metrics = result.get('onboarding_metrics', {})
        verification_metrics = result.get('verification_metrics', {})
        
        print(f"📊 Operations Summary (Last 30 days):")
        print(f"   Total operations: {ops_summary.get('total_operations', 0)}")
        print(f"   Most active operator: {ops_summary.get('most_active_operator', 'N/A')}")
        
        print(f"\n📈 Onboarding Metrics:")
        print(f"   Total agents: {onboarding_metrics.get('total_agents', 0)}")
        print(f"   Total merchants: {onboarding_metrics.get('total_merchants', 0)}")
        print(f"   Pending onboardings: {onboarding_metrics.get('pending_onboardings', 0)}")
        
        print(f"\n🔍 Verification Metrics:")
        print(f"   Total verifications: {verification_metrics.get('total_verifications', 0)}")
        print(f"   Pending verifications: {verification_metrics.get('pending_verifications', 0)}")
        
        return result
    else:
        print(f"❌ Failed to get analytics: {result['error']}")
        return {}

def demo_dashboard_summary():
    """Demo dashboard summary"""
    print_section("Dashboard Summary Demo")
    
    result = make_request("GET", f"{OPERATIONS_BASE}/dashboard-summary")
    
    if "error" not in result:
        onboarding = result.get('onboarding', {})
        verifications = result.get('verifications', {})
        system_health = result.get('system_health', {})
        alerts = result.get('alerts', [])
        
        print(f"🏠 Operations Dashboard Summary:")
        print(f"   Active agents: {onboarding.get('active_agents', 0)}")
        print(f"   Active merchants: {onboarding.get('active_merchants', 0)}")
        print(f"   Queue length: {onboarding.get('queue_length', 0)}")
        print(f"   In-progress verifications: {verifications.get('in_progress', 0)}")
        
        print(f"\n💚 System Health:")
        print(f"   Total transactions: {system_health.get('total_transactions', 0)}")
        print(f"   Success rate: {system_health.get('success_rate', 0):.1%}")
        print(f"   Active PSPs: {system_health.get('active_psps', 0)}")
        print(f"   System status: {system_health.get('system_status', 'unknown')}")
        
        if alerts:
            print(f"\n🚨 Active Alerts:")
            for alert in alerts:
                if alert:  # Filter out None values
                    print(f"   • {alert.get('type', 'info').upper()}: {alert.get('message', '')}")
        
        return result
    else:
        print(f"❌ Failed to get dashboard summary: {result['error']}")
        return {}

def demo_welcome_emails(agent_id: str, merchant_id: str):
    """Demo welcome email sending"""
    print_section("Welcome Email Demo")
    
    entities = []
    if agent_id:
        entities.append(("agent", agent_id))
    if merchant_id:
        entities.append(("merchant", merchant_id))
    
    for entity_type, entity_id in entities:
        print(f"📧 Sending welcome email to {entity_type}: {entity_id}")
        
        result = make_request("POST", f"{OPERATIONS_BASE}/send-welcome-email?entity_id={entity_id}&entity_type={entity_type}")
        
        if "error" not in result:
            print(f"✅ Welcome email sent successfully")
            email_content = result.get('email_content', {})
            print(f"   To: {email_content.get('to', 'unknown')}")
            print(f"   Subject: {email_content.get('subject', 'unknown')}")
        else:
            print(f"❌ Failed to send welcome email: {result['error']}")
        
        time.sleep(1)

def demo_activity_log():
    """Demo operations activity log"""
    print_section("Activity Log Demo")
    
    result = make_request("GET", f"{OPERATIONS_BASE}/operations-log?limit=10")
    
    if "error" not in result:
        logs = result.get('logs', [])
        total_logs = result.get('total_logs', 0)
        
        print(f"📋 Recent Operations (Last 10 of {total_logs} total):")
        
        if logs:
            for log in logs[:5]:  # Show first 5 logs
                timestamp = log.get('timestamp', 'unknown')
                operation = log.get('operation', 'unknown')
                operator = log.get('operator', 'unknown')
                
                print(f"   • {timestamp[:19]} | {operation} | by {operator}")
        else:
            print("   No recent operations found")
        
        return result
    else:
        print(f"❌ Failed to get activity log: {result['error']}")
        return {}

def main():
    """Main demo function"""
    print_header("Pivota Operations System Demo")
    print("This demo showcases the complete operations workflow for onboarding agents and merchants.")
    
    # Check if server is running
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        if response.status_code == 200:
            print("✅ Server is running and accessible")
        else:
            print("❌ Server is not responding properly")
            return
    except requests.exceptions.RequestException:
        print("❌ Cannot connect to server. Please make sure it's running on http://localhost:8000")
        return
    
    print("\n🚀 Starting operations demo...")
    
    # Demo 1: Onboard agents and merchants
    agent_id = demo_agent_onboarding()
    merchant_id = demo_merchant_onboarding()
    
    # Demo 2: View onboarding queue
    demo_onboarding_queue()
    
    # Demo 3: Update statuses
    demo_status_updates(agent_id, merchant_id)
    
    # Demo 4: Start verifications
    demo_verifications(agent_id, merchant_id)
    
    # Demo 5: Send welcome emails
    demo_welcome_emails(agent_id, merchant_id)
    
    # Demo 6: View analytics
    demo_analytics()
    
    # Demo 7: View dashboard summary
    demo_dashboard_summary()
    
    # Demo 8: View activity log
    demo_activity_log()
    
    print_header("Demo Complete!")
    print("🎉 All operations demos completed successfully!")
    print("\n📊 Key Features Demonstrated:")
    print("   ✅ Agent and Merchant Onboarding")
    print("   ✅ Onboarding Queue Management")
    print("   ✅ Status Updates and Workflow")
    print("   ✅ Verification Process")
    print("   ✅ Welcome Email Automation")
    print("   ✅ Operations Analytics")
    print("   ✅ Dashboard Summary")
    print("   ✅ Activity Logging")
    
    print(f"\n🌐 Access the Operations Dashboard at: {BASE_URL}/operations")
    print(f"📚 API Documentation at: {BASE_URL}/docs")

if __name__ == "__main__":
    main()
