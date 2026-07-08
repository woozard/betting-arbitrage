"""Tests for S411 exchange hedge pre-position helpers."""

from utils.bet_placement import (
    acknowledge_placed_leg,
    should_s411_exchange_hedge_preposition,
    sequential_arb_betting_enabled,
)


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.wake_calls = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl=None):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)

    def lpush(self, key, value):
        self.wake_calls.append((key, value))

    def scan(self, pattern):
        return [k for k in self.data if k.startswith(pattern.rstrip("*"))]


class _FakeCache:
    def __init__(self):
        self.redis = _FakeRedis()

    def arb_pair_key_from_arb(self, arb):
        return "pair:test"

    def event_date_for_arb(self, arb):
        return "2026-07-08"

    def mark_arb_leg_placed(self, arb, bookmaker, game_id):
        self.redis.set(f"leg:{bookmaker}", {"game_id": game_id})

    def lock_arb_scan(self, *args, **kwargs):
        pass

    def mark_game_pair_daily_bet(self, arb, bookmaker, game_id):
        pass

    def signal_bet_wake(self, bookmaker, payload=None):
        self.redis.lpush(f"arb:wake:{bookmaker}", payload or {})


def test_should_s411_exchange_hedge_preposition_for_4cast_pair(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.SEQUENTIAL_ARB_BETTING", False)
    monkeypatch.setattr(
        "utils.config.S411_EXCHANGE_HEDGE_PREPOSITION", True, raising=False
    )
    assert should_s411_exchange_hedge_preposition(
        "4casters", "sports411", "sports411", "moneyline"
    )
    assert not should_s411_exchange_hedge_preposition(
        "4casters", "sports411", "4casters", "moneyline"
    )
    assert not should_s411_exchange_hedge_preposition(
        "sports411", "betamapola", "sports411", "moneyline"
    )


def test_acknowledge_placed_leg_wakes_second_book_for_exchange_first(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.SEQUENTIAL_ARB_BETTING", False)
    cache = _FakeCache()
    arb = {
        "team_1": "A",
        "team_2": "B",
        "team_1_bookmaker": "4casters",
        "team_2_bookmaker": "sports411",
        "team_1_game_id": "1",
        "team_2_game_id": "2",
        "bet_type": "moneyline",
        "game_date": "2026-07-08",
    }

    class _Logger:
        def info(self, *args, **kwargs):
            pass

    acknowledge_placed_leg(cache, _Logger(), arb, "4casters", "1", team_name="A")
    assert any(k == "arb:wake:sports411" for k, _ in cache.redis.wake_calls)
