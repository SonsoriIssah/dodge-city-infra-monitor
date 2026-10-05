"""Logging setup shared by every command-line entry point."""
from __future__ import annotations

import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"
_HANDLER_NAME = "dcim-console"


class AsciiFormatter(logging.Formatter):
    """Formatter whose output is always ASCII (Windows cp1252 consoles cannot print every OSM name)."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text.encode("ascii", "backslashreplace").decode("ascii")


def setup_logging(level: int | str | None = None) -> logging.Logger:
    """Configure the root logger once (idempotent) and return it.

    The level comes from the argument, else the ``LOG_LEVEL`` environment variable, else INFO.
    Messages go to stderr so that stdout stays free for command output.
    """
    resolved = level if level is not None else os.environ.get("LOG_LEVEL", "INFO")
    if isinstance(resolved, str):
        resolved = logging.getLevelName(resolved.upper())
        if not isinstance(resolved, int):
            resolved = logging.INFO

    root = logging.getLogger()
    root.setLevel(resolved)
    for handler in root.handlers:
        if handler.get_name() == _HANDLER_NAME:
            handler.setLevel(resolved)
            return root

    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setLevel(resolved)
    handler.setFormatter(AsciiFormatter(LOG_FORMAT, DATE_FORMAT))
    root.addHandler(handler)

    # Request-level chatter of the HTTP client is not useful in pipeline logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return root


def get_logger(name: str) -> logging.Logger:
    """Module logger (thin wrapper so call sites do not import ``logging`` only for this)."""
    return logging.getLogger(name)
