from utils.betwar_odds import (
    moneyline_int_odds_acceptable,
    moneyline_odds_acceptable,
    parse_betslip_moneyline_odds,
    parse_displayed_american_odds,
)


def test_parse_displayed_odds_bare_favorite():
    assert parse_displayed_american_odds("104", -110) == -104
    assert parse_displayed_american_odds("104", 110) == 104


def test_parse_displayed_odds_explicit_sign():
    assert parse_displayed_american_odds("-125", 110) == -125
    assert parse_displayed_american_odds("+120", -115) == 120


def test_moneyline_odds_acceptable_rejects_opposite_side():
    assert not moneyline_odds_acceptable("-104", 110, tolerance=2)
    assert not moneyline_odds_acceptable("104", 110, tolerance=2)
    assert not moneyline_int_odds_acceptable(-104, 110, tolerance=2)


def test_moneyline_odds_acceptable_allows_small_move_same_side():
    assert moneyline_odds_acceptable("+108", 110, tolerance=2)
    assert moneyline_int_odds_acceptable(108, 110, tolerance=2)


def test_moneyline_odds_acceptable_rejects_large_move_same_side():
    assert not moneyline_odds_acceptable("+104", 110, tolerance=2)


def test_parse_betslip_moneyline_odds():
    slip = "[964] NY Yankees ML -125\nMIN Twins / NY Yankees\nRisk/Win 25 / 20"
    assert parse_betslip_moneyline_odds(slip, -115) == -125
