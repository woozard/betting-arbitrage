import time

from cache.redis_cache import RedisCache
from utils.config import REDIS, ARB_TTL_SECONDS


class ArbitrageCache:
    def __init__(self, ttl=30, arb_ttl=None, lock_ttl=86400):
        self.redis = RedisCache(REDIS['host'], REDIS['port'])
        self.ttl = ttl
        self.arb_ttl = ARB_TTL_SECONDS if arb_ttl is None else arb_ttl
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

    def _arb_opportunity_alert_key(self, pair_key):
        return f"arb_opportunity_alert_sent:{pair_key}"

    def _bet_confirmed_alert_key(self, bookmaker, bet_type, game_id):
        return f"bet_confirmed_alert_sent:{bookmaker}:{bet_type}:{game_id}"

    def is_arb_scan_locked(self, team_1, team_2, book_1, book_2, game_date=None):
        """Stop surfacing NEW arb alerts for this event + bookmaker pair."""
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        return bool(self.redis.get(self._arb_scan_locked_key(pair_key)))

    def lock_arb_scan(self, team_1, team_2, book_1, book_2, game_date=None):
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        self.redis.set(self._arb_scan_locked_key(pair_key), {"locked_at": "now"}, ttl=self.lock_ttl)

    def unlock_arb_scan(self, team_1, team_2, book_1, book_2, game_date=None):
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        self.redis.delete(self._arb_scan_locked_key(pair_key))

    def clear_leg_placed(self, bookmaker, bet_type, game_id):
        self.redis.delete(self._leg_placed_key(bookmaker, bet_type, game_id))

    def is_leg_placed(self, bookmaker, bet_type, game_id):
        """This bookmaker already has a confirmed leg for this game."""
        return bool(self.redis.get(self._leg_placed_key(bookmaker, bet_type, game_id)))

    def mark_leg_placed(self, bookmaker, bet_type, game_id):
        self.redis.set(
            self._leg_placed_key(bookmaker, bet_type, game_id),
            {"placed_at": "now"},
            ttl=self.lock_ttl,
        )

    def arb_opportunity_alert_already_sent(self, team_1, team_2, book_1, book_2, game_date=None):
        """Scanner ===== Arbitrage ===== Telegram dedup (per event + book pair per day)."""
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        return bool(self.redis.get(self._arb_opportunity_alert_key(pair_key)))

    def mark_arb_opportunity_alert_sent(self, team_1, team_2, book_1, book_2, game_date=None):
        pair_key = self.matchup_pair_key(team_1, team_2, book_1, book_2, game_date)
        self.redis.set(self._arb_opportunity_alert_key(pair_key), {"sent_at": "now"}, ttl=self.lock_ttl)

    def bet_confirmed_alert_already_sent(self, bookmaker, bet_type, game_id):
        """===== Moneyline Bet ===== Telegram dedup (per confirmed leg)."""
        return bool(self.redis.get(self._bet_confirmed_alert_key(bookmaker, bet_type, game_id)))

    def mark_bet_confirmed_alert_sent(self, bookmaker, bet_type, game_id):
        self.redis.set(
            self._bet_confirmed_alert_key(bookmaker, bet_type, game_id),
            {"sent_at": "now"},
            ttl=self.lock_ttl,
        )

    def _arb_complete_alert_key(self, pair_key):
        return f"arb_complete_alert_sent:{pair_key}"

    def arb_complete_alert_already_sent(self, pair_key):
        return bool(self.redis.get(self._arb_complete_alert_key(pair_key)))

    def mark_arb_complete_alert_sent(self, pair_key):
        self.redis.set(
            self._arb_complete_alert_key(pair_key),
            {"sent_at": "now"},
            ttl=self.lock_ttl,
        )

    def _partial_arb_alert_key(self, pair_key):
        return f"partial_arb_alert_sent:{pair_key}"

    def partial_arb_alert_already_sent(self, pair_key):
        return bool(self.redis.get(self._partial_arb_alert_key(pair_key)))

    def mark_partial_arb_alert_sent(self, pair_key):
        self.redis.set(
            self._partial_arb_alert_key(pair_key),
            {"sent_at": "now"},
            ttl=self.lock_ttl,
        )

    def _partial_exposure_key(self, pair_key):
        return f"partial_exposure:{pair_key}"

    def mark_partial_exposure(self, pair_key):
        self.redis.set(
            self._partial_exposure_key(pair_key),
            {"marked_at": time.time()},
            ttl=self.lock_ttl,
        )

    def clear_partial_exposure(self, pair_key):
        self.redis.delete(self._partial_exposure_key(pair_key))

    def has_partial_exposure(self):
        return bool(self.redis.scan("partial_exposure:*"))

    def remove_arbitrage_for_bookmaker(self, arb_data, bookmaker):
        """Remove only the confirming bookmaker's cache entry; keep the other leg actionable."""
        bet_type = arb_data.get("bet_type", "moneyline")
        if bookmaker == arb_data.get("team_1_bookmaker"):
            game_id = arb_data["team_1_game_id"]
        else:
            game_id = arb_data["team_2_game_id"]
        self.remove_arbitrage(bookmaker, bet_type, game_id)

    def remove_arbitrage_pair(self, arb_data):
        """Remove both bookmaker cache entries for a two-legged arb."""
        bet_type = arb_data.get("bet_type", "moneyline")
        self.remove_arbitrage(
            arb_data["team_1_bookmaker"], bet_type, arb_data["team_1_game_id"]
        )
        self.remove_arbitrage(
            arb_data["team_2_bookmaker"], bet_type, arb_data["team_2_game_id"]
        )

    def arb_age_seconds(self, arb_data):
        identified_at = arb_data.get("identified_at")
        if identified_at is None:
            return 0
        return max(0, time.time() - float(identified_at))

    def is_arb_stale(self, arb_data):
        """True when the arb has been actionable longer than arb_ttl (default 180s)."""
        return self.arb_age_seconds(arb_data) > self.arb_ttl