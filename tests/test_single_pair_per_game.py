"""Tests for one-pair-per-game matchup ownership."""
import sys
import types

sys.modules.setdefault("redis", types.ModuleType("redis"))

from cache.arbitrage_cache import ArbitrageCache


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


def _braves_arb(second_book: str) -> dict:
    return {
        "game_date": "2026-07-07",
        "game_datetime": "2026-07-07 23:15:00",
        "team_1": "Atlanta Braves",
        "team_2": "Pittsburgh Pirates",
        "team_1_bookmaker": "betamapola",
        "team_2_bookmaker": second_book,
        "team_1_game_id": "953-954",
        "team_2_game_id": "game-2",
        "bet_type": "moneyline",
        "identified_at": 1.0,
    }


def test_second_pair_blocked_after_first_pair_claims_game(monkeypatch):
    monkeypatch.setenv("SINGLE_PAIR_PER_GAME", "true")
    import utils.config as config_mod

    monkeypatch.setattr(config_mod, "SINGLE_PAIR_PER_GAME", True)

    cache = ArbitrageCache()
    cache.redis = MemRedis()

    arb_amapola = _braves_arb("4casters")
    arb_s411 = {
        **_braves_arb("sports411"),
        "team_1_bookmaker": "4casters",
        "team_2_bookmaker": "sports411",
        "team_2_game_id": "52459127",
    }

    cache.mark_arb_leg_placed(arb_amapola, "betamapola")

    owns, reason = cache.other_pair_owns_game_event(arb_s411)
    assert owns is True
    assert "another pair owns this game" in reason

    skip, skip_reason = cache.should_skip_arb_leg_placement(arb_s411, "sports411")
    assert skip is True
    assert "another pair owns" in skip_reason


def test_owner_cleared_after_complete(monkeypatch):
    monkeypatch.setenv("SINGLE_PAIR_PER_GAME", "true")
    import utils.config as config_mod

    monkeypatch.setattr(config_mod, "SINGLE_PAIR_PER_GAME", True)

    cache = ArbitrageCache()
    cache.redis = MemRedis()

    arb = _braves_arb("4casters")
    cache.mark_arb_leg_placed(arb, "betamapola")
    cache.mark_arb_leg_placed(arb, "4casters")
    cache.clear_game_event_owner(arb)

    owns, _ = cache.other_pair_owns_game_event(arb)
    assert owns is False
