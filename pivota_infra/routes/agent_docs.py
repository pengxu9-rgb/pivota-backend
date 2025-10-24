from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from typing import Any, Dict
import os
import json

router = APIRouter(prefix="/agent/docs", tags=["Agent Docs"])


@router.get("/overview")
async def docs_overview() -> Dict[str, Any]:
    return {
        "title": "Pivota Agent SDK Docs",
        "version": "1.0",
        "sections": [
            {"id": "quickstart", "title": "Quickstart", "path": "/agent/docs/quickstart.md"},
            {"id": "sdks", "title": "SDKs", "path": "/agent/docs/sdks"},
            {"id": "examples-python", "title": "Examples (Python)", "path": "/agent/docs/examples/python"},
            {"id": "examples-typescript", "title": "Examples (TypeScript)", "path": "/agent/docs/examples/typescript"},
            {"id": "endpoints", "title": "Endpoints", "path": "/agent/docs/endpoints"},
            {"id": "openapi", "title": "OpenAPI Spec", "path": "/agent/docs/openapi.json"},
        ],
    }


@router.get("/quickstart.md", response_class=PlainTextResponse)
async def quickstart_markdown() -> str:
    return (
        "# Pivota Agent SDK Quickstart\n\n"
        "## Install\n\n"
        "Python:\n\n"
        "```bash\n"
        "pip install pivota-agent-sdk\n"
        "```\n\n"
        "TypeScript:\n\n"
        "```bash\n"
        "npm install @pivota/agent-sdk\n"
        "```\n\n"
        "## Usage (Python)\n\n"
        "```python\n"
        "from pivota_agent import PivotaAgentClient\n"
        "client = PivotaAgentClient(api_key=\"YOUR_API_KEY\")\n"
        "print(client.health_check())\n"
        "merchants = client.list_merchants(limit=5)\n"
        "products = client.search_products(query=\"coffee\", limit=1)\n"
        "```\n\n"
        "## Usage (TypeScript)\n\n"
        "```ts\n"
        "import { PivotaAgentClient } from '@pivota/agent-sdk'\n"
        "const client = new PivotaAgentClient({ apiKey: process.env.PIVOTA_AGENT_API_KEY })\n"
        "const health = await client.healthCheck()\n"
        "```\n"
    )


@router.get("/sdks")
async def sdks_info() -> Dict[str, Any]:
    return {
        "python": {
            "install": "pip install pivota-agent-sdk",
            "example": (
                "from pivota_agent import PivotaAgentClient\n"
                "client = PivotaAgentClient(api_key='YOUR_API_KEY')\n"
                "print(client.health_check())\n"
            ),
        },
        "typescript": {
            "install": "npm install @pivota/agent-sdk",
            "example": (
                "import { PivotaAgentClient } from '@pivota/agent-sdk'\n"
                "const client = new PivotaAgentClient({ apiKey: process.env.PIVOTA_AGENT_API_KEY })\n"
                "console.log(await client.healthCheck())\n"
            ),
        },
    }


@router.get("/openapi.json")
async def agent_openapi_spec() -> Dict[str, Any]:
    try:
        base_dir = os.path.dirname(os.path.dirname(__file__))
        spec_path = os.path.join(base_dir, "generated-sdks", "agent-api-spec.json")
        with open(spec_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"OpenAPI spec not found: {e}")


@router.get("/examples/python", response_class=PlainTextResponse)
async def example_python() -> str:
    return (
        "from pivota_agent import PivotaAgentClient\n\n"
        "client = PivotaAgentClient(api_key='YOUR_API_KEY')\n"
        "print(client.health_check())\n"
        "merchants = client.list_merchants(limit=5)\n"
        "search = client.search_products(query='coffee', limit=1)\n"
        "prod = search['products'][0]\n"
        "order = client.create_order(\n"
        "    merchant_id=prod['merchant_id'],\n"
        "    items=[{ 'product_id': prod['id'], 'quantity': 2, 'product_title': prod['name'], 'unit_price': float(prod['price']), 'subtotal': float(prod['price']) * 2 }],\n"
        "    customer_email='buyer@example.com',\n"
        "    shipping_address={ 'name': 'John Doe', 'address_line1': '123 Main St', 'city': 'SF', 'state': 'CA', 'postal_code': '94105', 'country': 'US' }\n"
        ")\n"
    )


@router.get("/examples/typescript", response_class=PlainTextResponse)
async def example_typescript() -> str:
    return (
        "import { PivotaAgentClient } from '@pivota/agent-sdk'\n\n"
        "const client = new PivotaAgentClient({ apiKey: process.env.PIVOTA_AGENT_API_KEY })\n"
        "const merchants = await client.listMerchants({ limit: 5 })\n"
        "const search = await client.searchProducts({ query: 'coffee', limit: 1 } as any)\n"
        "const product = (search as any).products?.[0]\n"
        "const order = await client.createOrder({\n"
        "  merchant_id: (product as any).merchant_id,\n"
        "  customer_email: 'buyer@example.com',\n"
        "  items: [{ product_id: product.id, product_title: product.name, quantity: 2, unit_price: Number(product.price)||0, subtotal: (Number(product.price)||0)*2 }],\n"
        "  shipping_address: { name: 'John Doe', address_line1: '123 Main St', city: 'SF', state: 'CA', postal_code: '94105', country: 'US' }\n"
        "} as any)\n"
    )


@router.get("/endpoints")
async def endpoints_summary() -> Dict[str, Any]:
    return {
        "base_url": "https://web-production-fedb.up.railway.app/agent/v1",
        "auth": {"header": "X-API-Key", "example": "ak_live_xxx"},
        "endpoints": [
            {"method": "GET", "path": "/health", "desc": "Health check"},
            {"method": "GET", "path": "/merchants", "desc": "List merchants"},
            {"method": "GET", "path": "/products/search", "desc": "Search products"},
            {"method": "POST", "path": "/orders/create", "desc": "Create order"},
            {"method": "GET", "path": "/orders/{order_id}", "desc": "Get order"},
            {"method": "POST", "path": "/payments", "desc": "Create payment"},
        ],
    }





