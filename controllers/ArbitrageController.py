import asyncio
import time
from decimal import Decimal
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError

from database.config import __get_db1_session__
from database.models.Arbitrage import Arbitrage
from utils.config import TELEGRAM
from utils.logger import Logger
from utils.helpers import send_telegram_alert, send_testing_alert, send_monitoring_alert
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

    # --------------------------------------------------------
    # Scan Opportunities
    # --------------------------------------------------------
    def scan_opportunities(self):
        start = time.perf_counter()
    @time_it

        self.logger.info("========== Arbitrage - Scan Opportunities (START) ==========")
        try:
            all_odds = self.cache.get_odds(bet_type="moneyline")
            if not all_odds:
                return

            matches = {}
            for o in all_odds:
                key = (o["team_1"], o["team_2"])
                matches.setdefault(key, []).append(o)

            arb_found = 0

            for (team_1, team_2), odds_group in matches.items():
                for i in range(len(odds_group)):
                    for j in range(i + 1, len(odds_group)):
                        o1 = odds_group[i]
                        o2 = odds_group[j]

                        if o1["bookmaker"] == o2["bookmaker"]:
                            continue

                        # Case 1
                        arb_total = self.__calc_arb_total(
                            o1["moneyline_team_1"], o2["moneyline_team_2"]
                        )
                        if arb_total and arb_total < 1:
                            arb_found += 1
                            self.__insert_arbitrage(o1, o2, "o1", "o2", arb_total)

                        # Case 2
                        arb_total = self.__calc_arb_total(
                            o2["moneyline_team_1"], o1["moneyline_team_2"]
                        )
                        if arb_total and arb_total < 1:
                            arb_found += 1
                            self.__insert_arbitrage(o1, o2, "o2", "o1", arb_total)

            self.logger.info(
                f"Odds: {len(all_odds)} - Matches: {len(matches)} - Arbs: {arb_found}"
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
    def __insert_arbitrage(self, new_odds, existing, t1_from, t2_from, arb_total):
        try:
            o1 = new_odds
            o2 = existing

            t1 = o1 if t1_from == "o1" else o2
            t2 = o1 if t2_from == "o1" else o2

            t1_odds = Decimal(t1["moneyline_team_1"])
            t2_odds = Decimal(t2["moneyline_team_2"])
            profit_pct = round((Decimal(1) - arb_total) * 100, 2)
            # ---------------- DB ----------------
            arb = Arbitrage(
                sport=o1["sport"],
                league=o1["league"],
                game_date=datetime.fromisoformat(o1["game_datetime"]).date(),

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
                f"DB - Arbitrage Not Saved - "
                f"{t1['bookmaker']} vs {t2['bookmaker']} - "
                f"{o1['team_1']} vs {o1['team_2']}" 
            )

            # DB Insert Completed - Cache + Alert
            self.__cache_arbitrage(arb, t1, t2, o1)
            self.__send_alert(arb)

            return arb

        except IntegrityError:
            self.logger.warning(
                f"DB - Arbitrage Not Saved - "
                f"{t1['bookmaker']} vs {t2['bookmaker']} - "
                f"{o1['team_1']} vs {o1['team_2']}"  
            )
            self.db.rollback()
            return None

    # --------------------------------------------------------
    # Cache Arbitrage
    # --------------------------------------------------------
    def __cache_arbitrage(self, arb, t1, t2, o1):
        arb_data = {
            "sport": arb.sport,
            "league": arb.league,
            "game_date": str(arb.game_date),

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
            "read": False
        }

        # store in cache
        # self.cache.add_arbitrage(t1["bookmaker"], "moneyline", o1["game_id"], arb_data)
        # self.cache.add_arbitrage(t2["bookmaker"], "moneyline", o1["game_id"], arb_data)

        self.cache.add_arbitrage(arb.team_1_bookmaker, "moneyline", arb.team_1_game_id, arb_data)
        self.cache.add_arbitrage(arb.team_2_bookmaker, "moneyline", arb.team_2_game_id, arb_data)

    # --------------------------------------------------------
    # Send Alert
    # --------------------------------------------------------
    def __send_alert(self, arb):
        try:
            self.logger.info("========== Arbitrage - Send Alerts (START) ==========")

            alert = (
                f"===== Arbitrage =====\n"
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

            asyncio.run(send_telegram_alert(alert, TELEGRAM['arbitrage']))
            

        except Exception as e:
            self.db.rollback()
            self.logger.error("Arbitrage Alerts Failed", exc_info=True)
            asyncio.run(send_monitoring_alert("arbitrage-alerts", "system", e, TELEGRAM['arbitrage']))

        finally:
            self.logger.info("========== Arbitrage - Send Alerts (END) ==========")