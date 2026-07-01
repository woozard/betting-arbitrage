#!/usr/bin/env python3
"""
One-off Sports411 end-to-end placement test.
Lists live MLB moneylines, places $25 using LIVE board odds,
then saves to DB and sends the ===== Moneyline Bet ===== Telegram alert.
"""
import os

# Allow running placement test without local/docker MySQL (bet still places on S411).
os.environ.setdefault("SKIP_DB_BOOTSTRAP", "1")

import argparse
import re
import sys
import time
from datetime import datetime

from bs4 import BeautifulSoup

from cache.arbitrage_cache import ArbitrageCache
from controllers.Sports411Controller import Sports411Controller
from database.models.Accounts import Accounts
from utils.bet_placement import finalize_confirmed_bet
from utils.config import SPORTS411, TELEGRAM
from utils.helpers import is_game_pregame
from utils.logger import Logger
from utils.storage import Storage


def _parse_games_from_html(controller: Sports411Controller, html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    games = []

    for game in soup.select("div.sports-league-game"):
        game_id = game.get("idgame")
        if not game_id:
            continue
        mline1 = game.select_one(".mline-1 label.bet-indicator")
        mline2 = game.select_one(".mline-2 label.bet-indicator")
        if not mline1 or not mline2:
            continue

        def extract_team_odds(label):
            title = (label.get("title") or label.text or "").strip()
            match = re.match(r"^(.+?)\s+([+-]?\d+)", title)
            if match:
                return match.group(1).strip(), match.group(2).strip()
            text = label.text.strip()
            match = re.match(r"^(.+?)\s+([+-]?\d+)", text)
            if match:
                return match.group(1).strip(), match.group(2).strip()
            return None, None

        team_1, team_1_ml = extract_team_odds(mline1)
        team_2, team_2_ml = extract_team_odds(mline2)
        if not team_1 or not team_2 or not team_1_ml or not team_2_ml:
            continue

        game_datetime_str = controller._extract_game_datetime(game)
        if not game_datetime_str:
            game_datetime_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        games.append({
            "game_id": game_id,
            "team_1": team_1,
            "team_2": team_2,
            "team_1_ml": team_1_ml,
            "team_2_ml": team_2_ml,
            "game_datetime": game_datetime_str,
        })

    return games


def _scrape_live_games(controller: Sports411Controller, raw: bool = False) -> list:
    controller._ensure_betting_session()
    controller._return_to_sport_page()
    time.sleep(3)
    games = _parse_games_from_html(controller, controller.driver.page_source)
    if raw:
        return games
    return controller._dedupe_games_by_matchup(games)


def _normalize_odd(value) -> int:
    return int(float(value))


def _find_mirror_pick(games: list, team_name: str, target_odds: str):
    """Find a board row matching team + odds (may be duplicate matchup rows)."""
    matches = []
    target = _normalize_odd(target_odds)
    team_l = team_name.strip().lower()

    for g in games:
        for team_no, (name, odd) in (
            (1, (g["team_1"], g["team_1_ml"])),
            (2, (g["team_2"], g["team_2_ml"])),
        ):
            if team_l not in name.lower():
                continue
            if _normalize_odd(odd) != target:
                continue
            matches.append((g, team_no, name, odd))

    if not matches:
        return None

    # Prefer highest game_id (same rule as production dedupe).
    matches.sort(key=lambda m: int(m[0]["game_id"]), reverse=True)
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description="Sports411 manual $25 placement test")
    parser.add_argument("--stake", type=float, default=25.0)
    parser.add_argument("--pick", type=int, help="Game index from deduped list")
    parser.add_argument("--team", choices=("1", "2"), default="1")
    parser.add_argument("--team-name", help="Bet this team (e.g. 'Seattle Mariners')")
    parser.add_argument("--odds", help="Required American odds at click time (e.g. '-150')")
    parser.add_argument("--list-only", action="store_true", help="Only list games, do not bet")
    parser.add_argument("--no-proxy", action="store_true", help="Skip BrightData proxy (diagnostic)")
    parser.add_argument("--headed", action="store_true", help="Run Chrome with UI (use xvfb-run on server)")
    parser.add_argument("--no-stealth", action="store_true", help="Use plain Selenium Chrome (not uc)")
    parser.add_argument("--attach", action="store_true", help="Attach to plain Chrome via remote debugging")
    parser.add_argument("--production", action="store_true",
                        help="Production config: Selenium+proxy+headless (Jun 17 style)")
    parser.add_argument("--xdotool-full", action="store_true", help="xdotool for moneyline+stake+place bet")
    parser.add_argument("--allow-live", action="store_true", help="Allow betting on started/live games")
    args = parser.parse_args()

    if args.production:
        args.attach = True
        args.no_proxy = False
        args.headless = False
        os.environ.setdefault("SPORTS411_XDOTOOL_BET_ONLY", "1")

    if args.xdotool_full:
        os.environ["SPORTS411_XDOTOOL_BET_ONLY"] = "0"

    account = Accounts(
        account="8715",
        password="eqr0mjx-MXY*rcn1ana",
        label="Bettor",
    )

    logger = Logger.get_logger("s411-placement-test")
    storage = Storage(logger)
    cache = ArbitrageCache()

    print("=== Sports411 placement test (MLB) ===")
    print(f"Stake: ${args.stake:.2f}")
    if args.attach:
        use_proxy = not args.no_proxy
        headless = not args.headed
        attach_browser = True
        use_stealth = not args.no_stealth
    else:
        use_proxy = not args.no_proxy
        headless = not args.headed
        attach_browser = False
        use_stealth = False

    controller = Sports411Controller(
        account,
        SPORTS411,
        sport="baseball",
        use_proxy=use_proxy,
        headless=headless,
        use_stealth=use_stealth,
        attach_browser=attach_browser,
    )

    try:
        raw_games = _scrape_live_games(controller, raw=True)
        games = controller._dedupe_games_by_matchup(list(raw_games))
        if not raw_games:
            print("No MLB games found on the board. Site may be empty or still loading.")
            return 1

        print(f"\nFound {len(raw_games)} DOM row(s), {len(games)} deduped matchup(s):\n")
        for i, g in enumerate(games):
            pre = is_game_pregame(g["game_datetime"])
            tag = "pregame" if pre else "started/live"
            print(
                f"  [{i}] id={g['game_id']} | {g['team_1']} ({g['team_1_ml']}) vs "
                f"{g['team_2']} ({g['team_2_ml']}) | {g['game_datetime']} | {tag}"
            )

        if len(raw_games) != len(games):
            print("\n  Raw DOM rows (including duplicates):")
            for g in raw_games:
                print(
                    f"    id={g['game_id']} | {g['team_1']} ({g['team_1_ml']}) vs "
                    f"{g['team_2']} ({g['team_2_ml']}) | {g['game_datetime']}"
                )

        if args.list_only:
            print("\n--list-only: no bet placed.")
            return 0

        if args.team_name:
            if args.odds:
                mirror = _find_mirror_pick(raw_games, args.team_name, args.odds)
                if not mirror:
                    print(
                        f"\nNo board row found for {args.team_name} @ {args.odds}. "
                        "Lines may have moved — re-run --list-only."
                    )
                    return 1
                game, team_no, team_name, live_odd = mirror
                print(
                    f"\nMirror pick: {team_name} @ {live_odd} "
                    f"(game_id={game['game_id']}) from raw DOM rows"
                )
            else:
                team_l = args.team_name.strip().lower()
                match = None
                for g in raw_games:
                    if team_l in g["team_1"].lower():
                        match = (g, 1, g["team_1"], g["team_1_ml"])
                        break
                    if team_l in g["team_2"].lower():
                        match = (g, 2, g["team_2"], g["team_2_ml"])
                        break
                if not match:
                    print(f"\nNo board row found for team {args.team_name}.")
                    return 1
                game, team_no, team_name, live_odd = match
                print(
                    f"\nLive pick: {team_name} @ {live_odd} "
                    f"(game_id={game['game_id']}) from current board"
                )
        else:
            if args.allow_live:
                pick = args.pick if args.pick is not None else 0
                if pick < 0 or pick >= len(games):
                    print(f"\nInvalid pick index {pick}.")
                    return 1
            else:
                pregame_idx = [i for i, g in enumerate(games) if is_game_pregame(g["game_datetime"])]
                if not pregame_idx:
                    print("\nNo pregame games available to bet.")
                    return 1
                pick = args.pick if args.pick is not None and args.pick in pregame_idx else pregame_idx[0]
            game = games[pick]
            team_no = int(args.team)
            team_name = game["team_1"] if team_no == 1 else game["team_2"]
            live_odd = game["team_1_ml"] if team_no == 1 else game["team_2_ml"]
            print(
                f"\nPlacing test bet on [{pick}] {team_name} @ {live_odd} "
                f"(game_id={game['game_id']}) using LIVE board odds..."
            )

        bet_placed, stake = controller._Sports411Controller__execute_bet(
            game["game_id"], team_name, live_odd, args.stake
        )

        if not bet_placed:
            err = controller._last_bet_error or "unknown error"
            print(f"\nFAILED: {err}")
            return 1

        print(f"\nSUCCESS: book accepted bet on {team_name} @ {live_odd} for ${stake:.2f}")

        arb = {
            "sport": controller.sport_name,
            "league": controller.league,
            "game_date": game["game_datetime"],
            "game_datetime": game["game_datetime"],
            "team_1": game["team_1"],
            "team_2": game["team_2"],
            "bet_type": "moneyline",
            "team_1_bookmaker": "sports411",
            "team_2_bookmaker": "manual-test",
            "team_1_game_id": game["game_id"],
            "team_2_game_id": "manual-test",
            "identified_at": time.time(),
        }

        try:
            finalize_confirmed_bet(
                cache,
                storage,
                logger,
                arb,
                "sports411",
                team_no,
                team_name,
                game["game_id"],
                stake,
                live_odd,
                TELEGRAM,
            )
            print("DB save + Telegram alert sent (===== Moneyline Bet =====).")
        except Exception as notify_err:
            print(f"Bet placed on S411 but DB/Telegram step failed: {notify_err}")
        return 0

    finally:
        controller._quit_driver()


if __name__ == "__main__":
    sys.exit(main())
