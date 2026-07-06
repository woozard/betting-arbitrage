#!/usr/bin/env python3
"""Capture latest 4casters / Paradise bet screenshots and send to KC Arb Screenshots Telegram."""
import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from database.config import __get_db1_session__
from database.models.Accounts import Accounts
from database.models.ArbitrageBets import ArbitrageBets
from controllers.ParadiseWagerController import ParadiseWagerController
from utils.bet_placement import capture_bet_screenshot_for_alert, _send_ops_alert
from utils.bet_screenshot import bet_screenshot_path
from utils.config import (
    FOURCASTERS,
    FOURCASTERS_ACCOUNT,
    FOURCASTERS_LABEL,
    FOURCASTERS_PASSWORD,
    PARADIESWAGER_ACCOUNT,
    PARADIESWAGER_LABEL,
    PARADIESWAGER_PASSWORD,
    PARADISEWAGER,
    TELEGRAM,
)
from utils.fourcasters_client import FourCastersApiError, FourCastersClient
from utils.fourcasters_web import ensure_fourcasters_web_session, quit_fourcasters_driver
from utils.helpers import format_utc_timestamp
from utils.logger import Logger
from utils.stake_sizing import base_amount_stake_from_odds


def _latest_bet(session, bookmaker: str) -> ArbitrageBets | None:
    return (
        session.query(ArbitrageBets)
        .filter(ArbitrageBets.bookmaker == bookmaker)
        .order_by(ArbitrageBets.created_at.desc())
        .first()
    )


def _bet_summary(bet: ArbitrageBets) -> str:
    odds = bet.odds
    try:
        o = int(round(float(odds)))
        odds_str = f"+{o}" if o > 0 else str(o)
    except (TypeError, ValueError):
        odds_str = str(odds)
    stake = bet.stake
    try:
        stake_str = f"${float(stake):.2f}"
    except (TypeError, ValueError):
        stake_str = str(stake or "")
    return (
        f"{bet.bookmaker} | {bet.team_name} {odds_str} | {stake_str}\n"
        f"{bet.team_1} vs {bet.team_2} | game_id={bet.game_id}\n"
        f"placed {bet.created_at} UTC"
    )


def _stake_plan(bet: ArbitrageBets):
    try:
        base = float(bet.stake) if bet.stake is not None else None
    except (TypeError, ValueError):
        base = None
    return base_amount_stake_from_odds(bet.odds, base)


def capture_fourcasters(bet: ArbitrageBets, logger, *, dry_run: bool) -> str | None:
    if not FOURCASTERS_ACCOUNT or not FOURCASTERS_PASSWORD:
        logger.error("FOURCASTERS_ACCOUNT / FOURCASTERS_PASSWORD not set")
        return None

    api = FourCastersClient()
    try:
        api.login(FOURCASTERS_ACCOUNT, FOURCASTERS_PASSWORD)
    except FourCastersApiError as exc:
        logger.error(f"4casters API login failed: {exc}")
        return None

    driver = ensure_fourcasters_web_session(
        FOURCASTERS_ACCOUNT,
        FOURCASTERS_PASSWORD,
        logger,
        api_token=api.token,
    )
    if not driver:
        logger.error("4casters web session failed")
        return None

    path = bet_screenshot_path("4casters", bet.game_id)
    try:
        shot = capture_bet_screenshot_for_alert(
            logger,
            "4casters",
            {
                "team_1": bet.team_1,
                "team_2": bet.team_2,
                "game_date": str(bet.game_datetime)[:10] if bet.game_datetime else None,
            },
            bet.team_name,
            bet.game_id,
            _stake_plan(bet),
            bet.odds,
            driver=driver,
            open_bets_url="https://4casters.io/my-bets/active-wagers",
        )
        if dry_run:
            print(f"[dry-run] 4casters screenshot: {shot}")
        return shot
    finally:
        quit_fourcasters_driver(driver)


def capture_paradise(bet: ArbitrageBets, logger, *, dry_run: bool) -> str | None:
    if not PARADIESWAGER_ACCOUNT or not PARADIESWAGER_PASSWORD:
        logger.error("PARADIESWAGER_ACCOUNT / PARADIESWAGER_PASSWORD not set")
        return None

    account = Accounts(
        account=PARADIESWAGER_ACCOUNT,
        password=PARADIESWAGER_PASSWORD,
        label=PARADIESWAGER_LABEL,
    )
    controller = ParadiseWagerController(account, PARADISEWAGER, sport="baseball")
    controller.logger = logger

    try:
        controller._ParadiseWagerController__login()
        path = bet_screenshot_path("paradisewager", bet.game_id)
        shot = capture_bet_screenshot_for_alert(
            logger,
            "paradisewager",
            {
                "team_1": bet.team_1,
                "team_2": bet.team_2,
                "game_date": str(bet.game_datetime)[:10] if bet.game_datetime else None,
            },
            bet.team_name,
            bet.game_id,
            _stake_plan(bet),
            bet.odds,
            driver=controller.driver,
            open_bets_url=f"{controller.base_url}/v2/#/pendings",
        )
        if dry_run:
            print(f"[dry-run] Paradise screenshot: {shot}")
        return shot
    finally:
        controller._quit_driver()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--book",
        choices=("4casters", "paradisewager", "both"),
        default="both",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    chat_id = TELEGRAM.get("screenshots") or TELEGRAM.get("real_bets")
    if not chat_id and not args.dry_run:
        print("TELEGRAM_CHAT_SCREENSHOTS not set")
        return 1

    logger = Logger.get_logger("last-bet-screenshots")
    session = __get_db1_session__()
    books = []
    if args.book in ("4casters", "both"):
        books.append("4casters")
    if args.book in ("paradisewager", "both"):
        books.append("paradisewager")

    sent = 0
    try:
        for book in books:
            bet = _latest_bet(session, book)
            if not bet:
                logger.warning(f"No bets found in DB for {book}")
                continue

            header = (
                f"===== Last bet screenshot test ({book}) =====\n"
                f"As of: {format_utc_timestamp()}\n"
                f"{_bet_summary(bet)}"
            )
            print(header)

            if book == "4casters":
                shot = capture_fourcasters(bet, logger, dry_run=args.dry_run)
            else:
                shot = capture_paradise(bet, logger, dry_run=args.dry_run)

            if not shot:
                logger.error(f"Screenshot capture failed for {book}")
                continue

            print(f"Screenshot: {shot}")
            if args.dry_run:
                continue

            _send_ops_alert(
                logger,
                header,
                chat_id,
                label=f"{book} bet screenshot",
                photo_path=shot,
                photo_only=True,
            )
            sent += 1
    finally:
        session.close()

    if args.dry_run:
        return 0
    if sent == 0:
        return 1
    print(f"Sent {sent} screenshot(s) to TELEGRAM_CHAT_SCREENSHOTS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
