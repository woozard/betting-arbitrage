from utils.fourcasters_web import _wager_text_matches


def test_wager_text_matches_team_name():
    text = "BOSTON RED SOX -145\nBOSTON RED SOX @ LOS ANGELES ANGELS\n$29\n$20\nTAKEN"
    assert _wager_text_matches(text, "Boston Red Sox", "Boston Red Sox", "Los Angeles Angels")


def test_wager_text_matches_mascot_only():
    text = "RED SOX -145 @ ANGELS"
    assert _wager_text_matches(text, "Boston Red Sox", "Boston Red Sox", "Los Angeles Angels")


def test_wager_text_matches_other_team_false():
    text = "NEW YORK YANKEES -130 @ METS"
    assert not _wager_text_matches(text, "Boston Red Sox", "Boston Red Sox", "Los Angeles Angels")
