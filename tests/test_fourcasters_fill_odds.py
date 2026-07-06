from utils.stake_sizing import (
    american_odds_from_risk_win,
    base_amount_stake_from_odds,
    stake_from_fourcasters_fill,
)


def test_american_odds_from_risk_win_underdog():
    # Royals fill from 2026-07-06: risk=20.15 win=36.7225 → +182 display
    assert american_odds_from_risk_win(20.15, 36.7225) == 182


def test_american_odds_from_risk_win_favorite():
    assert american_odds_from_risk_win(29.15, 19.85) == -147


def test_stake_from_fourcasters_fill_uses_actual_amounts():
    fallback = base_amount_stake_from_odds(185, 20.0)
    fill = {"risk": 20.15, "win": 36.7225, "txID": "abc"}
    stake = stake_from_fourcasters_fill(fill, fallback)
    assert stake.risk == 20.15
    assert stake.to_win == 36.72
    assert stake.american_odds == 182


def test_stake_from_fourcasters_fill_falls_back_without_amounts():
    fallback = base_amount_stake_from_odds(185, 20.0)
    stake = stake_from_fourcasters_fill({}, fallback)
    assert stake is fallback
