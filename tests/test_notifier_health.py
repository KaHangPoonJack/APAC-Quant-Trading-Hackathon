"""Escalation when a notifier fails PERSISTENTLY (a revoked token used to fail
silently forever, one log line per attempt and nothing drawing attention)."""
import logging

import pytest

from core.notifier import TelegramNotifier

TOKEN = "123456789:AAHqvQbQCclZTG_oDohJqoptcI53Cpt3ezY"


# ── notifier health escalation ─────────────────────────────────────────
def _breaking(exc):
    def _raise(*_a, **_k):
        raise exc
    return _raise


def test_single_failure_does_not_escalate(monkeypatch):
    n = TelegramNotifier(TOKEN, "-1")
    monkeypatch.setattr("urllib.request.urlopen", _breaking(OSError("blip")))
    assert n.send("x") is False
    assert n.consecutive_failures == 1 and not n.degraded


def test_escalates_once_after_three_consecutive_failures(monkeypatch, caplog):
    n = TelegramNotifier(TOKEN, "-1")
    monkeypatch.setattr("urllib.request.urlopen", _breaking(OSError("down")))
    with caplog.at_level(logging.CRITICAL):
        for _ in range(6):
            n.send("x")
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1                      # latched, not per-attempt
    assert "being LOST" in criticals[0].getMessage()
    assert n.degraded and n.consecutive_failures == 6


def test_success_resets_and_reports_recovery(monkeypatch, caplog):
    n = TelegramNotifier(TOKEN, "-1")
    monkeypatch.setattr("urllib.request.urlopen", _breaking(OSError("down")))
    for _ in range(3):
        n.send("x")
    assert n.degraded

    class _Resp:
        def read(self): return b'{"ok": true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    with caplog.at_level(logging.WARNING):
        assert n.send("x") is True
    assert not n.degraded and n.consecutive_failures == 0
    assert any("recovered" in r.getMessage() for r in caplog.records)


def test_api_level_rejection_counts_as_a_failure(monkeypatch):
    """A bad chat_id returns ok=false over a healthy connection — that is still
    a lost alert, so it must count toward escalation."""
    class _NotOk:
        def read(self): return b'{"ok": false, "description": "chat not found"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    n = TelegramNotifier(TOKEN, "-1")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _NotOk())
    for _ in range(3):
        assert n.send("x") is False
    assert n.degraded
