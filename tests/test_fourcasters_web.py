from utils.fourcasters_web import (
    _fourcasters_nav_or_homepage_text,
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
        "01:30 AM 06 JUL KANSAS CITY ROYALS +185 "
        "PHILADELPHIA PHILLIES @ KANSAS CITY ROYALS $20 $37 TAKEN"
    )
    assert _fourcasters_wager_detail_valid(
        text,
        team_name="Kansas City Royals",
        team_1="Philadelphia Phillies",
        team_2="Kansas City Royals",
        odds=185,
    )
