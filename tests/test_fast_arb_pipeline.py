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


def test_inline_scan_second_leg_maps_odds_to_correct_books():
    """When o1 is the updating book, the alternate arb path must not swap odds onto wrong books."""
    from decimal import Decimal

    from utils.fast_arb_scan import build_arb_data
    from utils.helpers import align_cross_book_moneylines, format_arb_opportunity_alert

    o1 = {
        "sport": "baseball",
        "league": "MLB",
        "bookmaker": "4casters",
        "game_id": "fc-tigers",
        "game_datetime": "2026-07-05 19:30:00",
        "team_1": "Detroit Tigers",
        "team_2": "Texas Rangers",
        "moneyline_team_1": -120,
        "moneyline_team_2": 117,
    }
    o2 = {
        "sport": "baseball",
        "league": "MLB",
        "bookmaker": "paradisewager",
        "game_id": "pw-tigers",
        "game_datetime": "2026-07-05 19:30:00",
        "team_1": "Detroit Tigers",
        "team_2": "Texas Rangers",
        "moneyline_team_1": -121,
        "moneyline_team_2": 110,
    }
    a_t1, a_t2, b_t1, b_t2 = align_cross_book_moneylines(o1, o2)

    # Alternate cross-book path: bet Tigers on paradise, Rangers on 4casters.
    leg_a, leg_b, tf_a, tf_b = b_t1, a_t2, "o2", "o1"
    team_1_odds = leg_a
    team_2_odds = leg_b
    arb = build_arb_data(
        o1,
        o2,
        tf_a,
        tf_b,
        Decimal("1.0083"),
        team_1_odds=team_1_odds,
        team_2_odds=team_2_odds,
    )

    assert arb["team_1_bookmaker"] == "paradisewager"
    assert arb["team_2_bookmaker"] == "4casters"
    assert arb["team_1_odds"] == -121
    assert arb["team_2_odds"] == 117

    alert = format_arb_opportunity_alert(arb)
    assert "Tigers -121 paradise" in alert
    assert "Rangers +117 4casters" in alert
    assert "Rangers -121" not in alert
    assert "Tigers +117" not in alert
