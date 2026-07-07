from utils.bet_placement import (
    resolve_arb_leg_stake,
    sequential_arb_betting_enabled,
    should_defer_for_sequential_first_leg,
)
from utils.config import arb_pair_legs


class _FakeRedis:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, key):
        return self._data.get(key)


class _FakeCache:
    def __init__(self, *, leg_placed=None, summary=None):
        self._leg_placed = leg_placed or {}
        self.redis = _FakeRedis(summary or {})

    def arb_pair_key_from_arb(self, arb):
        return "pair:test"

    def is_arb_leg_placed(self, arb, bookmaker):
        return self._leg_placed.get((bookmaker or "").lower(), False)


def test_arb_pair_legs_fourcasters_first():
    legs = arb_pair_legs("sports411", "4casters")
    assert legs == ("4casters", "sports411")


def test_sequential_enabled_for_exchange_first_pair_without_env_flag(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.SEQUENTIAL_ARB_BETTING", False)
    assert sequential_arb_betting_enabled("sports411", "4casters") is True
    assert sequential_arb_betting_enabled("sports411", "betamapola") is False


def test_should_defer_second_leg_until_first_confirmed():
    arb = {
        "team_1": "A",
        "team_2": "B",
        "team_1_bookmaker": "4casters",
        "team_2_bookmaker": "sports411",
    }
    cache = _FakeCache(leg_placed={"4casters": False})
    assert should_defer_for_sequential_first_leg(
        cache, arb, "4casters", "sports411", "sports411", "moneyline"
    ) is True

    cache = _FakeCache(leg_placed={"4casters": True})
    assert should_defer_for_sequential_first_leg(
        cache, arb, "4casters", "sports411", "sports411", "moneyline"
    ) is False


def test_resolve_arb_leg_stake_sizes_from_first_leg_fill():
    arb = {
        "team_1": "Mariners",
        "team_2": "Orioles",
        "team_1_bookmaker": "4casters",
        "team_2_bookmaker": "sports411",
    }
    summary = {
        "leg1": {
            "risk": 20.15,
            "to_win": 22.63,
            "odds": 112,
            "base_amount": 20.0,
        }
    }
    cache = _FakeCache(
        leg_placed={"4casters": True},
        summary={"arb_real_bets_summary:pair:test": summary},
    )
    stake = resolve_arb_leg_stake(
        cache, arb, "4casters", "sports411", "sports411", -117, 20.0
    )
    assert stake == 19.71

    cache_first = _FakeCache()
    assert resolve_arb_leg_stake(
        cache_first, arb, "4casters", "sports411", "4casters", 112, 20.0
    ) == 20.0
