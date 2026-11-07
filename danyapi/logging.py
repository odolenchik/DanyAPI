from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Per-request correlation id. Set by RequestIDMiddleware and read by the log
# filter below, so every line written while a request is in flight carries the
# same id the response header reports. A ContextVar is safe here: the whole
# request runs in one task, and each task gets its own copy.
request_id_var: ContextVar[str] = ContextVar("danyapi_request_id", default="-")

DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
CONSOLE_HANDLER_NAME = "danyapi-console"
FILE_HANDLER_NAME = "danyapi-file"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record.

    The format string references ``%(request_id)s``; without this filter any
    handler using it would raise. Records written outside a request get "-".
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


_request_id_filter = RequestIdFilter()


def new_request_id() -> str:
    """Return the id for this request, generating one when middleware did not.

    Tests and internal callers invoke the endpoint functions directly, without
    going through the ASGI stack, so the ContextVar can still be at its default.
    """
    current = request_id_var.get()
    if current and current != "-":
        return current
    return uuid.uuid4().hex


def _known_level_names() -> dict[str, int]:
    # logging.getLevelNamesMapping() is 3.11+; fall back to the name registry
    # that has existed since Python 2 for older interpreters.
    getter = getattr(logging, "getLevelNamesMapping", None)
    if getter is not None:
        return getter()
    return dict(logging._nameToLevel)


def _resolve_level(level: str | None) -> str:
    normalized = str(level or "").strip().upper()
    if normalized in _known_level_names():
        return normalized
    return DEFAULT_LOG_LEVEL


def _coerce_max_bytes(value: int) -> int:
    if value and value > 0:
        return value
    return DEFAULT_MAX_BYTES


def _coerce_backup_count(value: int) -> int:
    if value >= 0:
        return value
    return DEFAULT_BACKUP_COUNT


def _make_file_handler(log_file: str, max_bytes: int, backup_count: int) -> RotatingFileHandler:
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        str(path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
    handler.addFilter(_request_id_filter)
    return handler


def _has_handler(root: logging.Logger, name: str) -> bool:
    return any(getattr(handler, "name", None) == name for handler in root.handlers)


def configure() -> None:
    from danyapi.config import settings

    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    level = _resolve_level(settings.log_level)
    root = logging.getLogger()
    root.setLevel(level)

    if not _has_handler(root, CONSOLE_HANDLER_NAME):
        console = logging.StreamHandler()
        console.name = CONSOLE_HANDLER_NAME
        console.setLevel(level)
        console.addFilter(_request_id_filter)
        console.setFormatter(logging.Formatter(DEFAULT_FORMAT))
        root.addHandler(console)

    if settings.log_file and not _has_handler(root, FILE_HANDLER_NAME):
        path = Path(settings.log_file)
        if path.is_dir():
            logging.getLogger("danyapi.logging").warning(
                "log file %s is a directory, using console only",
                path,
            )
        else:
            file_handler = _make_file_handler(
                str(path),
                _coerce_max_bytes(settings.log_max_bytes),
                _coerce_backup_count(settings.log_backup_count),
            )
            file_handler.name = FILE_HANDLER_NAME
            file_handler.setLevel(level)
            root.addHandler(file_handler)


def uvicorn_log_config() -> dict:
    from danyapi.config import settings

    level = _resolve_level(settings.log_level)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "uvicorn": {"handlers": [], "level": level, "propagate": True},
            "uvicorn.error": {"handlers": [], "level": level, "propagate": True},
            "uvicorn.access": {"handlers": [], "level": level, "propagate": True},
        },
    }
