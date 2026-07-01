from datetime import datetime

from utils.helpers import parse_odds

PERSISTABLE_BET_TYPES = ("moneyline", "spread")


def _odds_last_saved_key(bet_type: str, game_id: str) -> str:
    return f"{bet_type}:{game_id}"


def _odds_change_signature(odd_row: dict) -> tuple | None:
    bet_type = odd_row.get("bet_type")
    if bet_type == "moneyline":
        if odd_row.get("moneyline_team_1") is None and odd_row.get("moneyline_team_2") is None:
            return None
        return (odd_row.get("moneyline_team_1"), odd_row.get("moneyline_team_2"))
    if bet_type == "spread":
        if odd_row.get("spread_team_1") is None and odd_row.get("spread_team_2") is None:
            return None
        return (
            odd_row.get("spread_value"),
            odd_row.get("spread_team_1"),
            odd_row.get("spread_team_2"),
        )
    return None


def persist_odds_games(
    cache,
    storage,
    logger,
    games: list,
    sport_name: str,
    league: str,
    last_saved: dict,
    source: str = "watch",
    bet_types: tuple = PERSISTABLE_BET_TYPES,
) -> int:
    """Save odds rows when values changed vs last_saved[`bet_type:game_id`] signature."""
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
    saved_by_type = {bet_type: 0 for bet_type in bet_types}
    for odd_row in parsed_odds:
        bet_type = odd_row.get("bet_type")
        if bet_type not in bet_types:
            continue
        sig = _odds_change_signature(odd_row)
        if sig is None:
            continue
        game_id = odd_row.get("game_id")
        cache_key = _odds_last_saved_key(bet_type, game_id)
        if last_saved.get(cache_key) == sig:
            continue
        last_saved[cache_key] = sig
        cache.add_odds(odd_row)
        try:
            storage.save_odds(odd_row)
        except Exception as db_err:
            error_str = str(db_err).lower()
            if "arbitrage_odds" in error_str or "doesn't exist" in error_str or "1146" in error_str:
                logger.warning("⚠️ Table 'arbitrage_odds' issue - continuing")
            else:
                logger.warning(f"DB save failed: {db_err}")
        saved_by_type[bet_type] = saved_by_type.get(bet_type, 0) + 1

    saved = sum(saved_by_type.values())
    if saved:
        parts = [
            f"{saved_by_type.get(bt, 0)} {bt}"
            for bt in bet_types
            if saved_by_type.get(bt, 0)
        ]
        logger.info(
            f"Published {', '.join(parts)} update(s) from {len(games)} games ({source})"
        )
    return saved


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
    """Save moneyline and spread/run-line rows (alerts-only markets included)."""
    return persist_odds_games(
        cache,
        storage,
        logger,
        games,
        sport_name,
        league,
        last_saved,
        source=source,
        bet_types=PERSISTABLE_BET_TYPES,
    )
