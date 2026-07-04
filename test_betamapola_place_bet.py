#!/usr/bin/env python3
"""One-off Betamapola placement test via GetSportOffering API."""
import argparse
import os
import sys
import time

if "--api-only" in sys.argv:
    os.environ["BETAMAPOLA_API_PLACEMENT"] = "true"

from cache.arbitrage_cache import ArbitrageCache
from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from utils.bet_placement import finalize_confirmed_bet
from utils.config import BETAMAPOLA, BETAMAPOLA_ACCOUNT, BETAMAPOLA_PASSWORD, BETAMAPOLA_LABEL, TELEGRAM
from utils.logger import Logger
from utils.storage import Storage


def _find_team_game(games: list, team_name: str):
    team_l = team_name.strip().lower()
    for game in games:
        for team_no, name, odd in (
            (1, game.get("team_1"), game.get("moneyline", {}).get("team_1")),
            (2, game.get("team_2"), game.get("moneyline", {}).get("team_2")),
        ):
            if name and team_l in name.lower():
                return game, team_no, name, odd
    return None


def main():
    parser = argparse.ArgumentParser(description="Betamapola manual placement test")
    parser.add_argument("--stake", type=float, default=20.0)
    parser.add_argument("--team-name", required=True)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Force BETAMAPOLA_API_PLACEMENT=true for this run",
    )
    args = parser.parse_args()

    if not BETAMAPOLA_ACCOUNT or not BETAMAPOLA_PASSWORD:
        print("BETAMAPOLA_ACCOUNT and BETAMAPOLA_PASSWORD must be set")
        sys.exit(1)

    account = Accounts(
        account=BETAMAPOLA_ACCOUNT,
        password=BETAMAPOLA_PASSWORD,
        label=BETAMAPOLA_LABEL,
    )
    controller = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    if args.api_only:
        controller.API_PLACEMENT_ENABLED = True
    logger = Logger.get_logger("amapola-placement-test")
    storage = Storage(logger)
    cache = ArbitrageCache()

    try:
        controller._BetamapolaController__login()
        controller._BetamapolaController__ensure_sport_offering_loaded()
        lines = controller._fetch_game_lines_via_api()
        games = controller._parse_api_game_lines(lines)

        print(f"=== Betamapola placement test (MLB) ===\nStake: ${args.stake:.2f}\n")
        for i, g in enumerate(games):
            ml1 = (g.get("moneyline") or {}).get("team_1")
            ml2 = (g.get("moneyline") or {}).get("team_2")
            print(
                f"  [{i}] id={g.get('game_id')} | {g.get('team_1')} ({ml1}) vs "
                f"{g.get('team_2')} ({ml2}) | {g.get('game_datetime')}"
            )

        pick = _find_team_game(games, args.team_name)
        if not pick:
            print(f"\nNo game found for {args.team_name!r}")
            sys.exit(1)

        game, team_no, team_name, live_odd = pick
        print(
            f"\nLive pick: {team_name} @ {live_odd} (game_id={game['game_id']})"
        )
        if args.list_only:
            sys.exit(0)

        bet_placed, stake = controller._BetamapolaController__execute_bet(
            game["game_id"],
            team_name,
            str(live_odd),
            args.stake,
            team_1=game.get("team_1"),
            team_2=game.get("team_2"),
        )
        if not bet_placed:
            print(f"\nFAILED: {controller._last_bet_error or 'unknown'}")
            sys.exit(1)

        print(f"\nSUCCESS: {team_name} @ {live_odd} for ${stake:.2f}")
        arb = {
            "sport": controller.sport_name,
            "league": controller.league,
            "game_date": game["game_datetime"],
            "game_datetime": game["game_datetime"],
            "team_1": game["team_1"],
            "team_2": game["team_2"],
            "bet_type": "moneyline",
            "team_1_bookmaker": "betamapola",
            "team_2_bookmaker": "manual-test",
            "team_1_game_id": game["game_id"],
            "team_2_game_id": "manual-test",
            "identified_at": time.time(),
        }
        finalize_confirmed_bet(
            cache, storage, logger, arb, "betamapola",
            team_no, team_name, game["game_id"], stake, live_odd, TELEGRAM,
        )
        return 0
    finally:
        controller._quit_driver()


if __name__ == "__main__":
    sys.exit(main() or 0)
