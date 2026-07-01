import time
import json
import asyncio
import re
import tempfile
import os
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from utils.config import TELEGRAM, ZENROWS_API_KEY, is_active_arb_pair
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import (
    parse_to_mysql_datetime,
    parse_odds,
    send_monitoring_alert,
    is_game_pregame,
    debug_filepath,
    prune_debug_files,
    teams_same,
    arb_live_odds_acceptable,
)
from utils.bet_placement import (
    REAL_MONEY_BETTING_PAUSED_MSG,
    block_real_money_bet,
    finalize_confirmed_bet,
    maybe_notify_partial_arb_exposure,
    should_defer_for_sequential_first_leg,
    should_notify_failed_bet,
    should_pause_first_leg_for_exposure,
    odds_tolerance_for_placement,
)
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.betting_watchdog import (
    BettingLoopWatchdog,
    OddsScanHealthWatchdog,
    SessionUnauthorizedError,
)
from utils.stake_sizing import (
    BaseAmountStake,
    base_amount_stake_from_odds,
    format_base_amount_stake,
)
from utils.odds_watch import persist_moneyline_games
from utils.timing import time_it
from utils.chrome_temp import cleanup_stale_temp_dirs, handle_init_driver_failure
from cache.arbitrage_cache import ArbitrageCache

API_ORIGIN = "https://paradisewager.com/player-api"
WEBSITE_KEY = "paradisewager"
BET_TYPE_STRAIGHT = "S"
DEFAULT_JS_VERSION = "1.3.47"


class ParadiseWagerController:
    WAGER_SESSION_EXPIRED_MARKERS = (
        "please log in",
        "session expired",
        "logged out",
        "unauthorized",
        "invalid token",
        "http 401",
    )
    ODDS_WATCH_POLL_SECONDS = float(os.getenv("PARADISE_ODDS_POLL_SEC", "5"))
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("PARADISE_ODDS_FORCE_SCAN_SEC", "5"))
    ODDS_IDLE_POLL_SECONDS = float(os.getenv("PARADISE_ODDS_IDLE_POLL_SEC", "5"))
    ODDS_OBSERVER_SELECTORS = ["app-root", "app-schedule", ".schedule-container", "body"]

    def __init__(self, account, site, sport="baseball"):
        self.account_id = account.account
        self.password = account.password
        self.label = account.label if account.label else "N/A"
        self._force_wager_relogin = False
        self._last_bet_error = None

        self.bookmaker = site['bookmaker']
        self.website = site['website']

        self.logger = Logger.get_logger(self.bookmaker)
        self.storage = Storage(self.logger)
        self.cache = ArbitrageCache()

        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_name = "NBA"
            self.league = "NBA"
        elif self.sport in ["baseball", "mlb"]:
            self.sport_name = "MLB"
            self.league = "MLB"
        else:
            self.sport_name = "MLB"
            self.league = "MLB"

        # ParadiseWager schedule displays times in PT (per site UI)
        self.game_tz = "US/Pacific"

        self.base_url = f"https://{self.website}"
        self.login_url = f"{self.base_url}/v2/"
        self.schedule_url = f"{self.base_url}/v2/#/schedule"

        self._access_token = None
        self._token_expire = None
        self._js_version = DEFAULT_JS_VERSION
        self._schedule_cache = []

        try:
            self._create_driver()
        except Exception as e:
            self.logger.error(
                f"Initial driver creation failed in __init__ (betting() will retry with recovery): {e}"
            )
            handle_init_driver_failure(
                self.logger, self.user_data_dir, self.proxy_extension_dir
            )
            self.driver = None
            self.wait = None
            self.user_data_dir = None
            self.proxy_extension_dir = None

    def _set_sport(self, sport: str):
        """Switch sport context on an existing browser session."""
        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_name = "NBA"
            self.league = "NBA"
        elif self.sport in ["baseball", "mlb"]:
            self.sport_name = "MLB"
            self.league = "MLB"
        else:
            raise ValueError(f"Unsupported sport: {sport}. Use 'basketball'/'nba' or 'baseball'/'mlb'.")

    def _create_driver(self):
        proxy_host = "brd.superproxy.io"
        proxy_port = 33335
        proxy_user = "brd-customer-hl_70fad530-zone-arbitrage_bot"
        proxy_pass = "truzviha7wip"

        self.proxy_extension_dir = self._create_proxy_extension(
            proxy_host, proxy_port, proxy_user, proxy_pass
        )

        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-setuid-sandbox")
        options.add_argument(f'--load-extension={self.proxy_extension_dir}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-extensions-except=' + self.proxy_extension_dir)
        options.add_argument(
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
        )

        self.user_data_dir = tempfile.mkdtemp(prefix="chrome_user_data_")
        options.add_argument(f'--user-data-dir={self.user_data_dir}')

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.driver = webdriver.Chrome(options=options)
                try:
                    _ = self.driver.current_url
                except Exception as ve:
                    self.logger.warning(
                        f"Chrome created on attempt {attempt+1} but session is dead: {ve}"
                    )
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(5)
                    continue
                break
            except Exception as e:
                self.logger.warning(
                    f"Chrome driver start attempt {attempt+1}/{max_retries} failed: {e}"
                )
                if attempt == max_retries - 1:
                    raise
                time.sleep(5)

        self.wait = WebDriverWait(self.driver, 30)
        time.sleep(2)

    def _create_proxy_extension(self, host: str, port: int, user: str, password: str) -> str:
        ext_dir = tempfile.mkdtemp(prefix="brightdata_proxy_")
        manifest = {
            "manifest_version": 2,
            "name": "BrightData Proxy Auth",
            "version": "1.0",
            "permissions": [
                "proxy", "tabs", "unlimitedStorage", "storage",
                "webRequest", "webRequestBlocking",
            ],
            "background": {"scripts": ["background.js"]},
        }
        with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        background_js = f"""
        chrome.proxy.settings.set({{
            value: {{
                mode: "fixed_servers",
                rules: {{
                    singleProxy: {{
                        scheme: "http",
                        host: "{host}",
                        port: {port}
                    }}
                }}
            }},
            scope: "regular"
        }}, function() {{}});

        chrome.webRequest.onAuthRequired.addListener(
            function(details) {{
                return {{
                    authCredentials: {{
                        username: "{user}",
                        password: "{password}"
                    }}
                }};
            }},
            {{urls: ["<all_urls>"]}},
            ["blocking"]
        );
        """
        with open(os.path.join(ext_dir, "background.js"), "w") as f:
            f.write(background_js)

        self.logger.info(f"[OK] BrightData proxy extension created at: {ext_dir}")
        return ext_dir

    def _safe_send_monitoring_alert(self, ex):
        try:
            if TELEGRAM.get('bot_token'):
                asyncio.run(
                    send_monitoring_alert(
                        self.website, self.account_id, ex, TELEGRAM.get('arbitrage_monitoring')
                    )
                )
            else:
                self.logger.warning("TELEGRAM bot_token missing - skipping alert")
        except Exception as alert_err:
            self.logger.error(f"Failed to send monitoring alert: {alert_err}")

    @staticmethod
    def _normalize_us_odds(odds) -> str:
        try:
            val = int(float(odds))
            return f"+{val}" if val > 0 else str(val)
        except (TypeError, ValueError):
            text = str(odds).strip()
            return text if text.startswith(("+", "-")) else f"+{text}"

    @staticmethod
    def _decode_paradise_spread_magnitude(raw) -> int | None:
        """Decode Paradise spread price to unsigned American odds magnitude."""
        from utils.helpers import decimal_to_american

        if raw is None or raw == "":
            return None
        try:
            text = str(raw).strip()
            signed = float(text.replace("+", ""))
        except (TypeError, ValueError):
            return None

        if text.startswith("-") or text.startswith("+"):
            return int(abs(signed))

        val = int(abs(signed))
        # Paradise encodes decimal odds as integer cents for larger values (231 == 2.31).
        # Values under 200 are usually already American (+135), not 1.35.
        if val >= 200:
            dec = val / 100.0
            if 1.01 <= dec <= 5.0:
                return abs(int(decimal_to_american(dec)))

        if 1.0 < val <= 10.0:
            return abs(int(decimal_to_american(val)))

        return val

    @staticmethod
    def _american_from_handicap_and_raw(handicap, raw) -> str | None:
        mag = ParadiseWagerController._decode_paradise_spread_magnitude(raw)
        if mag is None:
            return None
        try:
            h = float(handicap)
        except (TypeError, ValueError):
            return ParadiseWagerController._normalize_us_odds(raw)

        if h < 0:
            return str(-mag)
        if h > 0:
            return ParadiseWagerController._normalize_us_odds(mag)
        return ParadiseWagerController._normalize_us_odds(raw)

    @staticmethod
    def _normalize_paradise_spread_american_odds(
        odds_1,
        odds_2,
        handicap_1=None,
        handicap_2=None,
    ):
        """Paradise spread API uses decimal*100 or unsigned American — use handicaps for sign."""
        def to_float(raw):
            if raw is None or raw == "":
                return None
            try:
                return float(str(raw).replace("+", ""))
            except (TypeError, ValueError):
                return None

        o1, o2 = to_float(odds_1), to_float(odds_2)
        if o1 is None or o2 is None:
            return odds_1, odds_2

        if (o1 > 0 and o2 < 0) or (o1 < 0 and o2 > 0):
            return (
                ParadiseWagerController._normalize_us_odds(o1),
                ParadiseWagerController._normalize_us_odds(o2),
            )

        if handicap_1 is not None or handicap_2 is not None:
            a1 = ParadiseWagerController._american_from_handicap_and_raw(handicap_1, odds_1)
            a2 = ParadiseWagerController._american_from_handicap_and_raw(handicap_2, odds_2)
            if a1 is not None and a2 is not None:
                return a1, a2

        if 1.0 < o1 <= 10.0 and 1.0 < o2 <= 10.0:
            from utils.helpers import decimal_to_american
            a1 = int(decimal_to_american(o1))
            a2 = int(decimal_to_american(o2))
            return (
                ParadiseWagerController._normalize_us_odds(a1),
                ParadiseWagerController._normalize_us_odds(a2),
            )

        hi = max(abs(o1), abs(o2))
        lo = min(abs(o1), abs(o2))
        if hi >= 250 and lo >= 100:
            from utils.helpers import decimal_to_american
            d1, d2 = abs(o1) / 100.0, abs(o2) / 100.0
            if 1.2 <= d1 <= 5.0 and 1.2 <= d2 <= 5.0:
                a1 = int(decimal_to_american(d1))
                a2 = int(decimal_to_american(d2))
                return (
                    ParadiseWagerController._normalize_us_odds(a1),
                    ParadiseWagerController._normalize_us_odds(a2),
                )

        m1 = ParadiseWagerController._decode_paradise_spread_magnitude(odds_1)
        m2 = ParadiseWagerController._decode_paradise_spread_magnitude(odds_2)
        if m1 is None or m2 is None:
            return odds_1, odds_2
        if m1 == m2:
            return str(int(o1)), str(int(o2))
        if m1 < m2:
            return str(-m1), ParadiseWagerController._normalize_us_odds(m2)
        return ParadiseWagerController._normalize_us_odds(m1), str(-m2)

    def _odds_text_matches(self, displayed: str, expected) -> bool:
        disp = self._normalize_us_odds((displayed or "").strip())
        exp = self._normalize_us_odds(expected)
        if disp == exp:
            return True
        raw = (displayed or "").strip()
        return exp in raw or raw == str(expected).strip()

    def _arb_odds_exact_match(self, displayed, expected) -> bool:
        tolerance = getattr(self, "_odds_tolerance", 0) or 0
        return arb_live_odds_acceptable(expected, displayed, tolerance)

    @staticmethod
    def _team_name_matches(candidate: str, expected: str) -> bool:
        return teams_same(candidate, expected)

    def _find_game_by_teams(self, schedule, team_name: str, team_1: str = None, team_2: str = None):
        for game in schedule or []:
            for team_no, field in ((1, "team_1"), (2, "team_2")):
                if self._team_name_matches(game.get(field), team_name):
                    self.logger.info(
                        f"Resolved schedule row by team name fallback: {game.get(field)} "
                        f"(game_id={game.get('game_id')})"
                    )
                    return game, team_no
            if team_1 and team_2:
                g1, g2 = game.get("team_1"), game.get("team_2")
                if (teams_same(g1, team_1) and teams_same(g2, team_2)) or (
                    teams_same(g1, team_2) and teams_same(g2, team_1)
                ):
                    if self._team_name_matches(g1, team_name):
                        return game, 1
                    if self._team_name_matches(g2, team_name):
                        return game, 2
        return None, None

    def _lookup_line_from_schedule(
        self, game_id: str, team_name: str, team_1: str = None, team_2: str = None
    ):
        self._refresh_schedule_cache()

        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        if len(rotations) >= 2:
            rot1, rot2 = rotations[0], rotations[1]
            for game in self._schedule_cache:
                gid = game.get("game_id") or ""
                parts = [p.strip() for p in gid.split("-") if p.strip()]
                if len(parts) < 2 or parts[0] != rot1 or parts[1] != rot2:
                    continue

                if self._team_name_matches(game.get("team_1"), team_name):
                    return game, 1
                if self._team_name_matches(game.get("team_2"), team_name):
                    return game, 2

        found = self._find_game_by_teams(self._schedule_cache, team_name, team_1, team_2)
        if found != (None, None):
            return found

        refreshed = self._refresh_schedule_cache()
        return self._find_game_by_teams(refreshed, team_name, team_1, team_2)

    def _api_call(self, method: str, path: str, body=None, auth: bool = True):
        """Execute player-api request inside the authenticated browser context."""
        token = self._access_token if auth else None
        script = """
            const method = arguments[0];
            const path = arguments[1];
            const body = arguments[2];
            const token = arguments[3];
            const headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
            };
            if (token) headers['Authorization'] = 'Bearer ' + token;
            const opts = { method, headers, credentials: 'include' };
            if (body !== null && body !== undefined) {
                opts.body = JSON.stringify(body);
            }
            return fetch('https://paradisewager.com/player-api' + path, opts)
                .then(async (resp) => {
                    let data = null;
                    try { data = await resp.json(); } catch (e) {}
                    return { status: resp.status, ok: resp.ok, data };
                });
        """
        try:
            return self.driver.execute_script(script, method, path, body, token)
        except Exception as e:
            self.logger.error(f"Browser API call failed ({method} {path}): {e}")
            return None

    def _detect_js_version(self):
        try:
            html = self.driver.page_source or ""
            match = re.search(r'main\.([a-f0-9]+)\.js', html)
            if not match:
                return
            js_url = f"{self.base_url}/scripts_v2/main.{match.group(1)}.js"
            script = """
                const url = arguments[0];
                return fetch(url, {credentials: 'include'})
                    .then(r => r.text())
                    .then(t => {
                        const m = t.match(/JSVersion=\"([^\"]+)\"/);
                        return m ? m[1] : null;
                    });
            """
            version = self.driver.execute_script(script, js_url)
            if version:
                self._js_version = version
                self.logger.info(f"Detected JS version: {version}")
        except Exception as e:
            self.logger.warning(f"Could not detect JS version, using default: {e}")

    def __login(self):
        try:
            self.logger.info(f"Account: {self.account_id}")
            self.logger.info(f"Label: {self.label}")
            self.logger.info("Opening ParadiseWager v2 login page")
            self.driver.get(self.login_url)
            time.sleep(5)

            login_debug = debug_filepath("debug_login_paradisewager")
            with open(login_debug, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.info(f"[SAVED] {login_debug}")

            self._detect_js_version()

            payload = {
                "userName": self.account_id,
                "password": self.password,
                "website": WEBSITE_KEY,
                "version": self._js_version,
            }
            result = self._api_call("POST", "/identity/customerLogin/", payload, auth=False)
            if not result or not result.get("ok"):
                status = (result or {}).get("status")
                raise Exception(f"customerLogin failed (HTTP {status})")

            data = result.get("data") or {}
            if data.get("ErrorMessage"):
                raise Exception(f"Login error: {data.get('ErrorMessage')}")

            # ParadiseWager returns AccessToken/ExpirationEpoch (not Token/Expire)
            token = data.get("AccessToken") or data.get("Token")
            if not token:
                raise Exception(f"Login response missing access token: {json.dumps(data)[:300]}")

            self._access_token = token
            self._token_expire = data.get("ExpirationEpoch") or data.get("Expire")
            self._force_wager_relogin = False

            self.driver.execute_script(
                """
                sessionStorage.setItem('access_token', arguments[0]);
                if (arguments[1]) sessionStorage.setItem('access_expire', String(arguments[1]));
                sessionStorage.setItem('Account', arguments[2]);
                sessionStorage.setItem('SelectedBetType', 'S');
                """,
                token,
                self._token_expire,
                self.account_id,
            )

            self.driver.get(self.schedule_url)
            time.sleep(3)
            self._ensure_odds_mutation_observer()
            self.logger.info("Login Successful (player-api token acquired)")
            return True

        except Exception as e:
            self.logger.error(f"Login Failed: {e}")
            with open(debug_filepath("debug_login_paradisewager_FAIL"), "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self._safe_send_monitoring_alert(e)
            raise

    @staticmethod
    def _is_full_game_period(period) -> bool:
        """ParadiseWager uses null (not 0) for full-game periods in the sports menu."""
        return period in (None, 0, "0")

    @staticmethod
    def _item_label(item: dict) -> str:
        parts = [
            item.get("SportSubType"),
            item.get("SportType"),
            item.get("Description"),
            item.get("LeagueName"),
            item.get("PeriodDescription"),
        ]
        return " ".join(str(p).strip() for p in parts if p)

    def _extract_menu_items(self, menu) -> list:
        """Flatten grouped sports menu (Items -> groups -> items) into parent rows."""
        items = []

        def _collect(node):
            if isinstance(node, dict):
                combined = node.get("CombinedItems")
                sport_sub = node.get("SportSubType") or node.get("Description")
                if combined and sport_sub:
                    items.append(node)
                for value in node.values():
                    _collect(value)
            elif isinstance(node, list):
                for entry in node:
                    _collect(entry)

        if isinstance(menu, dict):
            _collect(menu.get("Items") or menu)
        else:
            _collect(menu)

        return items

    def _sport_menu_item_matches(self, item: dict) -> bool:
        if not self._is_full_game_period(item.get("PeriodNumber")):
            return False

        label = self._item_label(item)
        label_l = label.lower()
        sport_sub = (item.get("SportSubType") or "").strip().upper()
        sport_type = (item.get("SportType") or "").strip().lower()
        desc = (item.get("Description") or "").strip().upper()

        if self.sport_name == "MLB":
            if sport_sub == "MLB" or desc == "MLB":
                return True
            if "international" in label_l or "college" in label_l or "ncaa" in label_l:
                return False
            if sport_type == "baseball" and not item.get("PeriodDescription"):
                return True
            return " mlb" in f" {label_l}" or label_l.startswith("mlb")
        if self.sport_name == "NBA":
            if sport_sub == "NBA" or desc == "NBA":
                return True
            if any(x in label_l for x in ("wnba", "college", "ncaa", "euro")):
                return False
            if sport_type == "basketball" and not item.get("PeriodDescription"):
                return True
            return " nba" in f" {label_l}" or label_l.startswith("nba")
        return False

    def _selection_from_menu_item(self, item: dict):
        combined = item.get("CombinedItems") or []
        for ci in combined:
            period = ci.get("PeriodNumber")
            if period is None:
                period = item.get("PeriodNumber", 0)
            if not self._is_full_game_period(period):
                continue
            sport_id = ci.get("IdSportType") or ci.get("IdSport") or ci.get("Id")
            if sport_id is not None:
                return {"IdSport": sport_id, "Period": 0}

        sport_id = item.get("IdSportType") or item.get("IdSport") or item.get("Id")
        if sport_id is not None and self._is_full_game_period(item.get("PeriodNumber")):
            return {"IdSport": sport_id, "Period": 0}
        return None

    def _fetch_sports_menu(self):
        result = self._api_call("GET", f"/api/wager/sportsavailablebyplayeronleague/{BET_TYPE_STRAIGHT}")
        if not result or not result.get("ok"):
            status = (result or {}).get("status")
            self.logger.warning(f"sportsavailablebyplayeronleague failed (HTTP {status})")
            if status == 401:
                raise SessionUnauthorizedError(
                    f"sportsavailablebyplayeronleague unauthorized (HTTP {status})"
                )
            return []
        return result.get("data") or []

    def _resolve_sport_selection(self):
        menu = self._fetch_sports_menu()
        menu_items = self._extract_menu_items(menu)

        for item in menu_items:
            if not self._sport_menu_item_matches(item):
                continue
            selection = self._selection_from_menu_item(item)
            if selection:
                self.logger.info(
                    f"Resolved {self.sport_name} sport: IdSport={selection['IdSport']}, "
                    f"Period={selection['Period']} ({self._item_label(item)[:120]})"
                )
                return selection

        # Log a sample of menu labels to aid debugging when matching fails
        sample_labels = [self._item_label(i) for i in menu_items[:20]]
        self.logger.warning(
            f"No {self.sport_name} entry in sports menu; "
            f"found {len(menu_items)} parent menu items. Sample: {sample_labels[:8]}"
        )
        return None

    def _fetch_schedule_raw(self):
        selection = self._resolve_sport_selection()
        if not selection:
            return []

        body = [selection]
        result = self._api_call(
            "POST",
            f"/api/wager/schedules/{BET_TYPE_STRAIGHT}/0",
            body,
        )
        if not result or not result.get("ok"):
            status = (result or {}).get("status")
            self.logger.warning(f"schedules API failed (HTTP {status})")
            if status == 401:
                raise SessionUnauthorizedError(
                    f"schedules API unauthorized (HTTP {status})"
                )
            return []

        data = result.get("data")
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _normalize_league_date(league_date: str) -> str:
        """API league dates arrive as ISO strings like 2026-06-12T00:00:00."""
        s = (league_date or "").strip()
        if not s:
            return ""
        if "T" in s:
            s = s.split("T", 1)[0]
        elif " " in s and len(s) > 10:
            s = s.split(" ", 1)[0]
        return s

    @staticmethod
    def _normalize_game_time(game_time: str) -> str:
        s = (game_time or "").strip()
        if not s:
            return ""
        if "T" in s:
            s = s.split("T", 1)[-1]
        return s

    @staticmethod
    def _format_game_datetime(league_date: str, game_time: str, tz_name: str):
        if not league_date and not game_time:
            return parse_to_mysql_datetime(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tz_name=tz_name)

        date_str = ParadiseWagerController._normalize_league_date(league_date)
        time_str = ParadiseWagerController._normalize_game_time(game_time)

        if date_str and time_str:
            normalized = parse_to_mysql_datetime(date_str, time_str, tz_name=tz_name)
            if normalized:
                return normalized

        if date_str:
            normalized = parse_to_mysql_datetime(date_str, time_str or "12:00 PM", tz_name=tz_name)
            if normalized:
                return normalized

        if time_str:
            normalized = parse_to_mysql_datetime(datetime.now().strftime("%Y-%m-%d"), time_str, tz_name=tz_name)
            if normalized:
                return normalized

        return parse_to_mysql_datetime(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tz_name=tz_name)

    @staticmethod
    def _extract_team_spread_entry(team: dict):
        line_sets = (team.get("ls") or {})
        for key in ("s", "sp", "ps", "rl"):
            spreads = line_sets.get(key) or []
            if not spreads:
                continue
            entry = spreads[0] if isinstance(spreads, list) else spreads
            if not isinstance(entry, dict):
                continue
            handicap = entry.get("h")
            if handicap is None:
                handicap = entry.get("p")
            if handicap is None:
                handicap = entry.get("line")
            odds = entry.get("o")
            if handicap is None or odds is None:
                continue
            try:
                return float(handicap), str(odds)
            except (TypeError, ValueError):
                continue
        return None, None

    def _parse_schedule_response(self, schedule_data: list):
        games = []
        parsed_rows = []

        for folder in schedule_data or []:
            sc = (folder or {}).get("sc") or {}
            tz_desc = sc.get("tz") or self.game_tz
            tz_name = self.game_tz
            if isinstance(tz_desc, str):
                tz_l = tz_desc.lower()
                if "pacific" in tz_l or tz_l == "pt":
                    tz_name = "US/Pacific"
                elif "eastern" in tz_l or tz_l == "et":
                    tz_name = "US/Eastern"

            for league in sc.get("schl") or []:
                league_date = league.get("d") or ""
                for game in league.get("g") or []:
                    teams = game.get("ts") or []
                    if len(teams) < 2:
                        continue

                    team_entries = []
                    for team in teams:
                        name = team.get("n") or ""
                        rotation = str(team.get("rn") or "").strip()
                        moneylines = (team.get("ls") or {}).get("m") or []
                        ml = moneylines[0] if moneylines else {}
                        spread_val, spread_odds = self._extract_team_spread_entry(team)
                        team_entries.append({
                            "name": name,
                            "rotation": rotation,
                            "line_id": ml.get("i"),
                            "odds": ml.get("o"),
                            "spread_val": spread_val,
                            "spread_odds": spread_odds,
                        })

                    if not team_entries[0]["rotation"] or not team_entries[1]["rotation"]:
                        continue
                    if team_entries[0]["odds"] is None or team_entries[1]["odds"] is None:
                        continue

                    game_id = f"{team_entries[0]['rotation']}-{team_entries[1]['rotation']}"
                    game_dt = self._format_game_datetime(
                        league_date, game.get("t") or game.get("to") or "", tz_name
                    )

                    spread_val = team_entries[0].get("spread_val")
                    spread_1_odds = team_entries[0].get("spread_odds")
                    spread_2_odds = team_entries[1].get("spread_odds")
                    if spread_val is None:
                        spread_val = team_entries[1].get("spread_val")
                        if spread_val is not None:
                            spread_val = -float(spread_val)
                    if spread_1_odds is not None and spread_2_odds is not None:
                        spread_1_odds, spread_2_odds = (
                            self._normalize_paradise_spread_american_odds(
                                spread_1_odds,
                                spread_2_odds,
                                team_entries[0].get("spread_val"),
                                team_entries[1].get("spread_val"),
                            )
                        )
                        from utils.helpers import sanitize_spread_odds
                        cleaned = sanitize_spread_odds(
                            {
                                "team_1_spread": spread_val,
                                "team_2_spread": -spread_val if isinstance(spread_val, (int, float)) else None,
                                "team_1_odds": spread_1_odds,
                                "team_2_odds": spread_2_odds,
                            }
                        )
                        if cleaned is None:
                            spread_1_odds = spread_2_odds = None
                        else:
                            spread_val = cleaned["team_1_spread"]
                            spread_1_odds = cleaned["team_1_odds"]
                            spread_2_odds = cleaned["team_2_odds"]

                    row = {
                        "bookmaker": self.bookmaker,
                        "sport": self.sport_name,
                        "league": self.league,
                        "game_id": game_id,
                        "game_datetime": game_dt,
                        "match": f"{team_entries[0]['name']} vs {team_entries[1]['name']}",
                        "team_1": team_entries[0]["name"],
                        "team_2": team_entries[1]["name"],
                        "moneyline": {
                            "team_1": str(team_entries[0]["odds"]),
                            "team_2": str(team_entries[1]["odds"]),
                        },
                        "spread": {
                            "team_1_spread": spread_val,
                            "team_2_spread": -spread_val if isinstance(spread_val, (int, float)) else None,
                            "team_1_odds": spread_1_odds,
                            "team_2_odds": spread_2_odds,
                        },
                        "total": {
                            "over_total": None, "under_total": None,
                            "over_odds": None, "under_odds": None,
                        },
                        "game_num": game.get("gn"),
                        "line_ids": {
                            "team_1": team_entries[0]["line_id"],
                            "team_2": team_entries[1]["line_id"],
                        },
                    }
                    games.append(row)
                    parsed_rows.append(row)

        self._schedule_cache = parsed_rows
        self.logger.info(f"Parsed {len(games)} {self.sport_name} full-game moneyline rows from schedule API")
        return games

    def _refresh_schedule_cache(self):
        raw = self._fetch_schedule_raw()
        return self._parse_schedule_response(raw)

    @staticmethod
    def _paradise_api_amount(stake: BaseAmountStake) -> float:
        """Paradise SaveBet Amount uses to-win when AmountCalculation is 'A'."""
        return stake.to_win

    @staticmethod
    def _build_straight_wager_payload(line_id, api_amount: float):
        return [{
            "BetType": BET_TYPE_STRAIGHT,
            "TotalPicks": 1,
            "IdTeaser": 0,
            "IsFreePlay": False,
            "Amount": float(api_amount),
            "RoundRobinOptions": [],
            "Wagers": [{
                "Id": line_id,
                "PitcherVisitor": False,
                "PitcherHome": False,
            }],
            "AmountCalculation": "A",
            "ContinueOnPush": True,
            "PropParlay": False,
        }]

    @staticmethod
    def _log_wager_api_payload(logger, stage: str, data, ticket_number=None):
        """Log full wager API JSON when ParadiseWager rejects or returns a non-success status."""
        try:
            payload = json.dumps(data, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = repr(data)
        if len(payload) > 6000:
            payload = payload[:6000] + "...(truncated)"
        ticket_note = f" ticket={ticket_number}" if ticket_number else ""
        logger.warning(f"PW wager API payload ({stage}{ticket_note}): {payload}")

    def _extract_bet_errors(self, data: dict) -> list:
        errors = []
        if not isinstance(data, dict):
            return errors
        for key in (
            "BetErrors",
            "Errors",
            "ErrorMessage",
            "Message",
            "StatusMessage",
            "RejectReason",
            "RejectMessage",
            "Description",
        ):
            val = data.get(key)
            if isinstance(val, list):
                errors.extend(str(v) for v in val if v not in (None, ""))
            elif isinstance(val, str) and val.strip():
                errors.append(val.strip())
            elif val not in (None, "", 0, False):
                errors.append(str(val))
        if data.get("HasLineChange"):
            errors.append("HasLineChange=true")
        details = data.get("Details") or []
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                if detail.get("HasLineChange"):
                    errors.append("Detail HasLineChange=true")
                for key in ("ErrorMessage", "Message", "Description", "RejectReason"):
                    val = detail.get(key)
                    if isinstance(val, str) and val.strip():
                        errors.append(val.strip())
                err = detail.get("Error") or {}
                if isinstance(err, dict):
                    for key in ("Description", "Message", "Code"):
                        val = err.get(key)
                        if val not in (None, ""):
                            errors.append(str(val))
        # De-dupe while preserving order
        seen = set()
        unique = []
        for err in errors:
            if err not in seen:
                seen.add(err)
                unique.append(err)
        return unique

    def _place_bet_via_api(self, line_id, stake: BaseAmountStake, timeout: int = 45):
        api_amount = self._paradise_api_amount(stake)
        self.logger.info(
            f"Paradise wager API | {format_base_amount_stake(stake)} | "
            f"api-amount(to-win)=${api_amount:.2f}"
        )
        details = self._build_straight_wager_payload(line_id, api_amount)

        add_result = self._api_call("POST", "/api/wager/AddBet/", details)
        if not add_result or not add_result.get("ok"):
            status = (add_result or {}).get("status")
            if status == 401:
                raise Exception(f"AddBet unauthorized (HTTP {status})")
            raise Exception(f"AddBet failed (HTTP {status})")

        add_data = add_result.get("data") or {}
        add_errors = self._extract_bet_errors(add_data)
        if add_errors:
            self._log_wager_api_payload(self.logger, "AddBet rejected", add_data)
            raise Exception(f"AddBet rejected: {add_errors[:5]}")

        delay_key = add_data.get("DelayKey") or ""
        save_body = {
            "CaptchaMessage": "",
            "DelayKey": delay_key,
            "DelaySeconds": 0,
            "Details": details,
            "PasswordConfirmation": self.password or "",
        }

        deadline = time.time() + timeout
        ticket_number = None
        while time.time() < deadline:
            save_result = self._api_call("POST", "/api/wager/SaveBet/", save_body)
            if not save_result or not save_result.get("ok"):
                status = (save_result or {}).get("status")
                if status == 401:
                    raise Exception(f"SaveBet unauthorized (HTTP {status})")
                raise Exception(f"SaveBet failed (HTTP {status})")

            save_data = save_result.get("data") or {}
            save_errors = self._extract_bet_errors(save_data)
            if save_errors and not save_data.get("TicketNumber"):
                self._log_wager_api_payload(self.logger, "SaveBet rejected", save_data)
                raise Exception(f"SaveBet rejected: {save_errors[:5]}")

            delay_seconds = save_data.get("DelaySeconds") or 0
            if delay_seconds and int(delay_seconds) > 0:
                save_body["DelayKey"] = save_data.get("DelayKey") or save_body["DelayKey"]
                wait_s = min(int(delay_seconds), 15)
                self.logger.info(f"SaveBet delay {wait_s}s (bookmaker processing)")
                time.sleep(wait_s)
                continue

            ticket_number = save_data.get("TicketNumber")
            if ticket_number:
                break
            time.sleep(1)

        if not ticket_number:
            raise Exception("SaveBet did not return TicketNumber")

        confirm_deadline = time.time() + timeout
        last_confirm_data = None
        while time.time() < confirm_deadline:
            confirm_result = self._api_call(
                "POST", "/api/wager/confirmBet/", {"TicketNumber": ticket_number}
            )
            if not confirm_result or not confirm_result.get("ok"):
                status = (confirm_result or {}).get("status")
                self._log_wager_api_payload(
                    self.logger, f"confirmBet HTTP {status}", confirm_result, ticket_number
                )
                raise Exception(f"confirmBet failed (HTTP {status})")

            confirm_data = confirm_result.get("data") or {}
            last_confirm_data = confirm_data
            status_code = confirm_data.get("Status")
            if status_code == 2:
                self.logger.info(f"Bet confirmed (TicketNumber={ticket_number})")
                return True, f"TicketNumber={ticket_number}"

            if status_code in (3, 4):
                errors = self._extract_bet_errors(confirm_data)
                self._log_wager_api_payload(
                    self.logger,
                    f"confirmBet rejected status={status_code}",
                    confirm_data,
                    ticket_number,
                )
                detail = errors[:5] if errors else ["no parsed rejection message"]
                raise Exception(
                    f"confirmBet rejected (status={status_code}): {detail}"
                )

            time.sleep(1)

        if last_confirm_data is not None:
            self._log_wager_api_payload(
                self.logger,
                "confirmBet timeout last response",
                last_confirm_data,
                ticket_number,
            )
        raise Exception(f"confirmBet not accepted within {timeout}s (ticket={ticket_number})")

    def _fetch_open_wagers_via_api(self):
        result = self._api_call("GET", "/api/customer/pendingfilteredcount/")
        if not result or not result.get("ok"):
            return []

        categories = result.get("data") or []
        if not isinstance(categories, list):
            return []

        picks = []
        for cat in categories[:5]:
            cat_id = cat.get("Id") if isinstance(cat, dict) else cat
            if cat_id is None:
                continue
            detail = self._api_call("GET", f"/api/customer/pendingfiltered/{cat_id}")
            if detail and detail.get("ok"):
                data = detail.get("data")
                if isinstance(data, list):
                    picks.extend(data)
                elif isinstance(data, dict):
                    picks.append(data)
        return picks

    def _pick_matches_open_wager(self, pick, team_name: str, team_1: str, team_2: str) -> bool:
        if not isinstance(pick, dict):
            return False
        text = json.dumps(pick).lower()
        return (
            team_name.lower() in text
            and team_1.lower() in text
            and team_2.lower() in text
        )

    def _has_existing_open_bet(self, team_name: str, team_1: str, team_2: str) -> bool:
        for pick in self._fetch_open_wagers_via_api():
            if self._pick_matches_open_wager(pick, team_name, team_1, team_2):
                return True
        return False

    def _message_requires_relogin(self, message: str) -> bool:
        msg_l = (message or "").lower()
        return any(marker in msg_l for marker in self.WAGER_SESSION_EXPIRED_MARKERS)

    def _invalidate_wager_session(self):
        self._force_wager_relogin = True
        self._access_token = None

    def _is_session_valid(self) -> bool:
        try:
            if self._force_wager_relogin or not self._access_token:
                return False
            url = (self.driver.current_url or "").lower()
            if self.website.lower() not in url:
                return False
            return True
        except Exception:
            return False

    def _ensure_betting_session(self):
        if self._force_wager_relogin or not self._is_session_valid():
            self.logger.info("Session invalid; performing full login")
            self.__login()
            return
        self.logger.info("Session valid; skipping login")

    def _refresh_session_before_wager(self):
        if self._force_wager_relogin or not self._access_token:
            self.logger.info("Wager session invalid; re-login before placement")
            self.__login()
            return
        self.logger.info("Token present; skipping login refresh before wager")

    def _recover_odds_session(self, reason: str, recover_driver: bool = False) -> bool:
        self.logger.warning(f"Recovering Paradise session: {reason}")
        self._invalidate_wager_session()
        if recover_driver:
            try:
                self._recover_driver()
            except Exception as e:
                self.logger.error(f"Driver recovery failed: {e}")
                return False
        try:
            self.__login()
            games = self._refresh_schedule_cache()
            if games and hasattr(self, "_scan_health"):
                self._scan_health.mark_success(len(games))
            return True
        except SessionUnauthorizedError as e:
            self.logger.error(f"Paradise session recovery still unauthorized: {e}")
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_failure(str(e))
            return False
        except Exception as e:
            self.logger.error(f"Paradise session recovery failed: {e}")
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_failure(str(e))
            return False

    def _ensure_odds_mutation_observer(self) -> bool:
        from utils.odds_observer import ensure_mutation_observer
        return ensure_mutation_observer(
            self.driver, self.ODDS_OBSERVER_SELECTORS, self.logger
        )

    def _tick_odds_on_idle(self, last_force_scan: float, idle_label: str = "betting-idle"):
        from utils.odds_watch_tick import tick_controller_odds_watch
        return tick_controller_odds_watch(
            self, last_force_scan, idle_label=idle_label
        )

    def _poll_odds_watch_once(
        self,
        source: str = "watch",
        force_relogin: bool = False,
        force_scan: bool = False,
        **kwargs,
    ) -> int:
        if not hasattr(self, "_last_saved_ml"):
            self._last_saved_ml = {}
        if force_relogin or force_scan or self._force_wager_relogin or not self._access_token:
            self.__login()
        try:
            games = self._refresh_schedule_cache()
        except SessionUnauthorizedError as e:
            if not self._recover_odds_session(str(e), recover_driver=False):
                if hasattr(self, "_scan_health"):
                    self._scan_health.mark_failure(str(e))
                return 0
            games = self._refresh_schedule_cache()

        if games:
            self._consecutive_empty_polls = 0
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_success(len(games))
        else:
            self._consecutive_empty_polls = getattr(self, "_consecutive_empty_polls", 0) + 1
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_failure("zero games from schedule API")
            if self._consecutive_empty_polls >= 5:
                self.logger.warning(
                    f"Paradise schedule empty {self._consecutive_empty_polls} polls; "
                    "attempting driver recovery + re-login"
                )
                self._recover_odds_session(
                    "repeated empty schedule polls", recover_driver=True
                )
                self._consecutive_empty_polls = 0
                try:
                    games = self._refresh_schedule_cache()
                    if games and hasattr(self, "_scan_health"):
                        self._scan_health.mark_success(len(games))
                except SessionUnauthorizedError as e:
                    if hasattr(self, "_scan_health"):
                        self._scan_health.mark_failure(str(e))

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
        if not hasattr(self, "_last_odds_force_scan"):
            self._last_odds_force_scan = 0.0
        try:
            self._last_odds_force_scan, processed = self._tick_odds_on_idle(
                self._last_odds_force_scan,
                idle_label="betting-idle",
            )
            if not processed:
                return
        except SessionUnauthorizedError as e:
            self._recover_odds_session(str(e), recover_driver=True)
            try:
                self._poll_odds_watch_once(source="betting-idle-relogin", force_relogin=True)
            except Exception as relogin_err:
                self.logger.warning(f"Idle odds poll after re-login failed: {relogin_err}")
        except Exception as e:
            msg = str(e).lower()
            if "401" in msg or "unauthorized" in msg:
                self._recover_odds_session(str(e), recover_driver=True)
                try:
                    self._poll_odds_watch_once(source="betting-idle-relogin", force_relogin=True)
                except Exception as relogin_err:
                    self.logger.warning(f"Idle odds poll after re-login failed: {relogin_err}")
            else:
                self.logger.warning(f"Idle odds poll failed: {e}")

    def watch_odds(
        self,
        poll_interval: float = None,
        force_scan_interval: int = None,
    ):
        poll_interval = poll_interval or self.ODDS_WATCH_POLL_SECONDS
        force_scan_interval = force_scan_interval or self.ODDS_WATCH_FORCE_SCAN_SECONDS

        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self._last_saved_ml = {}

        self.logger.info(
            f"========== Odds Watch ({self.sport_name}) (START) — "
            f"player-api poll {poll_interval}s, refresh {force_scan_interval}s =========="
        )

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()
        self._scan_health = OddsScanHealthWatchdog(self.logger)
        self._scan_health.start()
        self._consecutive_empty_polls = 0
        self._cleanup_stale_temp_dirs()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                self.__login()
                setup_ok = True
                break
            except Exception as e:
                self.logger.error(f"Odds watch setup failed (attempt {attempt}/5): {e}")
                self._recover_driver()
                time.sleep(5)

        if not setup_ok:
            self.logger.error("Could not start Paradise odds watch")
            return

        last_force_scan = 0.0

        try:
            while True:
                watchdog.beat()
                try:
                    last_force_scan, processed = self._tick_odds_on_idle(
                        last_force_scan, idle_label="watch"
                    )
                except SessionUnauthorizedError as e:
                    self.logger.warning(f"Odds watch poll unauthorized: {e}")
                    self._recover_odds_session(str(e), recover_driver=True)
                    processed = False
                except Exception as e:
                    msg = str(e).lower()
                    self.logger.warning(f"Odds watch poll failed: {e}")
                    if "401" in msg or "unauthorized" in msg:
                        self._recover_odds_session(str(e), recover_driver=True)
                    processed = False

                if not processed:
                    time.sleep(poll_interval)

        except KeyboardInterrupt:
            self.logger.info("Paradise odds watch stopped by user")
        except Exception as e:
            self.logger.error(f"Fatal Paradise odds watch error: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            self.logger.info(f"========== Odds Watch ({self.sport_name}) (END) ==========")

    @time_it
    def fetch_odds(self, refresh_interval=10, quit_driver=True):
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self.logger.info(
            f"========== Fetching Odds ({self.sport_name}) via player-api (START) =========="
        )
        prune_debug_files()

        try:
            self.__login()
            games = self._refresh_schedule_cache()

            if not games:
                debug_file = debug_filepath(f"debug_paradisewager_{self.sport_name.lower()}")
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(self.driver.page_source)
                self.logger.warning(
                    f"No games found for {self.sport_name}. Inspect {debug_file}"
                )

            odds_data = {
                "sport": self.sport_name,
                "league": self.league,
                "total_matches": len(games),
                "matches": games,
                "timestamp": datetime.now().isoformat(),
            }
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
            if quit_driver:
                self._quit_driver()
            self.logger.info(
                f"========= Fetching Odds ({self.sport_name}) via player-api (END) =========="
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
        blocked = block_real_money_bet(self.logger, stake)
        if blocked is not None:
            self._last_bet_error = REAL_MONEY_BETTING_PAUSED_MSG
            return blocked
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)

        try:
            for attempt in range(1, 3):
                try:
                    if attempt > 1:
                        self.logger.info(f"Retrying wager after re-login (attempt {attempt}/2)")
                    return self._execute_bet_attempt(
                        game_id, team_name, moneyline_odd, stake,
                        team_1=team_1, team_2=team_2,
                    )
                except Exception as e:
                    if attempt == 1 and self._message_requires_relogin(str(e)):
                        self.logger.warning(
                            f"Wager blocked by expired session ({e}); forcing re-login and retry"
                        )
                        self._invalidate_wager_session()
                        self.__login()
                        continue
                    raise
            return False, stake

        except Exception as e:
            self._last_bet_error = str(e)
            self.logger.error(f"Place Bet failed: {e}", exc_info=True)
            asyncio.run(
                send_monitoring_alert(
                    self.website, self.account_id, e, TELEGRAM.get('arbitrage_monitoring')
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
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)
        self.logger.info(
            f"Placing Bet | Game ID: {game_id} | Team: {team_name} | "
            f"Odds: {moneyline_odd} | {format_base_amount_stake(stake_plan)}"
        )

        self._refresh_session_before_wager()

        game_row, team_no = self._lookup_line_from_schedule(
            game_id, team_name, team_1=team_1, team_2=team_2
        )
        if not game_row or not team_no:
            raise Exception(
                f"Game {game_id} ({team_name}) not found in live {self.sport_name} schedule"
            )

        line_ids = game_row.get("line_ids") or {}
        line_id = line_ids.get(f"team_{team_no}")
        if not line_id:
            raise Exception(f"No moneyline line Id for {team_name} on game {game_id}")

        live_odds = (game_row.get("moneyline") or {}).get(f"team_{team_no}")
        if live_odds is not None:
            if not self._arb_odds_exact_match(str(live_odds), moneyline_odd):
                tol = getattr(self, "_odds_tolerance", 0) or 0
                raise Exception(
                    f"Line moved: live odds {live_odds} differ from arb odds {moneyline_odd}"
                    + (f" (tolerance ±{tol})" if tol > 0 else "")
                )
            if getattr(self, "_odds_tolerance", 0):
                try:
                    from utils.helpers import american_odds_to_int
                    if american_odds_to_int(live_odds) != american_odds_to_int(moneyline_odd):
                        self.logger.info(
                            f"Accepting line within ±{self._odds_tolerance}: "
                            f"arb {moneyline_odd} vs live {live_odds}"
                        )
                except (TypeError, ValueError):
                    pass

        confirmed, message = self._place_bet_via_api(line_id, stake_plan)
        if not confirmed:
            raise Exception(message or "Bet not accepted by bookmaker")

        self.logger.info(f"Bet accepted by bookmaker: {message}")
        return True, stake_plan

    def _quit_driver(self):
        driver = getattr(self, "driver", None)
        if not driver:
            return
        try:
            driver.quit()
        except Exception:
            pass
        self.driver = None
        self.wait = None

    def _cleanup_owned_chrome(self):
        import subprocess

        owned_profile = getattr(self, "user_data_dir", None)
        self._quit_driver()

        if owned_profile:
            try:
                subprocess.run(
                    ["pkill", "-f", owned_profile],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(1)
            except Exception:
                pass

    def _cleanup_stale_temp_dirs(self, max_age_seconds: int = 3600):
        cleanup_stale_temp_dirs(
            active_dirs=(
                getattr(self, "user_data_dir", None),
                getattr(self, "proxy_extension_dir", None),
            ),
            max_age_seconds=max_age_seconds,
            logger=self.logger,
        )

    def _recover_driver(self):
        self.logger.info("Recovering from Chrome driver crash...")
        owned_profile = getattr(self, "user_data_dir", None)
        self._cleanup_owned_chrome()

        for attr in ('user_data_dir', 'proxy_extension_dir'):
            d = getattr(self, attr, None)
            if d and os.path.isdir(d):
                try:
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
            setattr(self, attr, None)

        if owned_profile:
            try:
                import subprocess
                subprocess.run(
                    ["pkill", "-f", owned_profile],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(1)
            except Exception:
                pass

        self._invalidate_wager_session()
        self._create_driver()

    def _relogin_after_recovery(self) -> bool:
        try:
            self.__login()
            return True
        except Exception as e:
            self.logger.error(f"Re-login after driver recovery failed: {e}")
            return False

    def betting(self, stake: float = 1.0):
        self.logger = Logger.get_logger(f"{self.bookmaker}-betting")
        self.storage = Storage(self.logger)

        self.logger.info("==================== Betting (START) ====================")

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()
        self._scan_health = OddsScanHealthWatchdog(self.logger)
        self._scan_health.start()
        self._consecutive_empty_polls = 0

        self._cleanup_stale_temp_dirs()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                self._ensure_betting_session()
                games = self._refresh_schedule_cache()
                if games:
                    self._scan_health.mark_success(len(games))
                setup_ok = True
                break
            except SessionUnauthorizedError as e:
                self.logger.error(f"Initial setup unauthorized (attempt {attempt}/5): {e}")
                if self._recover_odds_session(str(e), recover_driver=(attempt >= 3)):
                    setup_ok = True
                    break
                self._recover_driver()
                time.sleep(8)
            except Exception as e:
                self.logger.error(f"Initial setup/login failed (attempt {attempt}/5): {e}")
                self._recover_driver()
                time.sleep(8)

        if not setup_ok:
            self.logger.error("Failed to establish a working browser session after recoveries.")
            self.logger.info("==================== Betting (END) ====================")
            return

        consecutive_recoveries = 0
        self._exposure_cleanup_at = 0.0
        while True:
            watchdog.beat()
            self._exposure_cleanup_at = tick_exposure_cleanup(
                self.cache, self.logger, self._exposure_cleanup_at
            )
            time.sleep(2)

            try:
                current_url = self.driver.current_url
            except Exception as e:
                self.logger.error(f"Driver error getting current URL: {e}. Attempting recovery...")
                self._recover_driver()
                consecutive_recoveries += 1
                if consecutive_recoveries >= 3:
                    backoff = min(60, 10 * consecutive_recoveries)
                    self.logger.warning(
                        f"Multiple recoveries ({consecutive_recoveries}). Backing off {backoff}s."
                    )
                    time.sleep(backoff)
                    consecutive_recoveries = 0
                if not self._relogin_after_recovery():
                    time.sleep(8)
                continue

            if self.website.lower() not in (current_url or "").lower():
                self.logger.warning(f"Unexpected URL detected ({current_url}). Re-establishing session...")
                self._recover_driver()
                consecutive_recoveries += 1
                if not self._relogin_after_recovery():
                    time.sleep(8)
                continue

            consecutive_recoveries = 0
            arbs = self.cache.get_arbitrage(bookmaker=self.bookmaker, bet_type='moneyline')
            if not arbs:
                self._maybe_poll_odds_while_idle()
                self.logger.info("Waiting for Arbitrage")
                continue

            self.logger.info(f"Arbitrage opportunities: {len(arbs)}")

            for arb in arbs:
                sport = arb.get('sport')
                league = arb.get('league')
                game_date = arb.get('game_date')
                game_datetime = arb.get('game_datetime')
                team_1 = arb.get("team_1")
                team_2 = arb.get("team_2")

                if sport != self.sport_name or league != self.league:
                    continue

                if not is_game_pregame(game_datetime):
                    self.logger.info(
                        f"Skipping arb (game started) | Match: {team_1} vs {team_2}"
                    )
                    continue

                self.logger.info(f"Arbitrage | Match: {team_1} vs {team_2}")

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
                    self.logger.warning("Bookmaker mismatch, skipping arb")
                    continue

                book_1 = arb.get("team_1_bookmaker")
                book_2 = arb.get("team_2_bookmaker")
                bet_type = arb.get("bet_type", "moneyline")

                if not is_active_arb_pair(book_1, book_2):
                    self.logger.info(
                        f"Skipping arb — inactive book pair {book_1} x {book_2} | "
                        f"{team_1} vs {team_2}"
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                if self.cache.is_arb_stale(arb):
                    age = self.cache.arb_age_seconds(arb)
                    self.logger.info(
                        f"Skipping dead arb (identified {age:.0f}s ago, max {self.cache.arb_ttl}s) | "
                        f"{team_1} vs {team_2}"
                    )
                    maybe_notify_partial_arb_exposure(
                        self.cache,
                        self.logger,
                        arb,
                        self.bookmaker,
                        stake,
                        f"Arb expired after {age:.0f}s without {self.bookmaker} leg",
                        TELEGRAM,
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                if self.cache.is_leg_placed(self.bookmaker, "moneyline", game_id):
                    self.logger.info(
                        f"Skipping — leg already confirmed on {self.bookmaker} | "
                        f"{team_name} | {team_1} vs {team_2}"
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                if should_pause_first_leg_for_exposure(
                    self.cache, book_1, book_2, self.bookmaker, arb, bet_type
                ):
                    self.logger.info(
                        f"Skipping arb — open partial exposure; pausing new first legs | "
                        f"{team_1} vs {team_2}"
                    )
                    continue

                if should_defer_for_sequential_first_leg(
                    self.cache, arb, book_1, book_2, self.bookmaker, bet_type
                ):
                    self.logger.info(
                        f"Waiting for first-leg confirmation before betting on "
                        f"{self.bookmaker} | {team_1} vs {team_2}"
                    )
                    continue

                self._odds_tolerance = odds_tolerance_for_placement(
                    self.cache, arb, book_1, book_2, self.bookmaker, bet_type
                )
                if self._odds_tolerance:
                    self.logger.info(
                        f"Second-leg odds tolerance ±{self._odds_tolerance} | {team_1} vs {team_2}"
                    )

                if self._has_existing_open_bet(team_name, team_1, team_2):
                    self.logger.warning(
                        f"Open wager detected via API for {team_name} on {self.bookmaker}; "
                        f"skipping duplicate placement"
                    )
                    continue

                bet_placed, stake_used = self.__execute_bet(
                    game_id, team_name, moneyline_odd, stake, team_1=team_1, team_2=team_2
                )
                if not bet_placed and should_notify_failed_bet(self._last_bet_error):
                    maybe_notify_partial_arb_exposure(
                        self.cache,
                        self.logger,
                        arb,
                        self.bookmaker,
                        stake,
                        self._last_bet_error or "Bet not accepted by bookmaker",
                        TELEGRAM,
                    )
                if (
                    not bet_placed
                    and self._last_bet_error
                    and "line moved" in self._last_bet_error.lower()
                ):
                    self.logger.warning(
                        f"Removing stale arb from cache (line moved) for {team_1} vs {team_2} "
                        f"on {self.bookmaker}"
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                if bet_placed:
                    self.logger.info("Bet Placement Completed")
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
                    self._refresh_schedule_cache()

        self.logger.info("==================== Betting (END) ====================")


def main():
    from database.models.Accounts import Accounts
    from utils.config import PARADISEWAGER, PARADIESWAGER_ACCOUNT, PARADIESWAGER_PASSWORD

    if not PARADIESWAGER_ACCOUNT or not PARADIESWAGER_PASSWORD:
        raise ValueError("PARADIESWAGER_ACCOUNT and PARADIESWAGER_PASSWORD must be set in .env")

    account = Accounts(
        account=PARADIESWAGER_ACCOUNT,
        password=PARADIESWAGER_PASSWORD,
        label='Reader',
    )
    controller = ParadiseWagerController(account, PARADISEWAGER, sport="baseball")
    controller.fetch_odds()


if __name__ == "__main__":
    main()