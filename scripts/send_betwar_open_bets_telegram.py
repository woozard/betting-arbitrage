#!/usr/bin/env python3
"""Capture BetWar My Bets and send open-wager summary + screenshot to real-bets Telegram."""
import argparse
import asyncio
import sys

from controllers.BetWarController import BetWarController
from database.models.Accounts import Accounts
from utils.bet_screenshot import capture_betwar_my_bets, bet_screenshot_path
from utils.config import BETWAR, BETWAR_ACCOUNT, BETWAR_PASSWORD, BETWAR_LABEL, TELEGRAM
from utils.helpers import format_utc_timestamp, send_telegram_alert, send_telegram_photo
from utils.logger import Logger


def _format_open_bets_message(my_bets_text: str) -> str:
    lines = [ln.strip() for ln in (my_bets_text or "").splitlines() if ln.strip()]
    skip = {"DESCRIPTION", "RISK / WIN", "MY BETS", "LOADING", "LOADING..."}
    entries = [ln for ln in lines if ln.upper() not in skip]

    def _is_risk_win(line: str) -> bool:
        return "/" in line and any(ch.isdigit() for ch in line)

    pairs = []
    i = 0
    while i < len(entries):
        if _is_risk_win(entries[i]) and i + 1 < len(entries) and not _is_risk_win(entries[i + 1]):
            pairs.append((entries[i + 1], entries[i]))
            i += 2
        elif not _is_risk_win(entries[i]) and i + 1 < len(entries) and _is_risk_win(entries[i + 1]):
            pairs.append((entries[i], entries[i + 1]))
            i += 2
        else:
            pairs.append((entries[i], ""))
            i += 1

    body_lines = [
        "===== BetWar Open Bets =====",
        f"As of: {format_utc_timestamp()}",
        "",
        f"Open wagers: {len(pairs)}",
        "",
    ]
    for desc, risk_win in pairs:
        body_lines.append(f"• {desc}")
        if risk_win:
            body_lines.append(f"  Risk / Win: {risk_win}")

    if len(pairs) == 0:
        body_lines.append("No open wagers on My Bets tab.")

    return "\n".join(body_lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BETWAR_ACCOUNT or not BETWAR_PASSWORD:
        print("BETWAR_ACCOUNT and BETWAR_PASSWORD must be set")
        sys.exit(1)

    chat_id = TELEGRAM.get("real_bets")
    if not chat_id and not args.dry_run:
        print("TELEGRAM_CHAT_REAL_BETS not set")
        sys.exit(1)

    logger = Logger.get_logger("betwar-open-bets-telegram")
    account = Accounts(account=BETWAR_ACCOUNT, password=BETWAR_PASSWORD, label=BETWAR_LABEL)
    controller = BetWarController(account, BETWAR, sport="baseball")
    controller.logger = logger

    out = args.output or bet_screenshot_path("betwar", "open_bets")
    try:
        controller._BetWarController__login()
        controller._BetWarController__ensure_sport_offering_loaded()
        my_bets_text = controller._my_bets_tab_text(timeout=15)
        message = _format_open_bets_message(my_bets_text)
        path = capture_betwar_my_bets(controller.driver, out, logger)
        if not path:
            print("Screenshot capture failed")
            sys.exit(1)

        print(message)
        print(f"Screenshot: {path}")

        if args.dry_run:
            return

        asyncio.run(send_telegram_alert(message, chat_id))
        caption = "\n".join(
            ln for ln in message.splitlines()
            if ln.strip() and not ln.startswith("=====")
        )[:1024]
        asyncio.run(send_telegram_photo(path, caption=caption, chat_id=chat_id))
        print("Sent to TELEGRAM_CHAT_REAL_BETS")
    finally:
        controller._quit_driver()


if __name__ == "__main__":
    main()
