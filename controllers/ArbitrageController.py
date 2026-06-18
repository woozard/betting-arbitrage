import asyncio
import time
from decimal import Decimal
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError

from database.config import __get_db1_session__
from database.models.Arbitrage import Arbitrage
from database.models.ArbitrageOdds import ArbitrageOdds
from utils.config import TELEGRAM, SEQUENTIAL_ARB_BETTING, is_active_arb_pair
from utils.logger import Logger
from utils.helpers import (
    send_telegram_alert,
    send_testing_alert,
    send_monitoring_alert,
    is_game_pregame,
    parse_game_datetime,
    format_utc_timestamp,
)
from utils.timing import time_it
from cache.arbitrage_cache import ArbitrageCache


class ArbitrageController:
    def __init__(self):
        # DB
        self.db: Session = __get_db1_session__()
        
        # Logger
        self.logger = Logger.get_logger("arbitrage")

        # Cache
        self.cache = ArbitrageCache()

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

    # --------------------------------------------------------
    # Long Running
    # --------------------------------------------------------
    def run(self, delay: int = 3):
        self.logger.info("========== Arbitrage (START) ==========")
        try:
            while True:
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
        team_a, team_b = sorted(
            [(row.get("team_1") or "").strip().lower(), (row.get("team_2") or "").strip().lower()]
        )
        dt = row.get("game_datetime") or ""
        date_key = (dt[:10] if isinstance(dt, str) else str(dt)[:10]) if dt else ""
        return row["bookmaker"], team_a, team_b, date_key

    @staticmethod
    def _prefer_odds_row(candidate: dict, current: dict) -> dict:
        if candidate["created_at"] > current["created_at"]:
            return candidate
        if candidate["created_at"] < current["created_at"]:
            return current

        # Same scrape timestamp: S411 often inserts two game_ids for one matchup.
        if candidate.get("bookmaker") == "sports411":
            try:
                cand_id = int(candidate.get("game_id") or 0)
                cur_id = int(current.get("game_id") or 0)
                return candidate if cand_id > cur_id else current
            except (TypeError, ValueError):
                pass
        return current

    def get_recent_moneyline_odds_from_db(self, minutes: int = 60):
        """Pull recent moneyline odds from DB (populated by controllers like Sports411 and Betamapola).

        Returns only the *latest* row per bookmaker per normalized matchup to avoid
        comparing stale historical snapshots (or duplicate S411 game_ids) against each other.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        rows = (
            self.db.query(ArbitrageOdds)
            .filter(ArbitrageOdds.bet_type == "moneyline")
            .filter(ArbitrageOdds.created_at >= cutoff)
            .all()
        )

        # Build list with created_at for deduping
        results = []
        for r in rows:
            results.append({
                "bookmaker": r.bookmaker,
                "bet_type": r.bet_type,
                "game_id": r.game_id,
                "team_1": r.team_1,
                "team_2": r.team_2,
                "moneyline_team_1": float(r.moneyline_team_1) if r.moneyline_team_1 is not None else None,
                "moneyline_team_2": float(r.moneyline_team_2) if r.moneyline_team_2 is not None else None,
                "sport": r.sport,
                "league": r.league,
                "game_datetime": r.game_datetime.isoformat() if r.game_datetime else None,
                "created_at": r.created_at,
            })

        # Deduplicate: keep only the most recent odds per bookmaker + matchup
        latest = {}
        for o in results:
            key = self._odds_dedup_key(o)
            if key not in latest:
                latest[key] = o
            else:
                latest[key] = self._prefer_odds_row(o, latest[key])

        # Remove the internal created_at before returning
        for o in latest.values():
            o.pop("created_at", None)

        return list(latest.values())

    # --------------------------------------------------------
    # Scan Opportunities
    # --------------------------------------------------------
    @time_it
    def scan_opportunities(self):
        self.logger.info("========== Arbitrage - Scan Opportunities (START) ==========")
        try:
            # Pull from DB (not Redis cache) so we compare odds persisted by different controllers
            all_odds = self.get_recent_moneyline_odds_from_db(minutes=60)

            matches = {}
            arb_found = 0

            if all_odds:
                for o in all_odds:
                    # Include date portion so that (if ever) same team names on different days don't cross
                    dt = o.get("game_datetime") or ""
                    date_key = (dt[:10] if isinstance(dt, str) else str(dt)[:10]) if dt else ""
                    key = (o["team_1"], o["team_2"], date_key)
                    matches.setdefault(key, []).append(o)

                best_arb = None
                best_match = None
                for (team_1, team_2, date_key), odds_group in matches.items():
                    for i in range(len(odds_group)):
                        for j in range(i + 1, len(odds_group)):
                            o1 = odds_group[i]
                            o2 = odds_group[j]

                            if o1["bookmaker"] == o2["bookmaker"]:
                                continue

                            if not self._allowed_arb_book_pair(
                                o1["bookmaker"], o2["bookmaker"]
                            ):
                                continue

                            # Case 1: bet team_1 on o1's book, team_2 on o2's book
                            arb_total = self.__calc_arb_total(
                                o1["moneyline_team_1"], o2["moneyline_team_2"]
                            )
                            if arb_total:
                                if best_arb is None or arb_total < best_arb:
                                    best_arb = arb_total
                                    best_match = {
                                        "team_1": o1["team_1"],
                                        "team_2": o1["team_2"],
                                        "book_1": o1["bookmaker"],
                                        "odds_1": o1["moneyline_team_1"],
                                        "book_2": o2["bookmaker"],
                                        "odds_2": o2["moneyline_team_2"],
                                    }
                                if arb_total < Decimal("1"):
                                    arb_found += 1
                                    self.__insert_arbitrage(o1, o2, "o1", "o2", arb_total)

                            # Case 2: bet team_1 on o2's book, team_2 on o1's book
                            arb_total = self.__calc_arb_total(
                                o2["moneyline_team_1"], o1["moneyline_team_2"]
                            )
                            if arb_total:
                                if best_arb is None or arb_total < best_arb:
                                    best_arb = arb_total
                                    best_match = {
                                        "team_1": o1["team_1"],
                                        "team_2": o1["team_2"],
                                        "book_1": o2["bookmaker"],
                                        "odds_1": o2["moneyline_team_1"],
                                        "book_2": o1["bookmaker"],
                                        "odds_2": o1["moneyline_team_2"],
                                    }
                                if arb_total < Decimal("1"):
                                    arb_found += 1
                                    self.__insert_arbitrage(o1, o2, "o2", "o1", arb_total)

                msg = f"Odds: {len(all_odds)} - Matches: {len(matches)} - Arbs: {arb_found}"
                if arb_found == 0 and best_arb is not None:
                    msg += f" (closest total prob: {float(best_arb):.4f})"
                self.logger.info(msg)

                # Log near-miss opportunities at or above break-even but below 1.02
                if (
                    best_arb is not None
                    and Decimal("1") <= best_arb < Decimal("1.02")
                    and best_match is not None
                ):
                    self.logger.info("========== Close Arb Opportunity (START) ==========")
                    self.logger.info(
                        f"Match: {best_match['team_1']} vs {best_match['team_2']} | Total Prob: {float(best_arb):.4f}"
                    )
                    self.logger.info(f"  {best_match['book_1']}: {best_match['odds_1']}")
                    self.logger.info(f"  {best_match['book_2']}: {best_match['odds_2']}")
                    self.logger.info("========== Close Arb Opportunity (END) ==========")

            else:
                self.logger.info(
                    f"Odds: 0 - Matches: 0 - Arbs: 0"
                )

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

    def __build_arb_data(self, o1, o2, t1_from, t2_from, arb_total):
        t1, t2 = self.__resolve_sides(o1, o2, t1_from, t2_from)
        game_dt = parse_game_datetime(o1.get("game_datetime"))
        game_date = game_dt.date() if game_dt else datetime.utcnow().date()

        return {
            "sport": o1["sport"],
            "league": o1["league"],
            "game_date": str(game_date),
            "game_datetime": game_dt.strftime("%Y-%m-%d %H:%M:%S") if game_dt else None,

            "team_1": o1["team_1"],
            "team_1_bookmaker": t1["bookmaker"],
            "team_1_game_id": t1["game_id"],
            "team_1_odds": float(t1["moneyline_team_1"]),

            "team_2": o1["team_2"],
            "team_2_bookmaker": t2["bookmaker"],
            "team_2_game_id": t2["game_id"],
            "team_2_odds": float(t2["moneyline_team_2"]),

            "bet_type": "moneyline",
            "arb_total_prob": float(arb_total),
            "profit_pct": float(round((Decimal(1) - arb_total) * 100, 2)),
            "read": False,
            "identified_at": time.time(),
        }

    def __store_arbitrage_cache(self, arb_data):
        self.cache.add_arbitrage(
            arb_data["team_1_bookmaker"], "moneyline", arb_data["team_1_game_id"], arb_data
        )
        self.cache.add_arbitrage(
            arb_data["team_2_bookmaker"], "moneyline", arb_data["team_2_game_id"], arb_data
        )

    def __insert_arbitrage(self, new_odds, existing, t1_from, t2_from, arb_total):
        o1 = new_odds
        o2 = existing
        t1, t2 = self.__resolve_sides(o1, o2, t1_from, t2_from)

        if not is_game_pregame(o1.get("game_datetime")) or not is_game_pregame(o2.get("game_datetime")):
            self.logger.info(
                f"Skipping arb (game started or unknown start time) - "
                f"{o1['team_1']} vs {o1['team_2']}"
            )
            return None

        game_dt = parse_game_datetime(o1.get("game_datetime"))
        game_date = game_dt.date() if game_dt else datetime.utcnow().date()
        if self.cache.is_arb_scan_locked(
            o1["team_1"], o1["team_2"], t1["bookmaker"], t2["bookmaker"], str(game_date)
        ):
            self.logger.info(
                f"Skipping arb (scan locked in Redis after prior leg) - "
                f"{o1['team_1']} vs {o1['team_2']} | {t1['bookmaker']} vs {t2['bookmaker']}"
            )
            return None

        arb_data = self.__build_arb_data(o1, o2, t1_from, t2_from, arb_total)

        try:
            t1_odds = Decimal(t1["moneyline_team_1"])
            t2_odds = Decimal(t2["moneyline_team_2"])
            profit_pct = round((Decimal(1) - arb_total) * 100, 2)
            game_dt = parse_game_datetime(o1.get("game_datetime"))

            arb = Arbitrage(
                sport=o1["sport"],
                league=o1["league"],
                game_date=game_dt.date() if game_dt else datetime.utcnow().date(),

                team_1=o1["team_1"],
                team_2=o1["team_2"],
                bet_type="moneyline",

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

            self.logger.info(
                f"DB - Arbitrage Saved - "
                f"{t1['bookmaker']} vs {t2['bookmaker']} - "
                f"{o1['team_1']} vs {o1['team_2']}" 
            )

            self.__store_arbitrage_cache(arb_data)
            game_date_str = str(arb.game_date)
            if self.cache.arb_opportunity_alert_already_sent(
                arb.team_1,
                arb.team_2,
                arb.team_1_bookmaker,
                arb.team_2_bookmaker,
                game_date_str,
            ):
                self.logger.info(
                    f"Skipping duplicate arb opportunity Telegram alert - {arb.team_1} vs {arb.team_2} | "
                    f"{arb.team_1_bookmaker} vs {arb.team_2_bookmaker}"
                )
            else:
                self.__send_alert(arb, arb_data.get("identified_at"))
                self.cache.mark_arb_opportunity_alert_sent(
                    arb.team_1,
                    arb.team_2,
                    arb.team_1_bookmaker,
                    arb.team_2_bookmaker,
                    game_date_str,
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

    # --------------------------------------------------------
    # Send Alert
    # --------------------------------------------------------
    def __send_alert(self, arb, identified_at=None):
        try:
            self.logger.info("========== Arbitrage - Send Alerts (START) ==========")

            alert = (
                f"===== Arbitrage =====\n"
                f"Identified At: {format_utc_timestamp(identified_at)}\n"
                f"Sport: {arb.sport}\n"
                f"League: {arb.league}\n"
                f"Date: {arb.game_date}\n"
                f"Match: {arb.team_1} vs {arb.team_2}\n"
                f"Bet Type: {arb.bet_type}\n\n"
                f"Team 1: {arb.team_1}\n"
                f"Bookmaker: {arb.team_1_bookmaker}\n"
                f"Odds: {arb.team_1_odds}\n\n"
                f"Team 2: {arb.team_2}\n"
                f"Bookmaker: {arb.team_2_bookmaker}\n"
                f"Odds: {arb.team_2_odds}\n\n"
                f"Total Probability: {arb.arb_total_prob}\n"
                f"Estimated Profit: {arb.profit_pct}%\n"
            )

            self.logger.info(f"========== Alert ==========")
            self.logger.info(alert)
            self.logger.info(f"========== Alert ==========")

            asyncio.run(
                send_telegram_alert(
                    alert,
                    TELEGRAM.get("ops")
                    if SEQUENTIAL_ARB_BETTING
                    else TELEGRAM.get("arbitrage"),
                )
            )
            

        except Exception as e:
            self.db.rollback()
            self.logger.error("Arbitrage Alerts Failed", exc_info=True)
            asyncio.run(send_monitoring_alert("arbitrage-alerts", "system", e, TELEGRAM.get('arbitrage_monitoring')))

        finally:
            self.logger.info("========== Arbitrage - Send Alerts (END) ==========")