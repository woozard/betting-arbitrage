import asyncio
import threading
import time
from decimal import Decimal
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError

from database.config import __get_db1_session__
from database.models.Arbitrage import Arbitrage
from database.models.ArbitrageOdds import ArbitrageOdds
from utils.config import (
    TELEGRAM,
    is_active_arb_pair,
    arb_max_total_prob_for_bet_type,
    min_arb_profit_pct_for_bet_type,
    TELEGRAM_ALERTS_ASYNC,
    SPREAD_ARB_MAX_PROFIT_PCT,
    SPREAD_ARB_SCAN_ENABLED,
    SPREAD_ODDS_MAX_AGE_SECONDS,
    SPREAD_ODDS_MAX_GAP_SECONDS,
    arb_opportunity_alert_chat_ids,
)
from utils.logger import Logger
from utils.helpers import (
    send_telegram_alert,
    send_testing_alert,
    send_monitoring_alert,
    is_game_pregame,
    parse_game_datetime,
    format_utc_timestamp,
    is_plausible_moneyline_pair,
    is_plausible_spread_pair,
    spread_odds_rows_fresh_for_arb,
    normalize_team,
    align_cross_book_moneylines,
    align_cross_book_spreads,
    spread_market_label,
    format_arb_opportunity_alert,
    spread_lines_from_row,
)
from utils.timing import time_it
from utils.game_registry import attach_canonical_game_ids, matchup_group_key, odds_dedup_key
from utils.match_identity import validate_cross_book_game_datetimes
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.bet_placement import wait_for_arb_execution_pause_clear
from cache.arbitrage_cache import ArbitrageCache


class ArbitrageController:
    def __init__(self, db: Session = None):
        # DB — pass a fresh session for long-lived callers (e.g. Telegram /scan).
        self.db: Session = db if db is not None else __get_db1_session__()
        
        # Logger
        self.logger = Logger.get_logger("arbitrage")

        # Cache
        self.cache = ArbitrageCache()
        self._exposure_cleanup_at = 0.0

    # --------------------------------------------------------
    # Static Helpers
    # --------------------------------------------------------
    @staticmethod
    def us_to_decimal(odds) -> Decimal:
        odds = Decimal(odds)
        return Decimal(1) + (odds / 100) if odds > 0 else Decimal(1) + (Decimal(100) / abs(odds))

    def implied_prob(self, odds) -> Decimal:
        return Decimal(1) / self.us_to_decimal(odds)

    @staticmethod
    def _allowed_arb_book_pair(book_1: str, book_2: str) -> bool:
        return is_active_arb_pair(book_1, book_2)

    # --------------------------------------------------------
    # Calculate total arbitrage probability
    # --------------------------------------------------------
    def __calc_arb_total(self, odds_1, odds_2):
        if not odds_1 or not odds_2:
            return None
        return self.implied_prob(odds_1) + self.implied_prob(odds_2)

    @staticmethod
    def _spread_cross_book_trusted(
        leg_1_odds,
        leg_2_odds,
        same_side_a,
        same_side_b,
        *,
        max_same_side_gap: float = 100,
        max_profit_pct: float | None = None,
        arb_total=None,
    ) -> bool:
        """Reject spread arbs when books disagree on the same side or profit is unrealistic."""
        if max_profit_pct is None:
            max_profit_pct = SPREAD_ARB_MAX_PROFIT_PCT
        try:
            if same_side_a is not None and same_side_b is not None:
                if abs(float(same_side_a) - float(same_side_b)) > max_same_side_gap:
                    return False
        except (TypeError, ValueError):
            pass

        if arb_total is None:
            return True

        profit_pct = float((Decimal(1) - arb_total) * 100)
        return profit_pct <= max_profit_pct

    @staticmethod
    def _spread_pair_fresh_enough(o1: dict, o2: dict) -> bool:
        return spread_odds_rows_fresh_for_arb(
            o1.get("created_at"),
            o2.get("created_at"),
            max_age_seconds=SPREAD_ODDS_MAX_AGE_SECONDS,
            max_gap_seconds=SPREAD_ODDS_MAX_GAP_SECONDS,
        )

    # --------------------------------------------------------
    # Long Running
    # --------------------------------------------------------
    def run(self, delay: float = None):
        if delay is None:
            from utils.config import ARB_SCAN_DELAY_SECONDS
            delay = ARB_SCAN_DELAY_SECONDS
        self.logger.info("========== Arbitrage (START) ==========")
        try:
            while True:
                self._exposure_cleanup_at = tick_exposure_cleanup(
                    self.cache, self.logger, self._exposure_cleanup_at
                )
                wait_for_arb_execution_pause_clear(
                    self.cache, self.logger, component="Arb scanner"
                )
                if self.cache.is_arb_execution_paused():
                    continue
                self.scan_opportunities()
                time.sleep(delay)
        except KeyboardInterrupt:
            self.logger.info("Arbitrage Stopped")
        except Exception as e:
            self.logger.error("Arbitrage Failed", exc_info=True)
            asyncio.run(send_monitoring_alert("arbitrage-controller", "system", e))
        finally:
            self.logger.info("========== Arbitrage (END) ==========")

    # DB-backed odds fetch (as assumed by design)
    # --------------------------------------------------------
    @staticmethod
    def _odds_dedup_key(row: dict) -> tuple:
        return odds_dedup_key(row)

    @staticmethod
    def _matchup_group_key(row: dict) -> tuple:
        return matchup_group_key(row)

    @staticmethod
    def _prefer_odds_row(candidate: dict, current: dict) -> dict:
        if candidate["created_at"] > current["created_at"]:
            return candidate
        if candidate["created_at"] < current["created_at"]:
            return current

        # Same scrape timestamp: prefer the row with a later (still pregame) start when tied.
        cand_dt = parse_game_datetime(candidate.get("game_datetime"))
        cur_dt = parse_game_datetime(current.get("game_datetime"))
        if cand_dt and cur_dt and cand_dt != cur_dt:
            return candidate if cand_dt > cur_dt else current
        return current

    def get_recent_moneyline_odds_from_db(
        self,
        minutes: int = 60,
        *,
        keep_created_at: bool = False,
        require_plausible_moneyline: bool = False,
    ):
        """Pull recent moneyline odds from DB (populated by controllers like Sports411 and Betamapola).

        Returns only the *latest* row per bookmaker per normalized matchup to avoid
        comparing stale historical snapshots (or duplicate S411 game_ids) against each other.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        rows = (
            self.db.query(ArbitrageOdds)
            .filter(ArbitrageOdds.bet_type == "moneyline")
            .filter(ArbitrageOdds.created_at >= cutoff)
            .order_by(ArbitrageOdds.created_at.desc())
            .all()
        )

        # Build list with created_at for deduping (query is newest-first).
        results = []
        for r in rows:
            ml_1 = float(r.moneyline_team_1) if r.moneyline_team_1 is not None else None
            ml_2 = float(r.moneyline_team_2) if r.moneyline_team_2 is not None else None
            if require_plausible_moneyline and not is_plausible_moneyline_pair(ml_1, ml_2):
                continue
            results.append({
                "bookmaker": r.bookmaker,
                "bet_type": r.bet_type,
                "game_id": r.game_id,
                "team_1": r.team_1,
                "team_2": r.team_2,
                "moneyline_team_1": ml_1,
                "moneyline_team_2": ml_2,
                "sport": r.sport,
                "league": r.league,
                "game_datetime": r.game_datetime.isoformat() if r.game_datetime else None,
                "created_at": r.created_at,
            })

        attach_canonical_game_ids(self.db, results)

        # Deduplicate: keep only the most recent odds per bookmaker + canonical game
        latest = {}
        for o in results:
            key = self._odds_dedup_key(o)
            if key not in latest:
                latest[key] = o
            else:
                latest[key] = self._prefer_odds_row(o, latest[key])

        if not keep_created_at:
            for o in latest.values():
                o.pop("created_at", None)

        return list(latest.values())

    def get_recent_spread_odds_from_db(
        self,
        minutes: int = 60,
        *,
        keep_created_at: bool = False,
        require_plausible_spread: bool = True,
    ):
        """Pull recent spread/run-line odds from DB."""
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        rows = (
            self.db.query(ArbitrageOdds)
            .filter(ArbitrageOdds.bet_type == "spread")
            .filter(ArbitrageOdds.created_at >= cutoff)
            .order_by(ArbitrageOdds.created_at.desc())
            .all()
        )

        results = []
        for r in rows:
            spread_1 = float(r.spread_team_1) if r.spread_team_1 is not None else None
            spread_2 = float(r.spread_team_2) if r.spread_team_2 is not None else None
            spread_value = float(r.spread_value) if r.spread_value is not None else None
            if require_plausible_spread and not is_plausible_spread_pair(
                spread_value, spread_1, spread_2
            ):
                continue
            results.append({
                "bookmaker": r.bookmaker,
                "bet_type": r.bet_type,
                "game_id": r.game_id,
                "team_1": r.team_1,
                "team_2": r.team_2,
                "spread_team_1": spread_1,
                "spread_team_2": spread_2,
                "spread_value": spread_value,
                "sport": r.sport,
                "league": r.league,
                "game_datetime": r.game_datetime.isoformat() if r.game_datetime else None,
                "created_at": r.created_at,
            })

        attach_canonical_game_ids(self.db, results)

        latest = {}
        for o in results:
            key = self._odds_dedup_key(o)
            if key not in latest:
                latest[key] = o
            else:
                latest[key] = self._prefer_odds_row(o, latest[key])

        if not keep_created_at:
            for o in latest.values():
                o.pop("created_at", None)

        return list(latest.values())

    # --------------------------------------------------------
    # Scan Opportunities
    # --------------------------------------------------------
    def _scan_moneyline_opportunities(self):
        all_odds = self.get_recent_moneyline_odds_from_db(minutes=60)
        return self._scan_cross_book_opportunities(
            all_odds,
            bet_type="moneyline",
            align_fn=align_cross_book_moneylines,
        )

    def _scan_spread_opportunities(self):
        all_odds = self.get_recent_spread_odds_from_db(
            minutes=60,
            keep_created_at=True,
        )
        return self._scan_cross_book_opportunities(
            all_odds,
            bet_type="spread",
            align_fn=align_cross_book_spreads,
        )

    def _scan_cross_book_opportunities(self, all_odds, bet_type: str, align_fn):
        matches = {}
        arb_found = 0
        best_arb = None
        best_match = None
        max_total_prob = Decimal(str(arb_max_total_prob_for_bet_type(bet_type)))
        min_profit_pct = min_arb_profit_pct_for_bet_type(bet_type)

        if not all_odds:
            return {
                "bet_type": bet_type,
                "odds_count": 0,
                "match_count": 0,
                "arb_found": 0,
                "best_arb": None,
                "best_match": None,
                "min_profit_pct": min_profit_pct,
                "max_total_prob": float(max_total_prob),
            }

        for o in all_odds:
            key = self._matchup_group_key(o)
            matches.setdefault(key, []).append(o)

        for odds_group in matches.values():
            for i in range(len(odds_group)):
                for j in range(i + 1, len(odds_group)):
                    o1 = odds_group[i]
                    o2 = odds_group[j]

                    if o1["bookmaker"] == o2["bookmaker"]:
                        continue

                    if not self._allowed_arb_book_pair(o1["bookmaker"], o2["bookmaker"]):
                        continue

                    aligned = align_fn(o1, o2)
                    if not aligned:
                        continue

                    if bet_type == "spread":
                        a_t1, a_t2, b_t1, b_t2, spread_value = aligned
                        if not self._spread_pair_fresh_enough(o1, o2):
                            continue
                    else:
                        a_t1, a_t2, b_t1, b_t2 = aligned
                        spread_value = None
                        if not is_plausible_moneyline_pair(a_t1, a_t2):
                            continue
                        if not is_plausible_moneyline_pair(b_t1, b_t2):
                            continue

                    arb_total = self.__calc_arb_total(a_t1, b_t2)
                    if arb_total:
                        if best_arb is None or arb_total < best_arb:
                            best_arb = arb_total
                            best_match = {
                                "bet_type": bet_type,
                                "spread_value": spread_value,
                                "sport": o1.get("sport"),
                                "team_1": o1["team_1"],
                                "team_2": o1["team_2"],
                                "book_1": o1["bookmaker"],
                                "odds_1": a_t1,
                                "book_2": o2["bookmaker"],
                                "odds_2": b_t2,
                            }
                        if arb_total < max_total_prob:
                            if bet_type != "spread" or self._spread_cross_book_trusted(
                                a_t1, b_t2, a_t1, b_t1, arb_total=arb_total
                            ):
                                arb_found += 1
                                self.__insert_arbitrage(
                                    o1,
                                    o2,
                                    "o1",
                                    "o2",
                                    arb_total,
                                    team_1_odds=a_t1,
                                    team_2_odds=b_t2,
                                    bet_type=bet_type,
                                    spread_value=spread_value,
                                )

                    arb_total = self.__calc_arb_total(b_t1, a_t2)
                    if arb_total:
                        if best_arb is None or arb_total < best_arb:
                            best_arb = arb_total
                            best_match = {
                                "bet_type": bet_type,
                                "spread_value": spread_value,
                                "sport": o1.get("sport"),
                                "team_1": o1["team_1"],
                                "team_2": o1["team_2"],
                                "book_1": o2["bookmaker"],
                                "odds_1": b_t1,
                                "book_2": o1["bookmaker"],
                                "odds_2": a_t2,
                            }
                        if arb_total < max_total_prob:
                            if bet_type != "spread" or self._spread_cross_book_trusted(
                                b_t1, a_t2, b_t1, a_t1, arb_total=arb_total
                            ):
                                arb_found += 1
                                self.__insert_arbitrage(
                                    o1,
                                    o2,
                                    "o2",
                                    "o1",
                                    arb_total,
                                    team_1_odds=b_t1,
                                    team_2_odds=a_t2,
                                    bet_type=bet_type,
                                    spread_value=spread_value,
                                )

        return {
            "bet_type": bet_type,
            "odds_count": len(all_odds),
            "match_count": len(matches),
            "arb_found": arb_found,
            "best_arb": best_arb,
            "best_match": best_match,
            "linked": sum(1 for o in all_odds if o.get("canonical_game_id")),
            "min_profit_pct": min_profit_pct,
            "max_total_prob": float(max_total_prob),
        }

    @time_it
    def scan_opportunities(self):
        if self.cache.is_arb_execution_paused():
            self.logger.info(
                "Skipping scan — execution pause active (bets in flight)"
            )
            return
        self.logger.info("========== Arbitrage - Scan Opportunities (START) ==========")
        try:
            scan_results = [self._scan_moneyline_opportunities()]
            if SPREAD_ARB_SCAN_ENABLED:
                scan_results.append(self._scan_spread_opportunities())

            for result in scan_results:
                bet_type = result["bet_type"]
                msg = (
                    f"{bet_type.title()}: Odds: {result['odds_count']} - "
                    f"Matches: {result['match_count']} - Arbs: {result['arb_found']} "
                    f"(min profit {result['min_profit_pct']:.2f}%)"
                )
                if result.get("linked"):
                    msg += f" (canonical-linked: {result['linked']}/{result['odds_count']})"
                if result["arb_found"] == 0 and result["best_arb"] is not None:
                    msg += f" (closest total prob: {float(result['best_arb']):.4f})"
                self.logger.info(msg)

                best_arb = result["best_arb"]
                best_match = result["best_match"]
                close_ceiling = min(Decimal("1.02"), Decimal(str(result["max_total_prob"])) + Decimal("0.01"))
                if (
                    best_arb is not None
                    and Decimal("1") <= best_arb < close_ceiling
                    and best_match is not None
                ):
                    market = best_match.get("bet_type", bet_type)
                    spread_value = best_match.get("spread_value")
                    market_label = (
                        spread_market_label(spread_value, best_match.get("sport"))
                        if market == "spread"
                        else market
                    )
                    self.logger.info("========== Close Arb Opportunity (START) ==========")
                    self.logger.info(
                        f"Market: {market_label} | Match: {best_match['team_1']} vs "
                        f"{best_match['team_2']} | Total Prob: {float(best_arb):.4f}"
                    )
                    self.logger.info(f"  {best_match['book_1']}: {best_match['odds_1']}")
                    self.logger.info(f"  {best_match['book_2']}: {best_match['odds_2']}")
                    self.logger.info("========== Close Arb Opportunity (END) ==========")

            self.db.commit()

        except Exception as e:
            self.db.rollback()
            self.logger.error("Arbitrage Scan Failed", exc_info=True)
            asyncio.run(send_monitoring_alert("arbitrage-scan", "system", e))

        finally:
            self.logger.info("========== Arbitrage - Scan Opportunities (END) ==========")

    # --------------------------------------------------------
    # Save Arbitrage
    # --------------------------------------------------------
    def __resolve_sides(self, o1, o2, t1_from, t2_from):
        t1 = o1 if t1_from == "o1" else o2
        t2 = o1 if t2_from == "o1" else o2
        return t1, t2

    def __build_arb_data(
        self,
        o1,
        o2,
        t1_from,
        t2_from,
        arb_total,
        team_1_odds=None,
        team_2_odds=None,
        bet_type: str = "moneyline",
        spread_value=None,
    ):
        t1, t2 = self.__resolve_sides(o1, o2, t1_from, t2_from)
        o1_src = o1 if t1_from == "o1" else o2
        o2_src = o2 if t2_from == "o2" else o1
        game_dt = parse_game_datetime(o1.get("game_datetime"))
        game_date = game_dt.date() if game_dt else datetime.utcnow().date()

        if bet_type == "spread":
            default_t1_odds = t1.get("spread_team_1")
            default_t2_odds = t2.get("spread_team_2")
            spread_line_team_1, _ = spread_lines_from_row(t1)
            _, spread_line_team_2 = spread_lines_from_row(t2)
        else:
            default_t1_odds = t1.get("moneyline_team_1")
            default_t2_odds = t2.get("moneyline_team_2")
            spread_line_team_1 = None
            spread_line_team_2 = None

        return {
            "sport": o1["sport"],
            "league": o1["league"],
            "game_date": str(game_date),
            "game_datetime": game_dt.strftime("%Y-%m-%d %H:%M:%S") if game_dt else None,
            "team_1_game_datetime": o1_src.get("game_datetime"),
            "team_2_game_datetime": o2_src.get("game_datetime"),

            "team_1": o1["team_1"],
            "team_1_bookmaker": t1["bookmaker"],
            "team_1_game_id": t1["game_id"],
            "team_1_odds": float(
                team_1_odds if team_1_odds is not None else default_t1_odds
            ),

            "team_2": o1["team_2"],
            "team_2_bookmaker": t2["bookmaker"],
            "team_2_game_id": t2["game_id"],
            "team_2_odds": float(
                team_2_odds if team_2_odds is not None else default_t2_odds
            ),

            "bet_type": bet_type,
            "spread_value": spread_value,
            "spread_line_team_1": spread_line_team_1,
            "spread_line_team_2": spread_line_team_2,
            "arb_total_prob": float(arb_total),
            "profit_pct": float(round((Decimal(1) - arb_total) * 100, 2)),
            "read": False,
            "identified_at": time.time(),
        }

    def __store_arbitrage_cache(self, arb_data):
        from utils.bet_placement import store_arbitrage_for_both_books

        store_arbitrage_for_both_books(self.cache, arb_data)

    def __insert_arbitrage(
        self,
        new_odds,
        existing,
        t1_from,
        t2_from,
        arb_total,
        team_1_odds=None,
        team_2_odds=None,
        bet_type: str = "moneyline",
        spread_value=None,
    ):
        o1 = new_odds
        o2 = existing
        t1, t2 = self.__resolve_sides(o1, o2, t1_from, t2_from)

        dt_reason = validate_cross_book_game_datetimes(
            o1.get("game_datetime"),
            o2.get("game_datetime"),
            team_1=o1.get("team_1") or "",
            team_2=o1.get("team_2") or "",
        )
        if dt_reason:
            self.logger.info(
                f"Skipping arb ({dt_reason}) - "
                f"{bet_type} {o1['team_1']} vs {o1['team_2']} | "
                f"{t1['bookmaker']} vs {t2['bookmaker']}"
            )
            return None

        if not is_game_pregame(o1.get("game_datetime")) or not is_game_pregame(o2.get("game_datetime")):
            self.logger.info(
                f"Skipping arb (game started or unknown start time) - "
                f"{o1['team_1']} vs {o1['team_2']}"
            )
            return None

        game_dt = parse_game_datetime(o1.get("game_datetime"))
        game_date = game_dt.date() if game_dt else datetime.utcnow().date()
        if self.cache.is_arb_scan_locked(
            o1["team_1"],
            o1["team_2"],
            t1["bookmaker"],
            t2["bookmaker"],
            str(game_date),
            bet_type=bet_type,
            spread_value=spread_value,
        ):
            self.logger.info(
                f"Skipping arb (scan locked in Redis after prior leg) - "
                f"{bet_type} {o1['team_1']} vs {o1['team_2']} | "
                f"{t1['bookmaker']} vs {t2['bookmaker']}"
            )
            return None

        if self.cache.is_arb_execution_paused():
            self.logger.info(
                f"Skipping arb (execution pause active) - "
                f"{bet_type} {o1['team_1']} vs {o1['team_2']} | "
                f"{t1['bookmaker']} vs {t2['bookmaker']}"
            )
            return None

        arb_stub = {
            "team_1": o1["team_1"],
            "team_2": o1["team_2"],
            "team_1_bookmaker": t1["bookmaker"],
            "team_2_bookmaker": t2["bookmaker"],
            "game_datetime": o1.get("game_datetime"),
            "game_date": str(game_date),
            "bet_type": bet_type,
        }
        if spread_value is not None:
            arb_stub["spread_value"] = spread_value
        owns, owner_reason = self.cache.other_pair_owns_game_event(arb_stub)
        if owns:
            self.logger.info(
                f"Skipping arb ({owner_reason}) - "
                f"{bet_type} {o1['team_1']} vs {o1['team_2']} | "
                f"{t1['bookmaker']} vs {t2['bookmaker']}"
            )
            return None

        if bet_type == "moneyline":
            t1_ml = team_1_odds if team_1_odds is not None else t1.get("moneyline_team_1")
            t2_ml = team_2_odds if team_2_odds is not None else t2.get("moneyline_team_2")
            if not is_plausible_moneyline_pair(t1_ml, t2_ml):
                self.logger.info(
                    f"Skipping arb (both legs same ML side) - "
                    f"{o1['team_1']} vs {o1['team_2']} | "
                    f"{t1['bookmaker']} {t1_ml} vs {t2['bookmaker']} {t2_ml}"
                )
                return None

        arb_data = self.__build_arb_data(
            o1,
            o2,
            t1_from,
            t2_from,
            arb_total,
            team_1_odds=team_1_odds,
            team_2_odds=team_2_odds,
            bet_type=bet_type,
            spread_value=spread_value,
        )

        try:
            if bet_type == "spread":
                default_t1_odds = t1.get("spread_team_1")
                default_t2_odds = t2.get("spread_team_2")
            else:
                default_t1_odds = t1.get("moneyline_team_1")
                default_t2_odds = t2.get("moneyline_team_2")

            t1_odds = Decimal(
                team_1_odds if team_1_odds is not None else default_t1_odds
            )
            t2_odds = Decimal(
                team_2_odds if team_2_odds is not None else default_t2_odds
            )
            profit_pct = round((Decimal(1) - arb_total) * 100, 2)
            game_dt = parse_game_datetime(o1.get("game_datetime"))

            arb = Arbitrage(
                sport=o1["sport"],
                league=o1["league"],
                game_date=game_dt.date() if game_dt else datetime.utcnow().date(),

                team_1=o1["team_1"],
                team_2=o1["team_2"],
                bet_type=bet_type,

                team_1_bookmaker=t1["bookmaker"],
                team_1_game_id=t1["game_id"],
                team_1_odds=t1_odds,

                team_2_bookmaker=t2["bookmaker"],
                team_2_game_id=t2["game_id"],
                team_2_odds=t2_odds,

                arb_total_prob=arb_total,
                profit_pct=profit_pct,
                read=False
            )

            self.db.add(arb)
            self.db.flush()

            market_label = (
                spread_market_label(spread_value, o1.get("sport"))
                if bet_type == "spread"
                else bet_type
            )
            self.logger.info(
                f"DB - Arbitrage Saved - {market_label} - "
                f"{t1['bookmaker']} vs {t2['bookmaker']} - "
                f"{o1['team_1']} vs {o1['team_2']}"
            )

            self.__store_arbitrage_cache(arb_data)
            self.__maybe_send_arb_opportunity_alert(
                arb.team_1,
                arb.team_2,
                arb.team_1_bookmaker,
                arb.team_2_bookmaker,
                str(arb.game_date),
                bet_type,
                spread_value,
                o1.get("sport"),
                arb=arb,
                arb_data=arb_data,
            )

            return arb

        except IntegrityError:
            self.logger.warning(
                f"DB - Arbitrage Not Saved (duplicate) - refreshing cache - "
                f"{t1['bookmaker']} vs {t2['bookmaker']} - "
                f"{o1['team_1']} vs {o1['team_2']}"  
            )
            self.db.rollback()
            self.__store_arbitrage_cache(arb_data)
            self.__maybe_send_arb_opportunity_alert(
                arb_data["team_1"],
                arb_data["team_2"],
                arb_data["team_1_bookmaker"],
                arb_data["team_2_bookmaker"],
                arb_data.get("game_date") or str(game_date),
                bet_type,
                spread_value,
                o1.get("sport"),
                arb_data=arb_data,
            )
            return None

    # --------------------------------------------------------
    # Cache Arbitrage
    # --------------------------------------------------------
    def __cache_arbitrage(self, arb, t1, t2, o1):
        arb_data = {
            "sport": arb.sport,
            "league": arb.league,
            "game_date": str(arb.game_date),
            "game_datetime": o1.get("game_datetime"),

            "team_1": arb.team_1,
            "team_1_bookmaker": arb.team_1_bookmaker,
            "team_1_game_id": arb.team_1_game_id,
            "team_1_odds": float(arb.team_1_odds),

            "team_2": arb.team_2,
            "team_2_bookmaker": arb.team_2_bookmaker,
            "team_2_game_id": arb.team_2_game_id,
            "team_2_odds": float(arb.team_2_odds),

            "bet_type": arb.bet_type,
            "arb_total_prob": float(arb.arb_total_prob),
            "profit_pct": float(arb.profit_pct),
            "read": False,
            "identified_at": time.time(),
        }

        self.__store_arbitrage_cache(arb_data)

    def __maybe_send_arb_opportunity_alert(
        self,
        team_1,
        team_2,
        book_1,
        book_2,
        game_date_str,
        bet_type,
        spread_value,
        sport,
        arb=None,
        arb_data=None,
    ):
        market_label = (
            spread_market_label(spread_value, sport)
            if bet_type == "spread"
            else bet_type
        )
        if self.cache.arb_opportunity_alert_already_sent(
            team_1,
            team_2,
            book_1,
            book_2,
            game_date_str,
            bet_type=bet_type,
            spread_value=spread_value,
        ):
            self.logger.info(
                f"Skipping duplicate arb opportunity Telegram alert - "
                f"{market_label} {team_1} vs {team_2} | {book_1} vs {book_2}"
            )
            return

        self.__send_alert(
            arb,
            (arb_data or {}).get("identified_at"),
            spread_value=spread_value,
            arb_data=arb_data,
        )
        self.cache.mark_arb_opportunity_alert_sent(
            team_1,
            team_2,
            book_1,
            book_2,
            game_date_str,
            bet_type=bet_type,
            spread_value=spread_value,
        )

    # --------------------------------------------------------
    # Send Alert
    # --------------------------------------------------------
    def __send_alert(self, arb, identified_at=None, spread_value=None, arb_data=None):
        try:
            self.logger.info("========== Arbitrage - Send Alerts (START) ==========")

            alert = format_arb_opportunity_alert(
                arb_data if arb_data else arb,
                spread_value=spread_value,
            )

            self.logger.info(f"========== Alert ==========")
            self.logger.info(alert)
            self.logger.info(f"========== Alert ==========")

            chat_ids = arb_opportunity_alert_chat_ids()
            if not chat_ids:
                self.logger.warning(
                    "No Telegram chat configured for arb opportunity alerts — skipping"
                )
                return

            for chat_id in chat_ids:
                if TELEGRAM_ALERTS_ASYNC:
                    threading.Thread(
                        target=lambda cid=chat_id: asyncio.run(
                            send_telegram_alert(alert, cid)
                        ),
                        daemon=True,
                    ).start()
                else:
                    asyncio.run(send_telegram_alert(alert, chat_id))
            

        except Exception as e:
            self.db.rollback()
            self.logger.error("Arbitrage Alerts Failed", exc_info=True)
            asyncio.run(send_monitoring_alert("arbitrage-alerts", "system", e, TELEGRAM.get('arbitrage_monitoring')))

        finally:
            self.logger.info("========== Arbitrage - Send Alerts (END) ==========")