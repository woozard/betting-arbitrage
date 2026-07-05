"""Sign-aware American moneyline odds matching for arb placement."""

from __future__ import annotations

from utils.helpers import american_odds_to_int, arb_live_odds_acceptable


def moneyline_int_odds_acceptable(
    live_odds: int, expected_odd, tolerance: int = 0
) -> bool:
    """True when live ML matches expected side (+/-) and value (within tolerance)."""
    try:
        expected = american_odds_to_int(expected_odd)
    except (TypeError, ValueError):
        return False
    if (expected > 0) != (live_odds > 0):
        return False
    if expected == live_odds:
        return True
    return tolerance > 0 and abs(expected - live_odds) <= tolerance


def moneyline_odds_acceptable(
    live, expected_odd, tolerance: int = 0
) -> bool:
    """Compare live ML (int or display string) to expected; never accept opposite side."""
    if live in (None, ""):
        return False
    try:
        live_int = american_odds_to_int(live)
    except (TypeError, ValueError):
        return False
    return moneyline_int_odds_acceptable(live_int, expected_odd, tolerance)


def arb_moneyline_odds_acceptable(expected, live, tolerance: int = 0) -> bool:
    """Sign-aware wrapper used by book controllers for pre-bet line checks."""
    if tolerance <= 0:
        try:
            return american_odds_to_int(expected) == american_odds_to_int(live)
        except (TypeError, ValueError):
            return False
    if not moneyline_odds_acceptable(live, expected, tolerance):
        return False
    return arb_live_odds_acceptable(expected, live, tolerance)
