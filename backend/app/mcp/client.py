"""MCP client used by the LangGraph workflow and the approval executor.

`MCPGateway` is a synchronous facade (the rest of the backend is sync) over the official SDK's async `Client`.
It owns an event-loop thread (anyio blocking portal), connects to each server lazily on first use, keeps that
session for the rest of the request, discovers tools with `tools/list`, and refuses to call a tool the server did
not advertise. Tool arguments are never logged.
"""
import json
import logging
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import Client, MCPError, StdioServerParameters
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.mcp.errors import MCPToolError, parse_tool_error

log = logging.getLogger(__name__)
BACKEND_DIR = Path(__file__).resolve().parents[2]
SERVER_MODULES = {"gmail": "app.mcp.gmail_server", "calendar": "app.mcp.calendar_server"}

ClientFactory = Callable[[], Client]


@dataclass(frozen=True)
class ToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _payload(result: Any) -> dict[str, Any]:
    if isinstance(result.structured_content, dict):
        return result.structured_content
    text = "".join(getattr(b, "text", "") for b in result.content)
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {"text": text}
    return value if isinstance(value, dict) else {"value": value}


class MCPGateway:
    def __init__(self, factories: dict[str, ClientFactory], *, timeout_seconds: float = 60.0, transport: str = "custom"):
        self._factories = factories
        self._timeout = timeout_seconds
        self.transport = transport
        self._stack: ExitStack | None = None
        self._portal: BlockingPortal | None = None
        self._clients: dict[str, Client] = {}
        self._tools: dict[str, dict[str, ToolInfo]] = {}
        self.calls: list[dict[str, Any]] = []  # server/tool/outcome/duration only: never arguments

    def __enter__(self) -> "MCPGateway":
        self._stack = ExitStack()
        self._portal = self._stack.enter_context(start_blocking_portal())
        return self

    def __exit__(self, *exc: object) -> None:
        stack, self._stack, self._portal = self._stack, None, None
        self._clients.clear()
        if stack is not None:
            try:
                stack.close()
            except Exception:  # noqa: BLE001 - a failing shutdown must not mask the request's own result
                log.warning("error while closing MCP connections", exc_info=True)

    @property
    def servers(self) -> list[str]:
        return list(self._factories)

    def _client(self, server: str) -> Client:
        if self._stack is None or self._portal is None:
            raise RuntimeError("MCPGateway must be used as a context manager")
        if server not in self._clients:
            if server not in self._factories:
                raise MCPToolError("unknown_server", f"no MCP server named {server!r}", server=server)
            try:
                client = self._stack.enter_context(self._portal.wrap_async_context_manager(self._factories[server]()))
            except Exception as exc:  # noqa: BLE001
                log.error("MCP connection to %s failed: %s", server, type(exc).__name__)
                raise MCPToolError("mcp_connection_failed", f"Could not connect to the {server} MCP server.",
                                   server=server) from exc
            self._clients[server] = client
        return self._clients[server]

    def list_tools(self, server: str) -> list[ToolInfo]:
        """Tool discovery (MCP tools/list), cached for the life of the connection."""
        if server not in self._tools:
            client = self._client(server)
            try:
                listing = self._portal.call(client.list_tools)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                raise MCPToolError("mcp_connection_failed", f"Could not list tools on the {server} MCP server.",
                                   server=server) from exc
            self._tools[server] = {
                t.name: ToolInfo(server, t.name, t.description or "", t.input_schema,
                                 t.annotations.read_only_hint if t.annotations else None)
                for t in listing.tools
            }
        return list(self._tools[server].values())

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in {t.name for t in self.list_tools(server)}:
            raise MCPToolError("unknown_tool", f"The {server} MCP server does not provide {tool!r}.",
                               server=server, tool=tool)
        client = self._client(server)
        args = {k: v for k, v in arguments.items() if v is not None}
        started = time.perf_counter()
        record: dict[str, Any] = {"server": server, "tool": tool}
        try:
            result = self._portal.call(partial(client.call_tool, tool, args,  # type: ignore[union-attr]
                                               read_timeout_seconds=self._timeout))
        except MCPError as exc:
            err = MCPToolError("mcp_protocol_error", f"{tool} failed: {exc}", server=server, tool=tool)
        except Exception as exc:  # noqa: BLE001 - transport failure / timeout
            log.error("MCP call %s.%s failed: %s", server, tool, type(exc).__name__)
            err = MCPToolError("mcp_connection_failed", f"The {server} MCP server did not respond.",
                               server=server, tool=tool)
        else:
            err = parse_tool_error("".join(getattr(b, "text", "") for b in result.content), server=server, tool=tool) \
                if result.is_error else None
        record.update(ok=err is None, ms=round((time.perf_counter() - started) * 1000))
        if err is not None:
            record["error"] = err.code
        self.calls.append(record)
        log.info("mcp %s.%s ok=%s %sms", server, tool, record["ok"], record["ms"])
        if err is not None:
            raise err
        return _payload(result)


# ---------- factories ----------
def stdio_factories(settings: Settings, user_id: uuid.UUID) -> dict[str, ClientFactory]:
    """Each server runs as `python -m app.mcp.<server>` and speaks MCP over stdin/stdout. The child reads the user's
    encrypted credentials from the database itself; only the user id is handed over."""
    env = {
        "MCP_USER_ID": str(user_id),
        "DATABASE_URL": settings.database_url,
        "JWT_SECRET": settings.jwt_secret,
        "TOKEN_ENCRYPTION_KEY": settings.token_encryption_key,
        "GOOGLE_CLIENT_ID": settings.google_client_id,
        "GOOGLE_CLIENT_SECRET": settings.google_client_secret,
        "APP_ENV": settings.app_env,
        "PYTHONPATH": str(BACKEND_DIR),
    }

    def factory(module: str) -> ClientFactory:
        params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env, cwd=str(BACKEND_DIR))
        return lambda: Client(params, mode="legacy", read_timeout_seconds=settings.mcp_timeout_seconds)

    return {name: factory(module) for name, module in SERVER_MODULES.items()}


def inprocess_factories(gmail_scope: Callable, calendar_scope: Callable) -> dict[str, ClientFactory]:
    """Same servers and MCP protocol (JSON-RPC over an in-memory transport) without a subprocess."""
    from app.mcp.calendar_server import build_calendar_server
    from app.mcp.gmail_server import build_gmail_server
    return {
        "gmail": lambda: Client(build_gmail_server(gmail_scope), mode="legacy"),
        "calendar": lambda: Client(build_calendar_server(calendar_scope), mode="legacy"),
    }


def gateway_for_user(db: Session, user_id: uuid.UUID, settings: Settings) -> MCPGateway:
    if settings.mcp_transport == "inprocess":
        from app.mcp.runtime import inprocess_calendar_scope, inprocess_gmail_scope
        factories = inprocess_factories(inprocess_gmail_scope(db, user_id), inprocess_calendar_scope(db, user_id))
    else:
        factories = stdio_factories(settings, user_id)
    return MCPGateway(factories, timeout_seconds=settings.mcp_timeout_seconds, transport=settings.mcp_transport)
