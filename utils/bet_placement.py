import asyncio
import re
import threading

from utils.config import (
    REAL_MONEY_BETTING_ENABLED,
    SPREAD_REAL_MONEY_BETTING_ENABLED,
    SEQUENTIAL_ARB_BETTING,
    TELEGRAM_ALERTS_ASYNC,
    SECOND_LEG_ODDS_TOLERANCE,
    arb_pair_legs,
    required_first_leg_book,
)
from utils.arb_placement import SPREAD_BETTING_UNSUPPORTED_BOOKS
from utils.helpers import send_telegram_alert, format_utc_timestamp
from utils.stake_sizing import BaseAmountStake, format_base_amount_stake

REAL_MONEY_BETTING_PAUSED_MSG = "Real money betting paused (REAL_MONEY_BETTING_ENABLED=false)"
SPREAD_BETTING_PAUSED_MSG = (
    "Spread real-money betting disabled (SPREAD_REAL_MONEY_BETTING_ENABLED=false)"
)


def real_money_betting_enabled() -> bool:
    return REAL_MONEY_BETTING_ENABLED


def spread_real_money_betting_enabled() -> bool:
    return SPREAD_REAL_MONEY_BETTING_ENABLED


def should_skip_spread_arb_for_placement(
    arb: dict, logger, bookmaker: str | None = None
) -> bool:
    """Alerts-only for spread arbs until explicitly enabled / supported on this book."""
    bet_type = (arb.get("bet_type") or "moneyline").lower()
    if bet_type != "spread":
        return False
    bm = (bookmaker or "").strip().lower()
    if bm in SPREAD_BETTING_UNSUPPORTED_BOOKS:
        logger.info(f"Spread real-money betting not implemented for {bm}")
        return True
    if spread_real_money_betting_enabled():
        return False
    logger.info(SPREAD_BETTING_PAUSED_MSG)
    return True


def block_real_money_bet(logger, stake: float, bet_type: str = "moneyline"):
    """Return (False, stake) when real-money betting is paused; else None."""
    bt = (bet_type or "moneyline").lower()
    if bt == "spread" and not spread_real_money_betting_enabled():
        logger.info(SPREAD_BETTING_PAUSED_MSG)
        return False, float(stake)
    if real_money_betting_enabled():
        return None
    logger.info(REAL_MONEY_BETTING_PAUSED_MSG)
    return False, float(stake)


def should_notify_failed_bet(last_error: str | None) -> bool:
    """Skip partial-exposure alerts when we deliberately did not attempt a bet."""
    if not last_error:
        return True
    if last_error.startswith("Real money betting paused"):
        return False
    return not last_error.startswith("Spread real-money betting disabled")


def _format_stake_line(stake) -> str:
    if isinstance(stake, BaseAmountStake):
        return format_base_amount_stake(stake)
    try:
        return f"${float(stake):.2f}"
    except (TypeError, ValueError):
        return str(stake)


def _stake_risk_amount(stake) -> float:
    if isinstance(stake, BaseAmountStake):
        return stake.risk
    return float(stake)


def format_bet_failure_reason(reason: str | None, bookmaker: str = "") -> str:
    """Turn raw Selenium/API exceptions into a short ops-friendly message."""
    if not reason:
        return "Bet not accepted by bookmaker"

    text = str(reason).strip()
    if "Stacktrace:" in text:
        text = text.split("Stacktrace:")[0].strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if lines:
        text = lines[0]

    text = re.sub(r"\s*\(Session info:.*", "", text).strip()
    book_label = (bookmaker or "bookmaker").strip() or "bookmaker"

    lower = text.lower()
    if "<!doctype" in lower or "not valid json" in lower:
        return (
            f"{book_label} session expired — site returned a login page "
            f"instead of betting data"
        )
    if "session expired" in lower or "please log in" in lower:
        return f"{book_label} session expired — re-login required"
    if "moneyline element not found" in lower or "moneyline not found" in lower:
        return text.replace("Message: ", "").strip()
    if "not visible on betwar bet board" in lower:
        return f"{book_label} game not loaded on bet board (API has lines, DOM does not)"
    if text.lower().startswith("message:"):
        text = text[8:].strip()

    if len(text) > 220:
        text = text[:217] + "..."
    return text or "Bet not accepted by bookmaker"


def _alerts_telegram_chat(telegram_config: dict):
    """Scanner /scan and ===== Arbitrage ===== opportunity alerts."""
    return telegram_config.get("arbitrage")


def _real_bets_telegram_chat(telegram_config: dict):
    """Leg confirmed, partial arb, and arb complete (real money)."""
    return telegram_config.get("real_bets")


def _send_ops_alert(logger, alert: str, chat_id, label: str = "Alert") -> bool:
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_REAL_BETS not set — skipping Telegram alert")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    asyncio.run(send_telegram_alert(alert, chat_id))
    return True


def _dispatch_ops_alert(logger, alert: str, chat_id, label: str = "Alert") -> bool:
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_REAL_BETS not set — skipping Telegram alert")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    if TELEGRAM_ALERTS_ASYNC:
        threading.Thread(
            target=_send_ops_alert,
            args=(logger, alert, chat_id, label),
            daemon=True,
        ).start()
        return True
    return _send_ops_alert(logger, alert, chat_id, label)


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
        f"Stake: {_format_stake_line(stake)}\n"
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
        f"Stake: {_format_stake_line(stake)}\n\n"
        f"Leg 2: {team_2}\n"
        f"Bookmaker: {book_2}\n"
        f"Odds: {odds_2}\n"
        f"Stake: {_format_stake_line(stake)}\n\n"
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

    reason_text = format_bet_failure_reason(reason, failed_book)
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
        f"Stake: {_format_stake_line(stake)}\n\n"
        f"FAILED: second leg on {failed_book}\n"
        f"Reason: {reason_text}\n"
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

    real_bets_chat = _real_bets_telegram_chat(telegram_config)
    alert = _build_partial_arb_alert(arb, other_book, failed_bookmaker, stake, reason)
    if _dispatch_ops_alert(logger, alert, real_bets_chat, label="Partial Arb Alert"):
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
        "stake": _stake_risk_amount(stake),
    }
    if isinstance(stake, BaseAmountStake):
        bet_data["to_win"] = stake.to_win
        bet_data["base_amount"] = stake.base_amount
    if storage.save_bet(bet_data):
        logger.info("DB - Bet Saved")
    else:
        logger.warning("DB - Bet Not Saved")

    real_bets_chat = _real_bets_telegram_chat(telegram_config)

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
        if _dispatch_ops_alert(logger, leg_alert, real_bets_chat, label="Leg Confirmed Alert"):
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
            if _dispatch_ops_alert(logger, complete_alert, real_bets_chat, label="Arb Complete Alert"):
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
            f"Stake: {_format_stake_line(stake)}\n"
            f"Status: Confirmed by bookmaker\n"
        )
        betting_chat = _real_bets_telegram_chat(telegram_config) or telegram_config.get("betting")
        if TELEGRAM_ALERTS_ASYNC:
            threading.Thread(
                target=lambda: asyncio.run(send_telegram_alert(alert, betting_chat)),
                daemon=True,
            ).start()
        else:
            asyncio.run(send_telegram_alert(alert, betting_chat))
        cache.mark_bet_confirmed_alert_sent(bookmaker, bet_type, game_id)
