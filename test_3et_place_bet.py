#!/usr/bin/env python3
"""
One-off 3et placement test — place MLB moneyline using live board odds.
"""
import os

os.environ.setdefault("SKIP_DB_BOOTSTRAP", "1")

import argparse
import json
import sys

from cache.arbitrage_cache import ArbitrageCache
from controllers.ThreeEtController import ThreeEtController
from database.models.Accounts import Accounts
from utils.bet_placement import finalize_confirmed_bet
from utils.config import THREEET, TELEGRAM
from utils.helpers import is_game_pregame
from utils.logger import Logger
from utils.storage import Storage


def main():
    parser = argparse.ArgumentParser(description="Place a test moneyline bet on 3et.com")
    parser.add_argument("--team-name", default="Miami Marlins")
    parser.add_argument("--stake", type=float, default=4.0)
    parser.add_argument("--odds", default=None, help="Expected American odds (optional guard)")
    args = parser.parse_args()

    from utils.config import THREEET_ACCOUNT, THREEET_PASSWORD, THREEET_LABEL

    if not THREEET_ACCOUNT or not THREEET_PASSWORD:
        print("THREEET_ACCOUNT and THREEET_PASSWORD must be set in .env")
        sys.exit(1)

    account = Accounts(
        account=THREEET_ACCOUNT,
        password=THREEET_PASSWORD,
        label=THREEET_LABEL,
    )
    controller = ThreeEtController(account, THREEET, sport="baseball")
    logger = Logger.get_logger("3et-test-place-bet")
    storage = Storage(logger)
    cache = ArbitrageCache()

    controller._login()
    raw = controller._fetch_competition_events_raw()
    pre = [e for e in raw if not e.get("inRunning")]
    print(f"Raw pregame events from 3et: {len(pre)}")
    for e in pre:
        if "marlin" in json.dumps(e).lower():
            print(f"  RAW marlins: {e.get('name')} id={e.get('id')} status={e.get('status')}")
            for mp in e.get("marketPeriods") or []:
                for mt in mp.get("marketTypes") or []:
                    t = mt.get("marketType")
                    runners = []
                    for m in mt.get("markets") or []:
                        runners.extend(m.get("runners") or [])
                    print(f"    market {t} runners={len(runners)}", [(r.get("name"), r.get("id")) for r in runners[:3]])
    games = controller._refresh_schedule_cache()
    if not games:
        print("No MLB games found on 3et")
        sys.exit(1)

    target = None
    team_query = (args.team_name or "").strip().lower()
    for game in games:
        if not is_game_pregame(game.get("game_datetime")):
            continue
        for team_no in (1, 2):
            name = game.get(f"team_{team_no}") or ""
            if controller._team_name_matches(name, args.team_name) or team_query in name.lower():
                target = (game, team_no)
                break
        if target:
            break

    if not target:
        print(f"Team not found in pregame schedule: {args.team_name}")
        for g in games:
            print(f"  - {g.get('team_1')} vs {g.get('team_2')} @ {g.get('game_datetime')}")
        sys.exit(1)

    game, team_no = target
    team_name = game[f"team_{team_no}"]
    live_odds = game["moneyline"][f"team_{team_no}"]
    expected_odds = args.odds or live_odds
    game_id = game["game_id"]

    print(f"Game: {game['team_1']} vs {game['team_2']}")
    print(f"Placing ${args.stake:.2f} on {team_name} ML {live_odds} (game_id={game_id})")

    ok, stake_used = controller.place_moneyline_bet(
        game_id,
        team_name,
        expected_odds,
        args.stake,
        team_1=game["team_1"],
        team_2=game["team_2"],
    )
    if not ok:
        print(f"Bet failed: {controller._last_bet_error}")
        sys.exit(1)

    print("Bet accepted on 3et")
    arb_stub = {
        "sport": "MLB",
        "league": "MLB",
        "team_1": game["team_1"],
        "team_2": game["team_2"],
        "game_datetime": game["game_datetime"],
        "team_1_bookmaker": THREEET["bookmaker"] if team_no == 1 else "test",
        "team_2_bookmaker": "test" if team_no == 1 else THREEET["bookmaker"],
        "team_1_odds": live_odds if team_no == 1 else "+100",
        "team_2_odds": "+100" if team_no == 1 else live_odds,
        "team_1_game_id": game_id if team_no == 1 else "test",
        "team_2_game_id": "test" if team_no == 1 else game_id,
        "bet_type": "moneyline",
    }
    finalize_confirmed_bet(
        cache,
        storage,
        logger,
        arb_stub,
        THREEET["bookmaker"],
        team_no,
        team_name,
        game_id,
        stake_used,
        live_odds,
        TELEGRAM,
    )
    print("Saved bet + Telegram alert sent")


if __name__ == "__main__":
    main()
