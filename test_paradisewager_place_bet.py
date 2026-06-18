#!/usr/bin/env python3
"""One-off ParadiseWager placement test via player-api (AddBet/SaveBet/confirmBet)."""
import argparse
import sys

from controllers.ParadiseWagerController import ParadiseWagerController
from database.models.Accounts import Accounts
from utils.config import (
    PARADISEWAGER,
    PARADIESWAGER_ACCOUNT,
    PARADIESWAGER_PASSWORD,
    PARADIESWAGER_LABEL,
)
from utils.logger import Logger


def _find_team_game(games: list, team_name: str):
    team_l = team_name.strip().lower()
    matches = []
    for game in games:
        for team_no, name in ((1, game.get("team_1")), (2, game.get("team_2"))):
            if not name or team_l not in name.lower():
                continue
            odds = (game.get("moneyline") or {}).get(f"team_{team_no}")
            matches.append((game, team_no, name, odds))
    if not matches:
        return None
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description="ParadiseWager manual placement test")
    parser.add_argument("--stake", type=float, default=12.0)
    parser.add_argument("--team-name", default="New York Yankees")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    if not PARADIESWAGER_ACCOUNT or not PARADIESWAGER_PASSWORD:
        print("PARADIESWAGER_ACCOUNT and PARADIESWAGER_PASSWORD must be set in .env")
        sys.exit(1)

    account = Accounts(
        account=PARADIESWAGER_ACCOUNT,
        password=PARADIESWAGER_PASSWORD,
        label=PARADIESWAGER_LABEL,
    )
    controller = ParadiseWagerController(account, PARADISEWAGER, sport="baseball")
    logger = Logger.get_logger("pw-placement-test")

    print("=== ParadiseWager placement test (MLB) ===")
    print(f"Stake: ${args.stake:.2f}")

    try:
        controller._ParadiseWagerController__login()
        games = controller._refresh_schedule_cache()
    except Exception as e:
        print(f"Login/schedule failed: {e}")
        sys.exit(1)

    if not games:
        print("No MLB games on schedule.")
        sys.exit(1)

    print(f"\nFound {len(games)} MLB game(s):\n")
    for i, game in enumerate(games):
        ml1 = (game.get("moneyline") or {}).get("team_1")
        ml2 = (game.get("moneyline") or {}).get("team_2")
        print(
            f"  [{i}] id={game.get('game_id')} | "
            f"{game.get('team_1')} ({ml1}) vs {game.get('team_2')} ({ml2}) | "
            f"{game.get('game_datetime')}"
        )

    pick = _find_team_game(games, args.team_name)
    if not pick:
        print(f"\nNo schedule row found for {args.team_name!r}.")
        sys.exit(1)

    game, team_no, team_name, live_odds = pick
    game_id = game["game_id"]
    team_1 = game.get("team_1")
    team_2 = game.get("team_2")

    print(
        f"\nLive pick: {team_name} @ {live_odds} (game_id={game_id}) "
        f"from current schedule"
    )

    if args.list_only:
        sys.exit(0)

    bet_placed, stake = controller._ParadiseWagerController__execute_bet(
        game_id,
        team_name,
        str(live_odds),
        args.stake,
        team_1=team_1,
        team_2=team_2,
    )

    if bet_placed:
        print(
            f"\nSUCCESS: ParadiseWager accepted {team_name} @ {live_odds} "
            f"for ${stake:.2f}"
        )
        sys.exit(0)

    err = controller._last_bet_error or "unknown error"
    print(f"\nFAILED: {err}")
    sys.exit(1)


if __name__ == "__main__":
    main()
