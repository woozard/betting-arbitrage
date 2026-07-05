from utils.helpers import (
    format_alert_ticket_line,
    format_arb_complete_alert,
    format_arb_game_schedule,
)
from utils.bet_placement import _build_leg_confirmed_alert


def _twins_arb():
    return {
        "sport": "MLB",
        "league": "MLB",
        "game_date": "2026-07-05",
        "game_datetime": "2026-07-05 23:05:00",
        "team_1": "Minnesota Twins",
        "team_2": "New York Yankees",
        "team_1_bookmaker": "betamapola",
        "team_2_bookmaker": "betwar",
        "team_1_odds": 120,
        "team_2_odds": -115,
        "bet_type": "moneyline",
        "profit_pct": 1.06,
        "identified_at": 1783268585.0,
    }


def test_format_arb_game_schedule_includes_date_and_time():
    line = format_arb_game_schedule(_twins_arb())
    assert line.startswith("2026-07-05")
    assert "PM" in line or "AM" in line


def test_format_alert_ticket_line():
    assert format_alert_ticket_line(135844851) == "Ticket: 135844851"
    assert format_alert_ticket_line(None) == ""
    assert format_alert_ticket_line(0) == ""


def test_leg_confirmed_alert_shows_game_odds_date_and_ticket():
    alert = _build_leg_confirmed_alert(
        _twins_arb(),
        "betamapola",
        team_no=1,
        team_name="Minnesota Twins",
        stake=20.0,
        moneyline_odd=120,
        other_book="betwar",
        other_leg_placed=True,
        ticket_number=135844851,
    )
    assert "Minnesota Twins vs New York Yankees" in alert
    assert "Date: 2026-07-05" in alert
    assert "Minnesota Twins +120" in alert
    assert "Ticket: 135844851" in alert
    assert "Leg 2 of 2" in alert


def test_leg_confirmed_alert_without_ticket():
    alert = _build_leg_confirmed_alert(
        _twins_arb(),
        "sports411",
        team_no=1,
        team_name="Minnesota Twins",
        stake=20.0,
        moneyline_odd=122,
        other_book="betwar",
        other_leg_placed=False,
    )
    assert "Ticket:" not in alert
    assert "Minnesota Twins +122" in alert
    assert "Waiting for leg 2" in alert


def test_arb_complete_uses_placed_odds_and_ticket():
    arb = _twins_arb()
    alert = format_arb_complete_alert(
        arb,
        outcome="complete",
        leg1_stake=(20.0, 24.0),
        leg2_stake=(25.0, 20.0),
        leg1_placed_odds=120,
        leg2_placed_odds=-125,
        leg1_ticket=135844851,
    )
    assert "Status: ✓ Complete" in alert
    assert "ENGINE FOUND:" in alert
    assert "PLACED:" in alert
    assert "Minnesota Twins vs New York Yankees" in alert
    assert "2026-07-05" in alert
    assert "Twins +120 amapola" in alert
    assert "Yankees -125 betwar" in alert
    assert "#135844851" in alert
    assert "-115" in alert  # engine odds for leg 2
