from utils.fourcasters_web import (
    _fourcasters_expanded_panel_valid,
    _fourcasters_nav_or_homepage_text,
    _fourcasters_odds_matches,
    _fourcasters_wager_detail_valid,
    _wager_text_matches,
)


def test_wager_text_matches_team_name():
    text = "BOSTON RED SOX -145\nBOSTON RED SOX @ LOS ANGELES ANGELS\n$29\n$20\nTAKEN"
    assert _wager_text_matches(text, "Boston Red Sox", "Boston Red Sox", "Los Angeles Angels")


def test_wager_text_matches_mascot_only():
    text = "RED SOX -145 @ ANGELS"
    assert _wager_text_matches(text, "Boston Red Sox", "Boston Red Sox", "Los Angeles Angels")


def test_wager_text_matches_other_team_false():
    text = "NEW YORK YANKEES -130 @ METS"
    assert not _wager_text_matches(text, "Boston Red Sox", "Boston Red Sox", "Los Angeles Angels")


def test_wager_detail_rejects_homepage_nav():
    text = "MLB FIGHTS SOCCER WNBA GOLF TENNIS CFL PROPS NBA Custom WALLET Balance $633"
    assert _fourcasters_nav_or_homepage_text(text)
    assert not _fourcasters_wager_detail_valid(
        text,
        team_name="Kansas City Royals",
        team_1="Philadelphia Phillies",
        team_2="Kansas City Royals",
        odds=185,
    )


def test_wager_detail_accepts_active_wager_row():
    text = (
        "06:10 PM 06 JUL\nKC ROYALS +182\n"
        "911 PHI PHILLIES @ 912 KC ROYALS\n$20.15\n$36.72\nTAKEN\n04:21 PM 06 JUL"
    )
    assert _fourcasters_wager_detail_valid(
        text,
        team_name="Kansas City Royals",
        team_1="Philadelphia Phillies",
        team_2="Kansas City Royals",
        odds=185,
    )


def test_odds_tolerance_accepts_line_move():
    text = "KC ROYALS +182 @ PHI $20 TAKEN"
    assert _fourcasters_odds_matches(text, 185)
    assert not _fourcasters_odds_matches(text, 150)


def test_expanded_panel_accepts_detail_block():
    text = (
        "Game:\n911 Philadelphia Phillies @ 912 Kansas City Royals\n"
        "League:\nMLB\nSide:\nKansas City Royals +181\n"
        "Bet:\nMoneyline\nRisk:\n$20.20\nWin:\n$36.63"
    )
    assert _fourcasters_expanded_panel_valid(
        text,
        team_name="Kansas City Royals",
        team_1="Philadelphia Phillies",
        team_2="Kansas City Royals",
        odds=185,
    )


def test_expanded_panel_rejects_collapsed_row():
    text = (
        "06:10 PM 06 JUL\nKC ROYALS +182\n"
        "911 PHI PHILLIES @ 912 KC ROYALS\n$20.15\n$36.72\nTAKEN"
    )
    assert not _fourcasters_expanded_panel_valid(
        text,
        team_name="Kansas City Royals",
        team_1="Philadelphia Phillies",
        team_2="Kansas City Royals",
        odds=185,
    )
