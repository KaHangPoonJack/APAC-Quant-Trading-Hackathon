"""Entrypoint: start the trading engine.

Usage:
    python -m scripts.run_trader [--config path/to/settings.yaml]

Uses the Roostoo key set selected by `roostoo.env` (TEST by default).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make the repo root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.logging_setup import setup_logging
from engine.factory import build_engine

log = logging.getLogger("run_trader")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Roostoo trading engine")
    parser.add_argument("--config", default=None, help="path to settings.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.logging.level, cfg.logging.dir)

    if not cfg.roostoo.has_keys:
        log.error("no Roostoo keys for env=%s — set roostoo.keys.%s in "
                  "config/settings.local.yaml or ROOSTOO_API_KEY_%s / "
                  "ROOSTOO_SECRET_KEY_%s", cfg.roostoo.env, cfg.roostoo.env,
                  cfg.roostoo.env, cfg.roostoo.env)
        return 1
    if cfg.roostoo.env == "COMPETITION":
        log.warning("roostoo.env=COMPETITION — orders go to the OFFICIAL competition account")

    build_engine(cfg).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
