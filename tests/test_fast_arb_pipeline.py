from utils.betting_loop import wait_for_arb_or_idle


class _FakeRedis:
    def __init__(self):
        self.pushed = []

    def lpush(self, key, value):
        self.pushed.append((key, value))

    def blpop(self, keys, timeout=0):
        return None


class _FakeCache:
    def __init__(self):
        self.redis = _FakeRedis()
        self.wake_calls = 0

    def wait_bet_wake(self, bookmaker, timeout_ms=None):
        self.wake_calls += 1
        return None


def test_wait_for_arb_or_idle_runs_idle_poll_on_interval():
    cache = _FakeCache()
    calls = {"n": 0}

    def poll():
        calls["n"] += 1

    woke, ts1 = wait_for_arb_or_idle(
        cache, "betamapola", idle_poll_fn=poll, idle_poll_interval=0.0, last_idle_poll_at=0.0
    )
    assert woke is False
    assert calls["n"] == 1
    assert ts1 > 0


def test_arbitrage_cache_wake_keys():
    from cache.arbitrage_cache import ArbitrageCache

    class MemRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, ex=None):
            self.data[key] = value

        def lpush(self, key, value):
            self.data.setdefault(key, [])
            self.data[key].insert(0, value)

        def blpop(self, keys, timeout=0):
            return None

        def get(self, key):
            return self.data.get(key)

        def delete(self, key):
            self.data.pop(key, None)

        def scan_iter(self, match):
            return []

        def scan(self, pattern):
            return []

        def pipeline(self):
            return self

        def execute(self):
            return []

    cache = ArbitrageCache()
    cache.redis = MemRedis()
    arb = {
        "team_1": "A",
        "team_2": "B",
        "team_1_bookmaker": "betamapola",
        "team_2_bookmaker": "paradisewager",
        "team_1_game_id": "1-2",
        "team_2_game_id": "1-2",
        "bet_type": "moneyline",
        "game_date": "2026-07-04",
    }
    cache.add_arbitrage("betamapola", "moneyline", "1-2", arb)
    assert "arb:wake:betamapola" in cache.redis.data
    assert "arb:wake:paradisewager" in cache.redis.data
