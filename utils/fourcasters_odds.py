"""4casters taker odds: API orderbook is gross; UI / fills are net after commission."""
from __future__ import annotations

from utils.config import FOURCASTERS_SCAN_ODDS_TICK_HAIRCUT
from utils.helpers import american_odds_to_int
from utils.moneyline_odds import arb_moneyline_odds_acceptable


def fourcasters_gross_to_net_taker_odds(american, tick_haircut: int | None = None) -> int:
    """Convert gross API orderbook odds to net taker odds (fixed tick haircut).

    4casters UI 'View Odds With Commission' is ~3 ticks below API gross (e.g. +114 → +111).
    Used for scanning/alerts only — placement still sends gross odds to the exchange API.
    """
    ticks = FOURCASTERS_SCAN_ODDS_TICK_HAIRCUT if tick_haircut is None else int(tick_haircut)
    odds = american_odds_to_int(american)
    if odds == 0:
        return 0
    return odds - ticks


def fourcasters_taker_odds_acceptable(
    expected_net,
    live_gross,
    tolerance: int = 0,
    tick_haircut: int | None = None,
) -> bool:
    """True when gross live book odds imply net taker odds matching the arb leg."""
    if live_gross in (None, ""):
        return False
    try:
        net_live = fourcasters_gross_to_net_taker_odds(live_gross, tick_haircut=tick_haircut)
    except (TypeError, ValueError):
        return False
    return arb_moneyline_odds_acceptable(expected_net, net_live, tolerance)


def fourcasters_format_net_odds(american, tick_haircut: int | None = None) -> str:
    """Format net taker odds for persistence (scanner / DB)."""
    net = fourcasters_gross_to_net_taker_odds(american, tick_haircut=tick_haircut)
    return f"+{net}" if net > 0 else str(net)
