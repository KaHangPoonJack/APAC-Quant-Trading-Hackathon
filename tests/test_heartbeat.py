"""Per-runner liveness heartbeats: file isolation and staleness classification.
No processes, no DB."""
from datetime import datetime, timedelta, timezone

import pytest

from core import heartbeat as hb


@pytest.fixture(autouse=True)
def tmp_heartbeat_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "HEARTBEAT_DIR", tmp_path)
    return tmp_path


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _beat(age_seconds: float, interval: float = 20.0, **extra) -> dict:
    ts = NOW - timedelta(seconds=age_seconds)
    return {"runner": "x", "ts": ts.isoformat(),
            "interval_seconds": interval, **extra}


# ── files ──────────────────────────────────────────────────────────────
def test_write_read_roundtrip_carries_detail():
    hb.write_heartbeat("trader", 20, env="TEST", pairs=["BTC/USD"])
    got = hb.read_heartbeat("trader")
    assert got["runner"] == "trader"
    assert got["interval_seconds"] == 20.0
    assert got["env"] == "TEST" and got["pairs"] == ["BTC/USD"]
    assert hb.classify_heartbeat(got)[0] == hb.ONLINE   # just written


def test_runners_do_not_share_a_file():
    """The whole point of the change: one runner's silence must not be read as
    another's, and one runner's beat must not mask another's death."""
    hb.write_heartbeat("aux", 30)
    assert hb.read_heartbeat("aux") is not None
    assert hb.read_heartbeat("trader") is None          # never started


def test_missing_and_corrupt_files_are_not_fatal(tmp_heartbeat_dir):
    assert hb.read_heartbeat("nope") is None
    hb.heartbeat_path("broken").write_text("{not json", encoding="utf-8")
    assert hb.read_heartbeat("broken") is None


def test_write_is_atomic_leaving_no_tmp_file(tmp_heartbeat_dir):
    hb.write_heartbeat("slow", 900)
    assert list(tmp_heartbeat_dir.glob("*.tmp")) == []


# ── staleness ──────────────────────────────────────────────────────────
def test_states_scale_with_each_runners_own_cadence():
    # 20s poll: stale within minutes.
    assert hb.classify_heartbeat(_beat(30, 20), NOW)[0] == hb.ONLINE
    assert hb.classify_heartbeat(_beat(300, 20), NOW)[0] == hb.STALE
    # 900s tick (slow runner): the SAME 300s age is still perfectly healthy.
    assert hb.classify_heartbeat(_beat(300, 900), NOW)[0] == hb.ONLINE
    assert hb.classify_heartbeat(_beat(4000, 900), NOW)[0] == hb.STALE
    assert hb.classify_heartbeat(_beat(20000, 900), NOW)[0] == hb.OFFLINE


def test_short_cadence_gets_a_60s_floor():
    """A 20s poll must not be called stale at 61s — one slow tick is normal."""
    assert hb.classify_heartbeat(_beat(59, 20), NOW)[0] == hb.ONLINE
    assert hb.classify_heartbeat(_beat(3599, 20), NOW)[0] == hb.STALE
    assert hb.classify_heartbeat(_beat(3601, 20), NOW)[0] == hb.OFFLINE


def test_missing_or_unparseable_beat():
    assert hb.classify_heartbeat(None, NOW) == (hb.OFFLINE, None)
    assert hb.classify_heartbeat({}, NOW) == (hb.OFFLINE, None)
    assert hb.classify_heartbeat({"ts": "garbage"}, NOW)[0] == hb.UNKNOWN
    assert hb.classify_heartbeat({"ts": None}, NOW)[0] == hb.UNKNOWN


def test_naive_timestamp_and_bad_interval_are_tolerated():
    naive = {"ts": (NOW - timedelta(seconds=10)).replace(tzinfo=None).isoformat()}
    assert hb.classify_heartbeat(naive, NOW)[0] == hb.ONLINE
    assert hb.classify_heartbeat(_beat(10, interval="oops"), NOW)[0] == hb.ONLINE


def test_clock_skew_into_the_future_is_not_stale():
    assert hb.classify_heartbeat(_beat(-120, 20), NOW)[0] == hb.ONLINE


def test_age_is_reported():
    state, age = hb.classify_heartbeat(_beat(45, 20), NOW)
    assert state == hb.ONLINE and age == pytest.approx(45.0)


# ── the beat must never be able to kill the runner ─────────────────────
def test_write_never_raises_when_replace_is_denied(monkeypatch, tmp_heartbeat_dir):
    """Windows holds a sharing violation on os.replace when a reader (the
    supervisor), Defender, or a OneDrive-synced folder has the target open. That
    must degrade to a missed beat, NEVER to an exception — a liveness signal
    killing the runner it monitors is strictly worse than no signal."""
    def denied(*_a, **_k):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(hb.os, "replace", denied)
    hb.write_heartbeat("research", 300)          # must not raise
    assert hb.read_heartbeat("research") is None
    # and it must not leave temp files piling up in logs/
    assert list(tmp_heartbeat_dir.glob("*.tmp")) == []


def test_write_survives_an_unwritable_directory(monkeypatch):
    def denied(*_a, **_k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(hb, "open", denied, raising=False)
    monkeypatch.setattr(hb.Path, "mkdir", denied)
    hb.write_heartbeat("slow", 900)               # must not raise


def test_replace_is_retried_through_a_transient_lock(monkeypatch):
    """The sharing violation clears in milliseconds, so a beat should still land
    rather than being silently dropped on the first collision."""
    real_replace, calls = hb.os.replace, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(hb.os, "replace", flaky)
    monkeypatch.setattr(hb.time, "sleep", lambda _s: None)
    hb.write_heartbeat("aux", 30)
    assert calls["n"] == 3
    assert hb.read_heartbeat("aux") is not None


def test_temp_name_is_process_unique(monkeypatch):
    """A restarting runner can briefly overlap with its replacement; both would
    otherwise write the same .tmp path and clobber each other."""
    import os as _os

    seen = []
    real = hb._replace_with_retry
    monkeypatch.setattr(hb, "_replace_with_retry",
                        lambda tmp, path, **k: (seen.append(str(tmp)), real(tmp, path))[1])
    hb.write_heartbeat("slow", 900)
    assert str(_os.getpid()) in seen[-1] and seen[-1].endswith(".tmp")


def test_min_write_interval_throttles_progress_beats(monkeypatch):
    """Progress callbacks can beat many times per pass; without throttling each
    one is a file replace, each a chance to collide with a reader."""
    writes = {"n": 0}
    real_replace = hb.os.replace

    def counting(src, dst):
        writes["n"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(hb.os, "replace", counting)
    hb._last_write.pop("research", None)
    for _ in range(50):
        hb.write_heartbeat("research", 300, min_write_interval=60.0)
    assert writes["n"] == 1                     # first lands, rest throttled
    hb.write_heartbeat("research", 300)     # unthrottled call always writes
    assert writes["n"] == 2
