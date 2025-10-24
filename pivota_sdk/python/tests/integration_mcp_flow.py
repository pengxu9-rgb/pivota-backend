import os
from pivota_agent.compat import MCPClient


def run_flow():
    api_key = os.getenv("PIVOTA_AGENT_API_KEY", "")
    if not api_key:
        raise SystemExit("Set PIVOTA_AGENT_API_KEY env var")

    client = MCPClient(base_url="https://web-production-fedb.up.railway.app", api_key=api_key)

    print("== Health ==")
    print(client.health_check())

    print("== Merchants ==")
    merchants = client.list_merchants()
    print(f"count={len(merchants)}")
    if not merchants:
        return

    first = merchants[0]
    print(f"first={first.get('business_name')} ({first.get('id')})")

    print("== Search (shoes, merchant-scoped) ==")
    products = client.search_products("shoes", merchant_ids=[first["id"]])
    print(f"count={len(products)}")

    if not products:
        print("no shoes for first merchant; trying cross-merchant coffee")
        products = client.search_products("coffee")
        print(f"coffee count={len(products)}")

    if not products:
        print("no products available to continue order/payment test")
        return

    product = products[0]
    chosen_merchant_id = product.get("merchant_id") or first["id"]
    print(f"chosen product={product.get('name')} merchant={chosen_merchant_id}")

    try:
        print("== Place order ==")
        order = client.place_order(chosen_merchant_id, {
            "products": [{"id": product.get("id"), "quantity": 1}],
            "customer": {"email": "buyer@example.com"}
        })
        print({k: order.get(k) for k in ("order_id", "total", "currency")})

        try:
            print("== Initiate payment ==")
            payment = client.initiate_payment(order.get("order_id") or order.get("id"), {
                "method": "card",
                "token": "tok_visa_test"
            })
            print({k: payment.get(k) for k in ("status", "payment_id", "psp_used")})
        except Exception as e:
            print("payment not available:", str(e))
    except Exception as e:
        print("order not available:", str(e))


if __name__ == "__main__":
    run_flow()





