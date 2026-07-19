"""PS3838.com API controller — MLB moneyline odds + arb placement."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

from cache.arbitrage_cache import ArbitrageCache
from utils.arb_placement import arb_leg_for_book, get_arbitrage_for_placement
from utils.bet_placement import (
    finalize_confirmed_bet,
    odds_tolerance_for_placement,
    resolve_arb_leg_stake,
    should_defer_for_sequential_first_leg,
    should_pause_first_leg_for_exposure,
    should_skip_arb_leg_in_betting_loop,
    should_skip_spread_arb_for_placement,
    mark_arb_execution_pause_on_placement_start,
)
from utils.betting_loop import wait_for_arb_or_idle
from utils.betting_watchdog import BettingLoopWatchdog, OddsScanHealthWatchdog
from utils.config import TELEGRAM, is_active_arb_pair
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.helpers import is_game_pregame, parse_to_mysql_datetime, send_monitoring_alert, teams_same
from utils.logger import Logger
from utils.moneyline_odds import arb_moneyline_odds_acceptable
from utils.odds_watch import persist_moneyline_games
from utils.ps3838_client import DEFAULT_BASEBALL_SPORT_ID, Ps3838ApiError, Ps3838Client
from utils.stake_sizing import BaseAmountStake, base_amount_stake_from_odds
from utils.storage import Storage
from utils.timing import time_it


class Ps3838Controller:
    ODDS_WATCH_POLL_SECONDS = float(os.getenv("PS3838_ODDS_POLL_SEC", "3"))
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("PS3838_ODDS_FORCE_SCAN_SEC", "15"))
    ODDS_IDLE_POLL_SECONDS = float(os.getenv("PS3838_ODDS_IDLE_POLL_SEC", "3"))
    MLB_LEAGUE_NAME_HINTS = tuple(
        h.strip().lower()
        for h in os.getenv(
            "PS3838_MLB_LEAGUE_HINTS",
            "mlb,major league baseball,usa - mlb",
        ).split(",")
        if h.strip()
    )

    def __init__(self, account, site, sport="baseball"):
        self.account_id = account.account
        self.password = account.password
        self.label = account.label if getattr(account, "label", None) else "N/A"

        self.bookmaker = site["bookmaker"]
        self.website = site["website"]

        sport_l = (sport or "baseball").strip().lower()
        if sport_l in ("baseball", "mlb"):
            self.sport_name = "MLB"
            self.league = "MLB"
            self._sport_id = int(os.getenv("PS3838_BASEBALL_SPORT_ID", str(DEFAULT_BASEBALL_SPORT_ID)))
        else:
            raise ValueError(f"PS3838 controller currently supports MLB only (got sport={sport})")

        self.api = Ps3838Client(self.account_id, self.password)
        self.cache = ArbitrageCache()
        self.logger = Logger.get_logger(f"{self.bookmaker}-betting")
        self.storage = Storage(self.logger)

        self._schedule_cache: list[dict] = []
        self._event_meta: dict[str, dict] = {}
        self._mlb_league_ids: list[int] | None = None
        self._last_bet_error = None
        self._last_place_response = None
        self._odds_tolerance = 0

    def _team_name_matches(self, a, b) -> bool:
        try:
            return bool(teams_same(a, b))
        except Exception:
            return (a or "").strip().lower() == (b or "").strip().lower()

    def _format_american_str(self, odds) -> str | None:
        if odds is None:
            return None
        try:
            n = int(round(float(odds)))
        except (TypeError, ValueError):
            return None
        return f"{n:+d}"

    def _login(self):
        self.logger.info(f"Account: {self.account_id} | Label: {self.label}")
        bal = self.api.get_balance()
        available = bal.get("availableBalance") or bal.get("balance") or bal.get("outstanding")
        currency = bal.get("currency") or ""
        self.logger.info(f"PS3838 login/balance OK (available={available} {currency})")

    def _resolve_mlb_league_ids(self, fixtures_or_leagues: dict | None = None) -> list[int]:
        if self._mlb_league_ids:
            return self._mlb_league_ids

        explicit = os.getenv("PS3838_MLB_LEAGUE_IDS", "").strip()
        if explicit:
            self._mlb_league_ids = [int(x) for x in explicit.split(",") if x.strip()]
            return self._mlb_league_ids

        leagues_payload = fixtures_or_leagues
        if not leagues_payload:
            leagues_payload = self.api.get_leagues(self._sport_id)

        ids: list[int] = []
        rows = leagues_payload.get("leagues") or leagues_payload.get("league") or []
        for lg in rows:
            name = (lg.get("name") or lg.get("leagueName") or "").strip()
            lname = name.lower()
            if any(h in lname for h in self.MLB_LEAGUE_NAME_HINTS):
                # Prefer main MLB board; skip props/corners when possible.
                if any(x in lname for x in ("corner", "booking", "hit", "run line prop", "innings")):
                    continue
                lid = lg.get("id") or lg.get("leagueId")
                if lid is not None:
                    ids.append(int(lid))

        # If fixtures payload used, league list is nested differently.
        if not ids and isinstance(fixtures_or_leagues, dict):
            for lg in fixtures_or_leagues.get("league") or []:
                name = (lg.get("name") or "").strip().lower()
                if "mlb" in name and "corner" not in name:
                    lid = lg.get("id")
                    if lid is not None:
                        ids.append(int(lid))

        self._mlb_league_ids = sorted(set(ids)) or None
        if not self._mlb_league_ids:
            self.logger.warning("Could not resolve MLB league ids from PS3838; using all baseball leagues")
            return []
        self.logger.info(f"PS3838 MLB league ids: {self._mlb_league_ids}")
        return self._mlb_league_ids

    def _parse_starts(self, starts) -> str | None:
        if not starts:
            return None
        try:
            # API times are GMT/UTC ISO.
            if isinstance(starts, str) and starts.endswith("Z"):
                dt = datetime.fromisoformat(starts.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(str(starts))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return parse_to_mysql_datetime(dt.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            try:
                return parse_to_mysql_datetime(str(starts))
            except Exception:
                return None

    def _build_games_from_feed(self, fixtures: dict, odds: dict) -> list[dict]:
        events_by_id: dict[int, dict] = {}
        league_by_event: dict[int, dict] = {}
        for lg in fixtures.get("league") or []:
            for ev in lg.get("events") or []:
                eid = ev.get("id")
                if eid is None:
                    continue
                events_by_id[int(eid)] = ev
                league_by_event[int(eid)] = lg

        odds_by_event: dict[int, dict] = {}
        for lg in odds.get("league") or []:
            for ev in lg.get("events") or []:
                eid = ev.get("id")
                if eid is None:
                    continue
                odds_by_event[int(eid)] = ev

        games = []
        pregame_only = os.getenv("PS3838_PREGAME_ONLY", "true").lower() in ("1", "true", "yes")
        for eid, ev in events_by_id.items():
            status = (ev.get("status") or "").upper()
            live_status = ev.get("liveStatus")
            # liveStatus: 0/2 pregame-ish, 1 live (per FAQ)
            if pregame_only and live_status == 1:
                continue
            if status not in ("O", "I", ""):
                # H = unavailable
                if status == "H":
                    continue

            odd_ev = odds_by_event.get(eid)
            if not odd_ev:
                continue
            periods = odd_ev.get("periods") or []
            period0 = None
            for p in periods:
                if int(p.get("number") or -1) == 0:
                    period0 = p
                    break
            if not period0:
                continue
            if int(period0.get("status") or 0) != 1:
                continue
            ml = period0.get("moneyline") or {}
            home_odds = ml.get("home")
            away_odds = ml.get("away")
            if home_odds is None or away_odds is None:
                continue

            lg = league_by_event.get(eid) or {}
            home = ev.get("home") or ev.get("homeTeam") or "Team2"
            away = ev.get("away") or ev.get("awayTeam") or "Team1"
            # Canonical ordering in this bot: team_1 = away, team_2 = home (matches 4casters).
            team_1 = away
            team_2 = home
            game_dt = self._parse_starts(ev.get("starts"))
            line_id = period0.get("lineId")
            max_ml = period0.get("maxMoneyLine") or period0.get("maxMoneyline")

            row = {
                "bookmaker": self.bookmaker,
                "sport": self.sport_name,
                "league": self.league,
                "game_id": str(eid),
                "game_datetime": game_dt,
                "match": f"{team_1} vs {team_2}",
                "team_1": team_1,
                "team_2": team_2,
                "max_risk": {"team_1": max_ml, "team_2": max_ml},
                "moneyline": {
                    "team_1": self._format_american_str(away_odds),
                    "team_2": self._format_american_str(home_odds),
                },
                "spread": {
                    "team_1_spread": None,
                    "team_2_spread": None,
                    "team_1_odds": None,
                    "team_2_odds": None,
                },
                "total": {
                    "over_total": None,
                    "under_total": None,
                    "over_odds": None,
                    "under_odds": None,
                },
                "line_ids": {"team_1": line_id, "team_2": line_id},
            }
            self._event_meta[str(eid)] = {
                "league_id": lg.get("id"),
                "league_name": lg.get("name"),
                "event_id": eid,
                "home": home,
                "away": away,
                "line_id": line_id,
                "period_number": 0,
                "home_odds": home_odds,
                "away_odds": away_odds,
                "starts": ev.get("starts"),
                "status": status,
                "live_status": live_status,
            }
            games.append(row)
        return games

    def _refresh_schedule_cache(self) -> list[dict]:
        league_ids = self._mlb_league_ids
        fixtures = self.api.get_fixtures(self._sport_id, league_ids=league_ids or None)
        if not league_ids:
            league_ids = self._resolve_mlb_league_ids(fixtures)
            if league_ids:
                fixtures = self.api.get_fixtures(self._sport_id, league_ids=league_ids)
        odds = self.api.get_odds(
            self._sport_id,
            league_ids=league_ids or None,
            odds_format="American",
        )
        games = self._build_games_from_feed(fixtures, odds)
        self._schedule_cache = games
        self.logger.info(f"Parsed {len(games)} pregame {self.sport_name} rows from PS3838")
        return games

    def list_mlb_games(self) -> list[dict]:
        """Public helper for ops scripts — returns parsed MLB game rows + meta."""
        self._login()
        games = self._refresh_schedule_cache()
        out = []
        for g in games:
            meta = self._event_meta.get(str(g["game_id"]), {})
            out.append({**g, "meta": meta})
        return out

    def _poll_odds_watch_once(self, source: str = "watch", **kwargs) -> int:
        if not hasattr(self, "_last_saved_ml"):
            self._last_saved_ml = {}
        try:
            games = self._refresh_schedule_cache()
        except Ps3838ApiError as e:
            self.logger.warning(f"PS3838 odds poll failed: {e}")
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_failure(str(e))
            return 0

        if games and hasattr(self, "_scan_health"):
            self._scan_health.mark_success(len(games))
        elif hasattr(self, "_scan_health"):
            self._scan_health.mark_failure("zero games from PS3838 feed")

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

    def _live_moneyline_for_team(self, game_id: str, team_no: int) -> str | None:
        self._refresh_schedule_cache()
        for g in self._schedule_cache:
            if str(g.get("game_id")) != str(game_id):
                continue
            ml = g.get("moneyline") or {}
            return ml.get(f"team_{team_no}")
        return None

    def _place_moneyline(
        self,
        game_id: str,
        team_no: int,
        risk_amount: float,
        expected_american: int,
    ) -> tuple[bool, str]:
        meta = self._event_meta.get(str(game_id))
        if not meta:
            self._refresh_schedule_cache()
            meta = self._event_meta.get(str(game_id))
        if not meta:
            raise Ps3838ApiError(f"No PS3838 meta for event {game_id}")

        team = "Team1" if team_no == 1 else "Team2"
        # Fixtures homeTeamType default is Team1=home on some books; PS3838 FAQ:
        # homeTeamType from leagues. We store team_1=away, team_2=home, so:
        # team_no 1 (away) -> Team2 if homeTeamType=Team1 (default Pinnacle).
        home_team_type = (meta.get("home_team_type") or "Team1").strip()
        if home_team_type == "Team1":
            team = "Team2" if team_no == 1 else "Team1"
        else:
            team = "Team1" if team_no == 1 else "Team2"

        line = self.api.get_line(
            sport_id=self._sport_id,
            league_id=int(meta["league_id"]),
            event_id=int(meta["event_id"]),
            period_number=0,
            bet_type="MONEYLINE",
            team=team,
            odds_format="American",
        )
        status = (line.get("status") or "").upper()
        if status not in ("SUCCESS", "OK", ""):
            # SUCCESS is normal; others may still include price
            if status and status not in ("SUCCESS",):
                if status in ("NOT_EXISTS", "OFFLINE", "PROCESSED_WITH_ERROR"):
                    raise Ps3838ApiError(f"GetLine status={status}: {line}")

        price = line.get("price") or line.get("odds")
        line_id = line.get("lineId") or meta.get("line_id")
        if price is not None:
            live = int(round(float(price)))
            if not arb_moneyline_odds_acceptable(
                str(expected_american), str(live), getattr(self, "_odds_tolerance", 0) or 0
            ):
                raise Ps3838ApiError(
                    f"Odds moved: expected {expected_american:+d} live {live:+d}"
                )

        resp = self.api.place_straight_bet(
            sport_id=self._sport_id,
            league_id=int(meta["league_id"]),
            event_id=int(meta["event_id"]),
            period_number=0,
            line_id=int(line_id),
            bet_type="MONEYLINE",
            stake=float(risk_amount),
            win_risk_stake="RISK",
            team=team,
            accept_better_line=True,
        )
        self._last_place_response = resp
        status = (resp.get("status") or resp.get("errorCode") or "").upper()
        if status in ("ACCEPTED", "SUCCESS", ""):
            bet_id = resp.get("betId") or (resp.get("straightBet") or {}).get("betId")
            return True, f"accepted betId={bet_id} status={status or 'OK'}"
        if status == "PENDING_ACCEPTANCE":
            uid = resp.get("uniqueRequestId")
            return True, f"pending_acceptance uniqueRequestId={uid}"
        raise Ps3838ApiError(f"PlaceBet rejected: {resp}")

    def __execute_bet(
        self,
        game_id,
        team_name,
        moneyline_odd,
        stake,
        team_1=None,
        team_2=None,
        bet_type="moneyline",
        spread_line=None,
    ):
        if bet_type != "moneyline":
            self._last_bet_error = "PS3838 only places moneyline in this build"
            return False, stake

        team_no = None
        for g in self._schedule_cache or self._refresh_schedule_cache():
            if str(g.get("game_id")) != str(game_id):
                continue
            if self._team_name_matches(g.get("team_1"), team_name):
                team_no = 1
                break
            if self._team_name_matches(g.get("team_2"), team_name):
                team_no = 2
                break
        if team_no is None:
            self._last_bet_error = f"team {team_name} not found on event {game_id}"
            return False, stake

        stake_plan = (
            stake
            if isinstance(stake, BaseAmountStake)
            else base_amount_stake_from_odds(stake, moneyline_odd)
        )
        try:
            american = int(stake_plan.american_odds)
            ok, msg = self._place_moneyline(
                str(game_id), team_no, float(stake_plan.risk), american
            )
            self.logger.info(f"PS3838 place: {msg}")
            return ok, stake_plan
        except Exception as e:
            self._last_bet_error = str(e)
            self.logger.error(f"PS3838 place failed: {e}")
            return False, stake_plan

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
        self.logger.info(f"========== Fetching Odds ({self.sport_name}) via PS3838 API (START) ==========")
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
            self.logger.info(f"========= Fetching Odds ({self.sport_name}) via PS3838 API (END) ==========")

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
            self.logger.error("Could not start PS3838 betting loop")
            return

        self._last_saved_ml = {}
        self._exposure_cleanup_at = 0.0
        last_idle_poll_at = 0.0
        self._poll_odds_watch_once(source="betting-start")

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

                self.logger.info(
                    f"Arbitrage opportunities: {len(arbs)} — pausing odds scan for placement"
                )

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
                    stake_amt = resolve_arb_leg_stake(
                        self.cache,
                        arb,
                        book_1,
                        book_2,
                        self.bookmaker,
                        wager_odds,
                        stake,
                        logger=self.logger,
                    )
                    mark_arb_execution_pause_on_placement_start(
                        self.cache,
                        arb,
                        book_1,
                        book_2,
                        self.bookmaker,
                        self.logger,
                    )

                    bet_placed, stake_used = self.__execute_bet(
                        game_id,
                        team_name,
                        wager_odds,
                        stake_amt,
                        team_1=team_1,
                        team_2=team_2,
                        bet_type=bet_type,
                        spread_line=spread_line,
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
                            wager_odds,
                            TELEGRAM,
                        )
                    else:
                        self.logger.error(
                            f"PS3838 bet failed: {self._last_bet_error} | {team_1} vs {team_2}"
                        )
                        self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
        finally:
            self.logger.info("==================== Betting (END) ====================")
