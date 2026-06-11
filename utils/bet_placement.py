import asyncio

from utils.helpers import send_telegram_alert


def finalize_confirmed_bet(
    cache,
    storage,
    logger,
    arb: dict,
    bookmaker: str,
    team_no: int,
    team_name: str,
    game_id: str,
    stake: float,
    moneyline_odd,
    telegram_config: dict,
):
    """Run after a bookmaker has confirmed bet acceptance (not merely clicked Place Bet)."""
    sport = arb.get("sport")
    league = arb.get("league")
    game_date = arb.get("game_date")
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    bet_type = arb.get("bet_type", "moneyline")
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")

    cache.mark_leg_placed(bookmaker, bet_type, game_id)
    cache.lock_arb_scan(team_1, team_2, book_1, book_2, game_date)

    other_book = book_2 if bookmaker == book_1 else book_1
    other_game_id = (
        arb["team_2_game_id"] if bookmaker == arb["team_1_bookmaker"] else arb["team_1_game_id"]
    )
    other_leg_placed = cache.is_leg_placed(other_book, bet_type, other_game_id)

    if other_leg_placed:
        cache.remove_arbitrage_pair(arb)
        logger.info(
            f"Leg confirmed | {bookmaker}/{team_name} | {team_1} vs {team_2} | "
            f"both legs confirmed; arb scan locked and cache cleared"
        )
    else:
        logger.info(
            f"Leg confirmed | {bookmaker}/{team_name} | {team_1} vs {team_2} | "
            f"arb scan locked; {other_book} leg remains actionable in cache "
            f"(simultaneous placement — not waiting for other book)"
        )

    bet_data = {
        "sport": sport,
        "league": league,
        "game_id": game_id,
        "game_datetime": game_date,
        "team_1": team_1,
        "team_2": team_2,
        "bookmaker": bookmaker,
        "bet_type": bet_type,
        "team_no": team_no,
        "team_name": team_name,
        "odds": moneyline_odd,
        "stake": stake,
    }
    if storage.save_bet(bet_data):
        logger.info("DB - Bet Saved")
    else:
        logger.warning("DB - Bet Not Saved")

    if not cache.moneyline_alert_already_sent(team_1, team_2, book_1, book_2, game_date):
        alert = (
            f"===== Moneyline Bet =====\n"
            f"Sport: {sport}\n"
            f"League: {league}\n"
            f"Date: {game_date}\n"
            f"Match: {team_1} vs {team_2}\n"
            f"Bet Type: {bet_type}\n"
            f"Team No: {team_no}\n"
            f"Team: {team_name}\n"
            f"Bookmaker: {bookmaker}\n"
            f"Odds: {moneyline_odd}\n"
            f"Stake: {stake}\n"
            f"Status: Confirmed by bookmaker\n"
        )
        logger.info("========== Alert ==========")
        logger.info(alert)
        logger.info("========== Alert ==========")
        asyncio.run(send_telegram_alert(alert, telegram_config.get("arbitrage")))
        cache.mark_moneyline_alert_sent(team_1, team_2, book_1, book_2, game_date)
    else:
        logger.info(
            f"Moneyline Telegram alert already sent for {team_1} vs {team_2} "
            f"({book_1}/{book_2}); skipping duplicate"
        )