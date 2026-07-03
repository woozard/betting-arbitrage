#!/usr/bin/env python3
"""Recover a BetWar leg already on My Bets and finalize the arb (leg 2 + complete alert)."""
import argparse
import sys
import time

from cache.arbitrage_cache import ArbitrageCache
from controllers.BetWarController import BetWarController
from database.models.Accounts import Accounts
from utils.bet_placement import capture_bet_screenshot_for_alert, finalize_confirmed_bet
from utils.config import (
    BETWAR,
    BETWAR_ACCOUNT,
    BETWAR_PASSWORD,
    BETWAR_LABEL,
    BET_STAKE,
    TELEGRAM,
)
from utils.logger import Logger
from utils.stake_sizing import base_amount_stake_from_odds
from utils.storage import Storage


def build_default_twins_yankees_ml_arb() -> dict:
    return {
        "sport": "MLB",
        "league": "MLB",
        "game_date": "2026-07-03",
        "game_datetime": "2026-07-03 23:05:00",
        "team_1": "Minnesota Twins",
        "team_2": "New York Yankees",
        "team_1_bookmaker": "4casters",
        "team_2_bookmaker": "betwar",
        "team_1_game_id": "6a468b3a349005b9eb186b37",
        "team_2_game_id": "963-964",
        "team_1_odds": 200.0,
        "team_2_odds": -190.0,
        "bet_type": "moneyline",
        "profit_pct": 1.0,
        "identified_at": time.time(),
    }


def main():
    parser = argparse.ArgumentParser(description="Finalize a BetWar leg found on My Bets")
    parser.add_argument("--team", default="New York Yankees")
    parser.add_argument("--game-id", default="963-964")
    parser.add_argument("--odds", type=float, default=-214.0, help="American odds on My Bets row")
    parser.add_argument("--stake", type=float, default=BET_STAKE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BETWAR_ACCOUNT or not BETWAR_PASSWORD:
        print("BETWAR_ACCOUNT and BETWAR_PASSWORD must be set")
        sys.exit(1)

    arb = build_default_twins_yankees_ml_arb()
    team_name = args.team
    game_id = args.game_id
    team_no = 2 if team_name == arb["team_2"] else 1
    wager_odds = args.odds
    stake_plan = base_amount_stake_from_odds(wager_odds, args.stake)

    logger = Logger.get_logger("betwar-recover-leg")
    cache = ArbitrageCache()
    storage = Storage(logger)

    other_book = arb["team_1_bookmaker"]
    other_game_id = arb["team_1_game_id"]
    print(
        f"Other leg {other_book} {other_game_id} placed: "
        f"{cache.is_leg_placed(other_book, 'moneyline', other_game_id)}"
    )

    account = Accounts(account=BETWAR_ACCOUNT, password=BETWAR_PASSWORD, label=BETWAR_LABEL)
    controller = BetWarController(account, BETWAR, sport="baseball")
    controller.logger = logger

    try:
        controller._BetWarController__login()
        controller._BetWarController__ensure_sport_offering_loaded()
        text = controller._my_bets_tab_text(timeout=15)
        logger.info(f"My Bets preview ({len(text)} chars):\n{text[:400]}")

        if not controller._my_bets_has_wager(team_name, stake_plan):
            print(
                f"No matching My Bets wager for {team_name} "
                f"(base ${args.stake:.2f}, to-win ${stake_plan.to_win:.2f})"
            )
            sys.exit(1)

        print(f"Found My Bets wager for {team_name} — ready to finalize leg 2")

        if args.dry_run:
            print("Dry run — not calling finalize_confirmed_bet")
            return

        screenshot_path = capture_bet_screenshot_for_alert(
            logger,
            "betwar",
            arb,
            team_name,
            game_id,
            stake_plan,
            wager_odds,
            driver=controller.driver,
        )
        finalize_confirmed_bet(
            cache,
            storage,
            logger,
            arb,
            "betwar",
            team_no,
            team_name,
            game_id,
            stake_plan,
            wager_odds,
            TELEGRAM,
            screenshot_path=screenshot_path,
        )
        print("Recovery complete — BetWar leg finalized; arb-complete alert sent if both legs set")
    finally:
        controller._quit_driver()


if __name__ == "__main__":
    main()
