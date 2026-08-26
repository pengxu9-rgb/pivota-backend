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
        "```\n"
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
        "import { PivotaAgentClient } from 'pivota-agent'\n\n"
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





