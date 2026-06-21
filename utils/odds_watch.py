from datetime import datetime

from utils.helpers import parse_odds


def persist_moneyline_games(
    cache,
    storage,
    logger,
    games: list,
    sport_name: str,
    league: str,
    last_saved: dict,
    source: str = "watch",
) -> int:
    """Save moneyline rows when odds changed vs last_saved[game_id] signature."""
    if not games:
        return 0

    if last_saved is None:
        last_saved = {}

    odds_data = {
        "sport": sport_name,
        "league": league,
        "total_matches": len(games),
        "matches": games,
        "timestamp": datetime.now().isoformat(),
    }
    parsed_odds = parse_odds(odds_data)
    saved = 0
    for odd_row in parsed_odds:
        if odd_row.get("bet_type") != "moneyline":
            continue
        sig = (odd_row.get("moneyline_team_1"), odd_row.get("moneyline_team_2"))
        game_id = odd_row.get("game_id")
        if last_saved.get(game_id) == sig:
            continue
        last_saved[game_id] = sig
        cache.add_odds(odd_row)
        try:
            storage.save_odds(odd_row)
        except Exception as db_err:
            error_str = str(db_err).lower()
            if "arbitrage_odds" in error_str or "doesn't exist" in error_str or "1146" in error_str:
                logger.warning("⚠️ Table 'arbitrage_odds' issue - continuing")
            else:
                logger.warning(f"DB save failed: {db_err}")
        saved += 1

    if saved:
        logger.info(
            f"Published {saved} moneyline update(s) from {len(games)} games ({source})"
        )
    return saved
