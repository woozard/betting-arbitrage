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

from utils.config import TELEGRAM, ZENROWS_API_KEY
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import (
    parse_to_mysql_datetime,
    parse_odds,
    send_monitoring_alert,
    is_game_pregame,
    debug_filepath,
    prune_debug_files,
)
from utils.bet_placement import finalize_confirmed_bet
from utils.betting_watchdog import BettingLoopWatchdog
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

    def _odds_text_matches(self, displayed: str, expected) -> bool:
        disp = self._normalize_us_odds((displayed or "").strip())
        exp = self._normalize_us_odds(expected)
        if disp == exp:
            return True
        raw = (displayed or "").strip()
        return exp in raw or raw == str(expected).strip()

    def _arb_odds_exact_match(self, displayed, expected) -> bool:
        return self._normalize_us_odds(displayed) == self._normalize_us_odds(expected)

    @staticmethod
    def _team_name_matches(candidate: str, expected: str) -> bool:
        cand = (candidate or "").strip().lower()
        exp = (expected or "").strip().lower()
        if not cand or not exp:
            return False
        return cand == exp or exp in cand or cand in exp

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
                        team_entries.append({
                            "name": name,
                            "rotation": rotation,
                            "line_id": ml.get("i"),
                            "odds": ml.get("o"),
                        })

                    if not team_entries[0]["rotation"] or not team_entries[1]["rotation"]:
                        continue
                    if team_entries[0]["odds"] is None or team_entries[1]["odds"] is None:
                        continue

                    game_id = f"{team_entries[0]['rotation']}-{team_entries[1]['rotation']}"
                    game_dt = self._format_game_datetime(
                        league_date, game.get("t") or game.get("to") or "", tz_name
                    )

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
                            "team_1_spread": None, "team_2_spread": None,
                            "team_1_odds": None, "team_2_odds": None,
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

    def _lookup_line_from_schedule(self, game_id: str, team_name: str):
        if not self._schedule_cache:
            self._refresh_schedule_cache()

        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        if len(rotations) < 2:
            return None, None

        rot1, rot2 = rotations[0], rotations[1]
        for game in self._schedule_cache:
            gid = game.get("game_id") or ""
            parts = [p.strip() for p in gid.split("-") if p.strip()]
            if len(parts) < 2:
                continue
            if parts[0] != rot1 or parts[1] != rot2:
                continue

            if self._team_name_matches(game.get("team_1"), team_name):
                return game, 1
            if self._team_name_matches(game.get("team_2"), team_name):
                return game, 2

        refreshed = self._refresh_schedule_cache()
        for game in refreshed:
            gid = game.get("game_id") or ""
            parts = [p.strip() for p in gid.split("-") if p.strip()]
            if len(parts) < 2 or parts[0] != rot1 or parts[1] != rot2:
                continue
            if self._team_name_matches(game.get("team_1"), team_name):
                return game, 1
            if self._team_name_matches(game.get("team_2"), team_name):
                return game, 2

        return None, None

    @staticmethod
    def _build_straight_wager_payload(line_id, stake: float):
        return [{
            "BetType": BET_TYPE_STRAIGHT,
            "TotalPicks": 1,
            "IdTeaser": 0,
            "IsFreePlay": False,
            "Amount": float(stake),
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

    def _extract_bet_errors(self, data: dict) -> list:
        errors = []
        if not isinstance(data, dict):
            return errors
        for key in ("BetErrors", "Errors", "ErrorMessage"):
            val = data.get(key)
            if isinstance(val, list):
                errors.extend(val)
            elif isinstance(val, str) and val.strip():
                errors.append(val)
        details = data.get("Details") or []
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                err = detail.get("Error") or {}
                if isinstance(err, dict) and err.get("Description"):
                    errors.append(err.get("Description"))
        return errors

    def _place_bet_via_api(self, line_id, stake: float, timeout: int = 45):
        details = self._build_straight_wager_payload(line_id, stake)

        add_result = self._api_call("POST", "/api/wager/AddBet/", details)
        if not add_result or not add_result.get("ok"):
            status = (add_result or {}).get("status")
            if status == 401:
                raise Exception(f"AddBet unauthorized (HTTP {status})")
            raise Exception(f"AddBet failed (HTTP {status})")

        add_data = add_result.get("data") or {}
        add_errors = self._extract_bet_errors(add_data)
        if add_errors:
            raise Exception(f"AddBet rejected: {add_errors[:3]}")

        delay_key = add_data.get("DelayKey") or ""
        save_body = {
            "CaptchaMessage": "",
            "DelayKey": delay_key,
            "DelaySeconds": 0,
            "Details": details,
            "PasswordConfirmation": "",
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
                raise Exception(f"SaveBet rejected: {save_errors[:3]}")

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
        while time.time() < confirm_deadline:
            confirm_result = self._api_call(
                "POST", "/api/wager/confirmBet/", {"TicketNumber": ticket_number}
            )
            if not confirm_result or not confirm_result.get("ok"):
                status = (confirm_result or {}).get("status")
                raise Exception(f"confirmBet failed (HTTP {status})")

            confirm_data = confirm_result.get("data") or {}
            status_code = confirm_data.get("Status")
            if status_code == 2:
                self.logger.info(f"Bet confirmed (TicketNumber={ticket_number})")
                return True, f"TicketNumber={ticket_number}"

            if status_code in (3, 4):
                errors = self._extract_bet_errors(confirm_data)
                raise Exception(f"confirmBet rejected (status={status_code}): {errors[:3]}")

            time.sleep(1)

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
            parsed_odds = parse_odds(odds_data)

            for odd_row in parsed_odds:
                if odd_row.get('bet_type') == 'moneyline':
                    self.cache.add_odds(odd_row)
                try:
                    self.storage.save_odds(odd_row)
                except Exception as db_err:
                    error_str = str(db_err).lower()
                    if "arbitrage_odds" in error_str or "doesn't exist" in error_str or "1146" in error_str:
                        self.logger.warning("[WARN] Table 'arbitrage_odds' issue - continuing")
                    else:
                        self.logger.warning(f"DB save failed: {db_err}")

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
        self.logger.info(
            f"Placing Bet | Game ID: {game_id} | Team: {team_name} | "
            f"Odds: {moneyline_odd} | Stake: {stake}"
        )

        self._refresh_session_before_wager()

        game_row, team_no = self._lookup_line_from_schedule(game_id, team_name)
        if not game_row or not team_no:
            raise Exception(
                f"Game {game_id} ({team_name}) not found in live {self.sport_name} schedule"
            )

        line_ids = game_row.get("line_ids") or {}
        line_id = line_ids.get(f"team_{team_no}")
        if not line_id:
            raise Exception(f"No moneyline line Id for {team_name} on game {game_id}")

        live_odds = (game_row.get("moneyline") or {}).get(f"team_{team_no}")
        if live_odds is not None and not self._arb_odds_exact_match(str(live_odds), moneyline_odd):
            raise Exception(
                f"Line moved: live odds {live_odds} differ from arb odds {moneyline_odd}"
            )

        confirmed, message = self._place_bet_via_api(line_id, stake)
        if not confirmed:
            raise Exception(message or "Bet not accepted by bookmaker")

        self.logger.info(f"Bet accepted by bookmaker: {message}")
        return True, stake

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

        self._cleanup_stale_temp_dirs()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                self._ensure_betting_session()
                self._refresh_schedule_cache()
                setup_ok = True
                break
            except Exception as e:
                self.logger.error(f"Initial setup/login failed (attempt {attempt}/5): {e}")
                self._recover_driver()
                time.sleep(8)

        if not setup_ok:
            self.logger.error("Failed to establish a working browser session after recoveries.")
            self.logger.info("==================== Betting (END) ====================")
            return

        consecutive_recoveries = 0
        while True:
            watchdog.beat()
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

                if self.cache.is_arb_stale(arb):
                    age = self.cache.arb_age_seconds(arb)
                    self.logger.info(
                        f"Skipping dead arb (identified {age:.0f}s ago, max {self.cache.arb_ttl}s) | "
                        f"{team_1} vs {team_2}"
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

                if self._has_existing_open_bet(team_name, team_1, team_2):
                    self.logger.warning(
                        f"Open wager detected via API for {team_name} on {self.bookmaker}; "
                        f"skipping duplicate placement"
                    )
                    continue

                bet_placed, stake = self.__execute_bet(
                    game_id, team_name, moneyline_odd, stake, team_1=team_1, team_2=team_2
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
                        stake,
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