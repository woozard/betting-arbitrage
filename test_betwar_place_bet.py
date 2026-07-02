#!/usr/bin/env python3
"""One-off BetWar placement test."""
import argparse
import sys

from controllers.BetWarController import BetWarController
from database.models.Accounts import Accounts
from utils.config import BETWAR, BETWAR_ACCOUNT, BETWAR_PASSWORD, BETWAR_LABEL
from utils.logger import Logger


def _find_team_game(games: list, team_name: str):
    team_l = team_name.strip().lower()
    for game in games:
        for name, odd in (
            (game.get("team_1"), game.get("moneyline", {}).get("team_1")),
            (game.get("team_2"), game.get("moneyline", {}).get("team_2")),
        ):
            if name and team_l in name.lower():
                return game, name, odd
    return None


def main():
    parser = argparse.ArgumentParser(description="BetWar manual placement test")
    parser.add_argument("--stake", type=float, default=10.0)
    parser.add_argument("--team-name", required=True)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    if not BETWAR_ACCOUNT or not BETWAR_PASSWORD:
        print("BETWAR_ACCOUNT and BETWAR_PASSWORD must be set")
        sys.exit(1)

    account = Accounts(
        account=BETWAR_ACCOUNT,
        password=BETWAR_PASSWORD,
        label=BETWAR_LABEL,
    )
    controller = BetWarController(account, BETWAR, sport="baseball")
    controller.logger = Logger.get_logger("betwar-placement-test")

    controller._BetWarController__login()
    controller._BetWarController__ensure_sport_offering_loaded()

    games = controller._fetch_lines_via_getlines_api()
    if not games:
        api_lines = controller._fetch_game_lines_via_api()
        if api_lines:
            games = controller._parse_api_game_lines(api_lines)
    if games:
        games = controller._filter_games_with_valid_moneylines(games)

    print(f"=== BetWar placement test (MLB) ===\nStake: ${args.stake:.2f}\n")
    for i, g in enumerate(games or []):
        ml1 = (g.get("moneyline") or {}).get("team_1")
        ml2 = (g.get("moneyline") or {}).get("team_2")
        print(
            f"  [{i}] id={g.get('game_id')} | {g.get('team_1')} ({ml1}) vs "
            f"{g.get('team_2')} ({ml2}) | {g.get('game_datetime')}"
        )

    pick = _find_team_game(games or [], args.team_name)
    if not pick:
        print(f"\nNo game found for {args.team_name!r}")
        sys.exit(1)

    game, team_name, live_odd = pick
    print(f"\nLive pick: {team_name} @ {live_odd} (game_id={game['game_id']})")
    if args.list_only:
        sys.exit(0)

    if args.verify_only:
        confirmed, message = controller._verify_open_bet_on_my_bets(
            args.team_name,
            stake=args.stake,
            team_1=game.get("team_1"),
            team_2=game.get("team_2"),
        )
        print(f"\nVERIFY: open_bet={confirmed} message={message}")
        try:
            controller.driver.quit()
        except Exception:
            pass
        sys.exit(0 if confirmed else 1)

    bet_placed, stake = controller._execute_bet_attempt(
        game["game_id"],
        team_name,
        str(live_odd),
        args.stake,
        team_1=game.get("team_1"),
        team_2=game.get("team_2"),
    )
    print(f"\nRESULT: placed={bet_placed} stake={stake}")
    if controller._last_bet_error:
        print(f"Error: {controller._last_bet_error}")

    try:
        controller.driver.quit()
    except Exception:
        pass

    sys.exit(0 if bet_placed else 2)


if __name__ == "__main__":
    main()
