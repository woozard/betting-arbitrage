from utils.bet_screenshot import (
    _s411_odds_needles,
    _s411_open_bets_row_valid,
    _s411_ticket_matches,
)


def test_s411_odds_needles_includes_ml_prefix():
    needles = _s411_odds_needles(-117)
    assert "-117" in needles
    assert "ML-117" in needles


def test_open_bets_row_valid_accepts_single_wager():
    text = (
        "Tampa Bay Rays ML-117 ( ACTION )\n"
        "New York Yankees vs Tampa Bay Rays\n"
        "MLB Straight Bet\n"
        "Game Start 07/08/2026 @ 12:45 PM\n"
        "Placed 07/08/2026 @ 08:30 AM\n"
        "Ticket # 736001492\n"
        "Risk: $23.40 | Win: $20.00"
    )
    assert _s411_open_bets_row_valid(
        text,
        team_name="Tampa Bay Rays",
        team_1="New York Yankees",
        team_2="Tampa Bay Rays",
        odds=-117,
        stake=23.40,
    )


def test_open_bets_row_rejects_full_pending_list():
    text = (
        "Tampa Bay Rays ML-117 ( ACTION )\n"
        "New York Yankees vs Tampa Bay Rays\n"
        "Risk: $23.40 | Win: $20.00\n"
        "Pittsburgh Pirates ML-153 ( ACTION )\n"
        "Risk: $30.60 | Win: $20.00\n"
        "Baltimore Orioles ML+102 ( ACTION )\n"
        "Risk: $20.00 | Win: $20.40"
    )
    assert not _s411_open_bets_row_valid(
        text,
        team_name="Tampa Bay Rays",
        team_1="New York Yankees",
        team_2="Tampa Bay Rays",
        odds=-117,
        stake=23.40,
    )


def test_open_bets_row_rejects_wrong_odds():
    text = (
        "Tampa Bay Rays ML-117 ( ACTION )\n"
        "New York Yankees vs Tampa Bay Rays\n"
        "Risk: $23.40 | Win: $20.00"
    )
    assert not _s411_open_bets_row_valid(
        text,
        team_name="Tampa Bay Rays",
        odds=-150,
        stake=23.40,
    )


def test_ticket_rejects_other_team_with_same_stake():
    cubs = (
        "Chicago Cubs ML+120 ( ACTION )\n"
        "Chicago Cubs vs Baltimore Orioles\n"
        "Risk: $20.00 | Win: $24.00"
    )
    assert not _s411_ticket_matches(
        cubs,
        team_name="Kansas City Royals",
        team_1="Kansas City Royals",
        team_2="New York Mets",
        odds=133,
        stake=20.0,
    )


def test_ticket_matches_royals_plus_133():
    royals = (
        "Kansas City Royals ML+133 ( ACTION )\n"
        "Kansas City Royals vs New York Mets\n"
        "Risk: $20.00 | Win: $26.60"
    )
    assert _s411_ticket_matches(
        royals,
        team_name="Kansas City Royals",
        team_1="Kansas City Royals",
        team_2="New York Mets",
        odds=133,
        stake=20.0,
    )


def test_ticket_matches_by_ticket_number():
    text = (
        "Chicago Cubs ML+120 ( ACTION )\n"
        "Chicago Cubs vs Baltimore Orioles\n"
        "MLB Straight Bet\n"
        "Ticket # 736031775\n"
        "Risk: $20.00 | Win: $24.00"
    )
    assert _s411_ticket_matches(
        text,
        team_name="Chicago Cubs",
        team_1="Chicago Cubs",
        team_2="Baltimore Orioles",
        odds=120,
        ticket_number="736031775",
    )
