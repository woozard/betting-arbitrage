from utils.hedge_stake import hedge_base_amount_from_first_leg, hedge_stake_from_first_leg
from utils.stake_sizing import BaseAmountStake, american_to_risk_from_win


def test_hedge_base_amount_minus_odds_mariners_example():
    """4cast fill risk=$20.15 win=$22.63 → S411 -117 hedge to-win ~$19.71."""
    base = hedge_base_amount_from_first_leg(20.15, 22.63, -117)
    assert base == 19.71
    risk = american_to_risk_from_win(base, -117)
    assert abs(risk - 23.06) < 0.02


def test_hedge_base_amount_plus_odds():
    base = hedge_base_amount_from_first_leg(20.0, 25.0, 150)
    assert base == round(45.0 / 2.5, 2)
    assert base == 18.0


def test_hedge_stake_from_first_leg_returns_base_amount_stake():
    first = BaseAmountStake(
        base_amount=20.0,
        american_odds=112,
        entry_field="risk",
        entry_amount=20.15,
        risk=20.15,
        to_win=22.63,
    )
    stake = hedge_stake_from_first_leg(first, -117)
    assert stake.base_amount == 19.71
    assert stake.entry_field == "to_win"
    assert stake.risk == american_to_risk_from_win(19.71, -117)
