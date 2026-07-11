"""4casters liquidity-based stake cap: reduce stake to fit available bet size."""

import utils.bet_placement as bp
from utils.bet_placement import (
    _round_down_to_50,
    fourcasters_liquidity_capped_base,
)


class _FakeCache:
    def __init__(self, max_risk_by_game):
        self._data = max_risk_by_game

    def get_fourcasters_max_risk(self, game_id):
        return self._data.get(str(game_id))


def _arb(odds_4c, game_id="g4c", four_side="team_1"):
    """4casters on the given side; S411 on the other."""
    if four_side == "team_1":
        return {
            "team_1_bookmaker": "4casters", "team_1_game_id": game_id, "team_1_odds": odds_4c,
            "team_2_bookmaker": "sports411", "team_2_game_id": "gs", "team_2_odds": -110,
        }
    return {
        "team_1_bookmaker": "sports411", "team_1_game_id": "gs", "team_1_odds": -110,
        "team_2_bookmaker": "4casters", "team_2_game_id": game_id, "team_2_odds": odds_4c,
    }


def test_round_down_to_50():
    assert _round_down_to_50(298.25) == 250
    assert _round_down_to_50(3534.24) == 3500
    assert _round_down_to_50(859.25) == 850
    assert _round_down_to_50(5923.53) == 5900
    assert _round_down_to_50(85925) == 85900
    assert _round_down_to_50(300) == 300


def test_no_cap_when_liquidity_sufficient():
    # +120 underdog: risk == base == 300; limit 500 >= 300 → unchanged.
    cache = _FakeCache({"g4c": {"team_1": 500, "team_2": 500}})
    assert fourcasters_liquidity_capped_base(cache, _arb(120), 300.0) == 300.0


def test_cap_underdog_matches_user_example():
    # desired 300, 4c limit 290 (< 300) → floor(290/50)*50 = 250.
    cache = _FakeCache({"g4c": {"team_1": 290, "team_2": 999}})
    assert fourcasters_liquidity_capped_base(cache, _arb(120), 300.0) == 250.0


def test_cap_favorite_keeps_risk_within_liquidity():
    # -161 favorite: base 300 -> risk 483. limit 290 → max_base=290/1.61≈180 → floor50=150.
    cache = _FakeCache({"g4c": {"team_1": 290, "team_2": 999}})
    capped = fourcasters_liquidity_capped_base(cache, _arb(-161), 300.0)
    assert capped == 150.0


def test_cap_clamps_to_50_minimum():
    cache = _FakeCache({"g4c": {"team_1": 30, "team_2": 999}})
    assert fourcasters_liquidity_capped_base(cache, _arb(120), 300.0) == 50.0


def test_no_cap_when_no_fourcasters_leg():
    cache = _FakeCache({})
    arb = {
        "team_1_bookmaker": "betamapola", "team_1_game_id": "x", "team_1_odds": 120,
        "team_2_bookmaker": "sports411", "team_2_game_id": "y", "team_2_odds": -110,
    }
    assert fourcasters_liquidity_capped_base(cache, arb, 300.0) == 300.0


def test_cap_reads_correct_side_when_4c_is_team_2():
    cache = _FakeCache({"g4c": {"team_1": 999, "team_2": 290}})
    assert fourcasters_liquidity_capped_base(
        cache, _arb(120, four_side="team_2"), 300.0
    ) == 250.0


def test_missing_max_risk_data_leaves_stake_unchanged():
    cache = _FakeCache({})  # nothing published yet
    assert fourcasters_liquidity_capped_base(cache, _arb(120), 300.0) == 300.0
