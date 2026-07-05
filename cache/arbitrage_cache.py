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

        stamped = dict(row)
        stamped["updated_at"] = time.time()
        filtered.append(stamped)
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
        self.signal_bet_wake_for_arb(arb_data)

    def _bet_wake_key(self, bookmaker: str) -> str:
        return f"arb:wake:{(bookmaker or '').strip().lower()}"

    def signal_bet_wake(self, bookmaker: str, payload: dict | None = None):
        """Wake a book's betting loop immediately (LPUSH wake queue)."""
        bm = (bookmaker or "").strip().lower()
        if not bm:
            return
        body = payload or {"ts": time.time()}
        self.redis.lpush(self._bet_wake_key(bm), body)

    def signal_bet_wake_for_arb(self, arb_data: dict):
        """Wake both legs as soon as an arb is actionable in Redis."""
        if not isinstance(arb_data, dict):
            return
        payload = {
            "ts": time.time(),
            "pair_key": self.arb_pair_key_from_arb(arb_data),
            "bet_type": arb_data.get("bet_type", "moneyline"),
        }
        for book in (
            arb_data.get("team_1_bookmaker"),
            arb_data.get("team_2_bookmaker"),
        ):
            self.signal_bet_wake(book, payload)

    def wait_bet_wake(self, bookmaker: str, timeout_ms: int | None = None) -> dict | None:
        """Block up to timeout_ms for a bet wake signal; returns payload or None."""
        from utils.config import BET_WAKE_BLPOP_MS

        bm = (bookmaker or "").strip().lower()
        if not bm:
            return None
        ms = BET_WAKE_BLPOP_MS if timeout_ms is None else timeout_ms
        timeout_sec = max(0.001, ms / 1000.0)
        result = self.redis.blpop(self._bet_wake_key(bm), timeout=timeout_sec)
        if not result:
            return None
        _, payload = result
        return payload if isinstance(payload, dict) else {"raw": payload}

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
    def event_date_for_arb(arb: dict) -> str:
        """Slate date for pair keys (Eastern for MLB), stable across UTC midnight."""
        from utils.helpers import parse_game_datetime

        gdt_raw = (arb or {}).get("game_datetime")
        if gdt_raw:
            gdt = parse_game_datetime(gdt_raw)
            if gdt:
                import pytz

                eastern = pytz.timezone("America/New_York")
                utc = pytz.utc.localize(gdt)
                return utc.astimezone(eastern).strftime("%Y-%m-%d")
        gd = (arb or {}).get("game_date")
        return str(gd)[:10] if gd else ""

    @staticmethod
    def arb_pair_key_from_arb(arb: dict) -> str:
        bet_type = ((arb or {}).get("bet_type") or "moneyline").strip().lower()
        spread_value = (arb or {}).get("spread_value") if bet_type == "spread" else None
        return ArbitrageCache.matchup_pair_key(
            (arb or {}).get("team_1"),
            (arb or {}).get("team_2"),
            (arb or {}).get("team_1_bookmaker"),
            (arb or {}).get("team_2_bookmaker"),
            ArbitrageCache.event_date_for_arb(arb),
            bet_type=bet_type,
            spread_value=spread_value,
        )

    @staticmethod
    def matchup_pair_key(team_1, team_2, book_1, book_2, game_date=None, bet_type=None, spread_value=None):
        teams = tuple(sorted([(team_1 or "").strip().lower(), (team_2 or "").strip().lower()]))
        books = tuple(sorted([(book_1 or "").strip().lower(), (book_2 or "").strip().lower()]))
        date = str(game_date or "")[:10]
        bt = (bet_type or "moneyline").strip().lower()
        if bt == "spread" and spread_value is not None:
            return f"{date}:{teams[0]}:{teams[1]}:{books[0]}:{books[1]}:{bt}:{spread_value}"
        return f"{date}:{teams[0]}:{teams[1]}:{books[0]}:{books[1]}:{bt}"

    @staticmethod
    def parse_matchup_pair_key(pair_key):
        parts = str(pair_key or "").split(":", 4)
        if len(parts) != 5:
            return None
        return {
            "game_date": parts[0],
            "team_1": parts[1],
            "team_2": parts[2],
            "book_1": parts[3],
            "book_2": parts[4],
        }

    def _arb_scan_locked_key(self, pair_key):
        return f"arb_scan_locked:{pair_key}"

    def _arb_opportunity_alert_key(self, pair_key):
        return f"arb_opportunity_alert_sent:{pair_key}"

    def _bet_confirmed_alert_key(self, bookmaker, bet_type, game_id):
        return f"bet_confirmed_alert_sent:{bookmaker}:{bet_type}:{game_id}"

    def is_arb_scan_locked(self, team_1, team_2, book_1, book_2, game_date=None, bet_type=None, spread_value=None):
        """Stop surfacing NEW arb alerts for this event + bookmaker pair."""
        pair_key = self.matchup_pair_key(
            team_1, team_2, book_1, book_2, game_date, bet_type=bet_type, spread_value=spread_value
        )
        return bool(self.redis.get(self._arb_scan_locked_key(pair_key)))

    def lock_arb_scan(self, team_1, team_2, book_1, book_2, game_date=None, bet_type=None, spread_value=None):
        pair_key = self.matchup_pair_key(
            team_1, team_2, book_1, book_2, game_date, bet_type=bet_type, spread_value=spread_value
        )
        self.redis.set(self._arb_scan_locked_key(pair_key), {"locked_at": "now"}, ttl=self.lock_ttl)

    def unlock_arb_scan(self, team_1, team_2, book_1, book_2, game_date=None, bet_type=None, spread_value=None):
        pair_key = self.matchup_pair_key(
            team_1, team_2, book_1, book_2, game_date, bet_type=bet_type, spread_value=spread_value
        )
        self.redis.delete(self._arb_scan_locked_key(pair_key))

    def _leg_placed_key(self, bookmaker, bet_type, game_id):
        """Legacy key (book+game only). Do not use for new arb coordination."""
        return f"leg_placed:{bookmaker}:{bet_type}:{game_id}"

    def _arb_leg_placed_key(self, pair_key, bookmaker, bet_type):
        bm = (bookmaker or "").strip().lower()
        bt = (bet_type or "moneyline").strip().lower()
        return f"arb_leg_placed:{pair_key}:{bm}:{bt}"

    def _book_game_leg_index_key(self, bookmaker, bet_type, game_id):
        bm = (bookmaker or "").strip().lower()
        bt = (bet_type or "moneyline").strip().lower()
        return f"book_game_leg:{bm}:{bt}:{game_id}"

    @staticmethod
    def game_id_for_book(arb: dict, bookmaker: str) -> str | None:
        bm = (bookmaker or "").strip().lower()
        if bm == (arb.get("team_1_bookmaker") or "").strip().lower():
            return arb.get("team_1_game_id")
        if bm == (arb.get("team_2_bookmaker") or "").strip().lower():
            return arb.get("team_2_game_id")
        return None

    def _register_book_game_leg(self, pair_key, bookmaker, bet_type, game_id):
        if not game_id:
            return
        key = self._book_game_leg_index_key(bookmaker, bet_type, game_id)
        data = self.redis.get(key) or {}
        if not isinstance(data, dict):
            data = {}
        data[pair_key] = time.time()
        self.redis.set(key, data, ttl=self.lock_ttl)

    def _unregister_book_game_leg(self, pair_key, bookmaker, bet_type, game_id):
        if not game_id:
            return
        key = self._book_game_leg_index_key(bookmaker, bet_type, game_id)
        data = self.redis.get(key) or {}
        if not isinstance(data, dict):
            return
        data.pop(pair_key, None)
        if data:
            self.redis.set(key, data, ttl=self.lock_ttl)
        else:
            self.redis.delete(key)

    def mark_arb_leg_placed(self, arb: dict, bookmaker: str, game_id: str | None = None):
        """Mark a confirmed leg scoped to this arb pair (not global book+game)."""
        pair_key = self.arb_pair_key_from_arb(arb)
        bet_type = (arb.get("bet_type") or "moneyline").strip().lower()
        bm = (bookmaker or "").strip().lower()
        gid = game_id or self.game_id_for_book(arb, bookmaker)
        self.redis.set(
            self._arb_leg_placed_key(pair_key, bookmaker, bet_type),
            {
                "game_id": gid,
                "placed_at": time.time(),
                "pair_key": pair_key,
                "bookmaker": bm,
                "bet_type": bet_type,
            },
            ttl=self.lock_ttl,
        )
        self._register_book_game_leg(pair_key, bookmaker, bet_type, gid)

    def is_arb_leg_placed(self, arb: dict, bookmaker: str) -> bool:
        """True when this book's leg is confirmed for this specific arb pair."""
        pair_key = self.arb_pair_key_from_arb(arb)
        bet_type = (arb.get("bet_type") or "moneyline").strip().lower()
        return bool(self.redis.get(self._arb_leg_placed_key(pair_key, bookmaker, bet_type)))

    def clear_arb_leg_placed(self, arb: dict, bookmaker: str) -> None:
        pair_key = self.arb_pair_key_from_arb(arb)
        bet_type = (arb.get("bet_type") or "moneyline").strip().lower()
        gid = self.game_id_for_book(arb, bookmaker)
        self.redis.delete(self._arb_leg_placed_key(pair_key, bookmaker, bet_type))
        self._unregister_book_game_leg(pair_key, bookmaker, bet_type, gid)

    def clear_arb_pair_legs(self, arb: dict) -> None:
        """Clear both books' pair-scoped leg flags for this arb."""
        for book in (arb.get("team_1_bookmaker"), arb.get("team_2_bookmaker")):
            if book:
                self.clear_arb_leg_placed(arb, book)

    def clear_arb_legs_for_pair_key(self, pair_key: str) -> None:
        """Clear all pair-scoped leg flags matching pair_key (exposure/summary cleanup)."""
        pattern = f"arb_leg_placed:{pair_key}:*"
        for key in self.redis.scan(pattern):
            payload = self.redis.get(key)
            if isinstance(payload, dict):
                self._unregister_book_game_leg(
                    pair_key,
                    payload.get("bookmaker"),
                    payload.get("bet_type"),
                    payload.get("game_id"),
                )
            self.redis.delete(key)

    def has_other_pair_partial_on_book_game(self, arb: dict, bookmaker: str) -> bool:
        """Block a new pair when another pair still has partial exposure on same book+game."""
        bet_type = (arb.get("bet_type") or "moneyline").strip().lower()
        game_id = self.game_id_for_book(arb, bookmaker)
        if not game_id:
            return False
        pair_key = self.arb_pair_key_from_arb(arb)
        index = self.redis.get(self._book_game_leg_index_key(bookmaker, bet_type, game_id)) or {}
        if not isinstance(index, dict):
            return False
        for other_pair in index:
            if other_pair == pair_key:
                continue
            if self.has_partial_exposure_for_pair(other_pair):
                return True
        return False

    def should_skip_arb_leg_placement(self, arb: dict, bookmaker: str) -> tuple[bool, str]:
        """Return (skip, reason) for betting-loop leg placement."""
        if self.is_game_pair_daily_bet_placed(arb, bookmaker):
            return True, "daily game/pair bet already placed on this book"
        if self.is_arb_leg_placed(arb, bookmaker):
            return True, "leg already confirmed for this pair"
        if self.has_other_pair_partial_on_book_game(arb, bookmaker):
            return True, "other pair has partial exposure on same book/game"
        return False, ""

    def _game_pair_daily_bet_key(self, pair_key: str, bookmaker: str, bet_type: str) -> str:
        bm = (bookmaker or "").strip().lower()
        bt = (bet_type or "moneyline").strip().lower()
        return f"game_pair_daily_bet:{pair_key}:{bm}:{bt}"

    def is_game_pair_daily_bet_placed(self, arb: dict, bookmaker: str) -> bool:
        """True when this book already placed on this matchup/pair today."""
        pair_key = self.arb_pair_key_from_arb(arb)
        bet_type = (arb.get("bet_type") or "moneyline").strip().lower()
        return bool(
            self.redis.get(self._game_pair_daily_bet_key(pair_key, bookmaker, bet_type))
        )

    def mark_game_pair_daily_bet(
        self, arb: dict, bookmaker: str, game_id: str | None = None
    ) -> None:
        """One bet per book per game/pair per day — survives leg-flag cleanup."""
        pair_key = self.arb_pair_key_from_arb(arb)
        bet_type = (arb.get("bet_type") or "moneyline").strip().lower()
        bm = (bookmaker or "").strip().lower()
        gid = game_id or self.game_id_for_book(arb, bookmaker)
        self.redis.set(
            self._game_pair_daily_bet_key(pair_key, bookmaker, bet_type),
            {
                "game_id": gid,
                "placed_at": time.time(),
                "pair_key": pair_key,
                "bookmaker": bm,
                "bet_type": bet_type,
            },
            ttl=self.lock_ttl,
        )

    def purge_legacy_leg_placed_keys(self) -> int:
        """Remove pre-pair-scoped leg_placed:* keys (global book+game flags)."""
        removed = 0
        for key in self.redis.scan("leg_placed:*"):
            self.redis.delete(key)
            removed += 1
        return removed

    # Legacy API — kept for scripts; prefer pair-scoped methods above.
    def clear_leg_placed(self, bookmaker, bet_type, game_id):
        self.redis.delete(self._leg_placed_key(bookmaker, bet_type, game_id))

    def is_leg_placed(self, bookmaker, bet_type, game_id):
        return bool(self.redis.get(self._leg_placed_key(bookmaker, bet_type, game_id)))

    def mark_leg_placed(self, bookmaker, bet_type, game_id):
        """Deprecated global book+game flag. Prefer mark_arb_leg_placed(arb, bookmaker)."""
        self.redis.set(
            self._leg_placed_key(bookmaker, bet_type, game_id),
            {"placed_at": time.time()},
            ttl=self.lock_ttl,
        )

    def arb_opportunity_alert_already_sent(
        self, team_1, team_2, book_1, book_2, game_date=None, bet_type=None, spread_value=None
    ):
        """Scanner ===== Arbitrage ===== Telegram dedup (per event + book pair per day)."""
        pair_key = self.matchup_pair_key(
            team_1, team_2, book_1, book_2, game_date, bet_type=bet_type, spread_value=spread_value
        )
        return bool(self.redis.get(self._arb_opportunity_alert_key(pair_key)))

    def mark_arb_opportunity_alert_sent(
        self, team_1, team_2, book_1, book_2, game_date=None, bet_type=None, spread_value=None
    ):
        pair_key = self.matchup_pair_key(
            team_1, team_2, book_1, book_2, game_date, bet_type=bet_type, spread_value=spread_value
        )
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

    def _real_bets_summary_key(self, pair_key):
        return f"real_bets_summary_sent:{pair_key}"

    def real_bets_summary_already_sent(self, pair_key):
        return bool(self.redis.get(self._real_bets_summary_key(pair_key)))

    def mark_real_bets_summary_sent(self, pair_key):
        self.redis.set(
            self._real_bets_summary_key(pair_key),
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

    def get_partial_exposure_meta(self, pair_key):
        data = self.redis.get(self._partial_exposure_key(pair_key))
        return data if isinstance(data, dict) else None

    def has_partial_exposure_for_pair(self, pair_key):
        return bool(self.redis.get(self._partial_exposure_key(pair_key)))

    def list_partial_exposure_pair_keys(self):
        keys = self.redis.scan("partial_exposure:*")
        prefix = "partial_exposure:"
        return [key[len(prefix):] for key in keys if key.startswith(prefix)]

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