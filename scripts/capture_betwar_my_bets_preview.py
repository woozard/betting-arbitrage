#!/usr/bin/env python3
"""One-off: log into BetWar, open My Bets, capture #pills-pending screenshot."""
import argparse
import logging
import sys

from controllers.BetWarController import BetWarController
from database.models.Accounts import Accounts
from utils.bet_screenshot import capture_betwar_my_bets, bet_screenshot_path
from utils.config import BETWAR, BETWAR_ACCOUNT, BETWAR_PASSWORD, BETWAR_LABEL
from utils.logger import Logger


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None, help="PNG path (default: screenshots/betwar_preview_<ts>.png)")
    args = parser.parse_args()

    if not BETWAR_ACCOUNT or not BETWAR_PASSWORD:
        print("BETWAR_ACCOUNT and BETWAR_PASSWORD must be set")
        sys.exit(1)

    logger = Logger.get_logger("betwar-screenshot-preview")
    logging.getLogger("betwar-screenshot-preview").setLevel(logging.INFO)

    account = Accounts(account=BETWAR_ACCOUNT, password=BETWAR_PASSWORD, label=BETWAR_LABEL)
    controller = BetWarController(account, BETWAR, sport="baseball")
    controller.logger = logger

    out = args.output or bet_screenshot_path("betwar", "preview")
    try:
        controller._BetWarController__login()
        controller._BetWarController__ensure_sport_offering_loaded()
        text = controller._my_bets_tab_text(timeout=15)
        logger.info(f"My Bets preview ({len(text)} chars): {text[:400]}")
        path = capture_betwar_my_bets(controller.driver, out, logger)
        if not path:
            print("Screenshot capture failed")
            sys.exit(1)
        print(path)
    finally:
        controller._quit_driver()


if __name__ == "__main__":
    main()
