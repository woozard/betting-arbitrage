from utils.betwar_odds import (
    moneyline_int_odds_acceptable,
    moneyline_odds_acceptable,
    my_bets_description_matches_matchup,
    my_bets_row_odds_matches_expected,
    parse_betslip_moneyline_odds,
    parse_displayed_american_odds,
    resolve_betwar_ml_side,
)


def test_parse_bare_odds_default_favorite_convention():
    """Bare board digits without GetLines → always negative (favorite)."""
    assert parse_displayed_american_odds("104") == -104
    assert parse_displayed_american_odds("125") == -125


def test_parse_bare_odds_uses_getlines_sign_not_expected_underdog():
    """Without GetLines, bare digits are favorite (-). Sign comes from GetLines when provided."""
    assert parse_displayed_american_odds("104") == -104
    assert parse_displayed_american_odds("104", authoritative_odd=-115) == -104
    assert parse_displayed_american_odds("110", authoritative_odd=120) == 110
    # Callers must pass GetLines live as authoritative — not arb expected on wrong row.
    assert parse_displayed_american_odds("104", authoritative_odd=110) == 104


def test_parse_displayed_odds_explicit_sign():
    assert parse_displayed_american_odds("-125") == -125
    assert parse_displayed_american_odds("+120") == 120


def test_moneyline_odds_acceptable_rejects_opposite_side():
    assert not moneyline_odds_acceptable("-104", 110, tolerance=2)
    assert not moneyline_odds_acceptable("104", 110, tolerance=2)
    assert not moneyline_int_odds_acceptable(-104, 110, tolerance=2)


def test_moneyline_odds_acceptable_allows_small_move_same_side():
    assert moneyline_odds_acceptable("+108", 110, tolerance=2)
    assert moneyline_odds_acceptable("108", 110, tolerance=2, authoritative_odd=110)
    assert moneyline_int_odds_acceptable(108, 110, tolerance=2)


def test_moneyline_odds_acceptable_rejects_large_move_same_side():
    assert not moneyline_odds_acceptable("+104", 110, tolerance=2)


def test_parse_betslip_moneyline_odds():
    slip = "[964] NY Yankees ML -125\nMIN Twins / NY Yankees\nRisk/Win 25 / 20"
    assert parse_betslip_moneyline_odds(slip, expected_odd=-115) == -125


def test_resolve_betwar_ml_side_requires_team_no_and_live_odds():
    ctx = resolve_betwar_ml_side(
        team_no=2,
        getlines_live_odds=-115,
        expected_odd=-118,
    )
    assert ctx["team_no"] == 2
    assert ctx["getlines_live_odds"] == -115
    assert ctx["required_sign"] == "favorite"


def test_resolve_betwar_ml_side_rejects_sign_mismatch():
    try:
        resolve_betwar_ml_side(
            team_no=1,
            getlines_live_odds=-115,
            expected_odd=120,
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "sign mismatch" in str(exc).lower()


def test_my_bets_matchup_scoping():
    desc = "MIN Twins / NY Yankees ML -125"
    assert my_bets_description_matches_matchup(
        desc, "Minnesota Twins", "New York Yankees"
    )
    assert not my_bets_description_matches_matchup(
        desc, "Boston Red Sox", "New York Yankees"
    )


def test_my_bets_row_odds_requires_same_sign():
    twins_yanks = "MIN Twins / NY Yankees ML -125"
    mets_braves = "NY Mets / ATL Braves ML -125"
    assert my_bets_row_odds_matches_expected(twins_yanks, -125)
    assert my_bets_row_odds_matches_expected(twins_yanks, -124)
    assert not my_bets_row_odds_matches_expected(twins_yanks, 120)
    assert my_bets_row_odds_matches_expected(mets_braves, -125)
