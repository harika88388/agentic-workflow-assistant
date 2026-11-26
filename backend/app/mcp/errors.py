"""Error contract shared by the MCP servers and client.

Servers raise `ToolError("[code] message")`; the SDK returns it as a `CallToolResult(is_error=True)` and the client
turns it back into `MCPToolError(code, message)`. Messages are user-safe: no tokens, headers or stack traces.
"""
import re
from collections.abc import Iterator
from contextlib import contextmanager

from mcp.server.mcpserver.exceptions import ToolError

from app.integrations.calendar.service import CalendarError, CalendarVerificationError
from app.integrations.errors import (
    GoogleAPIError, GoogleAuthError, GoogleBadRequestError, GoogleConflictError, GoogleNotFoundError,
    GoogleRateLimitError,
)
from app.integrations.gmail.service import GmailOperationError
from app.services.token_service import TokenUnavailableError

_CODE = re.compile(r"\[([a-z_]+)\]\s*(.*)", re.S)


class MCPToolError(Exception):
    def __init__(self, code: str, message: str, *, server: str | None = None, tool: str | None = None):
        super().__init__(message)
        self.code, self.message, self.server, self.tool = code, message, server, tool

    def to_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "server": self.server, "tool": self.tool}


def parse_tool_error(text: str, *, server: str, tool: str) -> MCPToolError:
    m = _CODE.search(text or "")
    if m:
        return MCPToolError(m.group(1), m.group(2).strip()[:500], server=server, tool=tool)
    if "validation error" in (text or ""):
        return MCPToolError("invalid_arguments", f"{tool} rejected its arguments", server=server, tool=tool)
    return MCPToolError("tool_failed", f"{tool} failed on the {server} server", server=server, tool=tool)


def _fail(code: str, message: str) -> ToolError:
    return ToolError(f"[{code}] {message}")


@contextmanager
def tool_errors() -> Iterator[None]:
    """Map integration exceptions to coded tool errors inside an MCP tool body."""
    try:
        yield
    except TokenUnavailableError as exc:
        raise _fail("google_reconnect_required", str(exc)) from exc
    except GoogleAuthError as exc:
        raise _fail("google_reconnect_required", "Google rejected the request; please reconnect your account.") from exc
    except GoogleNotFoundError as exc:
        raise _fail("not_found", "The requested item was not found in your Google account.") from exc
    except GoogleConflictError as exc:
        raise _fail("conflict", exc.message) from exc
    except GoogleRateLimitError as exc:
        raise _fail("google_rate_limited", "Google is rate-limiting requests; try again shortly.") from exc
    except GoogleBadRequestError as exc:
        raise _fail("bad_request", exc.message) from exc
    except GoogleAPIError as exc:
        raise _fail("google_unavailable", "Google is temporarily unavailable. Please try again.") from exc
    except CalendarVerificationError as exc:
        raise _fail("verification_failed", str(exc)) from exc
    except (CalendarError, GmailOperationError) as exc:
        raise _fail("operation_failed", str(exc)) from exc
    except ValueError as exc:
        raise _fail("invalid_arguments", str(exc)) from exc
