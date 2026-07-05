"""Tests for pair-scoped arb leg placement flags."""
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


def _brewers_arb(second_book: str) -> dict:
    return {
        "sport": "baseball",
        "league": "mlb",
        "game_date": "2026-07-05",
        "game_datetime": "2026-07-05 23:10:00",
        "team_1": "Arizona Diamondbacks",
        "team_2": "Milwaukee Brewers",
        "team_1_bookmaker": second_book,
        "team_2_bookmaker": "4casters",
        "team_1_game_id": "909-910",
        "team_2_game_id": "6a492e188d580dafd5953b39",
        "team_1_odds": 141,
        "team_2_odds": -116,
        "bet_type": "moneyline",
        "identified_at": 1.0,
    }


def test_leg_placed_is_scoped_to_pair_not_global_book_game():
    cache = ArbitrageCache()
    cache.redis = MemRedis()

    arb_betwar = _brewers_arb("betwar")
    arb_paradise = _brewers_arb("paradisewager")

    cache.mark_arb_leg_placed(arb_betwar, "4casters")

    assert cache.is_arb_leg_placed(arb_betwar, "4casters") is True
    assert cache.is_arb_leg_placed(arb_paradise, "4casters") is False

    skip, reason = cache.should_skip_arb_leg_placement(arb_paradise, "4casters")
    assert skip is False
    assert reason == ""


def test_other_pair_partial_blocks_same_book_game():
    cache = ArbitrageCache()
    cache.redis = MemRedis()

    arb_betwar = _brewers_arb("betwar")
    arb_paradise = _brewers_arb("paradisewager")

    cache.mark_arb_leg_placed(arb_betwar, "4casters")
    cache.mark_partial_exposure(cache.arb_pair_key_from_arb(arb_betwar))

    skip, reason = cache.should_skip_arb_leg_placement(arb_paradise, "4casters")
    assert skip is True
    assert "other pair has partial exposure" in reason


def test_clear_arb_pair_legs_allows_new_pair_after_stale_exposure():
    cache = ArbitrageCache()
    cache.redis = MemRedis()

    arb_betwar = _brewers_arb("betwar")
    arb_paradise = _brewers_arb("paradisewager")
    pair_key = cache.arb_pair_key_from_arb(arb_betwar)

    cache.mark_arb_leg_placed(arb_betwar, "4casters")
    cache.mark_partial_exposure(pair_key)
    cache.clear_partial_exposure(pair_key)
    cache.clear_arb_legs_for_pair_key(pair_key)

    assert cache.is_arb_leg_placed(arb_betwar, "4casters") is False
    skip, _ = cache.should_skip_arb_leg_placement(arb_paradise, "4casters")
    assert skip is False


def test_purge_legacy_leg_placed_keys():
    cache = ArbitrageCache()
    cache.redis = MemRedis()

    cache.mark_leg_placed("4casters", "moneyline", "game-1")
    assert cache.is_leg_placed("4casters", "moneyline", "game-1") is True

    removed = cache.purge_legacy_leg_placed_keys()
    assert removed == 1
    assert cache.is_leg_placed("4casters", "moneyline", "game-1") is False


def test_is_arb_leg_placed_does_not_read_legacy_global_key():
    cache = ArbitrageCache()
    cache.redis = MemRedis()

    arb = _brewers_arb("paradisewager")
    cache.mark_leg_placed("4casters", "moneyline", arb["team_2_game_id"])

    assert cache.is_leg_placed("4casters", "moneyline", arb["team_2_game_id"]) is True
    assert cache.is_arb_leg_placed(arb, "4casters") is False
