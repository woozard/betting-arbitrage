#!/usr/bin/env python3
"""Telegram ops bot — responds to /scan in TELEGRAM_CHAT_OPS with live arb status."""

import asyncio
import time

from utils.arb_scan_report import build_scan_report, split_telegram_messages
from utils.config import TELEGRAM
from utils.logger import Logger


HELP_TEXT = (
    "Commands:\n"
    "/scan — current MLB moneylines + arb % for active pairs\n"
    "/help — this message"
)


def _command_name(text: str) -> str:
    if not text:
        return ""
    token = text.strip().split()[0].lower()
    if "@" in token:
        token = token.split("@", 1)[0]
    return token


async def _send_plain(bot, chat_id: str, text: str):
    await bot.send_message(chat_id=chat_id, text=text)


async def _send_report(bot, chat_id: str, text: str):
    for part in split_telegram_messages(text):
        await bot.send_message(chat_id=chat_id, text=part)


async def run_bot():
    logger = Logger.get_logger("telegram-ops-bot")
    token = TELEGRAM.get("bot_token")
    ops_chat = TELEGRAM.get("ops")

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    if not ops_chat:
        raise RuntimeError("TELEGRAM_CHAT_OPS not set")

    from telegram import Bot

    bot = Bot(token=token)
    ops_chat_id = str(ops_chat)
    offset = 0

    logger.info(f"Telegram ops bot started (ops chat {ops_chat_id})")

    while True:
        try:
            updates = await bot.get_updates(
                offset=offset,
                timeout=30,
                allowed_updates=["message"],
            )
            for update in updates:
                offset = update.update_id + 1
                message = update.message
                if not message or not message.text:
                    continue

                chat_id = str(message.chat_id)
                if chat_id != ops_chat_id:
                    logger.info(f"Ignoring message from unauthorized chat {chat_id}")
                    continue

                cmd = _command_name(message.text)
                if cmd == "/scan":
                    logger.info("/scan requested")
                    try:
                        report = build_scan_report()
                        await _send_report(bot, chat_id, report)
                    except Exception as exc:
                        logger.error(f"/scan failed: {exc}", exc_info=True)
                        await _send_plain(bot, chat_id, f"Scan failed: {exc}")
                elif cmd in ("/help", "/start"):
                    await _send_plain(bot, chat_id, HELP_TEXT)
        except Exception as exc:
            logger.error(f"Telegram poll error: {exc}", exc_info=True)
            time.sleep(5)


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
