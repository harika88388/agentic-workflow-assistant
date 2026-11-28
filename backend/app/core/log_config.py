"""Centralised application logging.

* One stdout handler on the root logger. Vercel (and Docker) collect stdout, so nothing depends on local files.
* JSON lines in production (searchable in the Vercel log viewer), readable key=value text locally.
* Every line carries UTC time and the request-scoped context (request_id, user_id, execution_id, ...).
* Secrets and personal data are redacted in the formatter, so no handler can emit them by accident.
* A formatting failure degrades to a minimal line instead of raising into the application.
"""
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.logging_context import current_context, log_context

REDACTED = "[REDACTED]"
_HANDLER_FLAG = "_awa_handler"
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}
SENSITIVE_KEYS = re.compile(
    r"(?i)(pass(word|wd)|secret|api[_-]?key|authori[sz]ation|cookie|credential|encryption[_-]?key|private[_-]?key)"
    r"|token$|^(body|body_text|raw|raw_message)$")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"), f"Bearer {REDACTED}"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), REDACTED),  # JWTs / ID tokens
    (re.compile(r"\bya29\.[0-9A-Za-z\-_]+"), REDACTED),  # Google access tokens
    (re.compile(r"\b1//[0-9A-Za-z\-_]{20,}"), REDACTED),  # Google refresh tokens
    (re.compile(r"\bGOCSPX-[0-9A-Za-z\-_]+"), REDACTED),  # Google OAuth client secrets
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}"), REDACTED),  # Google API keys
    (re.compile(r"\bsk-(?:or-v1-)?[A-Za-z0-9\-_]{20,}"), REDACTED),  # OpenRouter / OpenAI-style keys
    (re.compile(r"\bnpg_[A-Za-z0-9]{8,}"), REDACTED),  # Neon passwords
    (re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=-])"), REDACTED),  # Fernet keys
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+@"), rf"\1{REDACTED}@"),  # credentials in URLs
    (re.compile(r"([?&](?:code|state|access_token|refresh_token|id_token|token|key)=)[^&\s]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)\b((?:client_)?secret|password|passwd|api_key|apikey|access_token|refresh_token|id_token|"
                r"jwt_secret|token_encryption_key|authorization)(\s*[=:]\s*)(\"?)[^\s\"',&]+"),
     rf"\1\2\3{REDACTED}"),
    (re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b"),
     r"\1***@\2"),  # mask the local part of email addresses
]


def redact(value: Any, _depth: int = 0) -> Any:
    """Redact secrets in strings and (recursively) in dict/list values; sensitive keys are blanked entirely."""
    if _depth > 6:
        return "[...]"
    if isinstance(value, str):
        for pattern, repl in _PATTERNS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and SENSITIVE_KEYS.search(k) and v not in (None, "", [], {})
                    else redact(v, _depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v, _depth + 1) for v in value]
    return value


def _iso(created: float) -> str:
    return datetime.fromtimestamp(created, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}


class _SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._format(record)
        except Exception as exc:  # noqa: BLE001 - logging must never break the request
            return json.dumps({"timestamp": _iso(time.time()), "level": "ERROR", "logger": "app.logging",
                               "message": f"log formatting failed: {type(exc).__name__}",
                               "original_logger": getattr(record, "name", "?")})

    def _format(self, record: logging.LogRecord) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class JsonFormatter(_SafeFormatter):
    def _format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": _iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        entry.update(redact(current_context()))
        entry.update(redact(_extras(record)))
        if record.exc_info:
            exc_type, exc, _ = record.exc_info
            entry["exception"] = {"type": getattr(exc_type, "__name__", str(exc_type)),
                                  "message": redact(str(exc))[:1000],
                                  "stack_trace": redact("".join(traceback.format_exception(*record.exc_info)))}
        elif record.stack_info:
            entry["stack_info"] = redact(record.stack_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(_SafeFormatter):
    def _format(self, record: logging.LogRecord) -> str:
        ctx = redact(current_context())
        fields = " ".join(f"{k}={ctx[k]}" for k in ("request_id", "user_id", "execution_id", "workflow_id") if ctx.get(k))
        extras = redact(_extras(record))
        extra_text = " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in extras.items())
        line = f"{_iso(record.created)} {record.levelname:<8} {record.name}: {redact(record.getMessage())}"
        line += f" [{fields}]" if fields else ""
        line += f" {extra_text}" if extra_text else ""
        if record.exc_info:
            line += "\n" + redact("".join(traceback.format_exception(*record.exc_info)))
        return line


_NOISY = {"httpx": logging.WARNING, "httpcore": logging.WARNING, "mcp": logging.WARNING,
          "uvicorn.access": logging.WARNING, "sqlalchemy.engine": logging.WARNING, "langgraph": logging.WARNING,
          "anyio": logging.WARNING, "urllib3": logging.WARNING, "google": logging.WARNING}


def configure_logging(level: str | None = None, fmt: str | None = None, stream=None) -> logging.Handler:
    """Install the application handler on the root logger (idempotent). Returns the handler."""
    level_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    production = os.environ.get("APP_ENV", "").lower() == "production" or bool(os.environ.get("VERCEL"))
    fmt = (fmt or os.environ.get("LOG_FORMAT") or ("json" if production else "text")).lower()
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stdout)
    setattr(handler, _HANDLER_FLAG, True)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))
    for name, lvl in _NOISY.items():
        logging.getLogger(name).setLevel(max(lvl, root.level))
    logging.raiseExceptions = not production  # a broken handler must not raise into requests in production
    return handler


# ---------------- HTTP request logging / correlation ----------------
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET_PATHS = {"/api/health"}
http_log = logging.getLogger("app.http")


def _route_template(scope, path: str) -> str:
    """The matched route with ids left as placeholders, e.g. /api/conversations/{conversation_id}. Routes of
    included routers only know their path below the include prefix, which is recovered from the request path."""
    template = getattr(scope.get("route"), "path", None)
    if not template:
        return "-"
    depth = template.rstrip("/").count("/")
    prefix = "/".join(path.rstrip("/").split("/")[:-depth]) if depth else path.rstrip("/")
    return f"{prefix}{template}"


class RequestContextMiddleware:
    """Pure ASGI middleware: assigns a request id (or accepts a well-formed X-Request-ID), exposes it in the
    response, logs one line per request (method, path without query string, status, duration) and turns unhandled
    exceptions into a logged stack trace plus a JSON 500 that carries the request id."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        incoming = headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        method, path = scope.get("method", "?"), scope.get("path", "?")
        state = {"status": 500, "started": False}
        started = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"], state["started"] = message["status"], True
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", request_id.encode())]
            await send(message)

        with log_context(request_id=request_id):
            http_log.debug("http request received method=%s path=%s client_request_id=%s", method, path,
                           bool(incoming) and incoming == request_id)
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:  # noqa: BLE001
                http_log.error("unhandled exception method=%s path=%s exception_type=%s", method, path,
                               type(exc).__name__, exc_info=True)
                if not state["started"]:
                    from starlette.responses import JSONResponse
                    resp = JSONResponse({"detail": {"code": "internal_error", "request_id": request_id,
                                                    "message": "Something went wrong on the server."}},
                                        status_code=500, headers={"X-Request-ID": request_id})
                    await resp(scope, receive, send)
                    state["status"] = 500
            finally:
                duration = round((time.perf_counter() - started) * 1000)
                status = state["status"]
                lvl = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
                if path in _QUIET_PATHS and status < 400:
                    lvl = logging.DEBUG
                route = _route_template(scope, path)
                http_log.log(lvl, "http request completed method=%s path=%s route=%s status=%d duration_ms=%d",
                             method, path, route, status, duration,
                             extra={"http": {"method": method, "path": path, "route": route, "status": status,
                                             "duration_ms": duration}})
