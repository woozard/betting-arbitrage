"""Inline cross-book arb scan on odds updates (Redis-only, wakes betting loops immediately)."""

from __future__ import annotations

import asyncio
import threading
import time
from decimal import Decimal

from utils.config import (
    TELEGRAM_ALERTS_ASYNC,
    SPREAD_ARB_MAX_PROFIT_PCT,
    SPREAD_ARB_SCAN_ENABLED,
    SPREAD_ODDS_MAX_AGE_SECONDS,
    SPREAD_ODDS_MAX_GAP_SECONDS,
    INLINE_ARB_SCAN_ENABLED,
    arb_max_total_prob_for_bet_type,
    is_active_arb_pair,
    arb_opportunity_alert_chat_ids,
)
from utils.game_registry import matchup_group_key
from utils.match_identity import validate_cross_book_game_datetimes
from utils.helpers import (
    align_cross_book_moneylines,
    align_cross_book_spreads,
    format_arb_opportunity_alert,
    is_game_pregame,
    is_plausible_moneyline_pair,
    parse_game_datetime,
    spread_lines_from_row,
    spread_market_label,
    spread_odds_rows_fresh_for_arb,
    send_telegram_alert,
)


def _calc_arb_total(odds_1, odds_2):
    if odds_1 is None or odds_2 is None:
        return None
    odds_1 = Decimal(str(odds_1))
    odds_2 = Decimal(str(odds_2))
    dec_1 = Decimal(1) + (odds_1 / 100) if odds_1 > 0 else Decimal(1) + (Decimal(100) / abs(odds_1))
    dec_2 = Decimal(1) + (odds_2 / 100) if odds_2 > 0 else Decimal(1) + (Decimal(100) / abs(odds_2))
    return Decimal(1) / dec_1 + Decimal(1) / dec_2


def _spread_cross_book_trusted(leg_1_odds, leg_2_odds, same_side_a, same_side_b, *, arb_total=None):
    try:
        if same_side_a is not None and same_side_b is not None:
            if abs(float(same_side_a) - float(same_side_b)) > 100:
                return False
    except (TypeError, ValueError):
        pass
    if arb_total is None:
        return True
    profit_pct = float((Decimal(1) - arb_total) * 100)
    return profit_pct <= SPREAD_ARB_MAX_PROFIT_PCT


def _spread_pair_fresh_enough(o1: dict, o2: dict) -> bool:
    from datetime import datetime

    def _as_dt(row):
        ts = row.get("updated_at") or row.get("created_at")
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(float(ts))
        return ts

    return spread_odds_rows_fresh_for_arb(
        _as_dt(o1),
        _as_dt(o2),
        max_age_seconds=SPREAD_ODDS_MAX_AGE_SECONDS,
        max_gap_seconds=SPREAD_ODDS_MAX_GAP_SECONDS,
    )


def _resolve_sides(o1, o2, t1_from, t2_from):
    t1 = o1 if t1_from == "o1" else o2
    t2 = o1 if t2_from == "o1" else o2
    return t1, t2


def build_arb_data(
    o1,
    o2,
    t1_from,
    t2_from,
    arb_total,
    *,
    team_1_odds=None,
    team_2_odds=None,
    bet_type: str = "moneyline",
    spread_value=None,
):
    t1, t2 = _resolve_sides(o1, o2, t1_from, t2_from)
    o1_src = o1 if t1_from == "o1" else o2
    o2_src = o2 if t2_from == "o2" else o1
    game_dt = parse_game_datetime(o1.get("game_datetime"))
    from datetime import datetime
    game_date_str = str(game_dt.date()) if game_dt else str(datetime.utcnow().date())

    if bet_type == "spread":
        default_t1_odds = t1.get("spread_team_1")
        default_t2_odds = t2.get("spread_team_2")
        spread_line_team_1, _ = spread_lines_from_row(t1)
        _, spread_line_team_2 = spread_lines_from_row(t2)
    else:
        default_t1_odds = t1.get("moneyline_team_1")
        default_t2_odds = t2.get("moneyline_team_2")
        spread_line_team_1 = None
        spread_line_team_2 = None

    return {
        "sport": o1["sport"],
        "league": o1["league"],
        "game_date": game_date_str,
        "game_datetime": game_dt.strftime("%Y-%m-%d %H:%M:%S") if game_dt else o1.get("game_datetime"),
        "team_1_game_datetime": o1_src.get("game_datetime"),
        "team_2_game_datetime": o2_src.get("game_datetime"),
        "team_1": o1["team_1"],
        "team_1_bookmaker": t1["bookmaker"],
        "team_1_game_id": t1["game_id"],
        "team_1_odds": float(team_1_odds if team_1_odds is not None else default_t1_odds),
        "team_2": o1["team_2"],
        "team_2_bookmaker": t2["bookmaker"],
        "team_2_game_id": t2["game_id"],
        "team_2_odds": float(team_2_odds if team_2_odds is not None else default_t2_odds),
        "bet_type": bet_type,
        "spread_value": spread_value,
        "spread_line_team_1": spread_line_team_1,
        "spread_line_team_2": spread_line_team_2,
        "arb_total_prob": float(arb_total),
        "profit_pct": float(round((Decimal(1) - arb_total) * 100, 2)),
        "read": False,
        "identified_at": time.time(),
    }


def _store_arb(cache, arb_data: dict):
    bet_type = arb_data.get("bet_type", "moneyline")
    cache.add_arbitrage(
        arb_data["team_1_bookmaker"], bet_type, arb_data["team_1_game_id"], arb_data
    )
    cache.add_arbitrage(
        arb_data["team_2_bookmaker"], bet_type, arb_data["team_2_game_id"], arb_data
    )


def _send_opportunity_alert(cache, logger, arb_data: dict):
    bet_type = arb_data.get("bet_type", "moneyline")
    spread_value = arb_data.get("spread_value")
    sport = arb_data.get("sport")
    if cache.arb_opportunity_alert_already_sent(
        arb_data["team_1"],
        arb_data["team_2"],
        arb_data["team_1_bookmaker"],
        arb_data["team_2_bookmaker"],
        arb_data.get("game_date"),
        bet_type=bet_type,
        spread_value=spread_value,
    ):
        logger.info(
            f"Skipping duplicate KC Arb Alerts telegram (already sent today) — "
            f"{arb_data['team_1']} vs {arb_data['team_2']} | "
            f"{arb_data['team_1_bookmaker']} vs {arb_data['team_2_bookmaker']} | "
            f"profit {arb_data.get('profit_pct')}%"
        )
        return

    alert = format_arb_opportunity_alert(arb_data, spread_value=spread_value)
    market_label = (
        spread_market_label(spread_value, sport) if bet_type == "spread" else bet_type
    )
    logger.info(
        f"Inline arb alert ({market_label}) {arb_data['team_1']} vs {arb_data['team_2']} | "
        f"{arb_data['team_1_bookmaker']} vs {arb_data['team_2_bookmaker']}"
    )
    chat_ids = arb_opportunity_alert_chat_ids()
    for chat_id in chat_ids:
        if TELEGRAM_ALERTS_ASYNC:
            threading.Thread(
                target=lambda cid=chat_id: asyncio.run(send_telegram_alert(alert, cid)),
                daemon=True,
            ).start()
        else:
            asyncio.run(send_telegram_alert(alert, chat_id))

    cache.mark_arb_opportunity_alert_sent(
        arb_data["team_1"],
        arb_data["team_2"],
        arb_data["team_1_bookmaker"],
        arb_data["team_2_bookmaker"],
        arb_data.get("game_date"),
        bet_type=bet_type,
        spread_value=spread_value,
    )


def _try_insert_pair(cache, logger, o1, o2, t1_from, t2_from, bet_type) -> int:
    dt_reason = validate_cross_book_game_datetimes(
        o1.get("game_datetime"),
        o2.get("game_datetime"),
        team_1=o1.get("team_1") or "",
        team_2=o1.get("team_2") or "",
    )
    if dt_reason:
        logger.info(
            f"Skipping inline arb ({dt_reason}) - "
            f"{o1.get('team_1')} vs {o1.get('team_2')} | "
            f"{o1.get('bookmaker')} vs {o2.get('bookmaker')}"
        )
        return 0

    if not is_game_pregame(o1.get("game_datetime")) or not is_game_pregame(o2.get("game_datetime")):
        return 0

    game_dt = parse_game_datetime(o1.get("game_datetime"))
    from datetime import datetime
    game_date = str(game_dt.date()) if game_dt else str(datetime.utcnow().date())

    t1, t2 = _resolve_sides(o1, o2, t1_from, t2_from)
    if bet_type == "spread":
        aligned = align_cross_book_spreads(o1, o2)
        if not aligned:
            return 0
        a_t1, a_t2, b_t1, b_t2, spread_value = aligned
        if not _spread_pair_fresh_enough(o1, o2):
            return 0
    else:
        aligned = align_cross_book_moneylines(o1, o2)
        if not aligned:
            return 0
        a_t1, a_t2, b_t1, b_t2 = aligned
        spread_value = None
        if not is_plausible_moneyline_pair(a_t1, a_t2):
            return 0
        if not is_plausible_moneyline_pair(b_t1, b_t2):
            return 0

    if cache.is_arb_scan_locked(
        o1["team_1"],
        o1["team_2"],
        t1["bookmaker"],
        t2["bookmaker"],
        game_date,
        bet_type=bet_type,
        spread_value=spread_value,
    ):
        return 0

    arb_stub = {
        "team_1": o1["team_1"],
        "team_2": o1["team_2"],
        "team_1_bookmaker": t1["bookmaker"],
        "team_2_bookmaker": t2["bookmaker"],
        "game_datetime": o1.get("game_datetime"),
        "game_date": game_date,
        "bet_type": bet_type,
    }
    if spread_value is not None:
        arb_stub["spread_value"] = spread_value
    owns, _ = cache.other_pair_owns_game_event(arb_stub)
    if owns:
        return 0

    inserted = 0
    max_total_prob = Decimal(str(arb_max_total_prob_for_bet_type(bet_type)))

    for leg_a, leg_b, tf_a, tf_b, same_a, same_b in (
        (a_t1, b_t2, t1_from, t2_from, a_t1, b_t1),
        (b_t1, a_t2, t2_from, t1_from, b_t1, a_t1),
    ):
        arb_total = _calc_arb_total(leg_a, leg_b)
        if not arb_total or arb_total >= max_total_prob:
            continue
        if bet_type == "spread" and not _spread_cross_book_trusted(
            leg_a, leg_b, same_a, same_b, arb_total=arb_total
        ):
            continue

        # leg_a/leg_b are always on tf_a/tf_b's book; must match build_arb_data(t1_from=tf_a, ...).
        team_1_odds = leg_a
        team_2_odds = leg_b
        if bet_type == "moneyline" and not is_plausible_moneyline_pair(
            team_1_odds, team_2_odds
        ):
            continue
        arb_data = build_arb_data(
            o1,
            o2,
            tf_a,
            tf_b,
            arb_total,
            team_1_odds=team_1_odds,
            team_2_odds=team_2_odds,
            bet_type=bet_type,
            spread_value=spread_value,
        )
        _store_arb(cache, arb_data)
        _send_opportunity_alert(cache, logger, arb_data)
        inserted += 1

    return inserted


def scan_inline_arbs_for_odds_row(cache, logger, odd_row: dict) -> int:
    """
    Compare a freshly published odds row against other books in Redis.
    On hit: write arb cache, wake both betting loops, fire Telegram alert.
    """
    if not INLINE_ARB_SCAN_ENABLED or not odd_row:
        return 0

    bet_type = odd_row.get("bet_type") or "moneyline"
    if bet_type == "spread" and not SPREAD_ARB_SCAN_ENABLED:
        return 0
    group_key = matchup_group_key(odd_row)
    all_rows = cache.get_odds(bet_type=bet_type)
    peers = [r for r in all_rows if matchup_group_key(r) == group_key]
    if len(peers) < 2:
        return 0

    source_bm = (odd_row.get("bookmaker") or "").strip().lower()
    found = 0
    for other in peers:
        other_bm = (other.get("bookmaker") or "").strip().lower()
        if other_bm == source_bm:
            continue
        if not is_active_arb_pair(source_bm, other_bm):
            continue

        found += _try_insert_pair(cache, logger, odd_row, other, "o1", "o2", bet_type)

    if found:
        logger.info(
            f"Inline arb scan: {found} arb(s) from {source_bm} {bet_type} update "
            f"({odd_row.get('team_1')} vs {odd_row.get('team_2')})"
        )
    return found
