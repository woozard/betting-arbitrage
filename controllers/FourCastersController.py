import asyncio
import os
import time
from datetime import datetime

from cache.arbitrage_cache import ArbitrageCache
from utils.bet_placement import (
    finalize_confirmed_bet,
    maybe_notify_partial_arb_exposure,
    should_defer_for_sequential_first_leg,
    should_pause_first_leg_for_exposure,
    odds_tolerance_for_placement,
)
from utils.betting_watchdog import (
    BettingLoopWatchdog,
    OddsScanHealthWatchdog,
)
from utils.config import TELEGRAM, is_active_arb_pair, FOURCASTERS_MLB_LEAGUE
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.fourcasters_client import FourCastersApiError, FourCastersClient
from utils.helpers import (
    fix_spread_odds_orientation,
    is_game_pregame,
    parse_to_mysql_datetime,
    send_monitoring_alert,
    teams_same,
    arb_live_odds_acceptable,
)
from utils.logger import Logger
from utils.odds_watch import persist_moneyline_games
from utils.storage import Storage
from utils.stake_sizing import base_amount_stake_from_odds, format_base_amount_stake
from utils.timing import time_it

MLB_RUN_LINE = 1.5


class FourCastersController:
    ODDS_WATCH_POLL_SECONDS = float(os.getenv("FOURCASTERS_ODDS_POLL_SEC", "5"))
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("FOURCASTERS_ODDS_FORCE_SCAN_SEC", "30"))
    ODDS_IDLE_POLL_SECONDS = float(os.getenv("FOURCASTERS_ODDS_IDLE_POLL_SEC", "5"))

    def __init__(self, account, site, sport="baseball"):
        self.account_id = account.account
        self.password = account.password
        self.label = account.label if account.label else "N/A"
        self._last_bet_error = None

        self.bookmaker = site["bookmaker"]
        self.website = site["website"]

        self.logger = Logger.get_logger(self.bookmaker)
        self.storage = Storage(self.logger)
        self.cache = ArbitrageCache()
        self.api = FourCastersClient()

        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_name = "NBA"
            self.league = "NBA"
            self._league_code = os.getenv("FOURCASTERS_NBA_LEAGUE", "NBA")
        elif self.sport in ["baseball", "mlb"]:
            self.sport_name = "MLB"
            self.league = "MLB"
            self._league_code = FOURCASTERS_MLB_LEAGUE
        else:
            self.sport_name = "MLB"
            self.league = "MLB"
            self._league_code = FOURCASTERS_MLB_LEAGUE

        self.game_tz = "US/Eastern"
        self._schedule_cache = []
        self._force_relogin = False

    @staticmethod
    def _format_american_str(value) -> str:
        val = int(round(float(value)))
        if val > 0:
            return f"+{val}"
        return str(val)

    @staticmethod
    def _team_name_matches(name_a: str, name_b: str) -> bool:
        return teams_same(name_a or "", name_b or "")

    @staticmethod
    def _participants(game: dict) -> tuple[dict, dict]:
        parts = game.get("participants") or []
        away = home = None
        for p in parts:
            if (p.get("homeAway") or "").lower() == "away":
                away = p
            elif (p.get("homeAway") or "").lower() == "home":
                home = p
        if away and home:
            return away, home
        if len(parts) >= 2:
            return parts[0], parts[1]
        return {}, {}

    @staticmethod
    def _best_order_odds(orders: list | None) -> int | None:
        if not orders:
            return None
        try:
            return int(orders[0].get("odds"))
        except (TypeError, ValueError, IndexError):
            return None

    @classmethod
    def _pick_run_line(cls, away_spreads: list, home_spreads: list, away_id: str, home_id: str):
        """Prefer standard ±1.5 run line; return (team_1_spread, t1_odds, t2_odds)."""
        away_candidates = []
        for order in away_spreads or []:
            if order.get("participantID") != away_id:
                continue
            try:
                sp = float(order.get("spread"))
            except (TypeError, ValueError):
                continue
            away_candidates.append((abs(abs(sp) - MLB_RUN_LINE), sp, order))

        away_candidates.sort(key=lambda x: x[0])
        for _, away_sp, away_order in away_candidates:
            target_home = round(-away_sp, 2)
            for order in home_spreads or []:
                if order.get("participantID") != home_id:
                    continue
                try:
                    home_sp = float(order.get("spread"))
                except (TypeError, ValueError):
                    continue
                if abs(home_sp - target_home) < 0.05 or abs(home_sp + away_sp) < 0.05:
                    t1_odds = cls._format_american_str(away_order.get("odds"))
                    t2_odds = cls._format_american_str(order.get("odds"))
                    return away_sp, t1_odds, t2_odds
        return None, None, None

    def _parse_game_row(self, game: dict) -> dict | None:
        if game.get("live") or game.get("ended"):
            return None
        if game.get("isSpecials"):
            return None
        period = (game.get("periodName") or "").strip().lower()
        if period and period not in ("full time", "game", "match"):
            return None

        away, home = self._participants(game)
        team_1 = (away.get("longName") or "").strip()
        team_2 = (home.get("longName") or "").strip()
        away_id = away.get("id")
        home_id = home.get("id")
        if not team_1 or not team_2 or not away_id or not home_id:
            return None

        ml_away = self._best_order_odds(game.get("awayMoneylines"))
        ml_home = self._best_order_odds(game.get("homeMoneylines"))
        if ml_away is None or ml_home is None:
            return None

        spread_val, spread_1_odds, spread_2_odds = self._pick_run_line(
            game.get("awaySpreads") or [],
            game.get("homeSpreads") or [],
            away_id,
            home_id,
        )
        if spread_val is not None and spread_1_odds and spread_2_odds:
            spread_1_odds, spread_2_odds = fix_spread_odds_orientation(
                spread_val, spread_1_odds, spread_2_odds
            )

        start = game.get("start") or ""
        game_dt = parse_to_mysql_datetime(
            start.replace("T", " ").replace("Z", "")[:19],
            tz_name="UTC",
        )
        if not game_dt:
            game_dt = parse_to_mysql_datetime(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

        game_id = str(game.get("id"))
        return {
            "bookmaker": self.bookmaker,
            "sport": self.sport_name,
            "league": self.league,
            "game_id": game_id,
            "game_datetime": game_dt,
            "match": f"{team_1} vs {team_2}",
            "team_1": team_1,
            "team_2": team_2,
            "moneyline": {
                "team_1": self._format_american_str(ml_away),
                "team_2": self._format_american_str(ml_home),
            },
            "spread": {
                "team_1_spread": spread_val,
                "team_2_spread": -spread_val if isinstance(spread_val, (int, float)) else None,
                "team_1_odds": spread_1_odds,
                "team_2_odds": spread_2_odds,
            },
            "total": {
                "over_total": None,
                "under_total": None,
                "over_odds": None,
                "under_odds": None,
            },
            "line_ids": {
                "team_1": away_id,
                "team_2": home_id,
            },
        }

    def _login(self):
        self.logger.info(f"Account: {self.account_id} | Label: {self.label}")
        data = self.api.login(self.account_id, self.password)
        user = data.get("user") or {}
        balance = user.get("displayBalance")
        self._force_relogin = False
        self.logger.info(
            f"4casters login successful (balance=${balance})"
        )

    def _ensure_session(self):
        if self._force_relogin or not self.api.token:
            self._login()
            return
        self.api.ensure_login(self.account_id, self.password)

    def _invalidate_session(self):
        self._force_relogin = True
        self.api.clear_session()

    def _refresh_schedule_cache(self) -> list:
        games_raw = self.api.get_orderbook(league=self._league_code)
        games = []
        for row in games_raw:
            parsed = self._parse_game_row(row)
            if parsed:
                games.append(parsed)
        self._schedule_cache = games
        self.logger.info(
            f"Parsed {len(games)} pregame {self.sport_name} rows from 4casters {self._league_code}"
        )
        return games

    def _find_game(self, game_id: str, team_name: str, team_1: str = None, team_2: str = None):
        for game in self._schedule_cache:
            if str(game.get("game_id")) != str(game_id):
                continue
            if team_1 and team_2:
                if not (
                    self._team_name_matches(game.get("team_1"), team_1)
                    and self._team_name_matches(game.get("team_2"), team_2)
                ):
                    continue
            if self._team_name_matches(game.get("team_1"), team_name):
                return game, 1
            if self._team_name_matches(game.get("team_2"), team_name):
                return game, 2
        refreshed = self._refresh_schedule_cache()
        for game in refreshed:
            if str(game.get("game_id")) != str(game_id):
                continue
            if self._team_name_matches(game.get("team_1"), team_name):
                return game, 1
            if self._team_name_matches(game.get("team_2"), team_name):
                return game, 2
        return None, None

    def _live_moneyline_for_team(self, game_id: str, team_no: int) -> str | None:
        rows = self.api.get_orderbook(game_id=game_id)
        if not rows:
            return None
        game = rows[0]
        odds = self._best_order_odds(
            game.get("awayMoneylines") if team_no == 1 else game.get("homeMoneylines")
        )
        return self._format_american_str(odds) if odds is not None else None

    def _arb_odds_exact_match(self, live_odds: str, expected_odds: str) -> bool:
        tol = getattr(self, "_odds_tolerance", 0) or 0
        if tol > 0:
            return arb_live_odds_acceptable(expected_odds, live_odds, tol)
        return str(live_odds).strip() == str(expected_odds).strip()

    def _place_bet_via_api(
        self,
        game_id: str,
        participant_id: str,
        american_odds: int,
        risk_amount: float,
        bet_type: str = "moneyline",
        spread_number: float | None = None,
        user_reference: str | None = None,
    ) -> tuple[bool, str]:
        order = {
            "gameID": game_id,
            "type": bet_type,
            "side": participant_id,
            "odds": int(american_odds),
            "bet": round(float(risk_amount), 2),
            "orderType": "fillAndKill",
            "userReference": user_reference or f"arb-{game_id[:8]}",
        }
        if bet_type == "spread" and spread_number is not None:
            order["number"] = float(spread_number)

        self.logger.info(
            f"4casters place | game={game_id} type={bet_type} side={participant_id} "
            f"odds={american_odds} risk=${risk_amount:.2f}"
        )
        results = self.api.place_orders([order])
        if not results:
            raise FourCastersApiError("Empty place-order response")

        result = results[0]
        if result.get("error"):
            raise FourCastersApiError(
                f"{result.get('errorType')}: {result.get('error')}"
            )

        matched = result.get("matched") or []
        if not matched:
            raise FourCastersApiError("Order not matched (fillAndKill found no liquidity)")

        fill = matched[0]
        tx_id = fill.get("txID") or fill.get("wagerRequestID")
        return True, f"matched tx={tx_id} risk={fill.get('risk')} win={fill.get('win')}"

    def _poll_odds_watch_once(self, source: str = "watch", force_relogin: bool = False, **kwargs) -> int:
        if not hasattr(self, "_last_saved_ml"):
            self._last_saved_ml = {}
        try:
            if force_relogin or self._force_relogin:
                self._login()
            else:
                self._ensure_session()
            games = self._refresh_schedule_cache()
        except FourCastersApiError as e:
            self.logger.warning(f"4casters odds poll failed: {e}")
            if "401" in str(e).lower() or "unauthorized" in str(e).lower():
                self._invalidate_session()
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_failure(str(e))
            return 0

        if games and hasattr(self, "_scan_health"):
            self._scan_health.mark_success(len(games))
        elif hasattr(self, "_scan_health"):
            self._scan_health.mark_failure("zero games from 4casters orderbook")

        return persist_moneyline_games(
            self.cache,
            self.storage,
            self.logger,
            games,
            self.sport_name,
            self.league,
            self._last_saved_ml,
            source=source,
        )

    def _maybe_poll_odds_while_idle(self):
        if not hasattr(self, "_last_idle_odds_poll"):
            self._last_idle_odds_poll = 0.0
        now = time.monotonic()
        if now - self._last_idle_odds_poll < self.ODDS_IDLE_POLL_SECONDS:
            return
        self._last_idle_odds_poll = now
        self._poll_odds_watch_once(source="betting-idle")

    def _safe_send_monitoring_alert(self, ex):
        try:
            if TELEGRAM.get("bot_token"):
                asyncio.run(
                    send_monitoring_alert(
                        self.website, self.account_id, ex, TELEGRAM.get("arbitrage_monitoring")
                    )
                )
        except Exception as alert_err:
            self.logger.error(f"Failed to send monitoring alert: {alert_err}")

    @time_it
    def fetch_odds(self, quit_driver=True):
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self.logger.info(
            f"========== Fetching Odds ({self.sport_name}) via 4casters API (START) =========="
        )
        try:
            self._login()
            games = self._refresh_schedule_cache()
            self._last_saved_ml = {}
            persist_moneyline_games(
                self.cache,
                self.storage,
                self.logger,
                games,
                self.sport_name,
                self.league,
                self._last_saved_ml,
                source="fetch",
            )
        except Exception as e:
            self.logger.error(f"fetch_odds failed: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            self.logger.info(
                f"========= Fetching Odds ({self.sport_name}) via 4casters API (END) =========="
            )

    def watch_odds(self, poll_interval: float = None, force_scan_interval: int = None):
        poll_interval = poll_interval or self.ODDS_WATCH_POLL_SECONDS
        force_scan_interval = force_scan_interval or self.ODDS_WATCH_FORCE_SCAN_SECONDS

        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self._last_saved_ml = {}
        self.logger.info(
            f"========== Odds Watch ({self.sport_name}) (START) — 4casters poll {poll_interval}s =========="
        )

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()
        self._scan_health = OddsScanHealthWatchdog(self.logger)
        self._scan_health.start()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                self._login()
                setup_ok = True
                break
            except Exception as e:
                self.logger.error(f"Odds watch setup failed (attempt {attempt}/5): {e}")
                time.sleep(5)

        if not setup_ok:
            self.logger.error("Could not start 4casters odds watch")
            return

        last_force_scan = 0.0
        try:
            while True:
                watchdog.beat()
                now = time.monotonic()
                force_scan = last_force_scan == 0.0 or (now - last_force_scan) >= force_scan_interval
                if force_scan:
                    last_force_scan = now
                try:
                    self._poll_odds_watch_once(
                        source="watch-refresh" if force_scan else "watch",
                        force_relogin=force_scan,
                    )
                except Exception as e:
                    self.logger.warning(f"Odds watch poll failed: {e}")
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            self.logger.info("4casters odds watch stopped by user")
        finally:
            self.logger.info(f"========== Odds Watch ({self.sport_name}) (END) ==========")

    def place_moneyline_bet(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd: str,
        stake: float = 1.0,
        team_1: str = None,
        team_2: str = None,
    ):
        return self.__execute_bet(
            game_id, team_name, moneyline_odd, stake,
            team_1=team_1, team_2=team_2,
        )

    def __execute_bet(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd: str,
        stake: float = 1.0,
        team_1: str = None,
        team_2: str = None,
    ):
        self.logger.info("========== Execute Bet (START) ==========")
        self._last_bet_error = None
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)
        try:
            for attempt in range(1, 3):
                try:
                    if attempt > 1:
                        self.logger.info(f"Retrying 4casters wager after re-login (attempt {attempt}/2)")
                    return self._execute_bet_attempt(
                        game_id, team_name, moneyline_odd, stake,
                        team_1=team_1, team_2=team_2,
                    )
                except FourCastersApiError as e:
                    if attempt == 1 and ("401" in str(e).lower() or "unauthorized" in str(e).lower()):
                        self._invalidate_session()
                        self._login()
                        continue
                    raise
            return False, stake_plan
        except Exception as e:
            self._last_bet_error = str(e)
            self.logger.error(f"Place Bet failed: {e}", exc_info=True)
            asyncio.run(
                send_monitoring_alert(
                    self.website, self.account_id, e, TELEGRAM.get("arbitrage_monitoring")
                )
            )
            return False, stake_plan
        finally:
            self.logger.info("========== Execute Bet (END) ==========")

    def _execute_bet_attempt(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd: str,
        stake: float = 1.0,
        team_1: str = None,
        team_2: str = None,
    ):
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)
        self.logger.info(
            f"Placing Bet | Game ID: {game_id} | Team: {team_name} | "
            f"Odds: {moneyline_odd} | {format_base_amount_stake(stake_plan)}"
        )
        self._ensure_session()

        game_row, team_no = self._find_game(game_id, team_name, team_1=team_1, team_2=team_2)
        if not game_row or not team_no:
            raise FourCastersApiError(
                f"Game {game_id} ({team_name}) not found in live {self.sport_name} schedule"
            )

        line_ids = game_row.get("line_ids") or {}
        participant_id = line_ids.get(f"team_{team_no}")
        if not participant_id:
            raise FourCastersApiError(f"No participant id for {team_name} on game {game_id}")

        live_odds = self._live_moneyline_for_team(game_id, team_no)
        if live_odds is not None and not self._arb_odds_exact_match(live_odds, moneyline_odd):
            tol = getattr(self, "_odds_tolerance", 0) or 0
            raise FourCastersApiError(
                f"Line moved: live odds {live_odds} differ from arb odds {moneyline_odd}"
                + (f" (tolerance ±{tol})" if tol > 0 else "")
            )

        try:
            american_odds = int(str(moneyline_odd).replace("+", ""))
        except ValueError as e:
            raise FourCastersApiError(f"Invalid American odds: {moneyline_odd}") from e

        use_odds = american_odds
        if live_odds is not None:
            try:
                use_odds = int(str(live_odds).replace("+", ""))
            except ValueError:
                use_odds = american_odds

        confirmed, message = self._place_bet_via_api(
            game_id,
            participant_id,
            use_odds,
            stake_plan.risk,
            bet_type="moneyline",
        )
        if not confirmed:
            raise FourCastersApiError(message or "Bet not accepted by bookmaker")
        self.logger.info(f"Bet accepted by bookmaker: {message}")
        return True, stake_plan

    def betting(self, stake: float = 1.0):
        self.logger = Logger.get_logger(f"{self.bookmaker}-betting")
        self.storage = Storage(self.logger)
        self.logger.info("==================== Betting (START) ====================")

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()
        self._scan_health = OddsScanHealthWatchdog(self.logger)
        self._scan_health.start()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                self._login()
                setup_ok = True
                break
            except Exception as e:
                self.logger.error(f"Betting setup failed (attempt {attempt}/5): {e}")
                time.sleep(5)

        if not setup_ok:
            self.logger.error("Could not start 4casters betting loop")
            return

        self._last_saved_ml = {}
        self._poll_odds_watch_once(source="betting-start", force_relogin=False)

        try:
            while True:
                watchdog.beat()
                tick_exposure_cleanup(self.cache, self.logger, TELEGRAM)
                self._maybe_poll_odds_while_idle()

                arbs = self.cache.get_arbitrage(
                    bookmaker=self.bookmaker,
                    bet_type="moneyline",
                )
                if not arbs:
                    time.sleep(1)
                    continue

                for arb in arbs:
                    sport = arb.get("sport")
                    league = arb.get("league")
                    game_datetime = arb.get("game_datetime")
                    bet_type = arb.get("bet_type", "moneyline")
                    team_1 = arb.get("team_1")
                    team_2 = arb.get("team_2")

                    if sport != self.sport_name or league != self.league:
                        continue
                    if not is_game_pregame(game_datetime):
                        self.logger.info(
                            f"Skipping arb (game started) | Match: {team_1} vs {team_2}"
                        )
                        continue

                    if arb.get("team_1_bookmaker") == self.bookmaker:
                        team_no = 1
                        game_id = arb.get("team_1_game_id")
                        team_name = team_1
                        moneyline_odd = arb.get("team_1_odds")
                    elif arb.get("team_2_bookmaker") == self.bookmaker:
                        team_no = 2
                        game_id = arb.get("team_2_game_id")
                        team_name = team_2
                        moneyline_odd = arb.get("team_2_odds")
                    else:
                        continue

                    book_1 = arb.get("team_1_bookmaker")
                    book_2 = arb.get("team_2_bookmaker")

                    if not is_active_arb_pair(book_1, book_2):
                        self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                        continue

                    if self.cache.is_arb_stale(arb):
                        self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                        continue

                    if self.cache.is_leg_placed(self.bookmaker, "moneyline", game_id):
                        self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                        continue

                    if should_pause_first_leg_for_exposure(
                        self.cache, book_1, book_2, self.bookmaker, arb, bet_type
                    ):
                        continue

                    if should_defer_for_sequential_first_leg(
                        self.cache, arb, book_1, book_2, self.bookmaker, bet_type
                    ):
                        continue

                    self._odds_tolerance = odds_tolerance_for_placement(
                        self.cache, arb, book_1, book_2, self.bookmaker, bet_type
                    )

                    bet_placed, stake_used = self.__execute_bet(
                        game_id, team_name, moneyline_odd, stake,
                        team_1=team_1, team_2=team_2,
                    )
                    if bet_placed:
                        finalize_confirmed_bet(
                            self.cache,
                            self.storage,
                            self.logger,
                            arb,
                            self.bookmaker,
                            team_no,
                            team_name,
                            game_id,
                            stake_used,
                            moneyline_odd,
                            TELEGRAM,
                        )
                    else:
                        maybe_notify_partial_arb_exposure(
                            self.cache,
                            self.logger,
                            arb,
                            self.bookmaker,
                            stake,
                            self._last_bet_error or "Bet not accepted by bookmaker",
                            TELEGRAM,
                        )
        except KeyboardInterrupt:
            self.logger.info("4casters betting stopped by user")
        finally:
            self.logger.info("==================== Betting (END) ====================")
