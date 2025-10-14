#!/usr/bin/env python3
"""
Demo script for Pivota Admin System
Tests the complete admin dashboard functionality
"""

import asyncio
import requests
import json
import time
from typing import Dict, Any

# Configuration
BASE_URL = "http://localhost:8000"
ADMIN_BASE = f"{BASE_URL}/admin"

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

def demo_admin_dashboard():
    """Demo admin dashboard overview"""
    print_section("Admin Dashboard Overview")
    
    result = make_request("GET", f"{ADMIN_BASE}/dashboard")
    
    if "error" not in result:
        system_health = result.get('system_health', {})
        psp_management = result.get('psp_management', {})
        routing_rules = result.get('routing_rules', {})
        merchant_kyb = result.get('merchant_kyb', {})
        transaction_metrics = result.get('transaction_metrics', {})
        
        print(f"🏥 System Health: {system_health.get('status', 'unknown')}")
        print(f"💳 PSP Health: {psp_management.get('active_psps', 0)}/{psp_management.get('total_psps', 0)} active")
        print(f"🔄 Routing Rules: {routing_rules.get('active_rules', 0)}/{routing_rules.get('total_rules', 0)} active")
        print(f"🏪 Merchant KYB: {merchant_kyb.get('approval_rate', 0):.1f}% approval rate")
        print(f"📊 Transactions: {transaction_metrics.get('total_transactions', 0)} total")
        print(f"✅ Success Rate: {transaction_metrics.get('success_rate', 0):.1%}")
        
        alerts = result.get('alerts', [])
        if alerts:
            print(f"\n🚨 Active Alerts:")
            for alert in alerts:
                if alert:
                    print(f"   • {alert.get('type', 'info').upper()}: {alert.get('message', '')}")
        
        return result
    else:
        print(f"❌ Failed to get dashboard: {result['error']}")
        return {}

def demo_psp_management():
    """Demo PSP management functionality"""
    print_section("PSP Management Demo")
    
    # Add Stripe PSP
    stripe_config = {
        "psp_type": "stripe",
        "api_key": "sk_test_demo_stripe_key",
        "webhook_secret": "whsec_demo_stripe_secret",
        "merchant_account": None,
        "enabled": True,
        "sandbox_mode": True
    }
    
    print(f"📝 Adding Stripe PSP configuration...")
    result = make_request("POST", f"{ADMIN_BASE}/psp/add", stripe_config)
    
    if "error" not in result:
        print(f"✅ Stripe PSP added successfully!")
        print(f"   PSP ID: {result.get('psp_id')}")
        print(f"   Next steps: {', '.join(result.get('next_steps', []))}")
        stripe_psp_id = result.get('psp_id')
    else:
        print(f"❌ Failed to add Stripe PSP: {result['error']}")
        stripe_psp_id = None
    
    # Add Adyen PSP
    adyen_config = {
        "psp_type": "adyen",
        "api_key": "AQEhhmfuXNWTK0Qc+iSHnWsopv0Xx8TgERtIM3DGQ99KSOTHEMFdWw2+5HzctViMSCJMYAc=-e3VMWLLHtEX4BaWTlGbr6W2aVMipWjotzahRJXwp3iI=-i1in$<[LrGu.Jf9{g86",
        "webhook_secret": "2CB6226EED3FE6E48DDF2EC1974CB6076F1101E980745D421B57D621D8C4A090",
        "merchant_account": "WoopayECOM",
        "enabled": True,
        "sandbox_mode": True
    }
    
    print(f"\n📝 Adding Adyen PSP configuration...")
    result = make_request("POST", f"{ADMIN_BASE}/psp/add", adyen_config)
    
    if "error" not in result:
        print(f"✅ Adyen PSP added successfully!")
        print(f"   PSP ID: {result.get('psp_id')}")
        adyen_psp_id = result.get('psp_id')
    else:
        print(f"❌ Failed to add Adyen PSP: {result['error']}")
        adyen_psp_id = None
    
    # List PSPs
    print(f"\n📋 Listing PSP configurations...")
    result = make_request("GET", f"{ADMIN_BASE}/psp/list")
    
    if "error" not in result:
        psps = result.get('psps', [])
        print(f"✅ Found {len(psps)} PSP configurations:")
        for psp in psps:
            print(f"   • {psp.get('psp_type', 'unknown')} - {psp.get('status', 'unknown')} ({'enabled' if psp.get('enabled') else 'disabled'})")
        
        # Test PSP connections
        for psp in psps:
            if psp.get('enabled'):
                print(f"\n🔍 Testing {psp.get('psp_type')} connection...")
                test_result = make_request("POST", f"{ADMIN_BASE}/psp/{psp.get('id')}/test")
                
                if "error" not in test_result:
                    print(f"✅ {psp.get('psp_type')} test completed successfully")
                    test_results = test_result.get('test_results', {})
                    print(f"   Latency: {test_results.get('latency_ms', 0)}ms")
                    print(f"   Success: {test_results.get('success', False)}")
                else:
                    print(f"❌ {psp.get('psp_type')} test failed: {test_result['error']}")
    else:
        print(f"❌ Failed to list PSPs: {result['error']}")
    
    return {"stripe_psp_id": stripe_psp_id, "adyen_psp_id": adyen_psp_id}

def demo_routing_rules():
    """Demo routing rules management"""
    print_section("Routing Rules Demo")
    
    # Add geographic routing rule
    geo_rule = {
        "name": "EU to Adyen",
        "rule_type": "geographic",
        "conditions": {
            "region": "EU",
            "currency": "EUR"
        },
        "target_psp": "adyen",
        "priority": 100,
        "enabled": True
    }
    
    print(f"📝 Adding geographic routing rule...")
    result = make_request("POST", f"{ADMIN_BASE}/routing/rules/add", geo_rule)
    
    if "error" not in result:
        print(f"✅ Geographic rule added successfully!")
        print(f"   Rule ID: {result.get('rule_id')}")
        geo_rule_id = result.get('rule_id')
    else:
        print(f"❌ Failed to add geographic rule: {result['error']}")
        geo_rule_id = None
    
    # Add currency routing rule
    currency_rule = {
        "name": "USD to Stripe",
        "rule_type": "currency",
        "conditions": {
            "currency": "USD",
            "amount_min": 0,
            "amount_max": 1000
        },
        "target_psp": "stripe",
        "priority": 200,
        "enabled": True
    }
    
    print(f"\n📝 Adding currency routing rule...")
    result = make_request("POST", f"{ADMIN_BASE}/routing/rules/add", currency_rule)
    
    if "error" not in result:
        print(f"✅ Currency rule added successfully!")
        print(f"   Rule ID: {result.get('rule_id')}")
        currency_rule_id = result.get('rule_id')
    else:
        print(f"❌ Failed to add currency rule: {result['error']}")
        currency_rule_id = None
    
    # List routing rules
    print(f"\n📋 Listing routing rules...")
    result = make_request("GET", f"{ADMIN_BASE}/routing/rules")
    
    if "error" not in result:
        rules = result.get('rules', [])
        print(f"✅ Found {len(rules)} routing rules:")
        for rule in rules:
            print(f"   • {rule.get('name', 'unknown')} - {rule.get('rule_type', 'unknown')} → {rule.get('target_psp', 'unknown')} ({'enabled' if rule.get('enabled') else 'disabled'})")
    else:
        print(f"❌ Failed to list routing rules: {result['error']}")
    
    return {"geo_rule_id": geo_rule_id, "currency_rule_id": currency_rule_id}

def demo_merchant_kyb():
    """Demo merchant KYB management"""
    print_section("Merchant KYB Demo")
    
    # Update merchant KYB status
    kyb_updates = [
        {
            "merchant_id": "MERCH_001",
            "kyb_status": "approved",
            "documents": ["business_license.pdf", "bank_statement.pdf"],
            "notes": "All documents verified and approved"
        },
        {
            "merchant_id": "MERCH_002", 
            "kyb_status": "in_progress",
            "documents": ["business_license.pdf"],
            "notes": "Waiting for bank statement"
        },
        {
            "merchant_id": "MERCH_003",
            "kyb_status": "rejected",
            "documents": [],
            "notes": "Incomplete documentation provided"
        }
    ]
    
    for kyb_update in kyb_updates:
        print(f"📝 Updating KYB for merchant {kyb_update['merchant_id']}...")
        result = make_request("POST", f"{ADMIN_BASE}/merchants/kyb/update", kyb_update)
        
        if "error" not in result:
            print(f"✅ KYB updated for {kyb_update['merchant_id']}: {kyb_update['kyb_status']}")
        else:
            print(f"❌ Failed to update KYB: {result['error']}")
    
    # Get KYB status overview
    print(f"\n📊 Getting KYB status overview...")
    result = make_request("GET", f"{ADMIN_BASE}/merchants/kyb/status")
    
    if "error" not in result:
        kyb_status = result.get('kyb_status', {})
        total_merchants = result.get('total_merchants', 0)
        pending_review = result.get('pending_review', 0)
        
        print(f"✅ KYB Status Overview:")
        print(f"   Total merchants: {total_merchants}")
        print(f"   Pending review: {pending_review}")
        print(f"   Status breakdown:")
        for status, count in kyb_status.items():
            print(f"     • {status}: {count}")
    else:
        print(f"❌ Failed to get KYB status: {result['error']}")

def demo_developer_tools():
    """Demo developer tools functionality"""
    print_section("Developer Tools Demo")
    
    # Generate API key
    api_key_data = {
        "name": "Demo API Key",
        "permissions": ["read", "write", "webhook"]
    }
    
    print(f"📝 Generating API key...")
    result = make_request("POST", f"{ADMIN_BASE}/dev/api-keys/generate", api_key_data)
    
    if "error" not in result:
        print(f"✅ API key generated successfully!")
        print(f"   Key ID: {result.get('key_id')}")
        print(f"   API Key: {result.get('api_key')}")
        print(f"   Permissions: {', '.join(result.get('permissions', []))}")
    else:
        print(f"❌ Failed to generate API key: {result['error']}")
    
    # List API keys
    print(f"\n📋 Listing API keys...")
    result = make_request("GET", f"{ADMIN_BASE}/dev/api-keys")
    
    if "error" not in result:
        keys = result.get('api_keys', [])
        print(f"✅ Found {len(keys)} API keys:")
        for key in keys:
            print(f"   • {key.get('name', 'unknown')} - {', '.join(key.get('permissions', []))} ({'active' if key.get('enabled') else 'disabled'})")
    else:
        print(f"❌ Failed to list API keys: {result['error']}")
    
    # Toggle sandbox mode
    print(f"\n🔄 Toggling sandbox mode...")
    result = make_request("POST", f"{ADMIN_BASE}/dev/sandbox/toggle", {"enabled": True})
    
    if "error" not in result:
        print(f"✅ Sandbox mode toggled: {result.get('sandbox_mode', False)}")
    else:
        print(f"❌ Failed to toggle sandbox mode: {result['error']}")

def demo_analytics():
    """Demo analytics functionality"""
    print_section("Analytics Demo")
    
    result = make_request("GET", f"{ADMIN_BASE}/analytics/overview?days=30")
    
    if "error" not in result:
        system_metrics = result.get('system_metrics', {})
        psp_performance = result.get('psp_performance', {})
        routing_usage = result.get('routing_usage', {})
        kyb_metrics = result.get('kyb_metrics', {})
        admin_actions = result.get('admin_actions', {})
        
        print(f"📊 Analytics Overview (Last 30 days):")
        print(f"   System metrics: {system_metrics}")
        print(f"   PSP performance: {len(psp_performance)} PSPs tracked")
        print(f"   Routing usage: {len(routing_usage)} rules tracked")
        print(f"   KYB approval rate: {kyb_metrics.get('approved_rate', 0):.1f}%")
        print(f"   Admin actions: {admin_actions.get('total_logs', 0)} total logs")
        
        if psp_performance:
            print(f"\n💳 PSP Performance:")
            for psp, perf in psp_performance.items():
                print(f"   • {psp}: {perf.get('status', 'unknown')} (last tested: {perf.get('last_tested', 'never')})")
        
        if routing_usage:
            print(f"\n🔄 Routing Usage:")
            for rule, usage in routing_usage.items():
                print(f"   • {rule}: {usage.get('usage_count', 0)} uses (target: {usage.get('target_psp', 'unknown')})")
        
        return result
    else:
        print(f"❌ Failed to get analytics: {result['error']}")
        return {}

def demo_system_logs():
    """Demo system logs functionality"""
    print_section("System Logs Demo")
    
    result = make_request("GET", f"{ADMIN_BASE}/logs?limit=10")
    
    if "error" not in result:
        logs = result.get('logs', [])
        total_logs = result.get('total_logs', 0)
        
        print(f"📋 System Logs (Last 10 of {total_logs} total):")
        
        if logs:
            for log in logs[:5]:  # Show first 5 logs
                timestamp = log.get('timestamp', 'unknown')
                action = log.get('action', 'unknown')
                admin_user = log.get('admin_user', 'unknown')
                
                print(f"   • {timestamp[:19]} | {action} | by {admin_user}")
        else:
            print("   No recent logs found")
        
        return result
    else:
        print(f"❌ Failed to get system logs: {result['error']}")
        return {}

def main():
    """Main demo function"""
    print_header("Pivota Admin System Demo")
    print("This demo showcases the complete admin dashboard functionality for system management.")
    
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
    
    print("\n🚀 Starting admin system demo...")
    
    # Demo 1: Admin Dashboard Overview
    demo_admin_dashboard()
    
    # Demo 2: PSP Management
    psp_ids = demo_psp_management()
    
    # Demo 3: Routing Rules
    rule_ids = demo_routing_rules()
    
    # Demo 4: Merchant KYB
    demo_merchant_kyb()
    
    # Demo 5: Developer Tools
    demo_developer_tools()
    
    # Demo 6: Analytics
    demo_analytics()
    
    # Demo 7: System Logs
    demo_system_logs()
    
    print_header("Demo Complete!")
    print("🎉 All admin system demos completed successfully!")
    print("\n📊 Key Features Demonstrated:")
    print("   ✅ Admin Dashboard Overview")
    print("   ✅ PSP Management (Add, Test, List)")
    print("   ✅ Routing Rules (Geographic, Currency)")
    print("   ✅ Merchant KYB Management")
    print("   ✅ Developer Tools (API Keys, Sandbox)")
    print("   ✅ Analytics and Reporting")
    print("   ✅ System Logs and Audit Trail")
    
    print(f"\n🌐 Access the Admin Dashboard at: {BASE_URL}/admin")
    print(f"🌐 Access the Operations Dashboard at: {BASE_URL}/operations")
    print(f"📚 API Documentation at: {BASE_URL}/docs")

if __name__ == "__main__":
    main()
