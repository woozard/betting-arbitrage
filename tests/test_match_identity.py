"""Tests for cross-book same-match identity guards."""

from datetime import datetime, timedelta

import pytz

from utils.match_identity import (
    canonical_schedule_key,
    game_datetimes_same_match,
    schedule_datetime_with_occurrence,
    validate_arb_same_match,
    validate_cross_book_game_datetimes,
)


def test_game_datetimes_same_match_within_two_hours():
    a = "2026-07-09 02:10:00"
    b = "2026-07-09 02:30:00"
    assert game_datetimes_same_match(a, b) is True


def test_game_datetimes_reject_next_day_same_clock():
  # Today 10:10 PM ET vs tomorrow 10:10 PM ET (24h apart)
    a = "2026-07-09 02:10:00"
    b = "2026-07-10 02:10:00"
    assert game_datetimes_same_match(a, b) is False


def test_validate_cross_book_rejects_mismatched_starts():
    reason = validate_cross_book_game_datetimes(
        "2026-07-09 02:10:00",
        "2026-07-10 02:10:00",
        team_1="Arizona Diamondbacks",
        team_2="San Diego Padres",
    )
    assert reason is not None
    assert "mismatch" in reason.lower()


def test_validate_arb_same_match_requires_leg_datetimes():
    good = {
        "team_1": "A",
        "team_2": "B",
        "bet_type": "moneyline",
        "game_datetime": "2026-07-09 02:10:00",
        "team_1_game_datetime": "2026-07-09 02:10:00",
        "team_2_game_datetime": "2026-07-09 02:15:00",
        "team_1_bookmaker": "4casters",
        "team_2_bookmaker": "sports411",
    }
    assert validate_arb_same_match(good) is None

    bad = dict(good)
    bad["team_2_game_datetime"] = "2026-07-10 02:10:00"
    assert validate_arb_same_match(bad) is not None


def test_schedule_datetime_with_occurrence_separates_duplicate_matchups():
    tz = "America/New_York"
    now_utc = datetime(2026, 7, 8, 20, 32, 0, tzinfo=pytz.UTC)  # 4:32 PM ET
    first = schedule_datetime_with_occurrence(
        "10:10 PM",
        occurrence_index=0,
        tz_name=tz,
        now_utc=now_utc,
    )
    second = schedule_datetime_with_occurrence(
        "10:10 PM",
        occurrence_index=1,
        tz_name=tz,
        now_utc=now_utc,
    )
    assert first is not None and second is not None
    assert not game_datetimes_same_match(first, second)


def test_canonical_schedule_key_uses_minute_precision():
    pair, key = canonical_schedule_key(
        "Arizona Diamondbacks",
        "San Diego Padres",
        "2026-07-09 02:10:00",
    )
    assert key == "2026-07-09 02:10"
    assert len(pair) == 2
