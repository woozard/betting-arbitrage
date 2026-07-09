from utils.fourcasters_liquidity import max_taker_risk_from_orders, remaining_order_win
from utils.stake_sizing import (
    BaseAmountStake,
    base_amount_stake_from_odds,
    cap_base_amount_stake_to_max_risk,
)


def test_remaining_order_win_respects_taken_ratio():
    order = {"sumUntaken": 100.0, "takenRatio": 0.25}
    assert remaining_order_win(order) == 75.0


def test_max_taker_risk_sums_matching_orders():
    orders = [
        {
            "participantID": "pid-a",
            "odds": -123,
            "sumUntaken": 20.34,
            "takenRatio": 0,
        },
        {
            "participantID": "pid-a",
            "odds": -123,
            "sumUntaken": 10.0,
            "takenRatio": 0,
        },
        {
            "participantID": "pid-b",
            "odds": -123,
            "sumUntaken": 999.0,
            "takenRatio": 0,
        },
    ]
    max_risk = max_taker_risk_from_orders(
        orders, participant_id="pid-a", gross_odds=-123
    )
    # (20.34 + 10) to-win @ -123 → risk ≈ 37.32
    assert max_risk == 37.32


def test_cap_base_amount_stake_to_max_risk_minus_odds():
    stake = base_amount_stake_from_odds(20.0, -123)
    capped = cap_base_amount_stake_to_max_risk(stake, 15.0)
    assert isinstance(capped, BaseAmountStake)
    assert capped.risk == 15.0
    assert capped.to_win < stake.to_win


def test_cap_base_amount_stake_no_change_when_liquidity_sufficient():
    stake = base_amount_stake_from_odds(20.0, -123)
    capped = cap_base_amount_stake_to_max_risk(stake, stake.risk + 100)
    assert capped is stake or capped.risk == stake.risk
