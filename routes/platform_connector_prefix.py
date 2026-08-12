"""Prefix constants + deprecation shim for the platform-connector routers.

WHY THIS EXISTS. Three backend routers served REST under `/mcp/*`
(`mcp_mgmt`, `mcp_e2e_test`, the retired `mcp_routes` simulation). None of them
speaks the Model Context Protocol: they manage Shopify/Wix/WooCommerce/
BigCommerce **store connectors**. Meanwhile the real MCP door — JSON-RPC,
`tools/list` / `tools/call`, OAuth-authenticated — is `POST /mcp` on
PIVOTA-Agent (ADR-021: the gateway owns the public protocol doors). Two
different services answering "/mcp" with two different protocols is a
wrong-host debugging incident waiting to happen; ADR-021 records that exact
class of incident happening twice in one day on 2026-07-31.

So the connector routes move to `/platform-connectors/*`, which says what they
actually are.

THE LEGACY ALIAS IS DELIBERATE. `/mcp/*` stays mounted and WORKING, because the
employee portal's MCP dashboard calls `/mcp/test/{merchant_id}` today. Breaking a
live operator surface to win a naming argument is the wrong trade. Alias
responses carry `Deprecation: true` + a `Link` header pointing at the successor,
and every alias hit logs a warning naming the path — so the remaining callers
identify themselves instead of having to be guessed at. Remove the alias mount
(and this module) once the logs go quiet and the portal is on the new path.
"""
from fastapi import Response

from utils.logger import logger

# The honest prefix these routes should always have had.
PLATFORM_CONNECTORS_PREFIX = "/platform-connectors"

# The legacy prefix, kept working for existing callers. NOT the MCP protocol
# door — that is `POST /mcp` on PIVOTA-Agent.
LEGACY_MCP_PREFIX = "/mcp"


async def legacy_mcp_prefix_deprecation(response: Response) -> None:
    """Mark a response served from the legacy `/mcp/*` alias.

    Mounted as a router-level dependency on the alias only, so the canonical
    prefix is untouched. Deliberately cannot fail the request: a deprecation
    notice must never be the reason a live operator call breaks.
    """
    try:
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = (
            f'<{PLATFORM_CONNECTORS_PREFIX}>; rel="successor-version"'
        )
        logger.warning(
            "Legacy /mcp/* prefix used for a platform-connector route; "
            "callers must move to %s (this alias is temporary, and /mcp is the "
            "MCP protocol door on the gateway, not this REST surface)",
            PLATFORM_CONNECTORS_PREFIX,
        )
    except Exception:  # pragma: no cover - defensive; never break the caller
        pass
