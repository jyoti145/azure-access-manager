"""Structured (JSON lines) logging with secret redaction.

Every log record is written to ``logs/azure-manager-YYYY-MM-DD.log`` as one JSON
object with the fields: timestamp, level, operation, resource, user, scope,
result, error, message. A redaction filter scrubs bearer tokens, JWTs and
secret-looking key/value pairs from every record before it is written.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "azure_manager"
STRUCTURED_FIELDS = ("operation", "resource", "user", "scope", "result", "error")

_REDACTIONS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "***JWT***"),
    (
        re.compile(
            r"(?i)([\"']?(?:client[_-]?secret|password|passwd|secret|access[_-]?token|refresh[_-]?token|"
            r"id[_-]?token|token|api[_-]?key|sig)[\"']?\s*[:=]\s*[\"']?)[^\"'&\s,;}]+"
        ),
        r"\1***",
    ),
]


def redact(text: Any) -> Any:
    """Remove credentials from a string. Non-strings are returned unchanged."""
    if not isinstance(text, str) or not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    secret = os.environ.get("AZURE_CLIENT_SECRET")
    if secret and len(secret) >= 6:
        text = text.replace(secret, "***")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(redact(a) for a in record.args) if isinstance(record.args, tuple) else record.args
        for field in STRUCTURED_FIELDS:
            if hasattr(record, field):
                setattr(record, field, redact(getattr(record, field)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        for field in STRUCTURED_FIELDS:
            payload[field] = getattr(record, field, None)
        payload["message"] = record.getMessage()
        details = getattr(record, "details", None)
        if details:
            payload["details"] = details
        if record.exc_info:
            # Exception text can contain request URLs; redact before writing.
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        parts = [f"[{record.levelname.lower()}]", record.getMessage()]
        for field in ("operation", "scope", "result", "error"):
            value = getattr(record, field, None)
            if value:
                parts.append(f"{field}={value}")
        return " ".join(parts)


def setup_logging(log_dir: str | Path = "logs", *, verbose: bool = False, quiet: bool = False) -> Path | None:
    """Configure file + console logging. Returns the log file path (None if it could not be created)."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    redactor = RedactingFilter()

    log_path: Path | None = None
    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / f"azure-manager-{datetime.now().strftime('%Y-%m-%d')}.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)
    except OSError:
        log_path = None

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else (logging.ERROR if quiet else logging.CRITICAL))
    console_handler.setFormatter(ConsoleLogFormatter())
    console_handler.addFilter(redactor)
    logger.addHandler(console_handler)

    # The Azure SDK logs full HTTP requests at INFO and credential failures at WARNING. Errors are
    # reported by this tool in readable form, so SDK logs only go to the console with --verbose.
    for noisy in ("azure", "msal", "urllib3"):
        sdk_logger = logging.getLogger(noisy)
        sdk_logger.handlers.clear()
        sdk_logger.propagate = False
        sdk_logger.setLevel(logging.WARNING)
        sdk_logger.addHandler(console_handler if verbose else logging.NullHandler())
    return log_path


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(
    operation: str,
    result: str,
    *,
    message: str = "",
    resource: str | None = None,
    user: str | None = None,
    scope: str | None = None,
    error: str | None = None,
    level: int = logging.INFO,
    details: dict[str, Any] | None = None,
) -> None:
    """Write one structured audit record."""
    get_logger().log(
        level,
        message or f"{operation}: {result}",
        extra={
            "operation": operation,
            "resource": resource,
            "user": user,
            "scope": scope,
            "result": result,
            "error": error,
            "details": details,
        },
    )
