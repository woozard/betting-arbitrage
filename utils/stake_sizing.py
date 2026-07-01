"""Base-amount stake sizing for arb legs.

Minus American odds → fill the **to-win** box with the base amount.
Plus American odds  → fill the **risk** box with the base amount.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from utils.config import BET_STAKE
from utils.helpers import american_odds_to_int

EntryField = Literal["risk", "to_win"]


@dataclass(frozen=True)
class BaseAmountStake:
    base_amount: float
    american_odds: int
    entry_field: EntryField
    entry_amount: float
    risk: float
    to_win: float

    def verification_amounts(self) -> tuple[float, ...]:
        vals = {
            round(self.risk, 2),
            round(self.to_win, 2),
            round(self.entry_amount, 2),
            round(self.base_amount, 2),
        }
        return tuple(sorted(vals))


def american_to_win_from_risk(risk: float, american_odds) -> float:
    odds = american_odds_to_int(american_odds)
    if odds > 0:
        return round(float(risk) * odds / 100.0, 2)
    return round(float(risk) * 100.0 / abs(odds), 2)


def american_to_risk_from_win(to_win: float, american_odds) -> float:
    odds = american_odds_to_int(american_odds)
    if odds > 0:
        return round(float(to_win) * 100.0 / odds, 2)
    return round(float(to_win) * abs(odds) / 100.0, 2)


def base_amount_stake_from_odds(
    american_odds,
    base_amount: float | None = None,
) -> BaseAmountStake:
    """Size one leg using the shared base-amount approach."""
    base = round(float(base_amount if base_amount is not None else BET_STAKE), 2)
    odds = american_odds_to_int(american_odds)
    if odds > 0:
        risk = base
        to_win = american_to_win_from_risk(risk, odds)
        entry_field: EntryField = "risk"
    else:
        to_win = base
        risk = american_to_risk_from_win(to_win, odds)
        entry_field = "to_win"
    return BaseAmountStake(
        base_amount=base,
        american_odds=odds,
        entry_field=entry_field,
        entry_amount=base,
        risk=risk,
        to_win=to_win,
    )


def format_base_amount_stake(stake: BaseAmountStake) -> str:
    field_label = "to-win" if stake.entry_field == "to_win" else "risk"
    return (
        f"base=${stake.base_amount:.2f} ({field_label} @ {stake.american_odds:+d}) | "
        f"risk=${stake.risk:.2f} to-win=${stake.to_win:.2f}"
    )


def stake_matches_verification_amount(stake, amount: float, tol: float = 0.011) -> bool:
    try:
        target = float(amount)
    except (TypeError, ValueError):
        return False
    if isinstance(stake, BaseAmountStake):
        return any(abs(target - val) < tol for val in stake.verification_amounts())
    try:
        return abs(target - float(stake)) < tol
    except (TypeError, ValueError):
        return False


def page_contains_stake_amount(page: str, stake: BaseAmountStake) -> bool:
    page_compact = (page or "").lower().replace(" ", "")
    for amount in stake.verification_amounts():
        for pattern in (
            f"risk:${amount:.2f}",
            f"risk:${amount:.0f}",
            f"win:${amount:.2f}",
            f"win:${amount:.0f}",
            f"to-win:${amount:.2f}",
            f"${amount:.2f}",
            f"${amount:.0f}",
            f"{amount:.2f}",
        ):
            if pattern.replace(" ", "").lower() in page_compact:
                return True
    return False
