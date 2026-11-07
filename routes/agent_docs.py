from fastapi import APIRouter, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse
from typing import Any, Dict, List

router = APIRouter(prefix="/agent/docs", tags=["Agent Docs"])

AGENT_DOC_PATH_PREFIXES = (
    "/agent/v1",
    "/agent/v2",
    "/agent/shop/v1",
)


def _is_documented_agent_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in AGENT_DOC_PATH_PREFIXES)


def _documented_agent_routes(app: Any) -> List[Any]:
    routes: List[Any] = []
    for route in app.routes:
        path = getattr(route, "path", "") or ""
        methods = getattr(route, "methods", set()) or set()
        if not path or not methods or not _is_documented_agent_path(path):
            continue
        routes.append(route)
    return routes


def build_agent_openapi_schema(app: Any, *, base_url: str = "") -> Dict[str, Any]:
    schema = get_openapi(
        title="Pivota Agent API",
        version="1.0.0",
        description="Production-ready API for agent integrations",
        routes=_documented_agent_routes(app),
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    schema["security"] = [{"ApiKeyAuth": []}]
    server_url = str(base_url or "").rstrip("/")
    if server_url:
        schema["servers"] = [{"url": server_url}]
    return schema


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
        "# Pivota Agent Developer Quickstart\n\n"
        "## Install\n\n"
        "Python:\n\n"
        "```bash\n"
        "pip install pivota-agent\n"
        "```\n\n"
        "TypeScript:\n\n"
        "```bash\n"
        "npm install pivota-agent\n"
        "```\n\n"
        "These SDK packages are convenience wrappers over the same production REST API and API key flow.\n\n"
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
        "import { PivotaAgentClient } from 'pivota-agent'\n"
        "const client = new PivotaAgentClient({ apiKey: process.env.PIVOTA_AGENT_API_KEY })\n"
        "const health = await client.healthCheck()\n"
        "```\n\n"
        "## Ordering\n\n"
        "Every order needs a live quote: `POST /agent/v1/quotes/preview`, then `POST /agent/v1/orders/create` "
        "with its `quote_id` (without it: `400 QUOTE_REQUIRED_BEFORE_PURCHASE`). "
        "Full flow: `/agent/docs/examples/python` and `/agent/docs/examples/typescript`.\n\n"
        "`GET /agent/v1/merchants` lists the portal-integrated merchants your key can order from over REST. "
        "Other merchants are listed with `?scope=protocol` and bought through the UCP door "
        "(`https://commerce.mcp.pivota.cc/ucp/mcp`); REST search results from them carry "
        "`merchant_id: \"external_seed\"`, which the order call refuses.\n"
    )


@router.get("/sdks")
async def sdks_info() -> Dict[str, Any]:
    return {
        "python": {
            "install": "pip install pivota-agent",
            "package_name": "pivota-agent",
            "status": "published",
            "note": "Official Python SDK package published as pivota-agent.",
            "example": (
                "from pivota_agent import PivotaAgentClient\n"
                "client = PivotaAgentClient(api_key='YOUR_API_KEY')\n"
                "print(client.health_check())\n"
            ),
        },
        "typescript": {
            "install": "npm install pivota-agent",
            "package_name": "pivota-agent",
            "status": "published",
            "note": "Official TypeScript/JavaScript SDK package published as pivota-agent.",
            "example": (
                "import { PivotaAgentClient } from 'pivota-agent'\n"
                "const client = new PivotaAgentClient({ apiKey: process.env.PIVOTA_AGENT_API_KEY })\n"
                "console.log(await client.healthCheck())\n"
            ),
        },
    }


@router.get("/openapi.json")
async def agent_openapi_spec(request: Request) -> Dict[str, Any]:
    # Memoized on app.state: this is the public front door for agent cold discovery (robots.txt
    # invites crawlers here and /openapi.json redirects here), and rebuilding walks every
    # app.route plus regenerates all pydantic JSON schemas per hit. Only the servers entry varies
    # per request, so it is overlaid on a shallow copy of the cached schema.
    app = request.app
    schema = getattr(app.state, "agent_openapi_schema_cache", None)
    if schema is None:
        schema = build_agent_openapi_schema(app)
        app.state.agent_openapi_schema_cache = schema
    server_url = str(request.base_url).rstrip("/")
    if server_url:
        return {**schema, "servers": [{"url": server_url}]}
    return schema


# Plain REST, so the examples hold whatever SDK a partner uses. Every order is quote-first: /orders/create
# without a quote_id returns 400 QUOTE_REQUIRED_BEFORE_PURCHASE (routes/agent_api.py), so the examples
# request a live quote and pass its id. GET /merchants (default scope) lists portal-integrated merchants;
# search results from other merchants carry merchant_id "external_seed" and cannot be ordered here.
@router.get("/examples/python", response_class=PlainTextResponse)
async def example_python() -> str:
    return (
        "import requests\n\n"
        "API = 'https://api.pivota.cc/agent/v1'\n"
        "H = {'X-API-Key': 'YOUR_API_KEY'}\n\n"
        "# Merchants you can order from over REST (portal-integrated). Others: ?scope=protocol.\n"
        "merchants = requests.get(f'{API}/merchants', headers=H, timeout=30).json()['merchants']\n"
        "merchant_id = merchants[0]['merchant_id']\n\n"
        "search = requests.get(f'{API}/products/search', headers=H, timeout=30,\n"
        "                      params={'query': 'coffee', 'merchant_id': merchant_id, 'limit': 1}).json()\n"
        "prod = search['products'][0]\n"
        "cart = requests.post(f'{API}/cart/validate', headers=H, timeout=30,\n"
        "                     params={'merchant_id': merchant_id, 'shipping_country': 'US'},\n"
        "                     json=[{'product_id': prod['id'], 'quantity': 1}]).json()\n"
        "item = {'product_id': prod['id'], 'variant_id': cart['items'][0]['variant_id'], 'quantity': 1}\n\n"
        "# A live quote is required before ordering.\n"
        "quote = requests.post(f'{API}/quotes/preview', headers=H, timeout=30,\n"
        "                      json={'merchant_id': merchant_id, 'items': [item]}).json()\n\n"
        "order = requests.post(f'{API}/orders/create', headers=H, timeout=30, json={\n"
        "    'quote_id': quote['quote_id'],\n"
        "    'merchant_id': merchant_id,\n"
        "    'items': [item],\n"
        "    'customer_email': 'buyer@example.com',\n"
        "    'shipping_address': {'name': 'John Doe', 'address_line1': '123 Main St', 'city': 'SF',\n"
        "                         'state': 'CA', 'postal_code': '94105', 'country': 'US'},\n"
        "}).json()\n"
        "print(order)\n"
    )


@router.get("/examples/typescript", response_class=PlainTextResponse)
async def example_typescript() -> str:
    return (
        "const API = 'https://api.pivota.cc/agent/v1'\n"
        "const H = { 'X-API-Key': process.env.PIVOTA_AGENT_API_KEY!, 'Content-Type': 'application/json' }\n\n"
        "// Merchants you can order from over REST (portal-integrated). Others: ?scope=protocol.\n"
        "const { merchants } = await (await fetch(`${API}/merchants`, { headers: H })).json()\n"
        "const merchantId = merchants[0].merchant_id\n\n"
        "const q = new URLSearchParams({ query: 'coffee', merchant_id: merchantId, limit: '1' })\n"
        "const search = await (await fetch(`${API}/products/search?${q}`, { headers: H })).json()\n"
        "const prod = search.products[0]\n"
        "const cart = await (await fetch(`${API}/cart/validate?merchant_id=${merchantId}&shipping_country=US`, {\n"
        "  method: 'POST', headers: H, body: JSON.stringify([{ product_id: prod.id, quantity: 1 }]),\n"
        "})).json()\n"
        "const item = { product_id: prod.id, variant_id: cart.items[0].variant_id, quantity: 1 }\n\n"
        "// A live quote is required before ordering.\n"
        "const quote = await (await fetch(`${API}/quotes/preview`, {\n"
        "  method: 'POST', headers: H, body: JSON.stringify({ merchant_id: merchantId, items: [item] }),\n"
        "})).json()\n\n"
        "const order = await (await fetch(`${API}/orders/create`, {\n"
        "  method: 'POST', headers: H, body: JSON.stringify({\n"
        "    quote_id: quote.quote_id,\n"
        "    merchant_id: merchantId,\n"
        "    items: [item],\n"
        "    customer_email: 'buyer@example.com',\n"
        "    shipping_address: { name: 'John Doe', address_line1: '123 Main St', city: 'SF', state: 'CA', postal_code: '94105', country: 'US' },\n"
        "  }),\n"
        "})).json()\n"
        "console.log(order)\n"
    )


def _derive_agent_routes(request: Request) -> List[Dict[str, Any]]:
    endpoints: List[Dict[str, Any]] = []
    for route in request.app.routes:
        path = getattr(route, "path", "") or ""
        methods = sorted(
            method
            for method in (getattr(route, "methods", set()) or set())
            if method not in {"HEAD", "OPTIONS"}
        )
        if not methods:
            continue
        if not _is_documented_agent_path(path):
            continue
        # Same surface as the OpenAPI schema, which leaves out include_in_schema=False routes.
        if not getattr(route, "include_in_schema", True):
            continue
        description = ((getattr(route, "endpoint", None).__doc__ or "").strip().splitlines() or [""])[0]
        for method in methods:
            endpoints.append(
                {
                    "method": method,
                    "path": path,
                    "desc": description or getattr(route, "name", "Agent endpoint"),
                }
            )
    endpoints.sort(key=lambda item: (item["path"], item["method"]))
    return endpoints


@router.get("/endpoints")
async def endpoints_summary(request: Request) -> Dict[str, Any]:
    return {
        "base_url": str(request.base_url).rstrip("/"),
        "auth": {"header": "X-API-Key", "example": "ak_live_xxx"},
        "endpoints": _derive_agent_routes(request),
    }





