"""
Runs the user's MCPClient sample against the live API through the compat wrapper.
"""
import os
from pivota_agent.compat import MCPClient


def main():
    api_key = os.getenv("PIVOTA_AGENT_API_KEY", "")
    if not api_key:
        raise SystemExit("Set PIVOTA_AGENT_API_KEY to a valid agent key")

    client = MCPClient(base_url="https://web-production-fedb.up.railway.app", api_key=api_key)

    print("Health:", client.health_check())

    merchants = client.list_merchants()
    print(f"Merchants: {len(merchants)}")
    if not merchants:
        raise SystemExit("No merchants available to test")

    first_id = merchants[0]["id"]
    products = client.search_products("shoes", merchant_ids=[first_id])
    print(f"Products returned: {len(products)}")

    if products:
        # Place order if backend supports it
        try:
            order = client.place_order(first_id, {
                "products": [{"id": products[0]["id"], "quantity": 1}],
                "customer": {"email": "buyer@example.com"}
            })
            print("Order:", order)

            try:
                payment = client.initiate_payment(order.get("order_id") or order.get("id"), {
                    "method": "card",
                    "token": "tok_visa_test"
                })
                print("Payment:", payment)
            except Exception as e:
                print("Payment endpoint not available:", e)

        except Exception as e:
            print("Order endpoint not available:", e)


if __name__ == "__main__":
    main()







