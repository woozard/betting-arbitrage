"""Size the second arb leg from the first leg's confirmed fill (risk + to-win)."""
from __future__ import annotations

from utils.helpers import american_odds_to_int
from utils.stake_sizing import BaseAmountStake, base_amount_stake_from_odds


def hedge_base_amount_from_first_leg(
    first_risk: float,
    first_to_win: float,
    hedge_odds,
) -> float:
    """Return the base-amount box value for a balanced hedge on leg 2.

    For plus American odds the base amount is risk; for minus odds it is to-win.
    """
    odds = american_odds_to_int(hedge_odds)
    total = float(first_risk) + float(first_to_win)
    if odds > 0:
        return round(total / (1.0 + odds / 100.0), 2)
    return round(total / (1.0 + abs(odds) / 100.0), 2)


def hedge_stake_from_first_leg(
    first: BaseAmountStake,
    hedge_odds,
) -> BaseAmountStake:
    """Build a full stake plan for leg 2 that balances profit vs leg 1's fill."""
    base = hedge_base_amount_from_first_leg(first.risk, first.to_win, hedge_odds)
    return base_amount_stake_from_odds(hedge_odds, base)
