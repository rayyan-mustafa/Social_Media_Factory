"""Structured JSON logging."""

from __future__ import annotations

import logging
import sys

from src.core.config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level.upper())

    handler = logging.StreamHandler(sys.stdout)
    formatter: logging.Formatter
    try:
        from pythonjsonlogger.jsonlogger import JsonFormatter

        formatter = JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    except Exception:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
    handler.setFormatter(formatter)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
