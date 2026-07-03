import asyncio
import os
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
from utils.arb_real_bets_summary import (
    record_confirmed_leg,
    schedule_complete_summary,
    schedule_failed_summary,
)
from utils.helpers import (
    send_telegram_alert,
    send_telegram_photo,
    format_utc_timestamp,
    format_arb_complete_alert,
    format_american_alert_odds,
    normalize_spread_value,
    BOOK_ALERT_LABELS,
)
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


def _format_real_money_stake(stake) -> str:
    """Human-readable stake for real-bets Telegram (risk + to-win)."""
    if isinstance(stake, BaseAmountStake):
        return (
            f"${stake.risk:.2f} risk · ${stake.to_win:.2f} to-win "
            f"(base ${stake.base_amount:.2f} @ {stake.american_odds:+d})"
        )
    try:
        return f"${float(stake):.2f} risk"
    except (TypeError, ValueError):
        return str(stake)


def _format_placed_bet_line(arb: dict, team_name: str, team_no: int, odds) -> str:
    """Single-leg bet description for an isolated real-money alert."""
    bet_type = arb.get("bet_type", "moneyline")
    odds_str = format_american_alert_odds(odds)
    if bet_type != "spread":
        return f"{team_name} {odds_str}"

    if team_no == 1:
        line = normalize_spread_value(arb.get("spread_line_team_1"))
        if line is None:
            line = normalize_spread_value(arb.get("spread_value"))
    else:
        line = normalize_spread_value(arb.get("spread_line_team_2"))
        if line is None:
            spread_value = normalize_spread_value(arb.get("spread_value"))
            line = -spread_value if spread_value is not None else None
    if line is not None:
        return f"{team_name} {line:+.1f} {odds_str}"
    return f"{team_name} {odds_str}"


def _book_alert_label(bookmaker: str) -> str:
    return BOOK_ALERT_LABELS.get(bookmaker, bookmaker or "book")


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
    """Single compact summary per arb (complete or failed)."""
    return telegram_config.get("real_bets")


def _screenshots_telegram_chat(telegram_config: dict):
    """Per-leg bet confirmations and screenshots."""
    return telegram_config.get("screenshots") or telegram_config.get("real_bets")


def _send_ops_alert(
    logger,
    alert: str,
    chat_id,
    label: str = "Alert",
    photo_path: str | None = None,
) -> bool:
    if not chat_id:
        logger.warning("Telegram screenshots chat not set — skipping leg alert")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    asyncio.run(send_telegram_alert(alert, chat_id))
    if photo_path and os.path.isfile(photo_path):
        caption_lines = [
            ln.strip()
            for ln in alert.splitlines()
            if ln.strip() and not ln.strip().startswith("=====")
        ]
        photo_caption = "\n".join(caption_lines[:4])[:1024] if caption_lines else None
        asyncio.run(send_telegram_photo(photo_path, caption=photo_caption, chat_id=chat_id))
    return True


def _dispatch_ops_alert(
    logger,
    alert: str,
    chat_id,
    label: str = "Alert",
    photo_path: str | None = None,
) -> bool:
    if not chat_id:
        logger.warning("Telegram screenshots chat not set — skipping leg alert")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    if TELEGRAM_ALERTS_ASYNC:
        threading.Thread(
            target=_send_ops_alert,
            args=(logger, alert, chat_id, label, photo_path),
            daemon=True,
        ).start()
        return True
    return _send_ops_alert(logger, alert, chat_id, label, photo_path)


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
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    book_label = _book_alert_label(bookmaker)
    other_label = _book_alert_label(other_book)
    pair = f"{_book_alert_label(book_1)} × {_book_alert_label(book_2)}"
    bet_line = _format_placed_bet_line(arb, team_name, team_no, moneyline_odd)

    if other_leg_placed:
        leg_no = 2
        header = "===== Bet Placed (Real Money) — Leg 2 of 2 ====="
        status = "Both legs confirmed — summary will post to Real Bets shortly"
    else:
        leg_no = 1
        header = "===== Bet Placed (Real Money) — Leg 1 of 2 ====="
        status = f"Waiting for leg 2 on {other_label}"

    market = bet_type
    if bet_type == "spread":
        market = f"spread ({arb.get('spread_value')})"

    return (
        f"{header}\n\n"
        f"Book: {book_label}\n"
        f"Leg: {leg_no} of 2 in {pair}\n"
        f"Identified At: {format_utc_timestamp(identified_at)}\n"
        f"Sport: {sport} · {league}\n"
        f"Date: {game_date}\n"
        f"Match: {team_1} vs {team_2}\n"
        f"Market: {market}\n\n"
        f"This bet: {bet_line}\n"
        f"Real money: {_format_real_money_stake(stake)}\n\n"
        f"Screenshot: attached below\n"
        f"Status: {status}\n"
    )


def _build_arb_complete_alert(arb: dict, stake) -> str:
    base_amount = stake.base_amount if isinstance(stake, BaseAmountStake) else None
    body = format_arb_complete_alert(
        arb,
        base_amount=base_amount,
        spread_value=arb.get("spread_value"),
    )
    return f"===== Arb Complete (Real Money) =====\n\n{body}"


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

    reason_text = format_bet_failure_reason(reason, failed_book)
    schedule_failed_summary(
        cache,
        logger,
        arb,
        telegram_config,
        failed_bookmaker,
        reason_text,
    )
    cache.mark_partial_arb_alert_sent(pair_key)


def capture_bet_screenshot_for_alert(
    logger,
    bookmaker: str,
    arb: dict,
    team_name: str,
    game_id: str,
    stake,
    odds,
    *,
    driver=None,
    open_bets_url: str | None = None,
    return_to_sport=None,
    extra_lines: list[str] | None = None,
) -> str | None:
    from utils.bet_screenshot import capture_confirmed_bet_screenshot

    bet_type = arb.get("bet_type", "moneyline")
    spread_line = arb.get("spread_value") if bet_type == "spread" else None
    return capture_confirmed_bet_screenshot(
        bookmaker=bookmaker,
        game_id=game_id,
        team_name=team_name,
        team_1=arb.get("team_1") or "",
        team_2=arb.get("team_2") or "",
        odds=odds,
        stake=stake,
        bet_type=bet_type,
        spread_line=spread_line,
        driver=driver,
        open_bets_url=open_bets_url,
        return_to_sport=return_to_sport,
        extra_lines=extra_lines,
        logger=logger,
    )


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
    screenshot_path: str | None = None,
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
    screenshots_chat = _screenshots_telegram_chat(telegram_config)

    record_confirmed_leg(
        cache, pair_key, arb, bookmaker, team_no, team_name, moneyline_odd, stake
    )

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
        if _dispatch_ops_alert(
            logger,
            leg_alert,
            screenshots_chat,
            label="Leg Confirmed Alert",
            photo_path=screenshot_path,
        ):
            cache.mark_bet_confirmed_alert_sent(bookmaker, bet_type, game_id)
    else:
        logger.info(
            f"Skipping duplicate leg-confirmed Telegram alert - {bookmaker}/{team_name} | "
            f"{team_1} vs {team_2} | game_id={game_id}"
        )

    if other_leg_placed:
        schedule_complete_summary(cache, logger, arb, telegram_config)
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
