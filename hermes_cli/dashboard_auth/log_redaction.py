"""Redact dashboard query credentials before Uvicorn formats log records."""
from __future__ import annotations

import copy
import logging
import re
import traceback
from urllib.parse import unquote

_QUERY_VALUE = re.compile(r'([?&])([^=\s&?#]+)=([^&\s#"<>]*)')
_SECRET_KEYS = frozenset({"token", "ticket", "internal", "access_token", "refresh_token", "session_token"})


def redact_query_credentials(value: str) -> str:
    """Retain route/diagnostics while removing even percent-encoded secret keys."""
    def replace(match: re.Match[str]) -> str:
        if unquote(match[2]).lower() in _SECRET_KEYS:
            return f"{match[1]}{match[2]}=[REDACTED]"
        return match[0]

    return _QUERY_VALUE.sub(replace, value)


class GatewayCredentialFilter(logging.Filter):
    """Protect protocol logs too: access_log=False alone does not protect WS URLs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            # Uvicorn AccessFormatter unpacks these five fields; keep their shape.
            record.args = tuple(
                redact_query_credentials(value) if isinstance(value, str) else value
                for value in record.args
            )
        else:
            record.msg = redact_query_credentials(record.getMessage())
            record.args = ()
        if record.exc_info:
            record.exc_text = redact_query_credentials("".join(traceback.format_exception(*record.exc_info)))
        elif record.exc_text:
            record.exc_text = redact_query_credentials(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_query_credentials(record.stack_info)
        return True


def redacted_uvicorn_log_config() -> dict:
    """Return an isolated standard config with filters on loggers AND handlers."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    name = "gateway_query_credentials"
    config.setdefault("filters", {})[name] = {"()": GatewayCredentialFilter}
    for category in ("handlers", "loggers"):
        for component in config[category].values():
            component.setdefault("filters", []).append(name)
    return config
