from utils.paradise_web import _format_odds_needles, _wager_text_matches


def test_format_odds_needles_positive():
    assert "(+140)" in _format_odds_needles("+140")
    assert "+140" in _format_odds_needles(140)


def test_format_odds_needles_negative():
    assert "-121" in _format_odds_needles("-121")


def test_wager_text_matches_team_and_odds():
    text = "Los Angeles Angels (+140)\n$28.00\n$39.20"
    assert _wager_text_matches(
        text,
        "Los Angeles Angels",
        "Boston Red Sox",
        "Los Angeles Angels",
        odds="+140",
    )


def test_wager_text_matches_wrong_odds():
    text = "Los Angeles Angels (+130)\n$28.00\n$39.20"
    assert not _wager_text_matches(
        text,
        "Los Angeles Angels",
        "Boston Red Sox",
        "Los Angeles Angels",
        odds="+140",
    )
