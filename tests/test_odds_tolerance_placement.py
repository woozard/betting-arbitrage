"""Tests for system-wide second-leg / hedge-completion odds tolerance."""
import sys
import types

sys.modules.setdefault("redis", types.ModuleType("redis"))

from cache.arbitrage_cache import ArbitrageCache
from utils.bet_placement import odds_tolerance_for_placement


class MemRedis:
    def __init__(self):
        self.data = {}

    def set(self, key, value, ex=None, ttl=None):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)

    def scan(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.data if k.startswith(prefix)]

    def scan_iter(self, match):
        return iter(self.scan(match))

    def pipeline(self):
        return self

    def execute(self):
        return []

    def lpush(self, key, value):
        pass

    def blpop(self, keys, timeout=0):
        return None


def _rockies_spread_arb() -> dict:
    return {
        "sport": "baseball",
        "league": "mlb",
        "game_date": "2026-07-06",
        "game_datetime": "2026-07-07 02:10:00",
        "team_1": "Colorado Rockies",
        "team_2": "Los Angeles Dodgers",
        "team_1_bookmaker": "betamapola",
        "team_2_bookmaker": "4casters",
        "team_1_game_id": "901-902",
        "team_2_game_id": "6a4beb",
        "team_1_odds": 123,
        "team_2_odds": -117,
        "bet_type": "spread",
        "spread_value": 1.5,
        "identified_at": 1.0,
    }


def test_spread_second_leg_book_always_gets_tolerance():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = _rockies_spread_arb()

    tol = odds_tolerance_for_placement(
        cache, arb, "betamapola", "4casters", "4casters", "spread"
    )
    assert tol == 5


def test_spread_first_leg_gets_tolerance_when_partial_exposure():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = _rockies_spread_arb()
    pair_key = cache.arb_pair_key_from_arb(arb)
    cache.mark_partial_exposure(pair_key)

    tol = odds_tolerance_for_placement(
        cache, arb, "betamapola", "4casters", "betamapola", "spread"
    )
    assert tol == 5


def test_spread_first_leg_gets_tolerance_when_other_leg_placed():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = _rockies_spread_arb()
    cache.mark_arb_leg_placed(arb, "4casters")

    tol = odds_tolerance_for_placement(
        cache, arb, "betamapola", "4casters", "betamapola", "spread"
    )
    assert tol == 5


def test_moneyline_first_leg_no_tolerance_without_hedge():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = _rockies_spread_arb()
    arb["bet_type"] = "moneyline"
    arb.pop("spread_value", None)

    tol = odds_tolerance_for_placement(
        cache, arb, "betamapola", "4casters", "betamapola", "moneyline"
    )
    assert tol == 0


def test_moneyline_first_leg_gets_tolerance_when_other_leg_placed():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = _rockies_spread_arb()
    arb["bet_type"] = "moneyline"
    arb.pop("spread_value", None)
    cache.mark_arb_leg_placed(arb, "4casters")

    tol = odds_tolerance_for_placement(
        cache, arb, "betamapola", "4casters", "betamapola", "moneyline"
    )
    assert tol == 2
