"""Per-runner liveness heartbeats.

Each runner process stamps its OWN file (`logs/heartbeat_<runner>.json`) every
loop; the supervisor reads them to tell whether that process
is actually alive (a process can be running yet wedged in a hung loop).

Staleness is judged against the runner's own declared cadence (carried in the
beat as `interval_seconds`), so a slow loop is not mistaken for a hang.

Plain JSON + atomic replace keeps this decoupled from the DB and safe for any
other process to read at any time. Pure `core` — no I/O beyond these files.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from core.config import ROOT

log = logging.getLogger(__name__)

HEARTBEAT_DIR = ROOT / "logs"
_PREFIX = "heartbeat_"

# Liveness states, worst-last.
ONLINE = "ONLINE"
STALE = "STALE"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"

# Runner keys that write a heartbeat. These MUST match the keys in
# scripts.run_all.RUNNERS (asserted by tests/test_run_all.py) — the supervisor
# looks up a child's heartbeat by its runner name.
TRADER = "trader"


def heartbeat_path(runner: str) -> Path:
    return HEARTBEAT_DIR / f"{_PREFIX}{runner}.json"


_last_write: Dict[str, float] = {}


def _replace_with_retry(tmp: Path, path: Path,
                        attempts: int = 5, delay: float = 0.05) -> None:
    """os.replace, retried through transient Windows sharing violations.

    On Windows `os.replace` needs delete access to the DESTINATION, and CPython's
    `open()` does not grant FILE_SHARE_DELETE — so while any reader (the
    supervisor), Defender, or a OneDrive/Desktop sync client has the file open,
    the rename fails with WinError 5. It clears in
    milliseconds, so a few short retries turn a hard failure into a no-op.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def write_heartbeat(runner: str, interval_seconds: float,
                    min_write_interval: float = 0.0, **detail) -> None:
    """Stamp `runner` as alive right now.

    `interval_seconds` is this runner's own loop cadence; it travels with the
    beat so readers can size the staleness window per runner. `detail` carries
    whatever context that runner wants recorded (env, pairs, ...).
    `min_write_interval` throttles writes (progress callbacks can fire many times
    per pass, and every write is a file replace).

    NEVER RAISES. A liveness signal must not be able to kill the runner it
    monitors: an unwritable heartbeat is a monitoring gap, not a trading fault.
    """
    now = time.monotonic()
    if min_write_interval > 0.0:
        last = _last_write.get(runner)
        if last is not None and (now - last) < min_write_interval:
            return

    payload = {
        "runner": runner,
        "ts": datetime.now(timezone.utc).isoformat(),
        "interval_seconds": float(interval_seconds),
        **detail,
    }
    path = heartbeat_path(runner)
    # PID in the temp name so two processes of the same runner (e.g. an old one
    # still exiting while the supervisor starts its replacement) cannot collide.
    tmp = path.parent / f"{path.stem}.{os.getpid()}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        _replace_with_retry(tmp, path)
        _last_write[runner] = now
    except OSError as exc:
        log.debug("heartbeat write failed for %s: %s", runner, exc)
        try:
            tmp.unlink(missing_ok=True)   # don't leave temp files behind
        except OSError:
            pass


def read_heartbeat(runner: str) -> Optional[dict]:
    try:
        with open(heartbeat_path(runner), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def classify_heartbeat(hb: Optional[dict],
                       now: Optional[datetime] = None) -> Tuple[str, Optional[float]]:
    """(state, age_seconds) for one beat. Pure — `now` is injectable for tests.

    ONLINE while within 3 cadences (floor 60s: covers a slow tick or a GC pause),
    STALE out to 10 cadences (floor 1h), OFFLINE beyond that.
    """
    if not hb:
        return OFFLINE, None
    try:
        ts = datetime.fromisoformat(hb.get("ts"))
    except (TypeError, ValueError):
        return UNKNOWN, None
    if ts.tzinfo is None:                      # tolerate a naive stamp
        ts = ts.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age = (now - ts).total_seconds()
    try:
        interval = float(hb.get("interval_seconds") or 15.0)
    except (TypeError, ValueError):
        interval = 15.0
    if age <= max(interval * 3.0, 60.0):
        return ONLINE, age
    if age <= max(interval * 10.0, 3600.0):
        return STALE, age
    return OFFLINE, age
