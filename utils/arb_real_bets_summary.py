"""Deferred single-message Real Bets summaries (complete or failed) per arb pair."""
from __future__ import annotations

import asyncio
import threading

from utils.config import (
    REAL_BETS_FAILED_SUMMARY_DELAY_SEC,
    REAL_BETS_SUMMARY_DELAY_SEC,
    TELEGRAM_ALERTS_ASYNC,
)
from utils.helpers import format_arb_complete_alert, send_telegram_alert
from utils.stake_sizing import BaseAmountStake

_timer_lock = threading.Lock()
_timers: dict[str, threading.Timer] = {}


def _summary_redis_key(pair_key: str) -> str:
    return f"arb_real_bets_summary:{pair_key}"


def _stake_tuple(stake) -> tuple[float, float] | None:
    if isinstance(stake, BaseAmountStake):
        return (float(stake.risk), float(stake.to_win))
    try:
        risk = float(stake)
    except (TypeError, ValueError):
        return None
    return (risk, risk)


def _leg_side(bookmaker: str, arb: dict) -> str:
    bm = (bookmaker or "").strip().lower()
    if bm == (arb.get("team_1_bookmaker") or "").strip().lower():
        return "leg1"
    return "leg2"


def record_confirmed_leg(
    cache,
    pair_key: str,
    arb: dict,
    bookmaker: str,
    team_no: int,
    team_name: str,
    odds,
    stake,
) -> None:
    _store_arb_snapshot(cache, pair_key, arb)
    data = cache.redis.get(_summary_redis_key(pair_key)) or {}
    side = _leg_side(bookmaker, arb)
    stake_pair = _stake_tuple(stake)
    data[side] = {
        "bookmaker": bookmaker,
        "team_no": team_no,
        "team_name": team_name,
        "odds": odds,
        "placed": True,
        "risk": stake_pair[0] if stake_pair else None,
        "to_win": stake_pair[1] if stake_pair else None,
    }
    cache.redis.set(_summary_redis_key(pair_key), data, ttl=cache.lock_ttl)


def record_failed_leg(cache, pair_key: str, arb: dict, bookmaker: str, reason: str) -> None:
    _store_arb_snapshot(cache, pair_key, arb)
    data = cache.redis.get(_summary_redis_key(pair_key)) or {}
    side = _leg_side(bookmaker, arb)
    data[side] = {
        "bookmaker": bookmaker,
        "placed": False,
        "failure": reason,
    }
    cache.redis.set(_summary_redis_key(pair_key), data, ttl=cache.lock_ttl)


def _store_arb_snapshot(cache, pair_key: str, arb: dict) -> None:
    data = cache.redis.get(_summary_redis_key(pair_key)) or {}
    if not data.get("arb"):
        snapshot = {k: arb.get(k) for k in (
            "sport", "league", "game_date", "team_1", "team_2",
            "team_1_bookmaker", "team_2_bookmaker", "team_1_odds", "team_2_odds",
            "team_1_game_id", "team_2_game_id", "bet_type", "profit_pct",
            "spread_value", "spread_line_team_1", "spread_line_team_2",
            "identified_at",
        )}
        data["arb"] = snapshot
        cache.redis.set(_summary_redis_key(pair_key), data, ttl=cache.lock_ttl)


def _cancel_timer(pair_key: str) -> None:
    with _timer_lock:
        timer = _timers.pop(pair_key, None)
    if timer:
        timer.cancel()


def _schedule_publish(
    pair_key: str,
    delay: float,
    callback,
) -> None:
    _cancel_timer(pair_key)
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    with _timer_lock:
        _timers[pair_key] = timer
    timer.start()


def _build_summary_alert(cache, pair_key: str, outcome: str) -> str | None:
    data = cache.redis.get(_summary_redis_key(pair_key)) or {}
    arb = data.get("arb")
    if not arb:
        return None

    leg1 = data.get("leg1") or {}
    leg2 = data.get("leg2") or {}

    def _leg_stake(side: dict) -> tuple[float, float] | None:
        if not side.get("placed"):
            return None
        risk = side.get("risk")
        to_win = side.get("to_win")
        if risk is None or to_win is None:
            return None
        return (float(risk), float(to_win))

    def _leg_failure(side: dict) -> str | None:
        if side.get("placed"):
            return None
        return side.get("failure") or "not placed"

    body = format_arb_complete_alert(
        arb,
        spread_value=arb.get("spread_value"),
        outcome=outcome,
        leg1_stake=_leg_stake(leg1),
        leg2_stake=_leg_stake(leg2),
        leg1_failure=_leg_failure(leg1),
        leg2_failure=_leg_failure(leg2),
    )
    if outcome == "complete":
        header = "===== Arb Complete (Real Money) ====="
    else:
        header = "===== Arb Failed (Real Money) ====="
    return f"{header}\n\n{body}"


def _send_real_bets_summary(logger, alert: str, chat_id, label: str) -> bool:
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_REAL_BETS not set — skipping Real Bets summary")
        return False
    logger.info(f"========== {label} ==========")
    logger.info(alert)
    logger.info(f"========== {label} ==========")
    try:
        asyncio.run(send_telegram_alert(alert, chat_id))
    except Exception:
        logger.exception(f"Failed to send Real Bets summary to Telegram | label={label}")
        return False
    return True


def _dispatch_real_bets_summary(
    logger,
    alert: str,
    chat_id,
    label: str,
    on_success=None,
) -> bool:
    def _run():
        if _send_real_bets_summary(logger, alert, chat_id, label) and on_success:
            on_success()

    if TELEGRAM_ALERTS_ASYNC:
        threading.Thread(target=_run, daemon=True).start()
        return True
    if not _send_real_bets_summary(logger, alert, chat_id, label):
        return False
    if on_success:
        on_success()
    return True


def _summary_outcome(data: dict) -> str:
    leg1 = data.get("leg1") or {}
    leg2 = data.get("leg2") or {}
    if leg1.get("placed") and leg2.get("placed"):
        return "complete"
    return "failed"


def _cleanup_legs_after_summary(cache, pair_key: str, outcome: str, data: dict) -> None:
    """Drop pair-scoped leg flags once a Real Bets summary is final."""
    arb = data.get("arb")
    leg1 = data.get("leg1") or {}
    leg2 = data.get("leg2") or {}

    if outcome == "complete":
        if arb:
            cache.clear_arb_pair_legs(arb)
        else:
            cache.clear_arb_legs_for_pair_key(pair_key)
        return

    if not arb:
        cache.clear_arb_legs_for_pair_key(pair_key)
        return

    if not leg1.get("placed"):
        cache.clear_arb_leg_placed(arb, arb.get("team_1_bookmaker"))
    if not leg2.get("placed"):
        cache.clear_arb_leg_placed(arb, arb.get("team_2_bookmaker"))


def _publish_summary(cache, logger, pair_key: str, outcome: str, telegram_config: dict) -> None:
    logger.info(f"Publishing Real Bets summary | pair_key={pair_key} outcome={outcome}")

    if cache.real_bets_summary_already_sent(pair_key):
        logger.info(
            f"Skipping duplicate Real Bets summary | pair_key={pair_key} outcome={outcome}"
        )
        return

    summary_data = cache.redis.get(_summary_redis_key(pair_key)) or {}
    alert = _build_summary_alert(cache, pair_key, outcome)
    if not alert:
        logger.warning(f"No Real Bets summary data for pair_key={pair_key}")
        return

    chat_id = telegram_config.get("real_bets")
    label = "Arb Complete Alert" if outcome == "complete" else "Arb Failed Alert"

    def _mark_sent():
        cache.mark_real_bets_summary_sent(pair_key)
        _cleanup_legs_after_summary(cache, pair_key, outcome, summary_data)
        cache.redis.delete(_summary_redis_key(pair_key))
        logger.info(f"Real Bets summary sent | pair_key={pair_key} outcome={outcome}")

    if not _dispatch_real_bets_summary(logger, alert, chat_id, label, on_success=_mark_sent):
        return


def schedule_complete_summary(cache, logger, arb: dict, telegram_config: dict) -> None:
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    pair_key = cache.arb_pair_key_from_arb(arb)

    _store_arb_snapshot(cache, pair_key, arb)
    delay = max(0.0, float(REAL_BETS_SUMMARY_DELAY_SEC))

    def _run():
        data = cache.redis.get(_summary_redis_key(pair_key)) or {}
        outcome = _summary_outcome(data)
        _publish_summary(cache, logger, pair_key, outcome, telegram_config)

    logger.info(
        f"Scheduling Real Bets complete summary in {delay:.0f}s | {team_1} vs {team_2}"
    )
    _schedule_publish(pair_key, delay, _run)


def schedule_failed_summary(
    cache,
    logger,
    arb: dict,
    telegram_config: dict,
    failed_bookmaker: str,
    reason: str,
) -> None:
    team_1 = arb.get("team_1")
    team_2 = arb.get("team_2")
    pair_key = cache.arb_pair_key_from_arb(arb)

    _store_arb_snapshot(cache, pair_key, arb)
    record_failed_leg(cache, pair_key, arb, failed_bookmaker, reason)

    delay = max(0.0, float(REAL_BETS_FAILED_SUMMARY_DELAY_SEC))

    def _run():
        _publish_summary(cache, logger, pair_key, "failed", telegram_config)

    logger.info(
        f"Scheduling Real Bets failed summary in {delay:.0f}s | {team_1} vs {team_2}"
    )
    _schedule_publish(pair_key, delay, _run)
