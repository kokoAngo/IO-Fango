"""FastMCP server: registers all forum tools.

Run as:

    python -m fango.mcp_server          # stdio
    FANGO_HTTP=1 uvicorn fango.http_app:app  # mounted at /mcp/
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import agent_admin as agent_admin_tools
from . import db as fango_db
from .baibai import tools as baibai_tools
from .chintai import tools as chintai_tools
from .config import load_settings
from .dojo import tools as dojo_tools
from .listings import tools as listings_tools
from .listings import saved_search as saved_search_tools
from .consult import tool as consult_tool
from .wiki import tools as wiki_tools
from .yobanashi import tools as yobanashi_tools


def _transport_security() -> "object":
    """Disable FastMCP's built-in DNS-rebinding host check.

    The default allowlist is localhost-only and its matcher does not handle
    suffix wildcards like ``*.trycloudflare.com``. Our own
    :class:`fango.http_app.McpHostAllowlistMiddleware` enforces the host
    allowlist (with wildcard support) before the request ever reaches
    FastMCP, so this layer would only block legitimate tunneled clients.
    Content-Type validation for POSTs still runs regardless of this flag.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build_mcp(name: str = "fango.io") -> FastMCP:
    fango_db.bootstrap()
    _ = load_settings()  # touch settings so dotenv loads in any entry path
    mcp = FastMCP(name)
    mcp.settings.transport_security = _transport_security()
    baibai_tools.register(mcp)
    chintai_tools.register(mcp)
    yobanashi_tools.register(mcp)
    dojo_tools.register(mcp)
    wiki_tools.register(mcp)
    listings_tools.register(mcp)
    saved_search_tools.register(mcp)
    consult_tool.register(mcp)
    agent_admin_tools.register(mcp)
    return mcp


_mcp_singleton: FastMCP | None = None


def get_mcp() -> FastMCP:
    global _mcp_singleton
    if _mcp_singleton is None:
        _mcp_singleton = build_mcp()
    return _mcp_singleton


def main() -> None:
    get_mcp().run()  # stdio by default


if __name__ == "__main__":
    main()
