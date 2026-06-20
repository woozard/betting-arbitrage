#!/usr/bin/env python3
"""Manual Polymarket MLB moneyline placement test."""
import argparse
import json
import sys

from controllers.PolymarketController import PolymarketController
from utils.config import POLYMARKET, POLYMARKET_PRIVATE_KEY
from utils.logger import Logger


def main():
    parser = argparse.ArgumentParser(description="Polymarket manual placement test")
    parser.add_argument("--team-name", default="White Sox")
    parser.add_argument("--stake", type=float, default=1.0)
    parser.add_argument("--game-slug", default=None, help="e.g. mlb-cws-det-2026-06-20")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    if not args.list_only and not POLYMARKET_PRIVATE_KEY:
        print(
            "POLYMARKET_PRIVATE_KEY is not set.\n"
            "Export your wallet key from Polymarket Settings and add to .env:\n"
            "  POLYMARKET_PRIVATE_KEY=0x...\n"
            "  POLYMARKET_FUNDER_ADDRESS=0xb8c1139c50d60e58a17a9bf73417419a0d8855d6  # optional\n"
            "  POLYMARKET_SIGNATURE_TYPE=1\n"
            "  POLYMARKET_RELAYER_API_KEY=...\n"
        )
        sys.exit(1)

    controller = PolymarketController(POLYMARKET, sport="baseball")
    controller.logger = Logger.get_logger("polymarket-placement-test")

    pick = controller.find_moneyline_market_for_team(
        args.team_name, game_slug=args.game_slug
    )
    print("=== Polymarket placement test (MLB) ===")
    print(
        f"Game: {pick['team_1']} vs {pick['team_2']} | slug={pick['game_id']} | "
        f"{pick['game_datetime']}"
    )
    print(
        f"Pick: {pick['team_name']} @ {pick['american_odds']} "
        f"(prob={pick['price_prob']:.3f}) | token={pick['token_id']}"
    )
    print(f"Stake: ${args.stake:.2f}")

    if args.list_only:
        return

    result = controller.place_moneyline_bet(
        args.team_name,
        stake_usd=args.stake,
        game_slug=args.game_slug,
    )
    print("\nOrder placed:")
    print(json.dumps(result["response"], indent=2, default=str))


if __name__ == "__main__":
    main()
