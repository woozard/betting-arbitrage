from utils.bet_placement import (
    mark_arb_execution_pause_if_first_leg,
    may_continue_arb_during_execution_pause,
    should_pause_for_arb_execution_cooldown,
)


class _FakeRedis:
    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ttl=None):
        self._data[key] = value


class _FakeCache:
    def __init__(self, *, paused=False, leg_placed=None):
        self.redis = _FakeRedis(
            {"arb_execution_pause": {"started_at": 1.0}} if paused else {}
        )
        self._leg_placed = leg_placed or {}

    def is_arb_execution_paused(self):
        return bool(self.redis.get("arb_execution_pause"))

    def mark_arb_execution_pause(self, arb=None):
        self.redis.set("arb_execution_pause", {"started_at": 1.0, "arb": arb})

    def arb_execution_pause_remaining_seconds(self):
        return 250.0

    def arb_pair_key_from_arb(self, arb):
        return "pair:test"

    def is_arb_leg_placed(self, arb, bookmaker):
        return self._leg_placed.get((bookmaker or "").lower(), False)


def _arb():
    return {
        "team_1": "Colorado Rockies",
        "team_2": "San Francisco Giants",
        "team_1_bookmaker": "sports411",
        "team_2_bookmaker": "4casters",
    }


def test_mark_pause_only_for_first_leg_book():
    cache = _FakeCache()
    mark_arb_execution_pause_if_first_leg(
        cache, _arb(), "sports411", "4casters", "4casters"
    )
    assert cache.is_arb_execution_paused()

    cache2 = _FakeCache()
    mark_arb_execution_pause_if_first_leg(
        cache2, _arb(), "sports411", "4casters", "sports411"
    )
    assert not cache2.is_arb_execution_paused()


def test_pause_blocks_new_first_leg():
    cache = _FakeCache(paused=True)
    arb = _arb()
    assert should_pause_for_arb_execution_cooldown(
        cache, arb, "sports411", "4casters", "4casters", "moneyline"
    )


def test_pause_allows_second_leg_after_first_confirmed():
    cache = _FakeCache(paused=True, leg_placed={"4casters": True})
    arb = _arb()
    assert not should_pause_for_arb_execution_cooldown(
        cache, arb, "sports411", "4casters", "sports411", "moneyline"
    )
    assert may_continue_arb_during_execution_pause(
        cache, arb, "sports411", "4casters", "sports411", "moneyline"
    )
