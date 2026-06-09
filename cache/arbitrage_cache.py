from cache.redis_cache import RedisCache
from utils.config import REDIS


class ArbitrageCache:
    def __init__(self, ttl=30, arb_ttl=180, lock_ttl=86400):
        self.redis = RedisCache(REDIS['host'], REDIS['port'])
        self.ttl = ttl
        self.arb_ttl = arb_ttl
        self.lock_ttl = lock_ttl

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
        self.redis.set(key, arb_data, ttl=self.arb_ttl)

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

    @staticmethod
    def matchup_pair_key(team_1, team_2, book_1, book_2, game_date=None):
        teams = tuple(sorted([(team_1 or "").strip().lower(), (team_2 or "").strip().lower()]))
        books = tuple(sorted([(book_1 or "").strip().lower(), (book_2 or "").strip().lower()]))
        date = str(game_date or "")[:10]
        return f"{date}:{teams[0]}:{teams[1]}:{books[0]}:{books[1]}"

    def _arb_scan_locked_key(self, pair_key):
        return f"arb_scan_locked:{pair_key}"

    def _leg_placed_key(self, bookmaker, bet_type, game_id):
        return f"leg_placed:{bookmaker}:{bet_type}:{game_id}"

    def _moneyline_alert_key(self, pair_key):
        return f"moneyline_alert_sent:{pair_key}"

    def is_arb_scan_locked(self, team_1, team_2, book_1, book_2, game_date=None):
        """Stop surfacing NEW arb alerts for this event + bookmaker pair."""
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        return bool(self.redis.get(self._arb_scan_locked_key(pair_key)))

    def lock_arb_scan(self, team_1, team_2, book_1, book_2, game_date=None):
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        self.redis.set(self._arb_scan_locked_key(pair_key), {"locked_at": "now"}, ttl=self.lock_ttl)

    def is_leg_placed(self, bookmaker, bet_type, game_id):
        """This bookmaker already has a confirmed leg for this game."""
        return bool(self.redis.get(self._leg_placed_key(bookmaker, bet_type, game_id)))

    def mark_leg_placed(self, bookmaker, bet_type, game_id):
        self.redis.set(
            self._leg_placed_key(bookmaker, bet_type, game_id),
            {"placed_at": "now"},
            ttl=self.lock_ttl,
        )

    def moneyline_alert_already_sent(self, team_1, team_2, book_1, book_2, game_date=None):
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        return bool(self.redis.get(self._moneyline_alert_key(pair_key)))

    def mark_moneyline_alert_sent(self, team_1, team_2, book_1, book_2, game_date=None):
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        self.redis.set(self._moneyline_alert_key(pair_key), {"sent_at": "now"}, ttl=self.lock_ttl)

    def remove_arbitrage_for_bookmaker(self, arb_data, bookmaker):
        """Remove only the confirming bookmaker's cache entry; keep the other leg actionable."""
        bet_type = arb_data.get("bet_type", "moneyline")
        if bookmaker == arb_data.get("team_1_bookmaker"):
            game_id = arb_data["team_1_game_id"]
        else:
            game_id = arb_data["team_2_game_id"]
        self.remove_arbitrage(bookmaker, bet_type, game_id)