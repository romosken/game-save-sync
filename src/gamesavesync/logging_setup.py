"""File logging under the app data directory."""

from __future__ import annotations

import logging
import sys

from gamesavesync.util import app_dir

_CONFIGURED = False


def setup_logging() -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("gamesavesync")
    if _CONFIGURED:
        return logger
    logger.setLevel(logging.INFO)
    log_dir = app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "gamesavesync.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(stream)
    logger.propagate = False
    _CONFIGURED = True
    return logger


def log_path():
    return app_dir() / "logs" / "gamesavesync.log"
