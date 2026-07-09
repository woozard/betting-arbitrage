"""4casters exchange liquidity: max taker risk available at a price level."""
from __future__ import annotations

from utils.moneyline_odds import arb_moneyline_odds_acceptable
from utils.stake_sizing import american_to_risk_from_win


def remaining_order_win(order: dict) -> float:
    """Win amount still available on a resting maker order."""
    try:
        win = float(order.get("sumUntaken") or 0)
    except (TypeError, ValueError):
        return 0.0
    try:
        taken = float(order.get("takenRatio") or 0)
    except (TypeError, ValueError):
        taken = 0.0
    return max(0.0, win * (1.0 - taken))


def max_taker_risk_from_orders(
    orders: list | None,
    *,
    participant_id: str,
    gross_odds: int,
    odds_tolerance: int = 0,
) -> float | None:
    """
    Sum available maker win at matching odds and convert to max taker risk.

    Orderbook orders are sorted best-price first; for fillAndKill we match at
    the requested gross American odds (optionally within tolerance).
    """
    if not orders:
        return None

    pid = str(participant_id or "")
    try:
        target_odds = int(gross_odds)
    except (TypeError, ValueError):
        return None

    total_win = 0.0
    for order in orders:
        if str(order.get("participantID") or "") != pid:
            continue
        try:
            order_odds = int(order.get("odds"))
        except (TypeError, ValueError):
            continue
        if odds_tolerance > 0:
            if not arb_moneyline_odds_acceptable(target_odds, order_odds, odds_tolerance):
                continue
        elif order_odds != target_odds:
            continue
        total_win += remaining_order_win(order)

    if total_win <= 0:
        return None
    return round(american_to_risk_from_win(total_win, target_odds), 2)
