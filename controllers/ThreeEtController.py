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
from utils.config import TELEGRAM, is_active_arb_pair, THREEET_MLB_COMPETITION_ID
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.helpers import (
    decimal_to_american,
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
from utils.threeet_client import ThreeEtApiError, ThreeEtClient
from utils.timing import time_it

MLB_COMPETITION_ID = THREEET_MLB_COMPETITION_ID
MONEYLINE_MARKET = "MONEY_LINE"
MONEYLINE_FALLBACK_MARKETS = ("ONE_X_TWO", "MATCH_ODDS")
HANDICAP_MARKET = "HANDICAP"
PLATFORM = "euro"


class ThreeEtController:
    ODDS_WATCH_POLL_SECONDS = float(os.getenv("THREEET_ODDS_POLL_SEC", "5"))
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("THREEET_ODDS_FORCE_SCAN_SEC", "30"))
    ODDS_IDLE_POLL_SECONDS = float(os.getenv("THREEET_ODDS_IDLE_POLL_SEC", "5"))

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
        self.api = ThreeEtClient()

        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_name = "NBA"
            self.league = "NBA"
            self._competition_id = int(os.getenv("THREEET_NBA_COMPETITION_ID", "0"))
        elif self.sport in ["baseball", "mlb"]:
            self.sport_name = "MLB"
            self.league = "MLB"
            self._competition_id = MLB_COMPETITION_ID
        else:
            self.sport_name = "MLB"
            self.league = "MLB"
            self._competition_id = MLB_COMPETITION_ID

        self.game_tz = "US/Eastern"
        self._schedule_cache = []
        self._force_relogin = False
        self._session_odds_type = "DECIMAL"

    @staticmethod
    def _format_american_str(value) -> str:
        val = int(round(float(value)))
        if val > 0:
            return f"+{val}"
        return str(val)

    @staticmethod
    def _is_us_odds_type(odds_type: str) -> bool:
        return (odds_type or "").upper() in ("US", "AMERICAN")

    @classmethod
    def _runner_price_quote(cls, price_row: dict) -> dict | None:
        """Return american display string and API odds value for bet placement."""
        agg = price_row.get("aggregatedPrices") or []
        if not agg:
            return None
        entry = agg[0]
        odds_type = (entry.get("oddsType") or "DECIMAL").upper()

        if cls._is_us_odds_type(odds_type):
            api_odds = entry.get("odds")
            if api_odds is None:
                api_odds = entry.get("displayOdds")
            if api_odds is None:
                return None
            api_odds = int(round(float(api_odds)))
            return {
                "american_str": cls._format_american_str(api_odds),
                "api_odds": api_odds,
                "odds_type": odds_type,
            }

        decimal_odds = entry.get("decimalOdds")
        if decimal_odds is None:
            decimal_odds = entry.get("odds")
        if decimal_odds is None:
            decimal_odds = entry.get("displayOdds")
        if decimal_odds is None:
            return None
        decimal_odds = float(decimal_odds)
        return {
            "american_str": cls._decimal_to_american_str(decimal_odds),
            "api_odds": decimal_odds,
            "odds_type": odds_type or "DECIMAL",
        }

    @staticmethod
    def _decimal_to_american_str(decimal_odds: float) -> str:
        american = decimal_to_american(decimal_odds, precision=0)
        if american > 0:
            return f"+{int(american)}"
        return str(int(american))

    @staticmethod
    def _team_name_matches(name_a: str, name_b: str) -> bool:
        return teams_same(name_a or "", name_b or "")

    def _login(self):
        self.logger.info(f"Account: {self.account_id} | Label: {self.label}")
        data = self.api.login(self.account_id, self.password)
        session = data.get("session") or {}
        self._session_odds_type = (session.get("oddsType") or "DECIMAL").upper()
        self._force_relogin = False
        self.logger.info(
            f"3et login successful (session token acquired, oddsType={self._session_odds_type})"
        )

    def _ensure_session(self):
        if self._force_relogin or not self.api.session_token:
            self._login()
            return
        self.api.ensure_login(self.account_id, self.password)

    def _invalidate_session(self):
        self._force_relogin = True
        self.api.clear_session()

    def _fetch_competition_events_raw(self) -> list:
        path = f"/data/v3/competitions/{self._competition_id}/events?summarised=true"
        data = self.api.get(path)
        content = data.get("content") if isinstance(data, dict) else None
        if not content:
            return []
        events = []
        for comp in content:
            events.extend(comp.get("events") or [])
        return events

    def _moneyline_market_types(self) -> tuple:
        return (MONEYLINE_MARKET,) + MONEYLINE_FALLBACK_MARKETS

    def _event_needs_detail(self, event: dict) -> bool:
        for mp in event.get("marketPeriods") or []:
            for mt in mp.get("marketTypes") or []:
                if mt.get("marketType") not in self._moneyline_market_types():
                    continue
                for market in mt.get("markets") or []:
                    if market.get("runners"):
                        return False
        return True

    def _enrich_event_from_detail(self, event: dict) -> dict:
        if not self._event_needs_detail(event):
            return event
        event_id = event.get("id")
        if not event_id:
            return event
        for query in (
            f"summarised=true&market-type={MONEYLINE_MARKET}",
            "summarised=false",
        ):
            try:
                detail = self.api.get(f"/data/v3/events/{event_id}?{query}")
            except ThreeEtApiError as e:
                self.logger.warning(f"3et event detail fetch failed for {event_id}: {e}")
                continue
            if isinstance(detail, dict) and detail.get("marketPeriods"):
                return detail
        return event

    def _collect_runner_ids(self, events: list) -> list[int]:
        ids = []
        ml_types = set(self._moneyline_market_types())
        for event in events:
            for mp in event.get("marketPeriods") or []:
                for mt in mp.get("marketTypes") or []:
                    if mt.get("marketType") not in ml_types and mt.get("marketType") != HANDICAP_MARKET:
                        continue
                    for market in mt.get("markets") or []:
                        for runner in market.get("runners") or []:
                            rid = runner.get("id")
                            if rid is not None:
                                ids.append(int(rid))
        return sorted(set(ids))

    def _fetch_runner_prices(self, runner_ids: list[int]) -> dict[int, dict]:
        if not runner_ids:
            return {}
        prices = {}
        chunk_size = 40
        for i in range(0, len(runner_ids), chunk_size):
            chunk = runner_ids[i : i + chunk_size]
            q = ",".join(str(rid) for rid in chunk)
            rows = self.api.get(f"/data/v3/runners/prices?runnerIds={q}")
            if not isinstance(rows, list):
                continue
            for row in rows:
                rid = row.get("runnerId")
                quote = self._runner_price_quote(row)
                if rid is not None and quote:
                    prices[int(rid)] = quote
            time.sleep(0.2)
        return prices

    def _parse_event_row(self, event: dict, price_map: dict[int, float]) -> dict | None:
        if event.get("inRunning"):
            return None
        if (event.get("status") or "").upper() not in ("OPEN", ""):
            return None

        team_1 = (event.get("participant1") or "").strip()
        team_2 = (event.get("participant2") or "").strip()
        if not team_1 or not team_2:
            return None

        ml_team_1 = ml_team_2 = None
        ml_id_1 = ml_id_2 = None
        spread_val = None
        spread_1_odds = spread_2_odds = None
        spread_id_1 = spread_id_2 = None

        for mp in event.get("marketPeriods") or []:
            for mt in mp.get("marketTypes") or []:
                mtype = mt.get("marketType")
                for market in mt.get("markets") or []:
                    runners = market.get("runners") or []
                    if mtype in self._moneyline_market_types() and len(runners) >= 2:
                        priced = []
                        for runner in runners[:2]:
                            rid = runner.get("id")
                            quote = price_map.get(int(rid)) if rid is not None else None
                            if not quote:
                                continue
                            priced.append((runner, quote))
                        if len(priced) < 2:
                            continue
                        for idx, (runner, quote) in enumerate(priced[:2]):
                            american = quote["american_str"]
                            name = runner.get("name") or ""
                            rid = runner.get("id")
                            matched = False
                            if self._team_name_matches(name, team_1):
                                ml_team_1 = american
                                ml_id_1 = rid
                                matched = True
                            elif self._team_name_matches(name, team_2):
                                ml_team_2 = american
                                ml_id_2 = rid
                                matched = True
                            if not matched:
                                if idx == 0:
                                    ml_team_1 = american
                                    ml_id_1 = rid
                                else:
                                    ml_team_2 = american
                                    ml_id_2 = rid
                    elif mtype == HANDICAP_MARKET and len(runners) >= 2:
                        for runner in runners[:2]:
                            rid = runner.get("id")
                            quote = price_map.get(int(rid)) if rid is not None else None
                            if not quote:
                                continue
                            american = quote["american_str"]
                            name = runner.get("name") or ""
                            handicap = runner.get("handicap")
                            if self._team_name_matches(name, team_1):
                                spread_val = handicap
                                spread_1_odds = american
                                spread_id_1 = rid
                            elif self._team_name_matches(name, team_2):
                                spread_2_odds = american
                                spread_id_2 = rid

        if ml_team_1 is None or ml_team_2 is None:
            return None

        start = event.get("startTime") or ""
        game_dt = parse_to_mysql_datetime(
            start.replace("T", " ").replace("Z", "")[:19],
            tz_name="UTC",
        )
        if not game_dt:
            game_dt = parse_to_mysql_datetime(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

        event_id = str(event.get("id"))
        if spread_val is not None:
            try:
                spread_val = float(spread_val)
            except (TypeError, ValueError):
                spread_val = None

        if spread_val is not None and spread_1_odds is not None and spread_2_odds is not None:
            spread_1_odds, spread_2_odds = fix_spread_odds_orientation(
                spread_val, spread_1_odds, spread_2_odds
            )

        return {
            "bookmaker": self.bookmaker,
            "sport": self.sport_name,
            "league": self.league,
            "game_id": event_id,
            "game_datetime": game_dt,
            "match": f"{team_1} vs {team_2}",
            "team_1": team_1,
            "team_2": team_2,
            "moneyline": {"team_1": ml_team_1, "team_2": ml_team_2},
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
                "team_1": ml_id_1,
                "team_2": ml_id_2,
                "spread_team_1": spread_id_1,
                "spread_team_2": spread_id_2,
            },
            "runner_prices": {
                "team_1": price_map.get(int(ml_id_1)) if ml_id_1 else None,
                "team_2": price_map.get(int(ml_id_2)) if ml_id_2 else None,
            },
        }

    def _refresh_schedule_cache(self) -> list:
        raw_events = self._fetch_competition_events_raw()
        pregame = [e for e in raw_events if not e.get("inRunning")]
        enriched = [self._enrich_event_from_detail(e) for e in pregame]
        runner_ids = self._collect_runner_ids(enriched)
        price_map = self._fetch_runner_prices(runner_ids)

        games = []
        retry_events = []
        for event in enriched:
            row = self._parse_event_row(event, price_map)
            if row:
                games.append(row)
            else:
                retry_events.append(event)

        if retry_events:
            missing_ids = [
                rid for rid in self._collect_runner_ids(retry_events)
                if rid not in price_map
            ]
            if missing_ids:
                self.logger.info(
                    f"Retrying 3et runner prices for {len(missing_ids)} ids "
                    f"({len(retry_events)} events missing ML)"
                )
                price_map.update(self._fetch_runner_prices(missing_ids))
            for event in retry_events:
                row = self._parse_event_row(event, price_map)
                if row:
                    games.append(row)

        self._schedule_cache = games
        self.logger.info(
            f"Parsed {len(games)} pregame {self.sport_name} rows from 3et competition "
            f"{self._competition_id}"
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

    def _arb_odds_exact_match(self, live_odds: str, expected_odds: str) -> bool:
        tol = getattr(self, "_odds_tolerance", 0) or 0
        if tol > 0:
            return arb_live_odds_acceptable(expected_odds, live_odds, tol)
        return str(live_odds).strip() == str(expected_odds).strip()

    def _place_bet_via_api(self, runner_id: int, stake: float, api_odds: float) -> tuple[bool, str]:
        body = {
            "bets": [{
                "odds": api_odds,
                "runnerId": int(runner_id),
                "stake": float(stake),
            }],
            "platform": PLATFORM,
            "isWebsocketPrice": False,
        }
        self.logger.info(
            f"3et place bet | runner={runner_id} stake=${stake:.2f} "
            f"odds={api_odds} type={self._session_odds_type}"
        )
        result = self.api.post("/betting/v3/bets", json_body=body)
        bets = []
        if isinstance(result, dict):
            bets = result.get("bets") or []
        elif isinstance(result, list):
            bets = result

        if not bets:
            raise ThreeEtApiError(f"Unexpected bet response: {str(result)[:500]}")

        bet = bets[0] if isinstance(bets[0], dict) else {}
        status = (bet.get("status") or "").upper()
        if status in ("ACCEPTED", "PENDING", "OPEN", "SUCCESS", "PLACED"):
            bet_id = bet.get("id") or bet.get("betId") or bet.get("requestId")
            return True, f"status={status} id={bet_id}"

        message = bet.get("message") or bet.get("rejectReason") or status or "rejected"
        raise ThreeEtApiError(f"Bet rejected: {message}")

    def _poll_odds_watch_once(self, source: str = "watch", force_relogin: bool = False, **kwargs) -> int:
        if not hasattr(self, "_last_saved_ml"):
            self._last_saved_ml = {}
        try:
            if force_relogin or self._force_relogin:
                self._login()
            else:
                self._ensure_session()
            games = self._refresh_schedule_cache()
        except ThreeEtApiError as e:
            self.logger.warning(f"3et odds poll failed: {e}")
            if "401" in str(e).lower() or "session" in str(e).lower():
                self._invalidate_session()
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_failure(str(e))
            return 0

        if games and hasattr(self, "_scan_health"):
            self._scan_health.mark_success(len(games))
        elif hasattr(self, "_scan_health"):
            self._scan_health.mark_failure("zero games from 3et schedule")

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
        self.logger.info(f"========== Fetching Odds ({self.sport_name}) via 3et API (START) ==========")
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
            self.logger.info(f"========= Fetching Odds ({self.sport_name}) via 3et API (END) ==========")

    def watch_odds(self, poll_interval: float = None, force_scan_interval: int = None):
        poll_interval = poll_interval or self.ODDS_WATCH_POLL_SECONDS
        force_scan_interval = force_scan_interval or self.ODDS_WATCH_FORCE_SCAN_SECONDS

        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self._last_saved_ml = {}
        self.logger.info(
            f"========== Odds Watch ({self.sport_name}) (START) — 3et API poll {poll_interval}s =========="
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
            self.logger.error("Could not start 3et odds watch")
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
            self.logger.info("3et odds watch stopped by user")
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
        try:
            for attempt in range(1, 3):
                try:
                    if attempt > 1:
                        self.logger.info(f"Retrying 3et wager after re-login (attempt {attempt}/2)")
                    return self._execute_bet_attempt(
                        game_id, team_name, moneyline_odd, stake,
                        team_1=team_1, team_2=team_2,
                    )
                except ThreeEtApiError as e:
                    if attempt == 1 and ("401" in str(e).lower() or "session" in str(e).lower()):
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
    ):
        self.logger.info(
            f"Placing Bet | Game ID: {game_id} | Team: {team_name} | "
            f"Odds: {moneyline_odd} | Stake: {stake}"
        )
        self._ensure_session()
        game_row, team_no = self._find_game(game_id, team_name, team_1=team_1, team_2=team_2)
        if not game_row or not team_no:
            raise ThreeEtApiError(
                f"Game {game_id} ({team_name}) not found in live {self.sport_name} schedule"
            )

        line_ids = game_row.get("line_ids") or {}
        runner_id = line_ids.get(f"team_{team_no}")
        if not runner_id:
            raise ThreeEtApiError(f"No moneyline runner for {team_name} on game {game_id}")

        live_odds = (game_row.get("moneyline") or {}).get(f"team_{team_no}")
        if live_odds is not None and not self._arb_odds_exact_match(str(live_odds), moneyline_odd):
            tol = getattr(self, "_odds_tolerance", 0) or 0
            raise ThreeEtApiError(
                f"Line moved: live odds {live_odds} differ from arb odds {moneyline_odd}"
                + (f" (tolerance ±{tol})" if tol > 0 else "")
            )

        quote = (game_row.get("runner_prices") or {}).get(f"team_{team_no}")
        if not quote:
            prices = self._fetch_runner_prices([int(runner_id)])
            quote = prices.get(int(runner_id))
        if not quote:
            raise ThreeEtApiError(f"No price quote for runner {runner_id}")

        api_odds = quote.get("api_odds")
        if api_odds is None:
            raise ThreeEtApiError(f"No API odds for runner {runner_id}")

        confirmed, message = self._place_bet_via_api(runner_id, stake, api_odds)
        if not confirmed:
            raise ThreeEtApiError(message or "Bet not accepted by bookmaker")
        self.logger.info(f"Bet accepted by bookmaker: {message}")
        return True, stake

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
                self._ensure_session()
                games = self._refresh_schedule_cache()
                if games:
                    self._scan_health.mark_success(len(games))
                setup_ok = True
                break
            except Exception as e:
                self.logger.error(f"Initial 3et setup failed (attempt {attempt}/5): {e}")
                self._invalidate_session()
                time.sleep(8)

        if not setup_ok:
            self.logger.error("Failed to establish 3et API session.")
            self.logger.info("==================== Betting (END) ====================")
            return

        self._exposure_cleanup_at = 0.0
        while True:
            watchdog.beat()
            self._exposure_cleanup_at = tick_exposure_cleanup(
                self.cache, self.logger, self._exposure_cleanup_at
            )
            time.sleep(2)

            arbs = self.cache.get_arbitrage(bookmaker=self.bookmaker, bet_type="moneyline")
            if not arbs:
                self._maybe_poll_odds_while_idle()
                self.logger.info("Waiting for Arbitrage")
                continue

            self.logger.info(f"Arbitrage opportunities: {len(arbs)}")
            for arb in arbs:
                sport = arb.get("sport")
                league = arb.get("league")
                game_datetime = arb.get("game_datetime")
                team_1 = arb.get("team_1")
                team_2 = arb.get("team_2")

                if sport != self.sport_name or league != self.league:
                    continue
                if not is_game_pregame(game_datetime):
                    self.logger.info(f"Skipping arb (game started) | Match: {team_1} vs {team_2}")
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
                bet_type = arb.get("bet_type", "moneyline")

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
                    game_id, team_name, moneyline_odd, stake, team_1=team_1, team_2=team_2
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