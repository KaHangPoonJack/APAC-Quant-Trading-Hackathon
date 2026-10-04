"""Centralised logging configuration.

Call `setup_logging()` once at process start; every module then uses
`logging.getLogger(__name__)` and inherits console + rotating-file handlers.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import ROOT

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    root = logging.getLogger()
    if root.handlers:  # idempotent: don't double-register on re-entry
        return

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(_FMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    path = (ROOT / log_dir)
    path.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        path / "roostoo_compet.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Quiet noisy HTTP internals unless we're debugging.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
