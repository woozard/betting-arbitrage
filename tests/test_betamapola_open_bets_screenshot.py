from utils.bet_screenshot import (
    _betamapola_open_bets_is_header_block,
    _betamapola_open_bets_row_valid,
    _betamapola_open_bets_ticket_valid,
    _betamapola_team_needles,
)


def test_team_needles_include_mascot():
    needles = _betamapola_team_needles("Philadelphia Phillies", "Philadelphia Phillies", "Kansas City Royals")
    assert "philadelphia phillies" in needles
    assert "phillies" in needles
    assert "royals" in needles


def test_open_bets_ticket_valid_accepts_full_card():
    text = (
        "TIK#\nACCEPTED DATE\nTYPE\nDETAILS\nRISK\nWIN\n"
        "135864249-1\nMON 7/6\n12:21 PM\nMoney Line\n"
        "Baseball - 911 Philadelphia Phillies -191 for Game Price Is Fixed\n"
        "Risk\n$38.20\nWin\n$20.00"
    )
    assert _betamapola_open_bets_ticket_valid(
        text,
        team_name="Philadelphia Phillies",
        team_1="Philadelphia Phillies",
        team_2="Kansas City Royals",
        odds=-191,
        ticket_number=135864249,
    )
    assert _betamapola_open_bets_is_header_block(text)
    assert not _betamapola_open_bets_row_valid(
        text,
        team_name="Philadelphia Phillies",
        odds=-191,
        ticket_number=135864249,
    )


def test_open_bets_row_valid_accepts_compact_wager_row():
    text = (
        "135864249-1\nMON 7/6\n12:21 PM\nMoney Line\n"
        "Baseball - 911 Philadelphia Phillies -191 for Game Price Is Fixed "
        "Pitchers: C SANCHEZ - L (action), N CAMERON - L (action)\n"
        "Risk\n$38.20\nWin\n$20.00"
    )
    assert _betamapola_open_bets_row_valid(
        text,
        team_name="Philadelphia Phillies",
        team_1="Philadelphia Phillies",
        team_2="Kansas City Royals",
        odds=-191,
        ticket_number=135864249,
    )
    assert not _betamapola_open_bets_is_header_block(text)


def test_open_bets_ticket_rejects_banner_only():
    text = "WAGER ACCEPTED\nTicket #135864249"
    assert not _betamapola_open_bets_ticket_valid(
        text,
        team_name="Philadelphia Phillies",
        odds=-191,
        ticket_number=135864249,
    )
