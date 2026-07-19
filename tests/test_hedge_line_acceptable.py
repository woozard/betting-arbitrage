"""Hedge completion accepts any still-profitable (or break-even) line."""

from utils.moneyline_odds import combined_arb_profit_pct, hedge_line_acceptable


def test_sparks_retry_still_profitable_against_wings_fill():
    # Real incident: arb +448, live +431 vs 4c fill ~-415 → still +0.59%.
    assert hedge_line_acceptable(-415, 431, min_profit_pct=0)
    profit = combined_arb_profit_pct(-415, 431)
    assert profit is not None and profit > 0


def test_break_even_floor_accepted():
    # Rough break-even pair around -110 / +110.
    assert hedge_line_acceptable(-110, 110, min_profit_pct=0)


def test_guaranteed_loss_rejected():
    assert not hedge_line_acceptable(-200, 150, min_profit_pct=0)


def test_min_profit_floor_enforced():
    # +0.59% should fail a 1% floor.
    assert not hedge_line_acceptable(-415, 431, min_profit_pct=1.0)
    assert hedge_line_acceptable(-415, 448, min_profit_pct=1.0)
