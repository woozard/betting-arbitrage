"""BetWar moneyline odds parsing and sign-aware placement checks."""

from __future__ import annotations

import re

from utils.helpers import american_odds_to_int, arb_live_odds_acceptable
from utils.moneyline_odds import (
    moneyline_int_odds_acceptable,
    moneyline_odds_acceptable,
)


def normalize_us_odds(odds) -> str:
    try:
        val = int(float(odds))
        return f"+{val}" if val > 0 else str(val)
    except (TypeError, ValueError):
        text = str(odds).strip()
        return text if text.startswith(("+", "-")) else f"+{text}"


def parse_displayed_american_odds(display_text, expected_odd=None) -> int | None:
    raw = (display_text or "").strip()
    if not raw:
        return None
    match = re.search(r"([-+]?\d+)", raw.replace("\u2212", "-"))
    if not match:
        return None
    token = match.group(1)
    if token.startswith(("+", "-")):
        try:
            return american_odds_to_int(token)
        except (TypeError, ValueError):
            return None
    try:
        val = int(token)
    except (TypeError, ValueError):
        return None
    if expected_odd is not None:
        try:
            exp = american_odds_to_int(expected_odd)
            if exp < 0:
                return -abs(val)
            if exp > 0:
                return abs(val)
        except (TypeError, ValueError):
            pass
    return val


def odds_text_matches(displayed: str, expected, tolerance: int = 0) -> bool:
    if tolerance > 0 and arb_live_odds_acceptable(expected, displayed, tolerance):
        return True
    disp = normalize_us_odds((displayed or "").strip())
    exp = normalize_us_odds(expected)
    if disp == exp:
        return True
    raw = (displayed or "").strip()
    if exp in raw or raw == str(expected).strip():
        return True
    try:
        exp_val = int(float(str(expected)))
        num_match = re.search(r"[-+]?\d+", raw.replace("\u2212", "-"))
        if num_match and exp_val < 0:
            disp_val = int(num_match.group(0).lstrip("+"))
            if disp_val == abs(exp_val):
                return True
    except (TypeError, ValueError):
        pass
    return False


def moneyline_odds_acceptable_betwar(
    display_text: str, expected_odd, tolerance: int = 0
) -> bool:
    if odds_text_matches(display_text, expected_odd, tolerance):
        return True
    if tolerance <= 0 or expected_odd in (None, ""):
        return False
    parsed = parse_displayed_american_odds(display_text, expected_odd)
    if parsed is None:
        return False
    return moneyline_int_odds_acceptable(parsed, expected_odd, tolerance)


# Backward-compatible alias used by BetWarController imports.
moneyline_odds_acceptable = moneyline_odds_acceptable_betwar


def parse_betslip_moneyline_odds(slip_text: str, expected_odd=None) -> int | None:
    if not slip_text:
        return None
    for pattern in (
        r"\bML\s*([-+]?\d+)\b",
        r"\bmoney\s*line\b[^\d]*([-+]?\d+)",
    ):
        match = re.search(pattern, slip_text, re.I)
        if match:
            parsed = parse_displayed_american_odds(match.group(1), expected_odd)
            if parsed is not None:
                return parsed
    return parse_displayed_american_odds(slip_text, expected_odd)
