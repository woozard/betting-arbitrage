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


def parse_displayed_american_odds(
    display_text,
    *,
    authoritative_odd=None,
) -> int | None:
    """Parse American odds from BetWar DOM or bet-slip text.

    - Explicit ``+`` / ``-`` in the display string are always trusted.
    - Bare digits (BetWar favorite display, e.g. ``104`` meaning -104) default to
      **negative** unless ``authoritative_odd`` from GetLines for this selection
      says underdog (+).
    - Never infer sign from the arb's *expected* odds alone — pass GetLines live
      odds as ``authoritative_odd`` when matching board cells.
    """
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

    if authoritative_odd is not None:
        try:
            auth = american_odds_to_int(authoritative_odd)
            if auth < 0:
                return -abs(val)
            if auth > 0:
                return abs(val)
            return -abs(val)
        except (TypeError, ValueError):
            pass

    # BetWar board convention: no sign on the cell → favorite juice (negative).
    return -abs(val)


def odds_text_matches(
    displayed: str,
    expected,
    tolerance: int = 0,
    *,
    authoritative_odd=None,
) -> bool:
    """Compare displayed BetWar odds to expected; sign-aware with optional tolerance."""
    if tolerance > 0 and arb_live_odds_acceptable(expected, displayed, tolerance):
        return True

    parsed = parse_displayed_american_odds(
        displayed, authoritative_odd=authoritative_odd or expected
    )
    if parsed is not None:
        try:
            exp = american_odds_to_int(expected)
        except (TypeError, ValueError):
            exp = None
        if exp is not None:
            if parsed == exp:
                return True
            if tolerance > 0 and moneyline_int_odds_acceptable(parsed, exp, tolerance):
                return True

    disp = normalize_us_odds((displayed or "").strip())
    exp = normalize_us_odds(expected)
    if disp == exp:
        return True
    raw = (displayed or "").strip()
    if exp in raw or raw == str(expected).strip():
        return True

    # Bare favorite on board: "104" vs expected -104
    try:
        exp_val = american_odds_to_int(expected)
        num_match = re.search(r"[-+]?\d+", raw.replace("\u2212", "-"))
        if num_match and exp_val < 0 and not num_match.group(0).startswith("+"):
            disp_val = int(num_match.group(0).lstrip("+"))
            if disp_val == abs(exp_val):
                return True
    except (TypeError, ValueError):
        pass
    return False


def moneyline_odds_acceptable_betwar(
    display_text: str,
    expected_odd,
    tolerance: int = 0,
    *,
    authoritative_odd=None,
) -> bool:
    if odds_text_matches(
        display_text,
        expected_odd,
        tolerance,
        authoritative_odd=authoritative_odd,
    ):
        return True
    if tolerance <= 0 or expected_odd in (None, ""):
        return False
    parsed = parse_displayed_american_odds(
        display_text, authoritative_odd=authoritative_odd or expected_odd
    )
    if parsed is None:
        return False
    return moneyline_int_odds_acceptable(parsed, expected_odd, tolerance)


# Backward-compatible alias used by BetWarController imports.
moneyline_odds_acceptable = moneyline_odds_acceptable_betwar


def resolve_betwar_ml_side(
    *,
    team_no: int | None,
    getlines_live_odds,
    expected_odd,
) -> dict:
    """Build placement context from GetLines — mandatory before ML DOM selection."""
    if team_no not in (1, 2):
        raise ValueError(f"GetLines did not resolve team_no (got {team_no})")
    if getlines_live_odds in (None, ""):
        raise ValueError("GetLines live moneyline odds missing for this selection")

    live = american_odds_to_int(getlines_live_odds)
    expected = american_odds_to_int(expected_odd)
    if (live > 0) != (expected > 0):
        raise ValueError(
            f"GetLines/arb sign mismatch: live {live:+d} vs expected {expected:+d}"
        )

    return {
        "team_no": team_no,
        "getlines_live_odds": live,
        "expected_odd": expected,
        "required_sign": "favorite" if live < 0 else "underdog",
    }


def parse_betslip_moneyline_odds(slip_text: str, expected_odd=None) -> int | None:
    if not slip_text:
        return None
    for pattern in (
        r"\bML\s*([-+]?\d+)\b",
        r"\bmoney\s*line\b[^\d]*([-+]?\d+)",
    ):
        match = re.search(pattern, slip_text, re.I)
        if match:
            parsed = parse_displayed_american_odds(
                match.group(1), authoritative_odd=expected_odd
            )
            if parsed is not None:
                return parsed
    return parse_displayed_american_odds(slip_text, authoritative_odd=expected_odd)


def my_bets_description_matches_matchup(description: str, team_1: str, team_2: str) -> bool:
    """Both teams from the arb must appear in the My Bets row description."""
    desc = (description or "").lower()
    if not desc or not team_1 or not team_2:
        return False
    if team_1.lower() in desc and team_2.lower() in desc:
        return True
    last_1 = team_1.strip().split()[-1].lower()
    last_2 = team_2.strip().split()[-1].lower()
    return bool(last_1 and last_2 and last_1 in desc and last_2 in desc)


def my_bets_row_odds_matches_expected(
    description: str,
    expected_odd,
    *,
    tolerance: int = 2,
) -> bool:
    """Require same ML sign as arb; optional small tolerance on the number."""
    parsed = parse_betslip_moneyline_odds(description, expected_odd=expected_odd)
    if parsed is None:
        return False
    try:
        expected = american_odds_to_int(expected_odd)
    except (TypeError, ValueError):
        return False
    if (parsed > 0) != (expected > 0):
        return False
    return moneyline_int_odds_acceptable(parsed, expected, tolerance)
