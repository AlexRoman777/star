from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from pythonjsonlogger.json import JsonFormatter

_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|authorization|cookie|filename|person|transcript|ocr|secret|token|"
    r"asset[_-]?id|task[_-]?id|immich[_-]?asset|prompt|model[_-]?output|result[_-]?payload|"
    r"target[_-]?ref)",
    re.IGNORECASE,
)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def current_trace_fields() -> dict[str, str]:
    """Kept so the JSON log shape stays stable; Star emits no traces."""

    return {"trace_id": "n/a", "span_id": "n/a"}


class PrivacyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "request_id" not in record.__dict__:
            record.request_id = "n/a"
        if isinstance(record.args, Mapping):
            record.args = redact(record.args)
        for key, value in current_trace_fields().items():
            if key not in record.__dict__:
                setattr(record, key, value)
        for key in list(record.__dict__):
            if _SENSITIVE_KEYS.search(key):
                setattr(record, key, "[REDACTED]")
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(trace_id)s %(span_id)s"
        )
    )
    handler.addFilter(PrivacyFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Successful local HTTP calls are represented by bounded operational
    # events. Logging every worker poll and Immich page at INFO obscures the
    # scheduler decisions an operator actually needs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
