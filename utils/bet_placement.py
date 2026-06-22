import asyncio
import threading

from utils.config import SEQUENTIAL_ARB_BETTING, TELEGRAM_ALERTS_ASYNC, SECOND_LEG_ODDS_TOLERANCE, arb_pair_legs, required_first_leg_book
from utils.helpers import send_telegram_alert, format_utc_timestamp


def _ops_telegram_chat(telegram_config: dict):
    return telegram_config.get("ops")


def _send_ops_alert(logger, alert: str, ops_chat, label: str = "Alert") -> bool:
    if not ops_chat:
        logger.warning("TELEGRAM_CHAT_OPS not set — skipping Telegram alert")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    asyncio.run(send_telegram_alert(alert, ops_chat))
    return True


def _dispatch_ops_alert(logger, alert: str, ops_chat, label: str = "Alert") -> bool:
    if not ops_chat:
        logger.warning("TELEGRAM_CHAT_OPS not set — skipping Telegram alert")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    if TELEGRAM_ALERTS_ASYNC:
        threading.Thread(
            target=_send_ops_alert,
            args=(logger, alert, ops_chat, label),
            daemon=True,
        ).start()
        return True
    return _send_ops_alert(logger, alert, ops_chat, label)


def is_first_leg_bookmaker(book_1: str, book_2: str, bookmaker: str) -> bool:
    legs = arb_pair_legs(book_1, book_2)
    if not legs:
        return False
    return legs[0] == (bookmaker or "").strip().lower()


def odds_tolerance_for_placement(
    cache, arb: dict, book_1: str, book_2: str, bookmaker: str, bet_type: str
) -> int:
    """±tolerance on configured second-leg books, or when the other leg is already confirmed."""
    if SECOND_LEG_ODDS_TOLERANCE <= 0:
        return 0
    bm = (bookmaker or "").strip().lower()
    if not is_first_leg_bookmaker(book_1, book_2, bm):
        return SECOND_LEG_ODDS_TOLERANCE
    other_book = book_2 if bm == (book_1 or "").strip().lower() else book_1
    other_game_id = (
        arb["team_1_game_id"] if other_book == book_1 else arb["team_2_game_id"]
    )
    if cache.is_leg_placed(other_book, bet_type, other_game_id):
        return SECOND_LEG_ODDS_TOLERANCE
    return 0


def should_defer_for_sequential_first_leg(
    cache, arb: dict, book_1: str, book_2: str, bookmaker: str, bet_type: str
) -> bool:
    if not SEQUENTIAL_ARB_BETTING:
        return False
    first_leg_book = required_first_leg_book(book_1, book_2, bookmaker)
    if not first_leg_book:
        return False
    first_leg_game_id = (
        arb["team_1_game_id"] if book_1 == first_leg_book else arb["team_2_game_id"]
    )
    return not cache.is_leg_placed(first_leg_book, bet_type, first_leg_game_id)


def should_pause_first_leg_for_exposure(
    cache,
    book_1: str,
    book_2: str,
    bookmaker: str,
    arb: dict | None = None,
    bet_type: str = "moneyline",
) -> bool:
    """Pause new first-leg arbs only while one-sided exposure exists on the same matchup.

    Still allow the configured first-leg book to complete the hedge when the
    other book's leg for *this* arb is already confirmed (parallel placement).
    """
    if not is_first_leg_bookmaker(book_1, book_2, bookmaker):
        return False
    if arb is None:
        return cache.has_partial_exposure()

    pair_key = cache.matchup_pair_key(
        arb.get("team_1"),
        arb.get("team_2"),
        book_1,
        book_2,
        arb.get("game_date"),
    )
    if not cache.has_partial_exposure_for_pair(pair_key):
        return False

    bm = (bookmaker or "").strip().lower()
    b1 = (book_1 or "").strip().lower()
    other_book = book_2 if bm == b1 else book_1
    other_game_id = (
        arb["team_1_game_id"] if other_book == b1 else arb["team_2_game_id"]
    )
    if cache.is_leg_placed(other_book, bet_type, other_game_id):
        return False
    return True


def _build_leg_confirmed_alert(
    arb: dict,
    bookmaker: str,
    team_no: int,
    team_name: str,
    stake: float,
    moneyline_odd,
    other_book: str,
    other_leg_placed: bool,
) -> str:
    identified_at = arb.get("identified_at")
    sport = arb.get("sport")
    league = arb.get("league")
    game_date = arb.get("game_date")
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    bet_type = arb.get("bet_type", "moneyline")

    if other_leg_placed:
        status = "Both legs now confirmed"
    else:
        status = f"Real money placed — waiting for {other_book} leg"

    return (
        f"===== Leg Confirmed (Real Money) =====\n"
        f"Identified At: {format_utc_timestamp(identified_at)}\n"
        f"Sport: {sport}\n"
        f"League: {league}\n"
        f"Date: {game_date}\n"
        f"Match: {team_1} vs {team_2}\n"
        f"Bet Type: {bet_type}\n"
        f"Team No: {team_no}\n"
        f"Team: {team_name}\n"
        f"Bookmaker: {bookmaker}\n"
        f"Odds: {moneyline_odd}\n"
        f"Stake: ${stake:.2f}\n"
        f"Status: {status}\n"
    )


def _build_arb_complete_alert(arb: dict, stake: float) -> str:
    identified_at = arb.get("identified_at")
    sport = arb.get("sport")
    league = arb.get("league")
    game_date = arb.get("game_date")
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    bet_type = arb.get("bet_type", "moneyline")
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    odds_1 = arb.get("team_1_odds")
    odds_2 = arb.get("team_2_odds")
    profit_pct = arb.get("profit_pct")

    profit_line = f"Estimated Profit: {profit_pct}%\n" if profit_pct is not None else ""
    return (
        f"===== Arbitrage Complete =====\n"
        f"Identified At: {format_utc_timestamp(identified_at)}\n"
        f"Sport: {sport}\n"
        f"League: {league}\n"
        f"Date: {game_date}\n"
        f"Match: {team_1} vs {team_2}\n"
        f"Bet Type: {bet_type}\n\n"
        f"Leg 1: {team_1}\n"
        f"Bookmaker: {book_1}\n"
        f"Odds: {odds_1}\n"
        f"Stake: ${stake:.2f}\n\n"
        f"Leg 2: {team_2}\n"
        f"Bookmaker: {book_2}\n"
        f"Odds: {odds_2}\n"
        f"Stake: ${stake:.2f}\n\n"
        f"{profit_line}"
        f"Status: Both legs confirmed\n"
    )


def _build_partial_arb_alert(
    arb: dict,
    confirmed_book: str,
    failed_book: str,
    stake: float,
    reason: str,
) -> str:
    identified_at = arb.get("identified_at")
    sport = arb.get("sport")
    league = arb.get("league")
    game_date = arb.get("game_date")
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    bet_type = arb.get("bet_type", "moneyline")
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")

    if confirmed_book == book_1:
        confirmed_team, confirmed_odds = team_1, arb.get("team_1_odds")
    else:
        confirmed_team, confirmed_odds = team_2, arb.get("team_2_odds")

    reason_line = f"Reason: {reason}\n" if reason else ""
    return (
        f"===== Partial Arb (One Leg Only) =====\n"
        f"Identified At: {format_utc_timestamp(identified_at)}\n"
        f"Sport: {sport}\n"
        f"League: {league}\n"
        f"Date: {game_date}\n"
        f"Match: {team_1} vs {team_2}\n"
        f"Bet Type: {bet_type}\n\n"
        f"CONFIRMED: {confirmed_team} on {confirmed_book}\n"
        f"Odds: {confirmed_odds}\n"
        f"Stake: ${stake:.2f}\n\n"
        f"FAILED: second leg on {failed_book}\n"
        f"{reason_line}"
        f"Status: Exposed — only one side placed; check books manually\n"
    )


def maybe_notify_partial_arb_exposure(
    cache,
    logger,
    arb: dict,
    failed_bookmaker: str,
    stake: float,
    reason: str,
    telegram_config: dict,
):
    """Alert once when one leg is confirmed but the other book failed to place."""
    bet_type = arb.get("bet_type", "moneyline")
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    game_date = arb.get("game_date")
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    pair_key = cache.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)

    if cache.partial_arb_alert_already_sent(pair_key):
        return

    other_book = book_2 if failed_bookmaker == book_1 else book_1
    other_game_id = (
        arb["team_1_game_id"] if other_book == book_1 else arb["team_2_game_id"]
    )
    failed_game_id = (
        arb["team_1_game_id"] if failed_bookmaker == book_1 else arb["team_2_game_id"]
    )

    if not cache.is_leg_placed(other_book, bet_type, other_game_id):
        return
    if cache.is_leg_placed(failed_bookmaker, bet_type, failed_game_id):
        return

    ops_chat = _ops_telegram_chat(telegram_config)
    alert = _build_partial_arb_alert(arb, other_book, failed_bookmaker, stake, reason)
    if _dispatch_ops_alert(logger, alert, ops_chat, label="Partial Arb Alert"):
        cache.mark_partial_arb_alert_sent(pair_key)


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
    pair_key = cache.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)

    cache.mark_leg_placed(bookmaker, bet_type, game_id)
    cache.lock_arb_scan(team_1, team_2, book_1, book_2, game_date)

    other_book = book_2 if bookmaker == book_1 else book_1
    other_game_id = (
        arb["team_2_game_id"] if bookmaker == arb["team_1_bookmaker"] else arb["team_1_game_id"]
    )
    other_leg_placed = cache.is_leg_placed(other_book, bet_type, other_game_id)

    if other_leg_placed:
        cache.remove_arbitrage_pair(arb)
        cache.clear_partial_exposure(pair_key)
        logger.info(
            f"Leg confirmed | {bookmaker}/{team_name} | {team_1} vs {team_2} | "
            f"both legs confirmed; arb scan locked and cache cleared"
        )
    else:
        cache.mark_partial_exposure(pair_key)
        logger.info(
            f"Leg confirmed | {bookmaker}/{team_name} | {team_1} vs {team_2} | "
            f"waiting for {other_book} leg before arb is complete"
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

    ops_chat = _ops_telegram_chat(telegram_config)

    if not cache.bet_confirmed_alert_already_sent(bookmaker, bet_type, game_id):
        leg_alert = _build_leg_confirmed_alert(
            arb,
            bookmaker,
            team_no,
            team_name,
            stake,
            moneyline_odd,
            other_book,
            other_leg_placed,
        )
        if _dispatch_ops_alert(logger, leg_alert, ops_chat, label="Leg Confirmed Alert"):
            cache.mark_bet_confirmed_alert_sent(bookmaker, bet_type, game_id)
    else:
        logger.info(
            f"Skipping duplicate leg-confirmed Telegram alert - {bookmaker}/{team_name} | "
            f"{team_1} vs {team_2} | game_id={game_id}"
        )

    if other_leg_placed:
        if cache.arb_complete_alert_already_sent(pair_key):
            logger.info(
                f"Skipping duplicate arb-complete Telegram alert | {team_1} vs {team_2}"
            )
        else:
            complete_alert = _build_arb_complete_alert(arb, stake)
            if _dispatch_ops_alert(logger, complete_alert, ops_chat, label="Arb Complete Alert"):
                cache.mark_arb_complete_alert_sent(pair_key)
        return

    if not SEQUENTIAL_ARB_BETTING:
        if cache.bet_confirmed_alert_already_sent(bookmaker, bet_type, game_id):
            return
        identified_at = arb.get("identified_at")
        alert = (
            f"===== Moneyline Bet =====\n"
            f"Identified At: {format_utc_timestamp(identified_at)}\n"
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
        betting_chat = telegram_config.get("betting") or telegram_config.get("arbitrage")
        if TELEGRAM_ALERTS_ASYNC:
            threading.Thread(
                target=lambda: asyncio.run(send_telegram_alert(alert, betting_chat)),
                daemon=True,
            ).start()
        else:
            asyncio.run(send_telegram_alert(alert, betting_chat))
        cache.mark_bet_confirmed_alert_sent(bookmaker, bet_type, game_id)
