"""Pivota as a ``StorefrontBackend`` for anthropics/commerce-agents.

A shopping agent built on the blueprint reaches its catalog through one
interface. This package implements that interface over Pivota's MCP tools, so
the agent searches a multi-merchant index, reads decision-grade product
records, and hands each merchant's cart to a hosted checkout page.

    from pivota_storefront import HttpMcpTransport, PivotaStorefrontBackend, PivotaShoppingSession

    backend = PivotaStorefrontBackend(HttpMcpTransport("https://commerce.mcp.pivota.cc/mcp"))
"""

from .backend import PivotaShoppingSession, PivotaStorefrontBackend
from .ids import ProductRef, decode_product_id, encode_product_id
from .transport import HttpMcpTransport, McpTransport, ToolCallError

__all__ = [
    "HttpMcpTransport",
    "McpTransport",
    "PivotaShoppingSession",
    "PivotaStorefrontBackend",
    "ProductRef",
    "ToolCallError",
    "decode_product_id",
    "encode_product_id",
]
