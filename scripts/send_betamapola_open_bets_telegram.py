#!/usr/bin/env python3
"""Capture Betamapola Open Bets page and send screenshot to KC Arb Screenshots Telegram."""
import argparse
import asyncio
import sys

from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from utils.config import BETAMAPOLA, BETAMAPOLA_ACCOUNT, BETAMAPOLA_PASSWORD, BETAMAPOLA_LABEL, TELEGRAM
from utils.helpers import format_utc_timestamp
from utils.logger import Logger


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--team-name", default="", help="Highlight row containing this team")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BETAMAPOLA_ACCOUNT or not BETAMAPOLA_PASSWORD:
        print("BETAMAPOLA_ACCOUNT and BETAMAPOLA_PASSWORD must be set")
        return 1

    chat_id = TELEGRAM.get("screenshots") or TELEGRAM.get("real_bets")
    if not chat_id and not args.dry_run:
        print("TELEGRAM_CHAT_SCREENSHOTS not set")
        return 1

    logger = Logger.get_logger("betamapola-open-bets-telegram")
    account = Accounts(
        account=BETAMAPOLA_ACCOUNT,
        password=BETAMAPOLA_PASSWORD,
        label=BETAMAPOLA_LABEL,
    )
    controller = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    controller.logger = logger

    try:
        controller._BetamapolaController__login()
        page_text = controller._load_open_bets_page_text()
        lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
        body_lines = [
            "===== Betamapola Open Bets =====",
            f"As of: {format_utc_timestamp()}",
            "",
        ]
        if "open bets" in page_text.lower():
            wager_lines = [
                ln for ln in lines
                if ln.lower() not in {"open bets", "include futures", "export to pdf", "export to excel"}
            ]
            body_lines.append(f"Visible rows/lines: {len(wager_lines)}")
            body_lines.extend(wager_lines[:24])
        else:
            body_lines.append("Could not read open-bets page content.")

        message = "\n".join(body_lines)
        print(message)

        team_name = args.team_name.strip()
        team_1 = ""
        team_2 = ""
        if team_name:
            for line in lines:
                if team_name.lower() in line.lower() and " vs " in line.lower():
                    parts = line.split(" vs ", 1)
                    team_1, team_2 = parts[0].strip(), parts[1].strip()
                    break

        shot = controller._notify_open_bets_screenshot(
            team_name or "Open Bets",
            team_1,
            team_2,
            "open_bets",
        )
        if not shot:
            print("Screenshot capture failed")
            return 1
        print(f"Screenshot: {shot}")
        if args.dry_run:
            return 0
        print("Sent to TELEGRAM_CHAT_SCREENSHOTS")
        return 0
    finally:
        controller._quit_driver()


if __name__ == "__main__":
    sys.exit(main())
