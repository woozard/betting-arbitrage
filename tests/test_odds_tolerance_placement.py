"""Point tolerances removed — hedges use profit-floor acceptance."""
import sys
import types

sys.modules.setdefault("redis", types.ModuleType("redis"))

from cache.arbitrage_cache import ArbitrageCache
from utils.bet_placement import (
    configure_leg_odds_policy,
    hedge_completion_reference_odds,
    odds_tolerance_for_placement,
)
from utils.moneyline_odds import hedge_line_acceptable


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


def test_point_tolerance_always_zero_for_spread_and_ml():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = _rockies_spread_arb()
    cache.mark_arb_leg_placed(arb, "4casters")

    assert (
        odds_tolerance_for_placement(
            cache, arb, "betamapola", "4casters", "betamapola", "spread"
        )
        == 0
    )
    arb_ml = dict(arb)
    arb_ml["bet_type"] = "moneyline"
    arb_ml.pop("spread_value", None)
    assert (
        odds_tolerance_for_placement(
            cache, arb_ml, "betamapola", "4casters", "betamapola", "moneyline"
        )
        == 0
    )


def test_spread_hedge_uses_profit_acceptance():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = _rockies_spread_arb()
    cache.mark_arb_leg_placed(arb, "4casters")

    ref = hedge_completion_reference_odds(
        cache, arb, "betamapola", "4casters", "betamapola", "spread"
    )
    assert int(float(ref)) == -117

    ref2, tol = configure_leg_odds_policy(
        cache, arb, "betamapola", "4casters", "betamapola", "spread"
    )
    assert tol == 0
    assert int(float(ref2)) == -117
    # Still-profitable juice move vs other-leg -117
    assert hedge_line_acceptable(ref2, 110, min_profit_pct=0)


def test_moneyline_first_leg_no_hedge_ref():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = _rockies_spread_arb()
    arb["bet_type"] = "moneyline"
    arb.pop("spread_value", None)

    ref, tol = configure_leg_odds_policy(
        cache, arb, "betamapola", "4casters", "4casters", "moneyline"
    )
    assert ref is None
    assert tol == 0
