import asyncio
import math
import os
import re
import threading
import time

from utils.config import (
    REAL_MONEY_BETTING_ENABLED,
    SPREAD_REAL_MONEY_BETTING_ENABLED,
    BETAMAPOLA_REAL_MONEY_BETTING_ENABLED,
    SEQUENTIAL_ARB_BETTING,
    PARALLEL_EXCHANGE_ARB_BETTING,
    TELEGRAM_ALERTS_ASYNC,
    SECOND_LEG_ODDS_TOLERANCE,
    SPREAD_SECOND_LEG_ODDS_TOLERANCE,
    BET_STAKE,
    arb_pair_legs,
    required_first_leg_book,
)
from utils.arb_placement import SPREAD_BETTING_UNSUPPORTED_BOOKS, arb_leg_for_book
from utils.match_identity import validate_arb_same_match
from utils.moneyline_arb import (
    validate_cross_leg_moneyline_signs,
    validate_moneyline_arb_payload,
)
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
    format_arb_game_schedule,
    format_alert_ticket_line,
    normalize_spread_value,
    american_odds_to_int,
    BOOK_ALERT_LABELS,
)
from utils.hedge_stake import hedge_base_amount_from_first_leg
from utils.stake_sizing import BaseAmountStake, format_base_amount_stake, base_amount_stake_from_odds

EXCHANGE_FIRST_BOOKMAKERS = frozenset({"4casters"})

REAL_MONEY_BETTING_PAUSED_MSG = "Real money betting paused (REAL_MONEY_BETTING_ENABLED=false)"
BETAMAPOLA_REAL_MONEY_PAUSED_MSG = (
    "Betamapola real-money betting paused (BETAMAPOLA_REAL_MONEY_BETTING_ENABLED=false)"
)
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


def block_real_money_bet(
    logger,
    stake: float,
    bet_type: str = "moneyline",
    bookmaker: str | None = None,
):
    """Return (False, stake) when real-money betting is paused; else None."""
    bt = (bet_type or "moneyline").lower()
    bm = (bookmaker or "").strip().lower()
    if bm == "betamapola" and not BETAMAPOLA_REAL_MONEY_BETTING_ENABLED:
        logger.info(BETAMAPOLA_REAL_MONEY_PAUSED_MSG)
        return False, float(stake)
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
    if last_error.startswith("Betamapola real-money betting paused"):
        return False
    return not last_error.startswith("Spread real-money betting disabled")


def should_skip_arb_leg_in_betting_loop(
    cache,
    logger,
    arb: dict,
    bookmaker: str,
    team_name: str,
    team_1: str,
    team_2: str,
) -> bool:
    """Return True when the betting loop should not attempt this arb leg."""
    ml_reason = validate_moneyline_arb_payload(arb)
    if ml_reason:
        logger.info(f"Skipping — {ml_reason} | {team_name} | {team_1} vs {team_2}")
        cache.remove_arbitrage_for_bookmaker(arb, bookmaker)
        return True

    leg = arb_leg_for_book(arb, bookmaker)
    cross_reason = validate_cross_leg_moneyline_signs(cache, arb, bookmaker, leg)
    if cross_reason:
        logger.info(f"Skipping — {cross_reason} | {team_name} | {team_1} vs {team_2}")
        cache.remove_arbitrage_for_bookmaker(arb, bookmaker)
        return True

    match_reason = validate_arb_same_match(arb)
    if match_reason:
        logger.error(
            f"Skipping — same-match guard: {match_reason} | {bookmaker} | "
            f"{team_name} | {team_1} vs {team_2}"
        )
        cache.remove_arbitrage_for_bookmaker(arb, bookmaker)
        return True

    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    bet_type = arb.get("bet_type", "moneyline")
    if should_pause_for_arb_execution_cooldown(
        cache, arb, book_1, book_2, bookmaker, bet_type, logger
    ):
        return True

    skip, reason = cache.should_skip_arb_leg_placement(arb, bookmaker)
    if not skip:
        return False
    if reason == "leg already confirmed for this pair":
        logger.info(
            f"Skipping — leg already confirmed on {bookmaker} | "
            f"{team_name} | {team_1} vs {team_2}"
        )
        cache.remove_arbitrage_for_bookmaker(arb, bookmaker)
    elif reason == "pair/book bet cooldown active on this book":
        logger.info(
            f"Skipping — pair/book bet cooldown on {bookmaker} | "
            f"{team_name} | {team_1} vs {team_2}"
        )
        cache.remove_arbitrage_for_bookmaker(arb, bookmaker)
    elif reason.startswith("another pair owns this game"):
        logger.info(
            f"Skipping — {reason} | {team_name} | {team_1} vs {team_2}"
        )
        cache.remove_arbitrage_for_bookmaker(arb, bookmaker)
    else:
        logger.info(f"Skipping — {reason} | {team_name} | {team_1} vs {team_2}")
    return True


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


_API_RECEIPT_SCREENSHOT_BOOKS = frozenset({"3et", "paradisewager"})


def _dispatch_other_api_leg_screenshot(
    cache,
    logger,
    arb: dict,
    bookmaker: str,
    telegram_config: dict,
) -> None:
    """When the second leg completes, send the other API book's receipt if it was placed earlier."""
    from utils.bet_screenshot import bet_screenshot_path, render_bet_receipt
    from utils.helpers import format_arb_game_schedule

    bet_type = arb.get("bet_type", "moneyline")
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    other_book = book_2 if bookmaker == book_1 else book_1
    other_bm = (other_book or "").strip().lower()
    if other_bm not in _API_RECEIPT_SCREENSHOT_BOOKS:
        return

    other_game_id = (
        arb["team_2_game_id"] if bookmaker == arb["team_1_bookmaker"] else arb["team_1_game_id"]
    )
    if not cache.is_arb_leg_placed(arb, other_book):
        return

    leg = arb_leg_for_book(arb, other_book)
    if not leg:
        return

    pair_key = cache.arb_pair_key_from_arb(arb)
    summary = cache.redis.get(f"arb_real_bets_summary:{pair_key}") or {}
    side = "leg1" if other_bm == (arb.get("team_1_bookmaker") or "").strip().lower() else "leg2"
    leg_data = summary.get(side) or {}

    team_name = leg_data.get("team_name") or leg.get("team_name")
    odds = leg_data.get("odds") if leg_data.get("odds") is not None else leg.get("odds")
    risk = leg_data.get("risk")
    to_win = leg_data.get("to_win")
    if risk is not None and to_win is not None:
        stake = BaseAmountStake(
            base_amount=float(leg_data.get("base_amount") or BET_STAKE),
            american_odds=american_odds_to_int(odds),
            entry_field="risk",
            entry_amount=float(risk),
            risk=float(risk),
            to_win=float(to_win),
        )
    else:
        stake = base_amount_stake_from_odds(odds, BET_STAKE)

    spread_line = leg.get("spread_line") if bet_type == "spread" else None
    path = bet_screenshot_path(other_book, other_game_id)
    shot = render_bet_receipt(
        path,
        other_book,
        team_1=arb.get("team_1") or "",
        team_2=arb.get("team_2") or "",
        team_name=team_name,
        odds=odds,
        stake=stake,
        bet_type=bet_type,
        spread_line=spread_line,
        game_date=format_arb_game_schedule(arb),
        ticket_number=leg_data.get("ticket_number"),
        logger=logger,
    )
    if not shot:
        return

    screenshots_chat = _screenshots_telegram_chat(telegram_config)
    _dispatch_ops_alert(
        logger,
        "",
        screenshots_chat,
        label="Leg Screenshot (deferred)",
        photo_path=shot,
        photo_only=True,
    )


def _real_bets_telegram_chat(telegram_config: dict):
    """Single compact summary per arb (complete or failed)."""
    return telegram_config.get("real_bets")


def _screenshots_telegram_chat(telegram_config: dict):
    """Per-leg bet confirmation screenshots (photo only, no captions)."""
    return telegram_config.get("screenshots")


def _send_ops_alert(
    logger,
    alert: str,
    chat_id,
    label: str = "Alert",
    photo_path: str | None = None,
    photo_only: bool = False,
) -> bool:
    if not chat_id:
        logger.warning(f"Telegram chat not set — skipping {label}")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    if photo_only and photo_path and os.path.isfile(photo_path):
        asyncio.run(send_telegram_photo(photo_path, caption=None, chat_id=chat_id))
        return True
    asyncio.run(send_telegram_alert(alert, chat_id))
    if photo_path and os.path.isfile(photo_path):
        if photo_only:
            asyncio.run(send_telegram_photo(photo_path, caption=None, chat_id=chat_id))
        else:
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
    photo_only: bool = False,
) -> bool:
    if not chat_id:
        logger.warning(f"Telegram chat not set — skipping {label}")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    # Photo uploads must complete before the caller exits (async thread was dropping screenshots).
    if TELEGRAM_ALERTS_ASYNC and not photo_path:
        threading.Thread(
            target=_send_ops_alert,
            args=(logger, alert, chat_id, label, photo_path, photo_only),
            daemon=True,
        ).start()
        return True
    return _send_ops_alert(logger, alert, chat_id, label, photo_path, photo_only)


def is_first_leg_bookmaker(book_1: str, book_2: str, bookmaker: str) -> bool:
    legs = arb_pair_legs(book_1, book_2)
    if not legs:
        return False
    return legs[0] == (bookmaker or "").strip().lower()


def parallel_arb_betting_enabled(book_1: str, book_2: str) -> bool:
    """Both legs place immediately on exchange-first pairs (e.g. 4casters + S411)."""
    if SEQUENTIAL_ARB_BETTING:
        return False
    if not PARALLEL_EXCHANGE_ARB_BETTING:
        return False
    legs = arb_pair_legs(book_1, book_2)
    if not legs:
        return False
    first, _ = legs
    return first in EXCHANGE_FIRST_BOOKMAKERS


def sequential_arb_betting_enabled(book_1: str, book_2: str) -> bool:
    """Sequential legs when env flag is on or the configured first leg is an exchange."""
    if parallel_arb_betting_enabled(book_1, book_2):
        return False
    if SEQUENTIAL_ARB_BETTING:
        return True
    legs = arb_pair_legs(book_1, book_2)
    if not legs:
        return False
    first, _ = legs
    return first in EXCHANGE_FIRST_BOOKMAKERS


def should_s411_exchange_hedge_preposition(
    book_1: str, book_2: str, bookmaker: str, bet_type: str
) -> bool:
    """True when S411 should pre-open betslip while waiting for an exchange leg-1 fill."""
    from utils.config import S411_EXCHANGE_HEDGE_PREPOSITION

    if parallel_arb_betting_enabled(book_1, book_2):
        return False
    if not S411_EXCHANGE_HEDGE_PREPOSITION:
        return False
    if (bet_type or "moneyline").strip().lower() != "moneyline":
        return False
    if (bookmaker or "").strip().lower() != "sports411":
        return False
    if not sequential_arb_betting_enabled(book_1, book_2):
        return False
    if is_first_leg_bookmaker(book_1, book_2, bookmaker):
        return False
    first_leg = required_first_leg_book(book_1, book_2, bookmaker)
    return first_leg in EXCHANGE_FIRST_BOOKMAKERS


def _second_leg_bookmaker(book_1: str, book_2: str, first_leg_book: str) -> str:
    bm = (first_leg_book or "").strip().lower()
    b1 = (book_1 or "").strip().lower()
    return book_2 if bm == b1 else book_1


def should_wait_for_s411_hedge_preposition(arb: dict, bookmaker: str) -> bool:
    """True when exchange leg 1 must wait for S411 betslip pre-position (Phase 2)."""
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    bet_type = arb.get("bet_type", "moneyline")
    if not is_first_leg_bookmaker(book_1, book_2, bookmaker):
        return False
    other_book = _second_leg_bookmaker(book_1, book_2, bookmaker)
    return should_s411_exchange_hedge_preposition(
        book_1, book_2, other_book, bet_type
    )


def wait_for_s411_hedge_preposition(cache, logger, arb: dict, bookmaker: str) -> bool:
    """
    Block until S411 reports betslip pre-position ready, or timeout.
    Returns True when ready; False on timeout (caller may still place leg 1).
    """
    from utils.config import S411_HEDGE_PREPOSITION_WAIT_SECONDS

    if not should_wait_for_s411_hedge_preposition(arb, bookmaker):
        return False

    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    pair_key = cache.arb_pair_key_from_arb(arb)
    hedge_book = _second_leg_bookmaker(book_1, book_2, bookmaker)

    cache.clear_hedge_preposition_ready(pair_key)
    cache.signal_bet_wake(
        hedge_book,
        {
            "reason": "hedge_preposition",
            "pair_key": pair_key,
            "ts": time.time(),
        },
    )
    logger.info(
        f"Waiting for S411 hedge pre-position | {team_1} vs {team_2} | "
        f"timeout={S411_HEDGE_PREPOSITION_WAIT_SECONDS:.0f}s"
    )

    deadline = time.time() + S411_HEDGE_PREPOSITION_WAIT_SECONDS
    while time.time() < deadline:
        if cache.is_hedge_preposition_ready(pair_key):
            logger.info(
                f"S411 hedge pre-position ready | {team_1} vs {team_2}"
            )
            return True
        time.sleep(0.05)

    logger.warning(
        f"S411 hedge pre-position not ready within "
        f"{S411_HEDGE_PREPOSITION_WAIT_SECONDS:.0f}s; placing leg 1 anyway | "
        f"{team_1} vs {team_2}"
    )
    return False


def store_arbitrage_for_both_books(cache, arb_data: dict) -> None:
    """Write arb to both book caches; wake both books immediately."""
    bet_type = arb_data.get("bet_type", "moneyline")
    entries = [
        (arb_data["team_1_bookmaker"], arb_data["team_1_game_id"]),
        (arb_data["team_2_bookmaker"], arb_data["team_2_game_id"]),
    ]
    book_1 = arb_data.get("team_1_bookmaker")
    book_2 = arb_data.get("team_2_bookmaker")
    if (
        not parallel_arb_betting_enabled(book_1, book_2)
        and should_s411_exchange_hedge_preposition(
            book_1, book_2, "sports411", bet_type
        )
    ):
        entries.sort(
            key=lambda row: 0 if (row[0] or "").strip().lower() == "sports411" else 1
        )
    for bookmaker, game_id in entries:
        cache.add_arbitrage(bookmaker, bet_type, game_id, arb_data)


def _planned_first_leg_stake(arb: dict, first_leg_book: str, default_base: float):
    """Stake plan for the exchange leg from scan odds (parallel hedge sizing)."""
    from utils.stake_sizing import base_amount_stake_from_odds

    leg = arb_leg_for_book(arb, first_leg_book)
    if not leg:
        return None
    return base_amount_stake_from_odds(leg.get("odds"), default_base)


def _confirmed_other_leg_stake(cache, arb: dict, bookmaker: str) -> BaseAmountStake | None:
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    other_book = book_2 if bookmaker == book_1 else book_1
    if not cache.is_arb_leg_placed(arb, other_book):
        return None

    pair_key = cache.arb_pair_key_from_arb(arb)
    summary = cache.redis.get(f"arb_real_bets_summary:{pair_key}") or {}
    other_bm = (other_book or "").strip().lower()
    b1 = (book_1 or "").strip().lower()
    side = "leg1" if other_bm == b1 else "leg2"
    leg_data = summary.get(side) or {}

    risk = leg_data.get("risk")
    to_win = leg_data.get("to_win")
    if risk is None or to_win is None:
        return None
    try:
        odds_int = american_odds_to_int(leg_data.get("odds"))
        risk_f = float(risk)
        win_f = float(to_win)
        base = float(leg_data.get("base_amount") or (risk_f if odds_int > 0 else win_f))
        entry_field = "risk" if odds_int > 0 else "to_win"
        entry_amount = risk_f if odds_int > 0 else win_f
        return BaseAmountStake(
            base_amount=base,
            american_odds=odds_int,
            entry_field=entry_field,
            entry_amount=entry_amount,
            risk=risk_f,
            to_win=win_f,
        )
    except (TypeError, ValueError):
        return None


def _round_down_to_50(amount: float) -> float:
    """Floor to the nearest lower multiple of 50 (clean book-facing numbers)."""
    return float(math.floor(float(amount) / 50.0) * 50)


def _round_down_stake(amount: float) -> float:
    """Clean book-facing stake, always rounding DOWN so we never exceed liquidity.

    >= 50 → nearest lower multiple of 50 (e.g. 290→250).
    < 50  → nearest lower multiple of 10 (e.g. 48.23→40, 25.42→20), min 10.
    """
    amount = float(amount)
    if amount >= 50:
        return float(math.floor(amount / 50.0) * 50)
    chunk = float(math.floor(amount / 10.0) * 10)
    return chunk if chunk >= 10 else 10.0


def fourcasters_liquidity_capped_base(
    cache, arb: dict, default_base: float, logger=None
) -> float:
    """Cap the base stake so the 4casters leg's risk fits available liquidity.

    Uses the per-team max taker risk published from scan data (no extra API call).
    Only reduces when the 4casters bet size limit is below the desired stake;
    the reduced amount is floored to a clean multiple of 50 and shared by both legs.
    """
    b1 = (arb.get("team_1_bookmaker") or "").strip().lower()
    b2 = (arb.get("team_2_bookmaker") or "").strip().lower()
    if b1 == "4casters":
        game_id, side, odds = arb.get("team_1_game_id"), "team_1", arb.get("team_1_odds")
    elif b2 == "4casters":
        game_id, side, odds = arb.get("team_2_game_id"), "team_2", arb.get("team_2_odds")
    else:
        return default_base

    getter = getattr(cache, "get_fourcasters_max_risk", None)
    if getter is None:
        return default_base
    data = getter(game_id)
    if not data:
        return default_base
    max_risk = data.get(side)
    if max_risk is None:
        return default_base

    try:
        desired_risk = base_amount_stake_from_odds(odds, default_base).risk
    except Exception:
        return default_base
    if desired_risk <= 0:
        return default_base

    max_risk = float(max_risk)
    if desired_risk <= max_risk:
        return default_base

    # Scale the base down so the placed risk fits under available liquidity,
    # then floor to a clean book-facing chunk (50s ≥ 50, else 10s, min 10).
    risk_per_base = desired_risk / float(default_base)
    max_base = max_risk / risk_per_base if risk_per_base > 0 else default_base
    capped = _round_down_stake(max_base)
    if logger:
        logger.info(
            f"4casters liquidity cap | max_risk=${max_risk:.2f} < desired_risk=${desired_risk:.2f} "
            f"→ base ${float(default_base):.2f}→${capped:.2f} (chunks of 50)"
        )
    return capped


def resolve_arb_leg_stake(
    cache,
    arb: dict,
    book_1: str,
    book_2: str,
    bookmaker: str,
    wager_odds,
    default_base: float,
    *,
    logger=None,
) -> float:
    """Return base-amount stake for this leg (fill-linked on second leg when sequential)."""
    # Cap the shared base to 4casters available liquidity (same value for both legs).
    default_base = fourcasters_liquidity_capped_base(
        cache, arb, default_base, logger=logger
    )
    if parallel_arb_betting_enabled(book_1, book_2):
        if is_first_leg_bookmaker(book_1, book_2, bookmaker):
            return default_base
        first_leg = required_first_leg_book(book_1, book_2, bookmaker)
        if not first_leg:
            return default_base
        planned = _planned_first_leg_stake(arb, first_leg, default_base)
        if planned is None:
            return default_base
        hedged_base = hedge_base_amount_from_first_leg(
            planned.risk, planned.to_win, wager_odds
        )
        if logger:
            logger.info(
                f"Parallel hedge stake | planned leg1 risk=${planned.risk:.2f} "
                f"to-win=${planned.to_win:.2f} → leg2 base=${hedged_base:.2f} "
                f"@ {wager_odds}"
            )
        return hedged_base

    if not sequential_arb_betting_enabled(book_1, book_2):
        return default_base
    if is_first_leg_bookmaker(book_1, book_2, bookmaker):
        return default_base

    first_stake = _confirmed_other_leg_stake(cache, arb, bookmaker)
    if first_stake is None:
        return default_base

    hedged_base = hedge_base_amount_from_first_leg(
        first_stake.risk, first_stake.to_win, wager_odds
    )
    if logger:
        logger.info(
            f"Fill-linked hedge stake | leg1 risk=${first_stake.risk:.2f} "
            f"to-win=${first_stake.to_win:.2f} → leg2 base=${hedged_base:.2f} "
            f"@ {wager_odds}"
        )
    return hedged_base


def odds_tolerance_for_placement(
    cache, arb: dict, book_1: str, book_2: str, bookmaker: str, bet_type: str
) -> int:
    """±tolerance on second-leg books and when completing a hedge (other leg / partial exposure)."""
    bt = (bet_type or "moneyline").strip().lower()
    tolerance = (
        SPREAD_SECOND_LEG_ODDS_TOLERANCE
        if bt == "spread"
        else SECOND_LEG_ODDS_TOLERANCE
    )
    if tolerance <= 0:
        return 0
    bm = (bookmaker or "").strip().lower()
    if not is_first_leg_bookmaker(book_1, book_2, bm):
        return tolerance
    pair_key = cache.arb_pair_key_from_arb(arb)
    other_book = book_2 if bm == (book_1 or "").strip().lower() else book_1
    if cache.is_arb_leg_placed(arb, other_book):
        return tolerance
    if cache.has_partial_exposure_for_pair(pair_key):
        return tolerance
    return 0


def should_defer_for_sequential_first_leg(
    cache, arb: dict, book_1: str, book_2: str, bookmaker: str, bet_type: str
) -> bool:
    if not sequential_arb_betting_enabled(book_1, book_2):
        return False
    first_leg_book = required_first_leg_book(book_1, book_2, bookmaker)
    if not first_leg_book:
        return False
    return not cache.is_arb_leg_placed(arb, first_leg_book)


def should_pause_for_arb_execution_cooldown(
    cache,
    arb: dict,
    book_1: str,
    book_2: str,
    bookmaker: str,
    bet_type: str = "moneyline",
    logger=None,
) -> bool:
    """True when a global execution pause is active and this leg must wait."""
    if not cache.is_arb_execution_paused():
        return False
    if may_continue_arb_during_execution_pause(
        cache, arb, book_1, book_2, bookmaker, bet_type
    ):
        return False
    if logger:
        remaining = cache.arb_execution_pause_remaining_seconds()
        logger.info(
            f"Skipping — arb execution pause ({remaining:.0f}s left) | "
            f"{bookmaker} | {arb.get('team_1')} vs {arb.get('team_2')}"
        )
    return True


def may_continue_arb_during_execution_pause(
    cache,
    arb: dict,
    book_1: str,
    book_2: str,
    bookmaker: str,
    bet_type: str = "moneyline",
) -> bool:
    """Allow the in-flight arb's second leg while blocking new first legs."""
    if parallel_arb_betting_enabled(book_1, book_2):
        pair_key = cache.arb_pair_key_from_arb(arb)
        pause_meta = cache.get_arb_execution_pause_meta() or {}
        if pause_meta.get("pair_key") == pair_key:
            return True
        return False
    if not sequential_arb_betting_enabled(book_1, book_2):
        return False
    first_leg = required_first_leg_book(book_1, book_2, bookmaker)
    if not first_leg:
        return False
    bm = (bookmaker or "").strip().lower()
    if bm == (first_leg or "").strip().lower():
        return False
    if cache.is_arb_leg_placed(arb, first_leg):
        return True
    pair_key = cache.arb_pair_key_from_arb(arb)
    if cache.has_partial_exposure_for_pair(pair_key):
        return True
    pause_meta = cache.get_arb_execution_pause_meta() or {}
    if pause_meta.get("pair_key") == pair_key:
        return True
    return False


def mark_arb_execution_pause_if_first_leg(
    cache,
    arb: dict,
    book_1: str,
    book_2: str,
    bookmaker: str,
    logger=None,
) -> None:
    """Start the global execution pause when committing to place leg 1."""
    mark_arb_execution_pause_on_placement_start(
        cache, arb, book_1, book_2, bookmaker, logger
    )


def mark_arb_execution_pause_on_placement_start(
    cache,
    arb: dict,
    book_1: str,
    book_2: str,
    bookmaker: str,
    logger=None,
) -> None:
    """Start execution pause when an arb begins placing (leg 1 sequential, or either leg parallel)."""
    if parallel_arb_betting_enabled(book_1, book_2):
        if cache.is_arb_execution_paused():
            return
    elif not is_first_leg_bookmaker(book_1, book_2, bookmaker):
        return
    if cache.is_arb_execution_paused():
        return
    from utils.config import ARB_EXECUTION_PAUSE_SECONDS

    cache.mark_arb_execution_pause(arb)
    if logger:
        mode = "parallel" if parallel_arb_betting_enabled(book_1, book_2) else "sequential"
        logger.info(
            f"Arb execution pause started ({ARB_EXECUTION_PAUSE_SECONDS}s, {mode}) — "
            f"blocking new arbs | {arb.get('team_1')} vs {arb.get('team_2')}"
        )


def wait_for_arb_execution_pause_clear(
    cache,
    logger=None,
    *,
    poll_seconds: float = 1.0,
    component: str = "Arb scanner",
) -> bool:
    """Block until the global execution pause clears. Returns True if we waited."""
    if not cache.is_arb_execution_paused():
        return False

    remaining = cache.arb_execution_pause_remaining_seconds()
    meta = cache.get_arb_execution_pause_meta() or {}
    match = meta.get("match") or "in-flight arb"
    if logger:
        logger.info(
            f"{component} paused — execution pause active ({remaining:.0f}s left) | "
            f"{match}"
        )

    while cache.is_arb_execution_paused():
        time.sleep(poll_seconds)

    if logger:
        logger.info(f"{component} resuming — execution pause cleared")
    return True


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

    pair_key = cache.arb_pair_key_from_arb(arb)
    bm = (bookmaker or "").strip().lower()
    b1 = (book_1 or "").strip().lower()
    other_book = book_2 if bm == b1 else book_1
    other_leg_placed = cache.is_arb_leg_placed(arb, other_book)

    if cache.is_arb_leg_placed(arb, bookmaker) and not other_leg_placed:
        return True
    if cache.has_partial_exposure_for_pair(pair_key) and not other_leg_placed:
        return True
    if cache.has_other_pair_partial_on_book_game(arb, bookmaker):
        return True
    return False


def _build_leg_confirmed_alert(
    arb: dict,
    bookmaker: str,
    team_no: int,
    team_name: str,
    stake: float,
    moneyline_odd,
    other_book: str,
    other_leg_placed: bool,
    *,
    ticket_number=None,
    orderbook_max_risk: float | None = None,
) -> str:
    identified_at = arb.get("identified_at")
    sport = arb.get("sport")
    league = arb.get("league")
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    bet_type = arb.get("bet_type", "moneyline")
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    book_label = _book_alert_label(bookmaker)
    other_label = _book_alert_label(other_book)
    pair = f"{_book_alert_label(book_1)} × {_book_alert_label(book_2)}"
    bet_line = _format_placed_bet_line(arb, team_name, team_no, moneyline_odd)
    game_schedule = format_arb_game_schedule(arb)
    ticket_line = format_alert_ticket_line(ticket_number)

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

    lines = [
        header,
        "",
        f"{team_1} vs {team_2}",
        f"Date: {game_schedule}",
        "",
        f"Book: {book_label} · leg {leg_no}/2 · {pair}",
        f"Market: {market}",
        f"Bet: {bet_line}",
        f"Stake: {_format_real_money_stake(stake)}",
    ]
    if orderbook_max_risk is not None and (bookmaker or "").strip().lower() == "4casters":
        try:
            lines.append(f"4c max size: ${float(orderbook_max_risk):.2f}")
        except (TypeError, ValueError):
            pass
    if ticket_line:
        lines.append(ticket_line)
    lines.extend([
        "",
        f"Arb spotted: {format_utc_timestamp(identified_at)}",
        f"Status: {status}",
        "",
    ])
    return "\n".join(lines)


def _build_repeat_leg1_exposure_alert(
    arb: dict,
    bookmaker: str,
    team_no: int,
    team_name: str,
    stake: float,
    moneyline_odd,
    other_book: str,
    *,
    ticket_number=None,
    orderbook_max_risk: float | None = None,
) -> str:
    """Warn when a second leg-1 fill lands on the same game (added unhedged exposure)."""
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    bet_type = arb.get("bet_type", "moneyline")
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    book_label = _book_alert_label(bookmaker)
    other_label = _book_alert_label(other_book)
    pair = f"{_book_alert_label(book_1)} × {_book_alert_label(book_2)}"
    bet_line = _format_placed_bet_line(arb, team_name, team_no, moneyline_odd)
    game_schedule = format_arb_game_schedule(arb)
    ticket_line = format_alert_ticket_line(ticket_number)

    market = bet_type
    if bet_type == "spread":
        market = f"spread ({arb.get('spread_value')})"

    lines = [
        "===== WARNING — Duplicate Leg 1 (Added Exposure) =====",
        "",
        f"{team_1} vs {team_2}",
        f"Date: {game_schedule}",
        "",
        f"Book: {book_label} · leg 1/2 · {pair}",
        f"Market: {market}",
        f"Bet: {bet_line}",
        f"Stake: {_format_real_money_stake(stake)}",
        "⚠️ Another leg-1 fill on this game — hedge still incomplete.",
        f"Still waiting for leg 2 on {other_label}",
    ]
    if orderbook_max_risk is not None:
        try:
            lines.append(f"Max Bet: ${float(orderbook_max_risk):.2f}")
        except (TypeError, ValueError):
            pass
    if ticket_line:
        lines.append(ticket_line)
    lines.extend([
        "",
        f"Arb spotted: {format_utc_timestamp(arb.get('identified_at'))}",
        "",
    ])
    return "\n".join(lines)


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
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    pair_key = cache.arb_pair_key_from_arb(arb)

    if cache.partial_arb_alert_already_sent(pair_key):
        return

    other_book = book_2 if failed_bookmaker == book_1 else book_1

    if not cache.is_arb_leg_placed(arb, other_book):
        return
    if cache.is_arb_leg_placed(arb, failed_bookmaker):
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
    ticket_number=None,
) -> str | None:
    from utils.bet_screenshot import capture_confirmed_bet_screenshot
    from utils.helpers import format_arb_game_schedule

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
        game_date=format_arb_game_schedule(arb),
        ticket_number=ticket_number,
        driver=driver,
        open_bets_url=open_bets_url,
        return_to_sport=return_to_sport,
        extra_lines=extra_lines,
        logger=logger,
    )


def acknowledge_placed_leg(
    cache,
    logger,
    arb: dict,
    bookmaker: str,
    game_id: str,
    *,
    team_name: str | None = None,
) -> None:
    """Mark leg + locks immediately after book acceptance, before slow screenshot work."""
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    bet_type = arb.get("bet_type", "moneyline")
    book_1 = arb.get("team_1_bookmaker")
    book_2 = arb.get("team_2_bookmaker")
    event_date = cache.event_date_for_arb(arb)
    spread_value = arb.get("spread_value") if bet_type == "spread" else None

    cache.mark_arb_leg_placed(arb, bookmaker, game_id)
    cache.lock_arb_scan(
        team_1, team_2, book_1, book_2, event_date,
        bet_type=bet_type, spread_value=spread_value,
    )
    cache.mark_game_pair_daily_bet(arb, bookmaker, game_id)

    if sequential_arb_betting_enabled(book_1, book_2) and is_first_leg_bookmaker(
        book_1, book_2, bookmaker
    ):
        bm = (bookmaker or "").strip().lower()
        b1 = (book_1 or "").strip().lower()
        other_book = book_2 if bm == b1 else book_1
        if other_book:
            cache.signal_bet_wake(
                other_book,
                {
                    "reason": "leg1_acknowledged",
                    "pair_key": cache.arb_pair_key_from_arb(arb),
                    "ts": time.time(),
                },
            )

    logger.info(
        f"Leg acknowledged (pre-screenshot) | {bookmaker}/{team_name or 'team'} | "
        f"{team_1} vs {team_2}"
    )


def finalize_confirmed_bet_with_screenshot(
    cache,
    storage,
    logger,
    arb: dict,
    bookmaker: str,
    team_no: int,
    team_name: str,
    game_id: str,
    stake,
    moneyline_odd,
    telegram_config: dict,
    *,
    driver=None,
    open_bets_url: str | None = None,
    return_to_sport=None,
    extra_lines: list[str] | None = None,
    ticket_number=None,
    placed_odds=None,
    leg_already_acknowledged: bool = False,
    orderbook_max_risk: float | None = None,
    async_screenshot: bool = False,
    screenshot_lock=None,
    driver_factory=None,
) -> None:
    """Acknowledge leg immediately, then capture screenshot + finish alerts/DB.

    When async_screenshot is True the leg is acknowledged synchronously (fast,
    ~API-ack latency) and the slow screenshot + alert/DB finalize runs in a
    background thread so the betting loop isn't blocked. Pass driver_factory
    (instead of driver) to also resolve/spin-up the screenshot browser off the
    critical path.
    """
    pair_key = cache.arb_pair_key_from_arb(arb)
    odds_for_record = placed_odds if placed_odds is not None else moneyline_odd

    if not leg_already_acknowledged:
        acknowledge_placed_leg(
            cache, logger, arb, bookmaker, game_id, team_name=team_name
        )
        record_confirmed_leg(
            cache,
            pair_key,
            arb,
            bookmaker,
            team_no,
            team_name,
            odds_for_record,
            stake,
            ticket_number=ticket_number,
            orderbook_max_risk=orderbook_max_risk,
        )

    def _screenshot_then_finalize():
        screenshot_path = None
        try:
            if screenshot_lock is not None:
                screenshot_lock.acquire()
            try:
                shot_driver = driver
                if shot_driver is None and driver_factory is not None:
                    shot_driver = driver_factory()
                screenshot_path = capture_bet_screenshot_for_alert(
                    logger,
                    bookmaker,
                    arb,
                    team_name,
                    game_id,
                    stake,
                    placed_odds if placed_odds is not None else moneyline_odd,
                    driver=shot_driver,
                    open_bets_url=open_bets_url,
                    return_to_sport=return_to_sport,
                    extra_lines=extra_lines,
                    ticket_number=ticket_number,
                )
            finally:
                if screenshot_lock is not None:
                    screenshot_lock.release()
        except Exception as exc:
            logger.warning(f"Bet screenshot failed (continuing to finalize): {exc}")

        try:
            finalize_confirmed_bet(
                cache,
                storage,
                logger,
                arb,
                bookmaker,
                team_no,
                team_name,
                game_id,
                stake,
                moneyline_odd,
                telegram_config,
                screenshot_path=screenshot_path,
                ticket_number=ticket_number,
                placed_odds=placed_odds,
                leg_already_acknowledged=True,
                orderbook_max_risk=orderbook_max_risk,
            )
        except Exception as exc:
            logger.error(f"Bet finalize failed: {exc}", exc_info=True)

    if async_screenshot:
        threading.Thread(target=_screenshot_then_finalize, daemon=True).start()
        return
    _screenshot_then_finalize()


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
    *,
    ticket_number=None,
    placed_odds=None,
    leg_already_acknowledged: bool = False,
    orderbook_max_risk: float | None = None,
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
    pair_key = cache.arb_pair_key_from_arb(arb)
    event_date = cache.event_date_for_arb(arb)
    spread_value = arb.get("spread_value") if bet_type == "spread" else None
    odds_for_record = placed_odds if placed_odds is not None else moneyline_odd

    if not leg_already_acknowledged:
        cache.mark_arb_leg_placed(arb, bookmaker, game_id)
        cache.lock_arb_scan(
            team_1, team_2, book_1, book_2, event_date,
            bet_type=bet_type, spread_value=spread_value,
        )
        cache.mark_game_pair_daily_bet(arb, bookmaker, game_id)

    other_book = book_2 if bookmaker == book_1 else book_1
    other_leg_placed = cache.is_arb_leg_placed(arb, other_book)

    if other_leg_placed:
        cache.remove_arbitrage_pair(arb)
        cache.clear_partial_exposure(pair_key)
        cache.clear_arb_pair_legs(arb)
        cache.clear_game_event_owner(arb)
        logger.info(
            f"Leg confirmed | {bookmaker}/{team_name} | {team_1} vs {team_2} | "
            f"both legs confirmed; arb scan locked and cache cleared"
        )
    else:
        cache.mark_partial_exposure(
            pair_key,
            game_datetime=arb.get("game_datetime") or game_date,
            arb=arb,
        )
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
        "odds": odds_for_record,
        "stake": _stake_risk_amount(stake),
    }
    if isinstance(stake, BaseAmountStake):
        bet_data["to_win"] = stake.to_win
        bet_data["base_amount"] = stake.base_amount
    if orderbook_max_risk is not None:
        bet_data["orderbook_max_risk"] = orderbook_max_risk
    if storage.save_bet(bet_data):
        logger.info("DB - Bet Saved")
    else:
        logger.warning("DB - Bet Not Saved")

    real_bets_chat = _real_bets_telegram_chat(telegram_config)
    screenshots_chat = _screenshots_telegram_chat(telegram_config)

    record_confirmed_leg(
        cache,
        pair_key,
        arb,
        bookmaker,
        team_no,
        team_name,
        odds_for_record,
        stake,
        ticket_number=ticket_number,
        orderbook_max_risk=orderbook_max_risk,
    )

    if not cache.bet_confirmed_alert_already_sent(bookmaker, bet_type, game_id):
        leg_alert = _build_leg_confirmed_alert(
            arb,
            bookmaker,
            team_no,
            team_name,
            stake,
            odds_for_record,
            other_book,
            other_leg_placed,
            ticket_number=ticket_number,
            orderbook_max_risk=orderbook_max_risk,
        )
        if real_bets_chat and _dispatch_ops_alert(
            logger,
            leg_alert,
            real_bets_chat,
            label="Leg Confirmed Alert",
        ):
            cache.mark_bet_confirmed_alert_sent(bookmaker, bet_type, game_id)
    else:
        if other_leg_placed:
            logger.info(
                f"Skipping duplicate leg-confirmed Telegram alert - {bookmaker}/{team_name} | "
                f"{team_1} vs {team_2} | game_id={game_id}"
            )
        else:
            repeat_alert = _build_repeat_leg1_exposure_alert(
                arb,
                bookmaker,
                team_no,
                team_name,
                stake,
                odds_for_record,
                other_book,
                ticket_number=ticket_number,
                orderbook_max_risk=orderbook_max_risk,
            )
            if real_bets_chat and _dispatch_ops_alert(
                logger,
                repeat_alert,
                real_bets_chat,
                label="Duplicate Leg 1 Exposure Alert",
            ):
                logger.info(
                    f"Sent duplicate leg-1 exposure warning - {bookmaker}/{team_name} | "
                    f"{team_1} vs {team_2} | game_id={game_id}"
                )
            else:
                logger.info(
                    f"Skipping duplicate leg-confirmed Telegram alert - {bookmaker}/{team_name} | "
                    f"{team_1} vs {team_2} | game_id={game_id}"
                )

    if screenshot_path and os.path.isfile(screenshot_path) and screenshots_chat:
        _dispatch_ops_alert(
            logger,
            "",
            screenshots_chat,
            label="Leg Screenshot",
            photo_path=screenshot_path,
            photo_only=True,
        )

    if other_leg_placed:
        _dispatch_other_api_leg_screenshot(
            cache, logger, arb, bookmaker, telegram_config
        )
        schedule_complete_summary(cache, logger, arb, telegram_config)
        return

    if not sequential_arb_betting_enabled(book_1, book_2):
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
