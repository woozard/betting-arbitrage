"""Shared helpers for arb betting loops (moneyline + spread)."""

from utils.config import spread_real_money_betting_enabled

SPREAD_BETTING_UNSUPPORTED_BOOKS = frozenset({"paradisewager"})


def get_arbitrage_for_placement(cache, bookmaker: str) -> list:
    """Return cached arbs this book should attempt to place (ML always; spread when enabled)."""
    bet_types = ["moneyline"]
    if spread_real_money_betting_enabled():
        bet_types.append("spread")

    results = []
    for bet_type in bet_types:
        results.extend(cache.get_arbitrage(bookmaker=bookmaker, bet_type=bet_type))
    return results


def arb_leg_for_book(arb: dict, bookmaker: str) -> dict | None:
    """Resolve this book's leg from a cached arb payload."""
    bm = (bookmaker or "").strip().lower()
    bet_type = (arb.get("bet_type") or "moneyline").lower()

    if (arb.get("team_1_bookmaker") or "").strip().lower() == bm:
        spread_line = arb.get("spread_line_team_1")
        if bet_type == "spread" and spread_line is None:
            spread_line = arb.get("spread_value")
        return {
            "team_no": 1,
            "game_id": arb.get("team_1_game_id"),
            "team_name": arb.get("team_1"),
            "odds": arb.get("team_1_odds"),
            "spread_line": spread_line,
            "bet_type": bet_type,
        }

    if (arb.get("team_2_bookmaker") or "").strip().lower() == bm:
        spread_line = arb.get("spread_line_team_2")
        if bet_type == "spread" and spread_line is None and arb.get("spread_value") is not None:
            try:
                spread_line = -float(arb.get("spread_value"))
            except (TypeError, ValueError):
                spread_line = None
        return {
            "team_no": 2,
            "game_id": arb.get("team_2_game_id"),
            "team_name": arb.get("team_2"),
            "odds": arb.get("team_2_odds"),
            "spread_line": spread_line,
            "bet_type": bet_type,
        }

    return None
