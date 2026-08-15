#!/usr/bin/env python3
"""
tokenscore Telegram bot.

    export TELEGRAM_BOT_TOKEN=...      # from @BotFather
    export TELEGRAM_OWNER_IDS=123456   # your user id, from @userinfobot
    python -m tokenscore.bot.main

Runs long-polling — no webhook, no public URL, works from any VPS.
"""
from __future__ import annotations

import logging
import os
import sys

from telegram import BotCommand
from telegram.ext import Application, CommandHandler

from . import handlers as h

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tokenscore")

COMMANDS: list[tuple[str, str, object]] = [
    ("start",           "Start bot",                        h.start),
    ("check",           "Check a token (gates only)",       h.check),
    ("score",           "Full multi-source scorecard",      h.score),
    ("pools",           "Search pools",                     h.pools),
    ("tokens",          "All pairs for a token",            h.tokens),
    ("pair",            "Single pool detail",               h.pair),
    ("profiles_latest", "Latest profiles",                  h.profiles_latest),
    ("boosts_latest",   "Latest boosts",                    h.boosts_latest),
    ("boosts_top",      "Top boosts",                       h.boosts_top),
    ("orders",          "Paid orders for a token",          h.orders),
    ("takeovers_latest", "Latest CTO",                      h.takeovers_latest),
    ("screen",          "Scans boosted tokens, ranked",     h.screen),
    ("alpha",           "High-conviction candidates",       h.alpha),
    ("early",           "Newest profiles, filtered",        h.early),
    ("market",          "Market scope",                     h.market),
]


def preflight() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN not set — create a bot via @BotFather")

    if not h.OWNER_IDS:
        sys.exit(
            "TELEGRAM_OWNER_IDS not set. This bot emits buy signals and must "
            "not run open to the public. Get your id from @userinfobot."
        )

    optional = {
        "BIRDEYE_API_KEY": "birdeye adapter disabled",
        "SOLSCAN_API_KEY": "solscan adapter disabled",
        "RUGCHECK_API_KEY": "rugcheck running on public rate limits",
    }
    for var, consequence in optional.items():
        if not os.getenv(var):
            log.warning("%s not set — %s", var, consequence)

    log.info("authorised users: %s", sorted(h.OWNER_IDS))
    return token


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [BotCommand(name, desc) for name, desc, _ in COMMANDS]
    )
    log.info("registered %d commands", len(COMMANDS))


def main() -> None:
    token = preflight()

    app = Application.builder().token(token).post_init(_post_init).build()
    for name, _, func in COMMANDS:
        app.add_handler(CommandHandler(name, func))

    log.info("polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
