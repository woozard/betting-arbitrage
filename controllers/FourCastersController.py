import asyncio
import os
import time
from datetime import datetime

from cache.arbitrage_cache import ArbitrageCache
from utils.arb_placement import get_arbitrage_for_placement, arb_leg_for_book
from utils.betting_loop import wait_for_arb_or_idle
from utils.bet_placement import (
    REAL_MONEY_BETTING_PAUSED_MSG,
    block_real_money_bet,
    finalize_confirmed_bet,
    finalize_confirmed_bet_with_screenshot,
    capture_bet_screenshot_for_alert,
    maybe_notify_partial_arb_exposure,
    should_defer_for_sequential_first_leg,
    resolve_arb_leg_stake,
    should_notify_failed_bet,
    mark_arb_execution_pause_if_first_leg,
    should_pause_first_leg_for_exposure,
    odds_tolerance_for_placement,
    should_skip_spread_arb_for_placement,
    should_skip_arb_leg_in_betting_loop,
    wait_for_s411_hedge_preposition,
)
from utils.betting_watchdog import (
    BettingLoopWatchdog,
    OddsScanHealthWatchdog,
)
from utils.config import TELEGRAM, is_active_arb_pair, FOURCASTERS_MLB_LEAGUE
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.fourcasters_client import FourCastersApiError, FourCastersClient
from utils.fourcasters_odds import (
    fourcasters_format_net_odds,
    fourcasters_gross_to_net_taker_odds,
    fourcasters_taker_odds_acceptable,
)
from utils.helpers import (
    is_game_pregame,
    parse_to_mysql_datetime,
    send_monitoring_alert,
    teams_same,
    american_odds_to_int,
)
from utils.moneyline_odds import arb_moneyline_odds_acceptable
from utils.logger import Logger
from utils.odds_watch import persist_moneyline_games
from utils.storage import Storage
from utils.stake_sizing import (
    BaseAmountStake,
    base_amount_stake_from_odds,
    cap_base_amount_stake_to_max_risk,
    format_base_amount_stake,
    stake_from_fourcasters_fill,
)
from utils.fourcasters_liquidity import max_taker_risk_from_orders
from utils.timing import time_it

MLB_RUN_LINE = 1.5


class FourCastersController:
    ODDS_WATCH_POLL_SECONDS = float(os.getenv("FOURCASTERS_ODDS_POLL_SEC", "5"))
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("FOURCASTERS_ODDS_FORCE_SCAN_SEC", "5"))
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
        self._screenshot_driver = None

    def _ensure_screenshot_driver(self):
        from utils.fourcasters_web import ensure_fourcasters_web_session

        self._ensure_session()
        if self._screenshot_driver is not None:
            try:
                _ = self._screenshot_driver.current_url
                return self._screenshot_driver
            except Exception:
                self._close_screenshot_driver()

        driver = ensure_fourcasters_web_session(
            self.account_id,
            self.password,
            self.logger,
            api_token=self.api.token,
        )
        self._screenshot_driver = driver
        return driver

    def _close_screenshot_driver(self):
        from utils.fourcasters_web import quit_fourcasters_driver

        quit_fourcasters_driver(self._screenshot_driver)
        self._screenshot_driver = None

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
    def _best_order_odds_gross(orders: list | None) -> int | None:
        if not orders:
            return None
        try:
            return int(orders[0].get("odds"))
        except (TypeError, ValueError, IndexError):
            return None

    @classmethod
    def _best_order_odds_net(cls, orders: list | None) -> int | None:
        gross = cls._best_order_odds_gross(orders)
        if gross is None:
            return None
        return fourcasters_gross_to_net_taker_odds(gross)

    @staticmethod
    def _best_order_odds(orders: list | None) -> int | None:
        """Net taker odds for scanner persistence (legacy name)."""
        return FourCastersController._best_order_odds_net(orders)

    @classmethod
    def _pick_run_line(cls, away_spreads: list, home_spreads: list, away_id: str, home_id: str):
        """Prefer standard ±1.5 run line; return (away_sp, home_sp, t1_odds, t2_odds)."""
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
                    t1_odds = fourcasters_format_net_odds(away_order.get("odds"))
                    t2_odds = fourcasters_format_net_odds(order.get("odds"))
                    return away_sp, home_sp, t1_odds, t2_odds
        return None, None, None, None

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

        spread_val, spread_2_val, spread_1_odds, spread_2_odds = self._pick_run_line(
            game.get("awaySpreads") or [],
            game.get("homeSpreads") or [],
            away_id,
            home_id,
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
                "team_2_spread": spread_2_val,
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

    def _live_moneyline_gross_for_team(self, game_id: str, team_no: int) -> str | None:
        orders = self._moneyline_orders_for_team(game_id, team_no)
        if not orders:
            return None
        odds = self._best_order_odds_gross(orders)
        return self._format_american_str(odds) if odds is not None else None

    def _moneyline_orders_for_team(self, game_id: str, team_no: int) -> list:
        rows = self.api.get_orderbook(game_id=game_id)
        if not rows:
            return []
        game = rows[0]
        return list(
            game.get("awayMoneylines") if team_no == 1 else game.get("homeMoneylines")
            or []
        )

    def _live_moneyline_for_team(self, game_id: str, team_no: int) -> str | None:
        gross = self._live_moneyline_gross_for_team(game_id, team_no)
        if gross is None:
            return None
        try:
            net = fourcasters_gross_to_net_taker_odds(gross)
        except (TypeError, ValueError):
            return gross
        return self._format_american_str(net)

    def _arb_odds_exact_match(self, live_odds: str, expected_odds: str) -> bool:
        tol = getattr(self, "_odds_tolerance", 0) or 0
        return arb_moneyline_odds_acceptable(expected_odds, live_odds, tol)

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
        self._last_fill = fill
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
        bet_type: str = "moneyline",
        spread_line: float | None = None,
    ):
        self.logger.info("========== Execute Bet (START) ==========")
        self._last_bet_error = None
        blocked = block_real_money_bet(self.logger, stake, bet_type=bet_type)
        if blocked is not None:
            self._last_bet_error = REAL_MONEY_BETTING_PAUSED_MSG
            return blocked
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)
        try:
            for attempt in range(1, 3):
                try:
                    if attempt > 1:
                        self.logger.info(f"Retrying 4casters wager after re-login (attempt {attempt}/2)")
                    return self._execute_bet_attempt(
                        game_id, team_name, moneyline_odd, stake,
                        team_1=team_1, team_2=team_2,
                        bet_type=bet_type,
                        spread_line=spread_line,
                    )
                except FourCastersApiError as e:
                    if attempt == 1 and ("401" in str(e).lower() or "unauthorized" in str(e).lower()):
                        self._invalidate_session()
                        self._login()
                        continue
                    raise
            return False, stake
        except Exception as e:
            self._last_bet_error = str(e)
            self.logger.error(f"Place Bet failed: {e}", exc_info=True)
            asyncio.run(
                send_monitoring_alert(
                    self.website, self.account_id, e, TELEGRAM.get("arbitrage_monitoring")
                )
            )
            return False, stake
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
        bet_type: str = "moneyline",
        spread_line: float | None = None,
    ):
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)
        market_label = (
            f"spread {spread_line:+.1f}" if bet_type == "spread" and spread_line is not None else bet_type
        )
        self.logger.info(
            f"Placing Bet | Game ID: {game_id} | Team: {team_name} | "
            f"Market: {market_label} | Odds: {moneyline_odd} | {format_base_amount_stake(stake_plan)}"
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

        spread_market = game_row.get("spread") or {}
        live_gross = None
        if bet_type == "spread":
            live_odds = spread_market.get(f"team_{team_no}_odds")
            spread_number = spread_line
            if spread_number is None:
                spread_number = spread_market.get(f"team_{team_no}_spread")
        else:
            live_odds = None
            live_gross = self._live_moneyline_gross_for_team(game_id, team_no)
            spread_number = None

        tol = getattr(self, "_odds_tolerance", 0) or 0
        if bet_type == "spread":
            if live_odds is not None and not self._arb_odds_exact_match(live_odds, moneyline_odd):
                raise FourCastersApiError(
                    f"Line moved: live odds {live_odds} differ from arb odds {moneyline_odd}"
                    + (f" (tolerance ±{tol})" if tol > 0 else "")
                )
        elif live_gross is not None and not fourcasters_taker_odds_acceptable(
            moneyline_odd, live_gross, tol
        ):
            net_live = fourcasters_gross_to_net_taker_odds(live_gross)
            raise FourCastersApiError(
                f"Line moved: live gross {live_gross} (net {net_live:+d}) "
                f"differ from arb net odds {moneyline_odd}"
                + (f" (tolerance ±{tol})" if tol > 0 else "")
            )

        try:
            american_odds = american_odds_to_int(moneyline_odd)
        except (TypeError, ValueError) as e:
            raise FourCastersApiError(f"Invalid American odds: {moneyline_odd}") from e

        use_odds = american_odds
        if bet_type == "spread" and live_odds is not None:
            try:
                use_odds = american_odds_to_int(live_odds)
            except (TypeError, ValueError):
                use_odds = american_odds
        elif live_gross is not None:
            try:
                use_odds = american_odds_to_int(live_gross)
            except (TypeError, ValueError):
                use_odds = american_odds

        api_bet_type = "spread" if bet_type == "spread" else "moneyline"
        self._last_orderbook_max_risk = None
        if bet_type == "moneyline":
            orders = self._moneyline_orders_for_team(game_id, team_no)
            max_risk = max_taker_risk_from_orders(
                orders,
                participant_id=participant_id,
                gross_odds=int(use_odds),
            )
            if max_risk is not None:
                self._last_orderbook_max_risk = max_risk
                self.logger.info(
                    f"4casters orderbook max risk ${max_risk:.2f} @ {use_odds:+d} "
                    f"(stake risk ${stake_plan.risk:.2f})"
                )
                if max_risk < stake_plan.risk - 0.005:
                    self.logger.info(
                        f"4casters capping stake to orderbook max ${max_risk:.2f} "
                        f"(requested risk ${stake_plan.risk:.2f})"
                    )
                    stake_plan = cap_base_amount_stake_to_max_risk(stake_plan, max_risk)

        confirmed, message = self._place_bet_via_api(
            game_id,
            participant_id,
            use_odds,
            stake_plan.risk,
            bet_type=api_bet_type,
            spread_number=spread_number,
        )
        if not confirmed:
            raise FourCastersApiError(message or "Bet not accepted by bookmaker")
        fill = getattr(self, "_last_fill", None) or {}
        actual_stake = stake_from_fourcasters_fill(fill, stake_plan)
        if actual_stake.american_odds != stake_plan.american_odds:
            self.logger.info(
                f"4casters effective fill odds: {actual_stake.american_odds:+d} "
                f"(arb net {moneyline_odd}, risk=${actual_stake.risk:.2f} win=${actual_stake.to_win:.2f})"
            )
        self.logger.info(f"Bet accepted by bookmaker: {message}")
        return True, actual_stake

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
        self._exposure_cleanup_at = 0.0
        last_idle_poll_at = 0.0
        self._poll_odds_watch_once(source="betting-start", force_relogin=False)

        try:
            while True:
                watchdog.beat()
                self._exposure_cleanup_at = tick_exposure_cleanup(
                    self.cache, self.logger, self._exposure_cleanup_at
                )

                arbs = get_arbitrage_for_placement(self.cache, self.bookmaker)
                if not arbs:
                    _, last_idle_poll_at = wait_for_arb_or_idle(
                        self.cache,
                        self.bookmaker,
                        idle_poll_fn=self._maybe_poll_odds_while_idle,
                        last_idle_poll_at=last_idle_poll_at,
                    )
                    continue

                self.logger.info(f"Arbitrage opportunities: {len(arbs)} — pausing odds scan for placement")

                for arb in arbs:
                    sport = arb.get("sport")
                    league = arb.get("league")
                    game_datetime = arb.get("game_datetime")
                    bet_type = arb.get("bet_type", "moneyline")

                    if should_skip_spread_arb_for_placement(arb, self.logger, self.bookmaker):
                        continue

                    leg = arb_leg_for_book(arb, self.bookmaker)
                    if not leg:
                        continue
                    team_no = leg["team_no"]
                    game_id = leg["game_id"]
                    team_name = leg["team_name"]
                    wager_odds = leg["odds"]
                    spread_line = leg.get("spread_line")
                    team_1 = arb.get("team_1")
                    team_2 = arb.get("team_2")

                    if sport != self.sport_name or league != self.league:
                        continue
                    if not is_game_pregame(game_datetime):
                        self.logger.info(
                            f"Skipping arb (game started) | Match: {team_1} vs {team_2}"
                        )
                        continue

                    book_1 = arb.get("team_1_bookmaker")
                    book_2 = arb.get("team_2_bookmaker")

                    if not is_active_arb_pair(book_1, book_2):
                        self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                        continue

                    if self.cache.is_arb_stale(arb):
                        self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                        continue

                    if should_skip_arb_leg_in_betting_loop(
                        self.cache,
                        self.logger,
                        arb,
                        self.bookmaker,
                        team_name,
                        team_1,
                        team_2,
                    ):
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

                    stake = resolve_arb_leg_stake(
                        self.cache,
                        arb,
                        book_1,
                        book_2,
                        self.bookmaker,
                        wager_odds,
                        stake,
                        logger=self.logger,
                    )

                    mark_arb_execution_pause_if_first_leg(
                        self.cache,
                        arb,
                        book_1,
                        book_2,
                        self.bookmaker,
                        self.logger,
                    )

                    wait_for_s411_hedge_preposition(
                        self.cache, self.logger, arb, self.bookmaker
                    )

                    bet_placed, stake_used = self.__execute_bet(
                        game_id,
                        team_name,
                        wager_odds,
                        stake,
                        team_1=team_1,
                        team_2=team_2,
                        bet_type=bet_type,
                        spread_line=spread_line,
                    )
                    if bet_placed:
                        fill = getattr(self, "_last_fill", None) or {}
                        extra_lines = []
                        tx_id = fill.get("txID") or fill.get("wagerRequestID")
                        if tx_id:
                            extra_lines.append(f"Tx: {tx_id}")
                        if fill.get("risk") is not None:
                            extra_lines.append(f"Risk: ${fill.get('risk')}")
                        if fill.get("win") is not None:
                            extra_lines.append(f"Win: ${fill.get('win')}")
                        placed_odds = (
                            stake_used.american_odds
                            if isinstance(stake_used, BaseAmountStake)
                            else None
                        )
                        finalize_confirmed_bet_with_screenshot(
                            self.cache,
                            self.storage,
                            self.logger,
                            arb,
                            self.bookmaker,
                            team_no,
                            team_name,
                            game_id,
                            stake_used,
                            wager_odds,
                            TELEGRAM,
                            extra_lines=extra_lines or None,
                            driver=self._ensure_screenshot_driver(),
                            open_bets_url="https://4casters.io/my-bets/active-wagers",
                            placed_odds=placed_odds,
                            orderbook_max_risk=getattr(self, "_last_orderbook_max_risk", None),
                        )
                    else:
                        if should_notify_failed_bet(self._last_bet_error):
                            maybe_notify_partial_arb_exposure(
                                self.cache,
                                self.logger,
                                arb,
                                self.bookmaker,
                                stake,
                                self._last_bet_error or "Bet not accepted by bookmaker",
                                TELEGRAM,
                            )
                    break
        except KeyboardInterrupt:
            self.logger.info("4casters betting stopped by user")
        finally:
            self._close_screenshot_driver()
            self.logger.info("==================== Betting (END) ====================")
