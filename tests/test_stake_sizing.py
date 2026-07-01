import pytest

from utils.stake_sizing import (
    american_to_risk_from_win,
    american_to_win_from_risk,
    base_amount_stake_from_odds,
)


def test_minus_odds_fills_to_win_box():
    stake = base_amount_stake_from_odds(-150, 20)
    assert stake.entry_field == "to_win"
    assert stake.entry_amount == 20
    assert stake.to_win == 20
    assert stake.risk == 30


def test_plus_odds_fills_risk_box():
    stake = base_amount_stake_from_odds(+150, 20)
    assert stake.entry_field == "risk"
    assert stake.entry_amount == 20
    assert stake.risk == 20
    assert stake.to_win == 30


def test_even_money_minus():
    stake = base_amount_stake_from_odds(-100, 20)
    assert stake.to_win == 20
    assert stake.risk == 20


def test_conversion_helpers():
    assert american_to_win_from_risk(20, +150) == 30
    assert american_to_risk_from_win(20, -150) == 30
