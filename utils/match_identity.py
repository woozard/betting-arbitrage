"""Cross-book match identity: same teams, same scheduled start."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from utils.helpers import parse_game_datetime

# Max allowed difference between leg start times (seconds).
_DEFAULT_MAX_DELTA = int(os.getenv("ARB_MATCH_MAX_START_DELTA_SECONDS", str(2 * 3600)))


def game_datetime_minute_key(value) -> str | None:
    """Normalize to UTC minute precision for grouping."""
    dt = parse_game_datetime(value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M")


def game_datetimes_same_match(
    dt_a,
    dt_b,
    *,
    max_delta_seconds: int | None = None,
) -> bool:
    """True when both legs refer to the same scheduled start (within tolerance)."""
    a = parse_game_datetime(dt_a)
    b = parse_game_datetime(dt_b)
    if a is None or b is None:
        return False
    limit = _DEFAULT_MAX_DELTA if max_delta_seconds is None else max_delta_seconds
    return abs((a - b).total_seconds()) <= limit


def validate_cross_book_game_datetimes(
    dt_a,
    dt_b,
    *,
    team_1: str = "",
    team_2: str = "",
    max_delta_seconds: int | None = None,
) -> str | None:
    """Return skip reason when two odds rows are not the same game; else None."""
    a = parse_game_datetime(dt_a)
    b = parse_game_datetime(dt_b)
    if a is None or b is None:
        return (
            f"missing game_datetime for same-match check — "
            f"{team_1} vs {team_2} (leg_a={dt_a!r}, leg_b={dt_b!r})"
        )
    if not game_datetimes_same_match(dt_a, dt_b, max_delta_seconds=max_delta_seconds):
        delta_h = abs((a - b).total_seconds()) / 3600.0
        return (
            f"game start mismatch — {team_1} vs {team_2} "
            f"({a.strftime('%Y-%m-%d %H:%M')} UTC vs "
            f"{b.strftime('%Y-%m-%d %H:%M')} UTC, Δ{delta_h:.1f}h)"
        )
    if a.date() != b.date():
        return (
            f"game date mismatch — {team_1} vs {team_2} "
            f"({a.date()} vs {b.date()})"
        )
    return None


def canonical_schedule_key(
    team_1: str,
    team_2: str,
    game_datetime,
    *,
    sport: str = "baseball",
    league: str = "mlb",
) -> tuple[tuple[str, str], str]:
    """Sorted team pair + UTC minute key (falls back to date-only when time unknown)."""
    from utils.team_registry import canonical_team

    pair = tuple(
        sorted(
            [
                canonical_team(team_1, sport=sport, league=league),
                canonical_team(team_2, sport=sport, league=league),
            ]
        )
    )
    minute_key = game_datetime_minute_key(game_datetime)
    if minute_key:
        return pair, minute_key
    text = str(game_datetime or "").strip()
    date_key = text[:10] if len(text) >= 10 else ""
    return pair, date_key


def validate_arb_same_match(arb: dict) -> str | None:
    """Block placement when arb payload does not describe one exact match."""
    team_1 = arb.get("team_1") or ""
    team_2 = arb.get("team_2") or ""
    bet_type = (arb.get("bet_type") or "moneyline").lower()

    leg_dts = [
        arb.get("team_1_game_datetime"),
        arb.get("team_2_game_datetime"),
    ]
    leg_dts = [d for d in leg_dts if d]
    arb_dt = arb.get("game_datetime")

    if len(leg_dts) >= 2:
        reason = validate_cross_book_game_datetimes(
            leg_dts[0],
            leg_dts[1],
            team_1=team_1,
            team_2=team_2,
        )
        if reason:
            return reason
        if arb_dt and not game_datetimes_same_match(arb_dt, leg_dts[0]):
            return (
                f"arb game_datetime does not match leg books — {team_1} vs {team_2}"
            )
        return None

    if not arb_dt:
        return f"missing game_datetime on arb — {team_1} vs {team_2}"

    if bet_type == "moneyline":
        book_1 = (arb.get("team_1_bookmaker") or "").strip().lower()
        book_2 = (arb.get("team_2_bookmaker") or "").strip().lower()
        if not book_1 or not book_2:
            return f"missing bookmaker pair on arb — {team_1} vs {team_2}"

    return None


def schedule_datetime_with_occurrence(
    time_str: str,
    *,
    occurrence_index: int,
    tz_name: str,
    now_utc: datetime | None = None,
) -> str | None:
    """
    Build UTC schedule string from a display time + duplicate-row index.

    When S411 lists the same matchup twice (today + tomorrow), occurrence_index
  0 is the nearer start and 1 is the next day at the same clock time.
    """
    import re

    import pytz

    from utils.helpers import parse_to_mysql_datetime

    try:
        time_str = (time_str or "").strip()
        if not time_str:
            return None
        is_pm = bool(re.search(r"pm", time_str, re.I))
        is_am = bool(re.search(r"am", time_str, re.I))
        clean = re.sub(r"\s*[APap][Mm]", "", time_str).strip()
        parts = clean.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if is_pm and hour != 12:
            hour += 12
        elif is_am and hour == 12:
            hour = 0

        tz = pytz.timezone(tz_name)
        now_local = datetime.now(tz) if now_utc is None else now_utc.astimezone(tz)
        candidate = now_local.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= now_local + timedelta(minutes=30):
            candidate += timedelta(days=1)
        candidate += timedelta(days=max(0, occurrence_index))
        local_str = candidate.strftime("%Y-%m-%d %H:%M:%S")
        return parse_to_mysql_datetime(local_str, tz_name=tz_name)
    except Exception:
        return None
