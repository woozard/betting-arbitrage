from utils.bet_placement import (
    mark_arb_execution_pause_if_first_leg,
    may_continue_arb_during_execution_pause,
    should_pause_for_arb_execution_cooldown,
    wait_for_arb_execution_pause_clear,
)


class _FakeRedis:
    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ttl=None):
        self._data[key] = value


class _FakeCache:
    def __init__(self, *, paused=False, leg_placed=None, partial_exposure=False, pause_pair_key=None):
        pause_meta = {"started_at": 1.0}
        if pause_pair_key:
            pause_meta["pair_key"] = pause_pair_key
        self.redis = _FakeRedis(
            {"arb_execution_pause": pause_meta} if paused else {}
        )
        self._leg_placed = leg_placed or {}
        self._partial_exposure = partial_exposure

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

    def has_partial_exposure_for_pair(self, pair_key):
        return self._partial_exposure

    def get_arb_execution_pause_meta(self):
        return self.redis.get("arb_execution_pause")


def _arb():
    return {
        "team_1": "Colorado Rockies",
        "team_2": "San Francisco Giants",
        "team_1_bookmaker": "sports411",
        "team_2_bookmaker": "4casters",
    }


def test_mark_pause_only_for_first_leg_book(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.PARALLEL_EXCHANGE_ARB_BETTING", False)
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


def test_pause_blocks_new_first_leg(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.PARALLEL_EXCHANGE_ARB_BETTING", False)
    cache = _FakeCache(paused=True)
    arb = _arb()
    assert should_pause_for_arb_execution_cooldown(
        cache, arb, "sports411", "4casters", "4casters", "moneyline"
    )


def test_pause_allows_second_leg_after_first_confirmed(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.PARALLEL_EXCHANGE_ARB_BETTING", False)
    cache = _FakeCache(paused=True, leg_placed={"4casters": True})
    arb = _arb()
    assert not should_pause_for_arb_execution_cooldown(
        cache, arb, "sports411", "4casters", "sports411", "moneyline"
    )
    assert may_continue_arb_during_execution_pause(
        cache, arb, "sports411", "4casters", "sports411", "moneyline"
    )


def test_pause_allows_second_leg_for_in_flight_pair_before_ack(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.PARALLEL_EXCHANGE_ARB_BETTING", False)
    cache = _FakeCache(paused=True, pause_pair_key="pair:test")
    arb = _arb()
    assert not should_pause_for_arb_execution_cooldown(
        cache, arb, "sports411", "4casters", "sports411", "moneyline"
    )


def test_pause_allows_second_leg_when_partial_exposure_exists(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.PARALLEL_EXCHANGE_ARB_BETTING", False)
    cache = _FakeCache(paused=True, partial_exposure=True)
    arb = _arb()
    assert not should_pause_for_arb_execution_cooldown(
        cache, arb, "sports411", "4casters", "sports411", "moneyline"
    )


def test_wait_for_execution_pause_clear_returns_false_when_not_paused():
    cache = _FakeCache()
    assert wait_for_arb_execution_pause_clear(cache) is False


def test_wait_for_execution_pause_clear_blocks_until_cleared(monkeypatch):
    cache = _FakeCache(paused=True, pause_pair_key="pair:test")
    calls = {"n": 0}

    def _paused():
        calls["n"] += 1
        if calls["n"] >= 2:
            cache.redis._data.pop("arb_execution_pause", None)
        return bool(cache.redis.get("arb_execution_pause"))

    monkeypatch.setattr(cache, "is_arb_execution_paused", _paused)
    monkeypatch.setattr("utils.bet_placement.time.sleep", lambda _s: None)
    assert wait_for_arb_execution_pause_clear(cache) is True
    assert calls["n"] >= 2
