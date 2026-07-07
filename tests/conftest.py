"""Pytest fixtures and stubs (no real Redis required in unit tests)."""
import os
import sys
import types

# Pair-scoped leg tests expect multi-pair behavior unless a test opts in.
os.environ.setdefault("SINGLE_PAIR_PER_GAME", "false")


class _FakeRedisClient:
    def __init__(self, **kwargs):
        pass

    def set(self, key, value, ex=None):
        return True

    def get(self, key):
        return None

    def delete(self, key):
        return 0

    def scan_iter(self, match=None):
        return iter([])

    def pipeline(self):
        return self

    def execute(self):
        return []

    def lpush(self, key, value):
        return 1

    def blpop(self, keys, timeout=0):
        return None


_redis_mod = types.ModuleType("redis")
_redis_mod.Redis = _FakeRedisClient
sys.modules["redis"] = _redis_mod
