"""Multi-bot Telegram routing: config parsing → registry → per-strategy fan-out.
No network — TelegramNotifier is constructed but never sent."""
from core.config import NotificationsConfig, TelegramChannel, _load_notifications
from core.notifier import (
    NotifierRegistry, NullNotifier, TelegramNotifier, build_registry,
)


# Valid-SHAPE fake tokens: TelegramChannel.ready now rejects anything that
# is not <digits>:<hash>, which is what catches pasted placeholders.
def _cfg(**channels_raw):
    return _load_notifications({"enabled": True, "telegram": {
        "chat_id": "SHARED", "channels": channels_raw}})


# ── config parsing ─────────────────────────────────────────────────────
def test_channels_parse_with_shared_chat_default():
    cfg = _cfg(system={"bot_token": "100001:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"},
              momentum={"bot_token": "100002:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "chat_id": "MOM_CHAT"})
    assert cfg.channels["system"].bot_token == "100001:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    assert cfg.channels["system"].chat_id == "SHARED"        # inherited default
    assert cfg.channels["momentum"].chat_id == "MOM_CHAT"  # per-channel wins
    assert cfg.channels["momentum"].ready
    assert not TelegramChannel(bot_token="x").ready          # needs both


def test_env_override_per_channel(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_PAIRS_V1", "ENVTOK")
    cfg = _load_notifications({"enabled": True, "telegram": {
        "chat_id": "SHARED",
        "channels": {"pairs-v1": {"bot_token": "100004:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "chat_id": "c"}}}})
    assert cfg.channels["pairs-v1"].bot_token == "ENVTOK"  # raw value kept   # env beats yaml


# ── registry routing ───────────────────────────────────────────────────
def test_registry_routes_each_strategy_to_its_own_bot():
    cfg = _cfg(system={"bot_token": "100001:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"},
              momentum={"bot_token": "100002:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"},
              mean_rev={"bot_token": "100003:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"})
    reg = build_registry(cfg)
    assert isinstance(reg.for_strategy("momentum"), TelegramNotifier)
    assert reg.for_strategy("momentum")._url != reg.for_strategy("mean_rev")._url
    assert isinstance(reg.system, TelegramNotifier)


def test_unconfigured_strategy_falls_back_to_system():
    cfg = _cfg(system={"bot_token": "100001:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"})
    reg = build_registry(cfg)
    # pairs-v1 has no dedicated bot → uses the system bot
    assert reg.for_strategy("pairs-v1") is reg.system
    assert isinstance(reg.for_strategy("pairs-v1"), TelegramNotifier)


def test_legacy_single_bot_becomes_system():
    cfg = _load_notifications({"enabled": True,
                              "telegram": {"bot_token": "100006:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "chat_id": "C"}})
    reg = build_registry(cfg)
    assert isinstance(reg.system, TelegramNotifier)
    assert reg.for_strategy("anything") is reg.system   # everything → the one bot


def test_disabled_or_empty_is_all_null():
    off = build_registry(_load_notifications({"enabled": False,
                                              "telegram": {"bot_token": "x", "chat_id": "y"}}))
    assert isinstance(off.system, NullNotifier)
    empty = build_registry(_load_notifications({"enabled": True, "telegram": {}}))
    assert isinstance(empty.system, NullNotifier)
    assert isinstance(empty.for_strategy("momentum"), NullNotifier)


def test_recorder_routes_trade_notify_by_strategy():
    """The recorder must pick the notifier by the trade's strategy name."""
    from engine.recorder import PersistenceService

    class Spy(NullNotifier):
        def __init__(self): self.msgs = []
        def send(self, text): self.msgs.append(text); return True

    ema, il, system = Spy(), Spy(), Spy()
    reg = NotifierRegistry({"momentum": ema, "mean_rev": il}, system)
    svc = PersistenceService(None, None, None, notifiers=reg)
    svc._notify_trade("OPEN", {"strategy": "momentum", "symbol": "BTC/USD",
                               "direction": "LONG", "qty": 1, "entry_price": 10,
                               "entry_ts": None, "tp": None, "sl": None})
    assert len(ema.msgs) == 1 and not il.msgs and not system.msgs
    # a strategy with no dedicated bot → system
    svc._notify_trade("OPEN", {"strategy": "pairs-v1", "symbol": "SOL/USD",
                               "direction": "LONG", "qty": 1, "entry_price": 10,
                               "entry_ts": None, "tp": None, "sl": None})
    assert len(system.msgs) == 1


def test_empty_channel_chat_id_falls_back_to_the_shared_default():
    """settings.yaml ships every channel with a placeholder `chat_id: ""`, so an
    empty value MUST mean 'not set' and inherit telegram.chat_id. Treating it as
    a deliberate blank made every channel ready=False and silently routed all
    alerts to a no-op notifier even though the tokens were filled in correctly."""
    cfg = _load_notifications({"enabled": True, "telegram": {
        "chat_id": "SHARED",
        "channels": {
            "system": {"bot_token": "100001:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "chat_id": ""},      # placeholder
            "momentum": {"bot_token": "100002:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"},              # key absent
            "mean_rev": {"bot_token": "100003:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "chat_id": "OWN"},
        }}})
    assert cfg.channels["system"].chat_id == "SHARED" and cfg.channels["system"].ready
    assert cfg.channels["momentum"].chat_id == "SHARED"
    assert cfg.channels["mean_rev"].chat_id == "OWN"      # explicit wins
    reg = build_registry(cfg)
    assert isinstance(reg.system, TelegramNotifier)
    assert isinstance(reg.for_strategy("momentum"), TelegramNotifier)


def test_channel_with_no_token_still_falls_back_to_system():
    """A strategy added after the bots were created (e.g. late-v1) must route
    to the system bot, never to a silent no-op."""
    cfg = _load_notifications({"enabled": True, "telegram": {
        "chat_id": "SHARED",
        "channels": {"system": {"bot_token": "100001:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "chat_id": ""},
                     "late-v1": {"bot_token": "", "chat_id": ""}}}})
    reg = build_registry(cfg)
    assert reg.for_strategy("late-v1") is reg.system
    assert isinstance(reg.for_strategy("late-v1"), TelegramNotifier)


# ── placeholder tokens must degrade, never crash ───────────────────────
def test_placeholder_token_is_not_accepted_as_configured():
    """A pasted placeholder is non-empty and so LOOKS configured, but produces a
    URL containing spaces — http.client then raised InvalidURL straight out of
    send(). It must read as not-ready instead, so routing falls back."""
    from core.config import TelegramChannel, looks_like_bot_token

    assert looks_like_bot_token("123456789:AAHqvQbQCclZTG_oDohJqoptcI53Cpt3ezY")
    assert not looks_like_bot_token("<system bot token>")
    assert not looks_like_bot_token("paste your token here")
    assert not looks_like_bot_token("")
    assert not looks_like_bot_token("nocolon12345678901234567890")

    bad = TelegramChannel(bot_token="<system bot token>", chat_id="-1001")
    assert not bad.ready and not bad.token_valid
    assert "placeholder" in bad.status
    good = TelegramChannel(bot_token="123456789:AAHqvQbQCclZTG_oDohJqoptcI53Cpt3ezY",
                           chat_id="-1001")
    assert good.ready and good.status == "ready"
    assert TelegramChannel(bot_token=good.bot_token).status == "no chat_id"


def test_a_bad_system_token_falls_back_to_noop_not_a_crash():
    """This is the real-world case: 4 valid strategy bots + a placeholder system
    token. The strategy bots must still work and system must become a no-op."""
    real = "123456789:AAHqvQbQCclZTG_oDohJqoptcI53Cpt3ezY"
    cfg = _load_notifications({"enabled": True, "telegram": {
        "chat_id": "-1001",
        "channels": {"system": {"bot_token": "<system bot token>"},
                     "momentum": {"bot_token": real}}}})
    reg = build_registry(cfg)
    assert isinstance(reg.for_strategy("momentum"), TelegramNotifier)
    assert isinstance(reg.system, NullNotifier)          # degraded, not broken


def test_send_never_raises_whatever_urllib_does(monkeypatch):
    """A notification is never worth crashing a trading loop or the supervisor.
    The old narrow except tuple missed http.client.InvalidURL (an HTTPException)."""
    import http.client

    n = TelegramNotifier("123456789:AAHqvQbQCclZTG_oDohJqoptcI53Cpt3ezY", "-1")
    for exc in (http.client.InvalidURL("control chars"),
                RuntimeError("boom"), KeyboardInterrupt if False else TypeError("x")):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(exc))
        assert n.send("hello") is False        # returns False, never raises
