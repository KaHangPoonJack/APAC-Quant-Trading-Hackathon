"""Diagnose + test the Telegram notification routing — no broker needed.

    python -m scripts.test_notification

Shows every configured bot (per-strategy + system), why each is/isn't ready, and
sends a real test message through each READY bot so you can confirm the right
strategy lands in the right chat. Run after a `git pull` on a new machine —
settings.local.yaml (the tokens) is git-ignored and never syncs.
"""
from __future__ import annotations

import sys

from core.config import load_config
from core.notifier import NotifierRegistry, TelegramNotifier, build_registry


def main() -> int:
    # A legacy Windows console (cp1252) cannot encode the arrows/emoji below;
    # without this the diagnostic itself dies before reporting anything.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    cfg = load_config()
    n = cfg.notifications

    print(f"notifications.enabled = {n.enabled}\n")
    if not n.enabled:
        print("Notifications are disabled — nothing will ever be sent.")
        return 1

    # Show the raw config per channel (source: settings.local.yaml or env).
    print("Configured channels (from settings.local.yaml / env):")
    rows = list(n.channels.items())
    if n.telegram_bot_token or n.telegram_chat_id:
        rows.append(("(legacy single bot -> system default)",
                     type("C", (), {"bot_token": n.telegram_bot_token,
                                    "chat_id": n.telegram_chat_id,
                                    "ready": bool(n.telegram_bot_token and n.telegram_chat_id)})()))
    if not rows:
        print("  (none) — add notifications.telegram.channels.* to settings.local.yaml")
    for name, ch in rows:
        status = getattr(ch, "status", "ready" if ch.ready else "not ready")
        tok = "<EMPTY>" if not ch.bot_token else (
            "<valid>" if getattr(ch, "token_valid", True) else "<BAD FORMAT>")
        print(f"  {name:22} token={tok:12} chat_id={ch.chat_id or '<EMPTY>':>14} "
              f"ready={str(ch.ready):5} {'' if ch.ready else '<- ' + status}")
    if any(ch.bot_token and not getattr(ch, "token_valid", True)
           for _, ch in rows):
        print("\n  A token is present but is NOT a Telegram token. Real tokens"
              "\n  look like 123456789:AAH... — replace the placeholder text in"
              "\n  config/settings.local.yaml with the value BotFather gave you.")

    reg = build_registry(n)
    print("\nResolved routing:")
    strat_keys = [k for k in reg.channels() if k != NotifierRegistry.SYSTEM]
    for k in strat_keys + [NotifierRegistry.SYSTEM]:
        target = (reg.system if k == NotifierRegistry.SYSTEM else reg.for_strategy(k))
        kind = "Telegram" if isinstance(target, TelegramNotifier) else "Null (no-op)"
        note = "" if isinstance(target, TelegramNotifier) else "  ← falls back / not configured"
        print(f"  {k:22} -> {kind}{note}")

    # Send a live test through every distinct READY bot.
    print("\nSending live test messages...")
    sent = 0
    seen = set()
    targets = [(NotifierRegistry.SYSTEM, reg.system)]
    targets += [(k, reg.for_strategy(k)) for k in strat_keys]
    for name, target in targets:
        if not isinstance(target, TelegramNotifier):
            continue
        key = (target._url, target._chat_id)  # de-dupe shared bots
        if key in seen:
            print(f"  {name}: (same bot as another channel — skipped duplicate)")
            continue
        seen.add(key)
        ok = target.send(f"✅ roostoo_compet test — this is the '{name}' bot.")
        print(f"  {name}: {'SENT' if ok else 'FAILED (see logged error)'}")
        sent += ok

    print(f"\n{sent} message(s) sent. If a bot didn't arrive: make sure you pressed "
          "START in that bot's chat and the chat_id is correct.")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
