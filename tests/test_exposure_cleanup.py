"""Tests for timezone-aware partial-exposure cleanup."""
import logging
import sys
import time
import types
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

sys.modules.setdefault("redis", types.ModuleType("redis"))

from cache.arbitrage_cache import ArbitrageCache
from utils.exposure_cleanup import _game_still_actionable, cleanup_stale_partial_exposure


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


def test_parse_spread_pair_key():
    key = (
        "2026-07-06:colorado rockies:los angeles dodgers:"
        "4casters:betamapola:spread:1.5"
    )
    parsed = ArbitrageCache.parse_matchup_pair_key(key)
    assert parsed["game_date"] == "2026-07-06"
    assert parsed["book_1"] == "4casters"
    assert parsed["book_2"] == "betamapola"
    assert parsed["bet_type"] == "spread"
    assert parsed["spread_value"] == "1.5"


def test_game_still_actionable_uses_game_datetime():
    future = (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    parsed = {"game_date": "2026-07-06"}
    assert _game_still_actionable(parsed, {"game_datetime": future}) is True


def test_game_still_actionable_eastern_slate_not_cleared_at_utc_midnight():
    parsed = {"game_date": "2026-07-06"}
    eastern = pytz.timezone("America/New_York")
    # Jul 7 02:01 UTC = Jul 6 22:01 EDT — still same Eastern slate day.
    fake_now = eastern.localize(datetime(2026, 7, 6, 22, 1, 0))
    with patch("utils.exposure_cleanup.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.strptime = datetime.strptime
        assert _game_still_actionable(parsed, {}) is True


def test_cleanup_keeps_pregame_exposure_without_game_datetime():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    pair_key = (
        "2026-07-06:colorado rockies:los angeles dodgers:"
        "4casters:betamapola:spread:1.5"
    )
    cache.mark_partial_exposure(pair_key)
    logger = logging.getLogger("test_exposure_cleanup")

    eastern = pytz.timezone("America/New_York")
    fake_now = eastern.localize(datetime(2026, 7, 6, 22, 1, 0))
    with patch("utils.exposure_cleanup.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.strptime = datetime.strptime
        cleared = cleanup_stale_partial_exposure(cache, logger, max_age_seconds=3600)

    assert cleared == 0
    assert cache.has_partial_exposure_for_pair(pair_key)


def test_cleanup_clears_after_game_start():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    pair_key = (
        "2026-07-06:colorado rockies:los angeles dodgers:"
        "4casters:betamapola:spread:1.5"
    )
    past_start = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    cache.mark_partial_exposure(pair_key, game_datetime=past_start)
    logger = logging.getLogger("test_exposure_cleanup")

    cleared = cleanup_stale_partial_exposure(cache, logger, max_age_seconds=3600)
    assert cleared == 1
    assert not cache.has_partial_exposure_for_pair(pair_key)


def test_cleanup_keeps_pregame_exposure_past_max_age():
    cache = ArbitrageCache()
    cache.redis = MemRedis()
    pair_key = (
        "2026-07-06:colorado rockies:los angeles dodgers:"
        "4casters:betamapola:spread:1.5"
    )
    future = (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    cache.redis.set(
        cache._partial_exposure_key(pair_key),
        {"marked_at": time.time() - 7200, "game_datetime": future},
    )
    logger = logging.getLogger("test_exposure_cleanup")

    cleared = cleanup_stale_partial_exposure(cache, logger, max_age_seconds=3600)
    assert cleared == 0
    assert cache.has_partial_exposure_for_pair(pair_key)
