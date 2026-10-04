"""Run the whole trading stack in ONE terminal.

    python -m scripts.run_all [--only a,b] [--skip a,b] [--pip] [--no-migrate]

Spawns each runner enabled in `runners.enabled` (settings.yaml) — today just the
trader — as its own child process, so a crash is contained and restarted.
All child output streams into this terminal with a per-runner prefix. Before spawning it runs
`alembic upgrade head` (idempotent — kills the post-git-pull schema error) and,
with --pip, `pip install -r requirements.txt`.

Crashed runners restart with backoff (5s -> 30s -> 120s) and the system Telegram
bot is alerted; 5 rapid failures in a row = give up on that runner (prevents
alert spam when e.g. Roostoo is unreachable or keys are missing). Runners are infinite loops, so ANY exit is
treated as a crash. Ctrl+C reaches every child (shared console) — the
supervisor waits for them to stop, then force-terminates stragglers.

On the EC2 box run it detached so it survives the Session Manager session,
e.g. `nohup python -m scripts.run_all > logs/run_all.out 2>&1 &` (or a
systemd unit) — the competition forbids manual intervention, so the bot must
keep running unattended.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ROOT, load_config
from core.heartbeat import ONLINE, UNKNOWN, classify_heartbeat, read_heartbeat
from core.notifier import Notifier, build_registry

# Runner name -> argv after `python`. Registry order = display/start order.
RUNNERS: Dict[str, List[str]] = {
    "trader": ["-m", "scripts.run_trader"],
}
PREFIX = {"trader": "TRDR"}
# Runners that stamp a liveness heartbeat. Names must match core.heartbeat's
# runner constants.
HEARTBEAT_RUNNERS = frozenset({"trader"})

_print_lock = threading.Lock()


def _say(prefix: str, text: str) -> None:
    """Print one supervisor/child line. NEVER raises: on a legacy Windows console
    (cp1252) an emoji in an alert would otherwise throw UnicodeEncodeError right
    out of the supervision loop — killing the supervisor at the exact moment a
    runner is in trouble, since every alert message carries one."""
    line = f"[{prefix:<4}] {text}"
    with _print_lock:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:  # noqa: BLE001 — logging must never stop supervision
            pass


# ── pure, unit-tested logic ────────────────────────────────────────────


def select_runners(enabled: Dict[str, bool],
                   only: Optional[List[str]] = None,
                   skip: Optional[List[str]] = None,
                   known: Iterable[str] = RUNNERS) -> List[str]:
    """Which runners to start. --only overrides the config flags entirely
    (an explicit ask wins even if the flag is off); --skip always subtracts."""
    known = list(known)
    for name in (only or []) + (skip or []):
        if name not in known:
            raise ValueError(f"unknown runner {name!r} — choose from {known}")
    names = [n for n in known if (n in only if only else enabled.get(n, False))]
    return [n for n in names if n not in (skip or [])]


def stale_alert_reason(hb: Optional[dict], uptime_seconds: float,
                       now=None, grace_seconds: float = 300.0) -> Optional[str]:
    """Why a RUNNING child looks wedged, or None if it looks fine.

    A crashed child is caught by exit code; this catches the other failure mode —
    the process is alive but its loop stopped beating (hung SDK call, deadlock).
    `grace_seconds` covers startup (time sync, recovery, benchmark backfill)
    before the first beat, so alerting earlier would be a false alarm.
    """
    if uptime_seconds < grace_seconds:
        return None
    if hb is None:
        return "no heartbeat written since start"
    state, age = classify_heartbeat(hb, now)
    if state == ONLINE:
        return None
    if state == UNKNOWN:
        return "heartbeat unreadable"
    return f"heartbeat {state} ({int(age or 0)}s since last beat)"


class RestartPolicy:
    """Backoff for one runner. A run that survived STABLE_SECONDS resets the
    ladder; MAX_FAST_FAILS crashes in a row without a stable run = give up
    (returns None) so a dead API or missing key doesn't spam restart alerts forever."""

    STEPS = (5.0, 30.0, 120.0)
    STABLE_SECONDS = 600.0
    MAX_FAST_FAILS = 5

    def __init__(self) -> None:
        self._attempt = 0
        self._fast_fails = 0

    def on_exit(self, uptime_seconds: float) -> Optional[float]:
        """Delay before restarting after a run of `uptime_seconds`, or None."""
        if uptime_seconds >= self.STABLE_SECONDS:
            self._attempt = 0
            self._fast_fails = 0
        else:
            self._fast_fails += 1
            if self._fast_fails >= self.MAX_FAST_FAILS:
                return None
        delay = self.STEPS[min(self._attempt, len(self.STEPS) - 1)]
        self._attempt += 1
        return delay


# ── process supervision ────────────────────────────────────────────────


class Child:
    def __init__(self, name: str) -> None:
        self.name = name
        self.proc: Optional[subprocess.Popen] = None
        self.policy = RestartPolicy()
        self.started_at = 0.0
        self.restart_at: Optional[float] = None
        self.gave_up = False
        self.stale_since: Optional[float] = None   # latch: alert once per episode


def _pump(prefix: str, proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        _say(prefix, line.rstrip())


def _spawn(name: str) -> subprocess.Popen:
    # PYTHONUNBUFFERED so piped child logs stream line-by-line, not on exit.
    # PYTHONIOENCODING so the child encodes its stdout as UTF-8 rather than the
    # Windows locale codepage — we decode the pipe as UTF-8, and a mismatch
    # mangles every em-dash/emoji in the log lines into replacement chars.
    env = {**os.environ, "PYTHONUNBUFFERED": "1",
           "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, *RUNNERS[name]], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    threading.Thread(target=_pump, args=(PREFIX[name], proc),
                     daemon=True, name=f"pump-{name}").start()
    return proc


def _run_step(desc: str, argv: List[str]) -> bool:
    _say("SUP", f"{desc}...")
    code = subprocess.run([sys.executable, *argv], cwd=str(ROOT)).returncode
    _say("SUP", f"{desc}: {'ok' if code == 0 else f'FAILED (exit {code})'}")
    return code == 0


def _shutdown(children: List[Child], timeout: float = 30.0) -> None:
    # Ctrl+C already hit every child (they share this console); just wait.
    _say("SUP", "stopping — waiting for runners to exit...")
    try:
        deadline = time.monotonic() + timeout
        for c in children:
            if c.proc is None or c.proc.poll() is not None:
                continue
            try:
                c.proc.wait(timeout=max(1.0, deadline - time.monotonic()))
                _say("SUP", f"{c.name} stopped")
            except subprocess.TimeoutExpired:
                _say("SUP", f"{c.name} still up after {timeout:.0f}s — terminating")
                c.proc.terminate()
                try:
                    c.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    c.proc.kill()
    except KeyboardInterrupt:  # second Ctrl+C = kill everything now
        for c in children:
            if c.proc is not None and c.proc.poll() is None:
                c.proc.kill()
    _say("SUP", "all runners stopped")


def _check_heartbeat(c: Child, now: float, notifier: Notifier) -> None:
    """Alert once when a live child stops beating, and once when it recovers.
    Runners that write no heartbeat are skipped."""
    if c.name not in HEARTBEAT_RUNNERS:
        return
    reason = stale_alert_reason(read_heartbeat(c.name), now - c.started_at)
    if reason and c.stale_since is None:
        c.stale_since = now
        msg = (f"🩺 run_all: {c.name} is RUNNING but not beating — {reason}. "
               "The process is up; its loop may be wedged.")
        _say("SUP", msg)
        notifier.send(msg)
    elif reason is None and c.stale_since is not None:
        c.stale_since = None
        msg = f"✅ run_all: {c.name} is beating again."
        _say("SUP", msg)
        notifier.send(msg)


def _supervise(children: List[Child], restart: bool, notifier: Notifier) -> int:
    while True:
        time.sleep(1)
        now = time.monotonic()
        for c in children:
            if c.proc is not None and c.proc.poll() is not None:
                code, uptime = c.proc.returncode, now - c.started_at
                c.proc = None
                if not restart:
                    _say("SUP", f"{c.name} exited (code {code}) — auto_restart off")
                    c.gave_up = True
                    continue
                delay = c.policy.on_exit(uptime)
                if delay is None:
                    c.gave_up = True
                    msg = (f"🛑 run_all: {c.name} keeps crashing (exit {code}) — "
                           "giving up on it. Fix the cause, then restart run_all.")
                else:
                    c.restart_at = now + delay
                    msg = (f"⚠️ run_all: {c.name} exited (code {code}) after "
                           f"{uptime:.0f}s — restarting in {delay:.0f}s")
                _say("SUP", msg)
                notifier.send(msg)
            elif c.proc is None and c.restart_at is not None and now >= c.restart_at:
                c.restart_at = None
                c.proc, c.started_at = _spawn(c.name), now
                c.stale_since = None
                _say("SUP", f"restarted {c.name}")
            elif c.proc is not None:
                _check_heartbeat(c, now, notifier)
        if all(c.gave_up for c in children):
            _say("SUP", "no runners left alive — exiting")
            return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the whole trading stack in one terminal",
        epilog=f"runners: {', '.join(RUNNERS)}")
    parser.add_argument("--only", default=None,
                        help="comma-separated runners to start (ignores config flags)")
    parser.add_argument("--skip", default=None,
                        help="comma-separated runners to NOT start")
    parser.add_argument("--pip", action="store_true",
                        help="pip install -r requirements.txt first (after git pull)")
    parser.add_argument("--no-migrate", action="store_true",
                        help="skip the automatic `alembic upgrade head`")
    parser.add_argument("--config", default=None, help="path to settings.yaml")
    args = parser.parse_args()

    # Emit UTF-8 regardless of the console codepage so alert emoji survive; _say
    # still degrades gracefully if even this is unavailable.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — older/odd streams
        pass

    cfg = load_config(args.config)
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else None
    try:
        names = select_runners(cfg.runners.enabled, split(args.only), split(args.skip))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not names:
        print("error: nothing to run — enable runners in settings.yaml "
              "(runners.enabled) or pass --only", file=sys.stderr)
        return 2

    if args.pip and not _run_step("pip install -r requirements.txt",
                                  ["-m", "pip", "install", "-r", "requirements.txt"]):
        return 1
    if cfg.persistence.enabled and not args.no_migrate and not _run_step(
            "alembic upgrade head", ["-m", "alembic", "upgrade", "head"]):
        return 1

    notifier = build_registry(cfg.notifications).system
    children = [Child(n) for n in names]
    for c in children:
        c.proc, c.started_at = _spawn(c.name), time.monotonic()
    started = ", ".join(names)
    off = ", ".join(n for n in RUNNERS if n not in names)
    _say("SUP", f"started: {started}" + (f"  (off: {off})" if off else ""))
    notifier.send(f"▶️ run_all up: {started}" + (f" (off: {off})" if off else ""))

    try:
        return _supervise(children, cfg.runners.auto_restart, notifier)
    except KeyboardInterrupt:
        _shutdown(children)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
