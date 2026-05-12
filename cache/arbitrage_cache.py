from cache.redis_cache import RedisCache
from utils.config import REDIS


class ArbitrageCache:
    def __init__(self, ttl=30):
        self.redis = RedisCache(REDIS['host'], REDIS['port'])
        self.ttl = ttl

    # ---------------- Key format ----------------
    def _odd_key(self, row):
        return f"odds:{row['bookmaker']}:{row['bet_type']}:{row['game_id']}"

    def _arb_key(self, bookmaker, bet_type, game_id):
        return f"arbitrage:{bookmaker}:{bet_type}:{game_id}"

    # ---------------- Add / Update Odds ----------------
    def add_odds(self, row):
        key = self._odd_key(row)
        existing = self.redis.get(key) or []

        unique_key = (
            row.get("moneyline_key")
            or row.get("spread_key")
            or row.get("total_key")
        )

        filtered = [
            r for r in existing
            if (
                r.get("moneyline_key") != unique_key and
                r.get("spread_key") != unique_key and
                r.get("total_key") != unique_key
            )
        ]

        filtered.append(row)
        self.redis.set(key, filtered, ttl=self.ttl)

    # ---------------- Bulk Add ----------------
    def add_many(self, rows):
        grouped = {}

        for row in rows:
            key = self._odd_key(row)
            grouped.setdefault(key, []).append(row)

        pipe = self.redis.pipeline()

        for key, new_rows in grouped.items():
            existing = self.redis.get(key) or []
            combined = existing + new_rows

            seen = set()
            result = []

            for r in combined:
                unique_key = (
                    r.get("moneyline_key")
                    or r.get("spread_key")
                    or r.get("total_key")
                )

                if unique_key not in seen:
                    seen.add(unique_key)
                    result.append(r)

            # ✅ No json.dumps
            pipe.set(key, result, ex=self.ttl)

        pipe.execute()

    # ---------------- Get Odds ----------------
    def get_odds(self, bookmaker=None, bet_type=None, game_id=None):
        b = bookmaker or "*"
        t = bet_type or "*"
        g = game_id or "*"

        pattern = f"odds:{b}:{t}:{g}"
        keys = self.redis.scan(pattern)

        results = []
        for key in keys:
            data = self.redis.get(key)
            if isinstance(data, list):
                results.extend(data)

        return results

    # ---------------- Arbitrage Methods ----------------
    def add_arbitrage(self, bookmaker, bet_type, game_id, arb_data):
        key = self._arb_key(bookmaker, bet_type, game_id)

        # ✅ No json.dumps
        self.redis.set(key, arb_data, ttl=self.ttl)

    def get_arbitrage(self, bookmaker=None, bet_type=None, game_id=None):
        b = bookmaker or "*"
        t = bet_type or "*"
        g = game_id or "*"

        pattern = f"arbitrage:{b}:{t}:{g}"
        keys = self.redis.scan(pattern)

        results = []
        for key in keys:
            data = self.redis.get(key)

            # ✅ Ensure dict (avoid broken cache entries)
            if isinstance(data, dict):
                results.append(data)

        return results

    def remove_arbitrage(self, bookmaker, bet_type, game_id):
        key = self._arb_key(bookmaker, bet_type, game_id)
        self.redis.delete(key)