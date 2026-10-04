"""Supervisor (scripts.run_all) pure logic: runner selection, restart backoff,
and the runners config block. No processes are spawned."""
from datetime import datetime, timedelta, timezone

import pytest

from core.config import _load_runners
from core.heartbeat import ONLINE
from scripts.run_all import (
    HEARTBEAT_RUNNERS, RUNNERS, RestartPolicy, select_runners, stale_alert_reason,
)

ALL = list(RUNNERS)
CFG_ALL_ON = {n: True for n in ALL}


# ── select_runners ─────────────────────────────────────────────────────
def test_config_flags_pick_runners():
    assert select_runners({"trader": True}) == ["trader"]
    assert select_runners({"trader": False}) == []


def test_flag_for_a_removed_runner_is_ignored():
    # an old settings file may still say `dashboard: true`
    assert select_runners({"trader": True, "dashboard": True}) == ["trader"]


def test_missing_flag_means_disabled():
    assert select_runners({}) == []


def test_only_overrides_config_flags():
    # explicitly asked for → starts even though the config flag is off
    assert select_runners({"trader": False}, only=["trader"]) == ["trader"]


def test_skip_subtracts_from_config_selection():
    assert select_runners(CFG_ALL_ON, skip=["trader"]) == []


def test_skip_beats_only():
    assert select_runners(CFG_ALL_ON, only=["trader"], skip=["trader"]) == []


def test_unknown_runner_name_rejected():
    with pytest.raises(ValueError, match="unknown runner"):
        select_runners(CFG_ALL_ON, only=["trade"])
    with pytest.raises(ValueError, match="unknown runner"):
        select_runners(CFG_ALL_ON, skip=["dash"])


# ── RestartPolicy ──────────────────────────────────────────────────────
def test_backoff_ladder_holds_at_max():
    p = RestartPolicy()
    fast = RestartPolicy.STABLE_SECONDS / 10
    assert [p.on_exit(fast) for _ in range(4)] == [5.0, 30.0, 120.0, 120.0]


def test_five_rapid_failures_gives_up():
    p = RestartPolicy()
    delays = [p.on_exit(1.0) for _ in range(RestartPolicy.MAX_FAST_FAILS)]
    assert delays[-1] is None and all(d is not None for d in delays[:-1])


def test_stable_run_resets_ladder_and_fail_count():
    p = RestartPolicy()
    for _ in range(RestartPolicy.MAX_FAST_FAILS - 1):   # one short of give-up
        p.on_exit(1.0)
    assert p.on_exit(RestartPolicy.STABLE_SECONDS + 1) == 5.0  # ladder reset
    # fail count also reset: another near-limit burst still restarts
    for _ in range(RestartPolicy.MAX_FAST_FAILS - 1):
        assert p.on_exit(1.0) is not None


# ── hung-loop detection (process alive but not beating) ────────────────
_NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _hb(age_seconds: float, interval: float = 30.0) -> dict:
    return {"ts": (_NOW - timedelta(seconds=age_seconds)).isoformat(),
            "interval_seconds": interval}


def test_startup_grace_suppresses_false_alarms():
    """Startup (time sync, recovery, benchmark backfill) precedes the first
    beat — alerting during that window would cry wolf on every single start."""
    assert stale_alert_reason(None, uptime_seconds=10, now=_NOW) is None
    assert stale_alert_reason(None, uptime_seconds=299, now=_NOW) is None
    assert stale_alert_reason(None, uptime_seconds=301, now=_NOW) is not None


def test_healthy_beat_never_alerts():
    assert stale_alert_reason(_hb(5), uptime_seconds=10_000, now=_NOW) is None


def test_wedged_loop_is_reported_with_age():
    reason = stale_alert_reason(_hb(5_000, 30), uptime_seconds=10_000, now=_NOW)
    assert reason is not None and "5000s" in reason


def test_missing_and_unreadable_beats_are_distinguished():
    assert "no heartbeat" in stale_alert_reason(None, 10_000, now=_NOW)
    assert "unreadable" in stale_alert_reason({"ts": "junk"}, 10_000, now=_NOW)


def test_slow_cadence_runner_is_not_called_wedged():
    """A runner that beats every 900s: a 600s-old beat is healthy, not a hang."""
    assert stale_alert_reason(_hb(600, 900), uptime_seconds=10_000, now=_NOW) is None


def test_every_runner_is_heartbeat_monitored():
    """Guards the name coupling: the supervisor looks a child's heartbeat up by
    its runner key, so a rename in one place must not silently un-monitor it."""
    assert HEARTBEAT_RUNNERS == set(RUNNERS)


def test_heartbeat_runner_names_match_the_core_constants():
    from core import heartbeat as hbmod
    assert HEARTBEAT_RUNNERS == {hbmod.TRADER}


# ── config block ───────────────────────────────────────────────────────
def test_runners_config_parses_flags_and_default():
    cfg = _load_runners({"enabled": {"trader": True}})
    assert cfg.enabled == {"trader": True}
    assert cfg.auto_restart is True                      # default on
    assert _load_runners({"auto_restart": False}).auto_restart is False
    assert _load_runners({}).enabled == {}               # block absent → empty


# ── the supervisor's own logging must never kill it ────────────────────
def test_say_survives_a_console_that_cannot_encode_emoji(monkeypatch, capsys):
    """Every alert message carries an emoji. On a legacy Windows console (cp1252)
    print() raises UnicodeEncodeError — which, unguarded, would propagate out of
    the supervision loop and kill the supervisor exactly when a runner is in
    trouble. It must degrade the text instead."""
    import builtins
    from scripts import run_all as ra

    real_print = builtins.print
    calls = {"n": 0}

    def picky_print(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:                      # first attempt fails, as cp1252 would
            raise UnicodeEncodeError("charmap", "x", 0, 1, "undefined")
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", picky_print)
    ra._say("SUP", "🩺 wedged — REAL positions unprotected")   # must not raise
    monkeypatch.setattr(builtins, "print", real_print)
    assert calls["n"] == 2                                     # fell back and printed


def test_say_never_raises_even_if_stdout_is_broken(monkeypatch):
    import builtins
    from scripts import run_all as ra

    def dead_print(*a, **k):
        raise OSError("stdout closed")

    monkeypatch.setattr(builtins, "print", dead_print)
    ra._say("SUP", "anything")        # swallowed — supervision continues
