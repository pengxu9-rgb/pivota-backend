#!/usr/bin/env python3
"""
FINAL END-TO-END TEST
=====================
Tests the complete flow:
1. Agent creation
2. Product search (if available)
3. Order creation via Agent
4. Payment confirmation (if PSP configured)
5. Shopify order creation (if MCP connected)
6. Email notification verification
"""

import sys
import json
import requests
import time
from datetime import datetime

# Add SDK to path
sys.path.insert(0, 'pivota_sdk')

# Configuration
BASE_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"  # chydantest
YOUR_EMAIL = "peng@chydan.com"  # Your email for order confirmation


class Colors:
    HEADER = '\033[95m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    OKBLUE = '\033[94m'


def print_header(msg):
    print(f"\n{Colors.BOLD}{Colors.HEADER}{'='*70}{Colors.ENDC}")
    print(f"{Colors.BOLD}{Colors.HEADER}  {msg}{Colors.ENDC}")
    print(f"{Colors.BOLD}{Colors.HEADER}{'='*70}{Colors.ENDC}\n")


def print_success(msg):
    print(f"{Colors.OKGREEN}✅ {msg}{Colors.ENDC}")


def print_error(msg):
    print(f"{Colors.FAIL}❌ {msg}{Colors.ENDC}")


def print_warning(msg):
    print(f"{Colors.WARNING}⚠️  {msg}{Colors.ENDC}")


def print_info(msg):
    print(f"{Colors.OKBLUE}ℹ️  {msg}{Colors.ENDC}")


class FinalE2ETest:
    def __init__(self):
        self.admin_token = None
        self.agent = None
        self.order = None
        self.results = []
        
    def get_admin_token(self):
        """Get admin token"""
        resp = requests.get(f"{BASE_URL}/auth/admin-token")
        return resp.json()["token"]
    
    def test_agent_creation(self):
        """Step 1: Create Agent"""
        print_header("STEP 1: CREATE AGENT")
        
        agent_data = {
            "agent_name": f"E2E Test Agent {datetime.now().strftime('%H%M%S')}",
            "agent_type": "e2e_test",
            "description": "Final End-to-End Test Agent",
            "rate_limit": 1000,
            "daily_quota": 10000,
            "owner_email": YOUR_EMAIL
        }
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        resp = requests.post(f"{BASE_URL}/agents/create", json=agent_data, headers=headers)
        
        if resp.status_code == 200:
            data = resp.json()
            self.agent = data.get('agent', data)
            print_success(f"Agent created: {self.agent['agent_id']}")
            print_info(f"API Key: {self.agent['api_key'][:30]}...")
            self.results.append(("Agent Creation", "SUCCESS", self.agent['agent_id']))
            return True
        else:
            print_error(f"Agent creation failed: {resp.text}")
            self.results.append(("Agent Creation", "FAILED", resp.text[:100]))
            return False
    
    def test_product_search(self):
        """Step 2: Search Products"""
        print_header("STEP 2: SEARCH PRODUCTS (OPTIONAL)")
        
        try:
            from pivota_agent import PivotaAgent
            agent = PivotaAgent(api_key=self.agent['api_key'], base_url=BASE_URL)
            
            # Try to search products
            try:
                products = agent.search_products(merchant_id=MERCHANT_ID, limit=5)
                if products:
                    print_success(f"Found {len(products)} products")
                    for p in products[:3]:
                        print_info(f"  • {p.get('title', 'Unknown')} - ${p.get('price', 0)}")
                    self.results.append(("Product Search", "SUCCESS", f"{len(products)} products"))
                else:
                    print_warning("No products found")
                    self.results.append(("Product Search", "NO DATA", "0 products"))
            except Exception as e:
                print_warning(f"Product search not available: {e}")
                self.results.append(("Product Search", "SKIPPED", str(e)[:100]))
        except ImportError:
            print_warning("SDK not available, skipping product search")
            self.results.append(("Product Search", "SKIPPED", "SDK not found"))
    
    def test_order_creation(self):
        """Step 3: Create Order via Agent"""
        print_header("STEP 3: CREATE ORDER VIA AGENT")
        
        try:
            from pivota_agent import PivotaAgent
            agent = PivotaAgent(api_key=self.agent['api_key'], base_url=BASE_URL)
            
            items = [
                {
                    "product_id": "final_test_001",
                    "product_title": "Premium Test Product",
                    "quantity": 1,
                    "unit_price": "149.99",
                    "subtotal": "149.99"
                },
                {
                    "product_id": "final_test_002",
                    "product_title": "Standard Test Product",
                    "quantity": 2,
                    "unit_price": "49.99",
                    "subtotal": "99.98"
                }
            ]
            
            shipping = {
                "name": "E2E Test Customer",
                "address_line1": "123 Test Street",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94102",
                "country": "US",
                "phone": "+14155551234"
            }
            
            self.order = agent.create_order(
                merchant_id=MERCHANT_ID,
                customer_email=YOUR_EMAIL,
                items=items,
                shipping_address=shipping,
                currency="USD"
            )
            
            print_success(f"Order created: {self.order['order_id']}")
            print_info(f"Total: ${self.order['total']} {self.order['currency']}")
            print_info(f"Status: {self.order.get('status', 'Unknown')}")
            
            if self.order.get('payment_intent_id'):
                print_success(f"Payment Intent: {self.order['payment_intent_id'][:30]}...")
            else:
                print_warning("No payment intent generated (PSP may not be configured)")
            
            self.results.append(("Order Creation", "SUCCESS", self.order['order_id']))
            return True
            
        except Exception as e:
            print_error(f"Order creation failed: {e}")
            self.results.append(("Order Creation", "FAILED", str(e)[:100]))
            return False
    
    def test_payment_confirmation(self):
        """Step 4: Confirm Payment"""
        print_header("STEP 4: PAYMENT CONFIRMATION (IF AVAILABLE)")
        
        if not self.order or not self.order.get('payment_intent_id'):
            print_warning("Skipping payment - no payment intent available")
            self.results.append(("Payment", "SKIPPED", "No payment intent"))
            return False
        
        payment_data = {
            "order_id": self.order["order_id"],
            "payment_method_id": "pm_card_visa"  # Test card
        }
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        resp = requests.post(f"{BASE_URL}/orders/payment/confirm", json=payment_data, headers=headers)
        
        if resp.status_code == 200:
            result = resp.json()
            if result.get('payment_status') == 'succeeded':
                print_success("Payment confirmed!")
                self.results.append(("Payment", "SUCCESS", "Payment confirmed"))
                return True
            else:
                print_warning(f"Payment status: {result.get('payment_status')}")
                self.results.append(("Payment", "PENDING", result.get('payment_status')))
        else:
            print_error(f"Payment confirmation failed: {resp.text[:100]}")
            self.results.append(("Payment", "FAILED", resp.text[:100]))
        
        return False
    
    def test_shopify_order(self):
        """Step 5: Check Shopify Order"""
        print_header("STEP 5: SHOPIFY ORDER STATUS")
        
        if not self.order:
            print_warning("No order to check")
            self.results.append(("Shopify Order", "SKIPPED", "No order"))
            return
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        
        # Wait and check for Shopify order
        print_info("Checking for Shopify order creation...")
        for i in range(5):
            time.sleep(2)
            resp = requests.get(f"{BASE_URL}/orders/{self.order['order_id']}", headers=headers)
            
            if resp.status_code == 200:
                order_data = resp.json()
                if order_data.get('shopify_order_id'):
                    print_success(f"Shopify order created: {order_data['shopify_order_id']}")
                    self.results.append(("Shopify Order", "SUCCESS", order_data['shopify_order_id']))
                    return True
            
            print_info(f"  Attempt {i+1}/5...")
        
        print_warning("Shopify order not created (may still be processing)")
        self.results.append(("Shopify Order", "PENDING", "Still processing"))
        return False
    
    def test_email_notification(self):
        """Step 6: Email Notification"""
        print_header("STEP 6: EMAIL NOTIFICATION")
        
        if self.order:
            print_info(f"📧 Order confirmation should be sent to: {YOUR_EMAIL}")
            print_info("")
            print_info("Please check your email (including spam folder) for:")
            print_info("  • Order confirmation from Shopify")
            print_info("  • Payment receipt")
            print_info("  • Shipping information")
            
            self.results.append(("Email", "CHECK EMAIL", YOUR_EMAIL))
        else:
            print_warning("No order was created, no email will be sent")
            self.results.append(("Email", "SKIPPED", "No order"))
    
    def print_summary(self):
        """Print test summary"""
        print_header("TEST SUMMARY")
        
        # Print results table
        print(f"{'Step':<20} {'Status':<15} {'Details':<35}")
        print("-" * 70)
        
        for step, status, details in self.results:
            # Color code the status
            if status == "SUCCESS":
                status_str = f"{Colors.OKGREEN}{status:<15}{Colors.ENDC}"
            elif status == "FAILED":
                status_str = f"{Colors.FAIL}{status:<15}{Colors.ENDC}"
            elif status in ["SKIPPED", "PENDING"]:
                status_str = f"{Colors.WARNING}{status:<15}{Colors.ENDC}"
            else:
                status_str = f"{status:<15}"
            
            print(f"{step:<20} {status_str} {details:<35}")
        
        # Overall result
        print("\n" + "=" * 70)
        success_count = sum(1 for _, status, _ in self.results if status == "SUCCESS")
        total_count = len(self.results)
        
        if success_count == total_count:
            print(f"{Colors.BOLD}{Colors.OKGREEN}🎉 ALL TESTS PASSED! ({success_count}/{total_count}){Colors.ENDC}")
        elif success_count > 0:
            print(f"{Colors.BOLD}{Colors.WARNING}⚠️  PARTIAL SUCCESS ({success_count}/{total_count}){Colors.ENDC}")
        else:
            print(f"{Colors.BOLD}{Colors.FAIL}❌ TESTS FAILED ({success_count}/{total_count}){Colors.ENDC}")
        
        # Important notes
        print(f"\n{Colors.BOLD}📝 IMPORTANT NOTES:{Colors.ENDC}")
        print("1. Payment processing requires valid PSP credentials")
        print("2. Shopify order creation requires MCP connection")
        print("3. Email notifications depend on both PSP and MCP")
        print(f"4. Check your email at: {YOUR_EMAIL}")
        
        # Save results
        with open("test_final_e2e_results.json", "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "results": self.results,
                "order_id": self.order['order_id'] if self.order else None,
                "agent_id": self.agent['agent_id'] if self.agent else None
            }, f, indent=2)
            print(f"\n{Colors.OKBLUE}Results saved to: test_final_e2e_results.json{Colors.ENDC}")
    
    def run(self):
        """Run the complete test suite"""
        print(f"\n{Colors.BOLD}{Colors.HEADER}{'🚀' * 35}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.HEADER}FINAL END-TO-END TEST SUITE{Colors.ENDC}")
        print(f"{Colors.BOLD}Environment: {BASE_URL}{Colors.ENDC}")
        print(f"{Colors.BOLD}Merchant: {MERCHANT_ID}{Colors.ENDC}")
        print(f"{Colors.BOLD}Customer: {YOUR_EMAIL}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.HEADER}{'🚀' * 35}{Colors.ENDC}\n")
        
        try:
            # Get admin token
            print_info("Obtaining admin token...")
            self.admin_token = self.get_admin_token()
            print_success("Admin token obtained")
            
            # Run tests
            if self.test_agent_creation():
                self.test_product_search()
                if self.test_order_creation():
                    self.test_payment_confirmation()
                    self.test_shopify_order()
                    self.test_email_notification()
            
            # Print summary
            self.print_summary()
            
        except Exception as e:
            print_error(f"Test suite failed: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    test = FinalE2ETest()
    test.run()
