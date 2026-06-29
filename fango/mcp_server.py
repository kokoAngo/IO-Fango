"""FastMCP server: registers all forum tools.

Run as:

    python -m fango.mcp_server          # stdio
    FANGO_HTTP=1 uvicorn fango.http_app:app  # mounted at /mcp/
"""
from __future__ import annotations

import functools
import inspect
import logging

from mcp.server.fastmcp import FastMCP

from . import agent_admin as agent_admin_tools
from . import db as fango_db
from .baibai import tools as baibai_tools
from .chintai import tools as chintai_tools
from .config import load_settings
from .dojo import tools as dojo_tools
from .listings import tools as listings_tools
from .listings import saved_search as saved_search_tools
from .brokers import tools as broker_tools
from .consult import tool as consult_tool
from . import forum_post_tools
from .wiki import tools as wiki_tools
from .chat import tools as chat_tools

log = logging.getLogger(__name__)


def _record_mcp_call(tool_name: str) -> None:
    """Log one row per tool invocation, then publish a live-feed event.

    Both steps are best-effort — telemetry should never fail a tool call.
    """
    try:
        conn = fango_db.connect()
        try:
            conn.execute(
                "INSERT INTO rate_limit_events(scope, key) VALUES ('mcp_call', ?)",
                (tool_name,),
            )
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover
        log.debug("mcp call telemetry write failed: %s", exc)
    try:
        from .events import Event, publish
        publish(Event(type="mcp_call", payload={"tool": tool_name}))
    except Exception as exc:  # pragma: no cover
        log.debug("mcp call event publish failed: %s", exc)


def _record_activity(tool_name: str, kwargs: dict) -> None:
    """Append a human-readable activity-stream line for this tool call.

    Captures the calling agent (if any) and the call kwargs. Best-effort — the
    activity layer must never fail a tool call.
    """
    try:
        from .auth import current_agent_var
        from . import activity
        activity.record(tool_name, current_agent_var.get(), kwargs)
    except Exception as exc:  # pragma: no cover
        log.debug("activity record failed: %s", exc)


def _instrument(mcp: FastMCP) -> None:
    """Wrap ``mcp.tool()`` so every registered function records a call event.

    This is a process-local monkey-patch on the FastMCP instance — it does not
    touch the upstream class. Each tool body still runs unchanged; we just
    prepend a fire-and-forget INSERT.
    """
    original_tool = mcp.tool

    def tool_with_telemetry(*args, **kwargs):
        decorator = original_tool(*args, **kwargs)

        def wrap_and_register(fn):
            # Preserve the tool's sync/async nature: an async tool MUST stay a
            # coroutine function, or FastMCP (which checks iscoroutinefunction)
            # would call it without awaiting — running it inline on the event
            # loop and freezing concurrent SSR requests. So branch on fn's kind.
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapped(*a, **kw):
                    _record_mcp_call(fn.__name__)
                    return await fn(*a, **kw)
            else:
                @functools.wraps(fn)
                def wrapped(*a, **kw):
                    _record_mcp_call(fn.__name__)
                    # エージェント実況 stream retired — per-forum live feeds replace it.
                    # Re-enable by uncommenting (also restore /activity + nav/widget).
                    # _record_activity(fn.__name__, kw)
                    return fn(*a, **kw)
            return decorator(wrapped)

        return wrap_and_register

    mcp.tool = tool_with_telemetry


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
    _instrument(mcp)  # so every tool call below records one event
    baibai_tools.register(mcp)
    chintai_tools.register(mcp)
    chat_tools.register(mcp)
    dojo_tools.register(mcp)
    # 一時停止: wiki 栏目(人+agent 共に停止)。再開時はこの行のコメントを外す。
    # wiki_tools.register(mcp)
    listings_tools.register(mcp)
    saved_search_tools.register(mcp)
    broker_tools.register(mcp)
    consult_tool.register(mcp)
    agent_admin_tools.register(mcp)
    forum_post_tools.register(mcp)
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
