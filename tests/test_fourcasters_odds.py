from utils.fourcasters_odds import (
    fourcasters_format_net_odds,
    fourcasters_gross_to_net_taker_odds,
    fourcasters_taker_odds_acceptable,
)
from utils.stake_sizing import american_odds_from_risk_win


def test_gross_to_net_plus_odds():
    assert fourcasters_gross_to_net_taker_odds(114) == 111
    assert fourcasters_gross_to_net_taker_odds(179) == 176


def test_gross_to_net_minus_odds():
    assert fourcasters_gross_to_net_taker_odds(-105) == -108
    assert fourcasters_gross_to_net_taker_odds(-117) == -120


def test_format_net_odds():
    assert fourcasters_format_net_odds(114) == "+111"
    assert fourcasters_format_net_odds(-105) == "-108"


def test_taker_odds_acceptable_matches_gross_live_to_net_arb():
    assert fourcasters_taker_odds_acceptable(111, 114, tolerance=0)
    assert fourcasters_taker_odds_acceptable(176, 179, tolerance=0)
    assert fourcasters_taker_odds_acceptable(175, 179, tolerance=2)
    assert not fourcasters_taker_odds_acceptable(170, 179, tolerance=0)


def test_athletics_fill_matches_net_scan():
    """2026-07-07 Athletics +179 API order → +176 effective fill."""
    fill_odds = american_odds_from_risk_win(20.15, 35.53)
    assert fill_odds == 176
    assert fourcasters_gross_to_net_taker_odds(179) == fill_odds


def test_mariners_scan_expectation():
    """Gross +114 on API → scan +111; fill landed +112."""
    assert fourcasters_gross_to_net_taker_odds(114) == 111
    fill_odds = american_odds_from_risk_win(20.15, 22.63)
    assert fill_odds == 112
    assert abs(fourcasters_gross_to_net_taker_odds(114) - fill_odds) <= 1
