import time
import json
import asyncio
import re
import tempfile
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import sqlalchemy.exc

from utils.config import (
    PROXY1,
    PROXY2,
    TELEGRAM,
    ZENROWS_API_KEY,
    is_active_arb_pair,
    BETAMAPOLA_API_PLACEMENT,
)
from utils.betamapola_offering import parse_get_sport_offering_response
from utils.betamapola_wager_api import (
    accept_betamapola_line_changes,
    betamapola_process_ticket_via_api,
    click_line_via_dom_button,
    ensure_betamapola_stake_ready,
    parse_process_ticket_response,
    wait_for_angular_game_lines,
    wait_for_betamapola_wager_items,
)
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import parse_to_mysql_datetime, parse_odds, currency_to_float, send_telegram_alert, send_monitoring_alert, send_testing_alert, is_game_pregame, debug_filepath, prune_debug_files, get_debug_dir, normalize_team, teams_same, resolve_ticosports_spread_lines, spread_values_match
from utils.moneyline_odds import arb_moneyline_odds_acceptable
from utils.arb_placement import get_arbitrage_for_placement, arb_leg_for_book
from utils.betting_loop import wait_for_arb_or_idle
from utils.ticosports_wager import (
    click_line_via_angular,
    wager_network_entry_confirms,
    pick_looks_like_open_wager,
    betslip_text_confirms_wager,
    betamapola_wager_item_count,
    betamapola_betslip_is_empty,
    invoke_betamapola_process_ticket,
    sync_betamapola_stake_models,
)
from utils.bet_screenshot import capture_betamapola_confirmation, bet_screenshot_path
from utils.bet_placement import (
    REAL_MONEY_BETTING_PAUSED_MSG,
    block_real_money_bet,
    finalize_confirmed_bet,
    acknowledge_placed_leg,
    capture_bet_screenshot_for_alert,
    maybe_notify_partial_arb_exposure,
    should_defer_for_sequential_first_leg,
    should_notify_failed_bet,
    should_pause_first_leg_for_exposure,
    odds_tolerance_for_placement,
    should_skip_spread_arb_for_placement,
    should_skip_arb_leg_in_betting_loop,
)
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.betting_watchdog import BettingLoopWatchdog
from utils.stake_sizing import (
    base_amount_stake_from_odds,
    format_base_amount_stake,
)
from utils.stake_entry import fill_betslip_stake_input
from utils.odds_watch import persist_moneyline_games
from utils.timing import time_it
from utils.chrome_temp import cleanup_stale_temp_dirs, handle_init_driver_failure
from cache.arbitrage_cache import ArbitrageCache


class BetamapolaController:
    WAGER_SESSION_EXPIRED_MARKERS = (
        "please log in",
        "session expired",
        "logged out",
    )
    ODDS_WATCH_POLL_SECONDS = float(os.getenv("BETAMAPOLA_ODDS_POLL_SEC", "5"))
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("BETAMAPOLA_ODDS_FORCE_SCAN_SEC", "5"))
    ODDS_IDLE_POLL_SECONDS = float(os.getenv("BETAMAPOLA_ODDS_IDLE_POLL_SEC", "5"))
    ODDS_OBSERVER_SELECTORS = ["#GameLines", "#gamesAccordion", "#GameLinesCtrl"]
    API_PLACEMENT_ENABLED = BETAMAPOLA_API_PLACEMENT
    PENDING_CHECK_CACHE_TTL = 45

    # ===================================================================
    # Betamapola.com - uses identical browser/scraping stack as Sports411
    # (Selenium + BrightData proxy extension + ZenRows for odds polling)
    # ===================================================================
    def __init__(self, account, site, sport="baseball"):  # MLB primary for this book

        # Credentials
        self.account_id = account.account
        self.password = account.password
        self.label = account.label if account.label else "N/A"
        self._force_wager_relogin = False
        self._last_bet_error = None
        self._betamapola_ticket_submitted = False
        self._pending_check_cache = {}
        self._last_screenshot_path = None
        self._api_offering_failures = 0
        self._last_full_game_line_count = 0
        self._thin_offering_recovery_attempted = False

        # Site Config
        self.bookmaker = site['bookmaker']
        self.website = site['website']

        # Logger & Storage
        self.logger = Logger.get_logger(self.bookmaker)
        self.storage = Storage(self.logger)

        # Cache
        self.cache = ArbitrageCache()

        # === Sport configuration ===
        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_name = "NBA"
            self.league = "NBA"
        elif self.sport in ["baseball", "mlb"]:
            self.sport_name = "MLB"
            self.league = "MLB"
        else:
            # Default to MLB (currently primary offering)
            self.sport_name = "MLB"
            self.league = "MLB"

        # Timezone for game times returned by this book's API / page.
        # All game_datetimes are normalized to UTC via pytz for consistent matching
        # across bookmakers that may display times in ET vs PT etc.
        self.game_tz = 'US/Eastern'

        # Set URLs (SPA after login)
        self.base_url = f"https://www.{self.website}"
        self.login_url = f"https://{self.website}"
        self.dashboard_url = f"https://{self.website}/sports#/"
        self.sport_url = f"https://{self.website}/sports#/"

        # Create BrightData-proxied Chrome (with retries + fresh temps). Extracted so
        # _recover_driver can also use it for full re-initialization after crashes.
        # We catch here so a flaky first creation does not kill the entry script before
        # betting() (and its recovery loop) ever runs.
        try:
            self._create_driver()
        except Exception as e:
            self.logger.error(f"Initial driver creation failed in __init__ (betting() will retry with recovery): {e}")
            handle_init_driver_failure(
                self.logger, self.user_data_dir, self.proxy_extension_dir
            )
            self.driver = None
            self.wait = None
            self.user_data_dir = None
            self.proxy_extension_dir = None

    def _create_driver(self):
        """Build ChromeOptions + BrightData MV2 proxy extension and launch webdriver.Chrome
        with a 3-attempt retry. Used from __init__ and from _recover_driver.
        """
        # === BrightData Proxy Extension (exact same as Sports411Controller) ===
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
        options.add_argument("--disable-accelerated-2d-canvas")
        options.add_argument(f'--load-extension={self.proxy_extension_dir}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-extensions-except=' + self.proxy_extension_dir)
        options.add_argument(
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
        )

        # Enable performance logging so we can capture network requests (XHR/Fetch)
        options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

        # Use a unique user data dir to avoid profile conflicts in service restarts
        self.user_data_dir = tempfile.mkdtemp(prefix="chrome_user_data_")
        options.add_argument(f'--user-data-dir={self.user_data_dir}')

        # Retry driver creation - Chrome + extension is flaky under systemd
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.driver = webdriver.Chrome(options=options)

                # Smoke test: the webdriver object was returned but the actual browser
                # process can die immediately (common with proxy extensions). Verify it responds.
                try:
                    _ = self.driver.current_url
                except Exception as ve:
                    self.logger.warning(f"Chrome created on attempt {attempt+1} but session is dead: {ve}")
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(5)
                    continue

                break
            except Exception as e:
                self.logger.warning(f"Chrome driver start attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(5)

        self.wait = WebDriverWait(self.driver, 30)
        time.sleep(2)  # brief stabilization

    def _relogin_after_recovery(self) -> bool:
        """After _recover_driver() has given us a brand-new Chrome instance (unlogged),
        re-perform login and navigate to the target sport page so we are in a usable state.
        Returns True if successful.
        """
        try:
            self.__login()
            self.__ensure_sport_offering_loaded()
            return True
        except Exception as e:
            self.logger.error(f"Re-login and navigation after driver recovery failed: {e}")
            return False

    # === helper methods from Sports411Controller / Web5Controller ===
    def _create_proxy_extension(self, host: str, port: int, user: str, password: str) -> str:
        """Dynamically creates a Chrome Proxy Extension with authentication (MV2 for compatibility)"""
        ext_dir = tempfile.mkdtemp(prefix="brightdata_proxy_")
        manifest = {
            "manifest_version": 2,
            "name": "BrightData Proxy Auth",
            "version": "1.0",
            "permissions": [
                "proxy",
                "tabs",
                "unlimitedStorage",
                "storage",
                "webRequest",
                "webRequestBlocking"
            ],
            "background": {
                "scripts": ["background.js"]
            }
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

    def _zenrows_get(self, url: str, js_render: bool = True, wait: int = 20000):
        """Zenrows helper - identical pattern to Sports411Controller"""
        params = {
            "apikey": ZENROWS_API_KEY,
            "url": url,
            "js_render": "true" if js_render else "false",
            "wait": str(wait),
            "premium_proxy": "true",
            "antibot": "true",
            "proxy_country": "us",
        }
        for attempt in range(3):
            try:
                resp = requests.get("https://api.zenrows.com/v1/", params=params, timeout=180)
                resp.raise_for_status()
                self.logger.info(f"[OK] Zenrows request successful for {url}")
                return resp.text
            except Exception as e:
                self.logger.error(f"Zenrows request failed (attempt {attempt + 1}): {e}")
                if attempt == 2:
                    raise
                time.sleep(5)
        raise Exception("Zenrows failed after 3 attempts")

    def _safe_send_monitoring_alert(self, ex):
        """Safe version - does NOT crash if token is missing (same as Sports411)"""
        try:
            if TELEGRAM.get('bot_token'):
                asyncio.run(
                    send_monitoring_alert(self.website, self.account_id, ex, TELEGRAM.get('arbitrage_monitoring')))
            else:
                self.logger.warning("TELEGRAM bot_token missing - skipping alert")
        except Exception as alert_err:
            self.logger.error(f"Failed to send monitoring alert: {alert_err}")

    # --------------------------------------------------------
    # Login (adapted for Betamapola form - exact same IDs + button)
    # --------------------------------------------------------
    def __login(self):
        try:
            self.logger.info(f"Account: {self.account_id}")
            self.logger.info(f"Label: {self.label}")

            self.logger.info("Opening Login Page")
            self.driver.get(self.login_url)
            time.sleep(6)

            login_debug = debug_filepath("debug_login_betamapola")
            with open(login_debug, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.info(f"[SAVED] {login_debug}")

            # Hard block detection (reuse pattern)
            page_source_lower = self.driver.page_source.lower()
            if "sorry, you have been blocked" in page_source_lower or "attention required" in page_source_lower:
                self.logger.error("[BLOCK] HARD BLOCK DETECTED - SWITCHING TO ZENROWS")
                self.logger.info("Using Zenrows for login...")
                html = self._zenrows_get(self.login_url)
                self.logger.info("[OK] Zenrows login page retrieved successfully")
                return True

            # Normal Selenium login - IDs are IDENTICAL to Sports411 (account / password)
            account_input = self.wait.until(
                EC.presence_of_element_located((By.ID, "account"))
            )
            password_input = self.wait.until(
                EC.presence_of_element_located((By.ID, "password"))
            )

            account_input.clear()
            account_input.send_keys(self.account_id)
            password_input.clear()
            password_input.send_keys(self.password)

            # Betamapola uses <button id="LogInAccount" type="submit">LOGIN</button>
            try:
                login_btn = self.driver.find_element(By.ID, "LogInAccount")
            except Exception:
                login_btn = self.driver.find_element(
                    By.CSS_SELECTOR, "button[data-action='login'], form.login-form button[type='submit']"
                )

            login_btn.click()

            self.wait.until(EC.url_contains("/sports"))
            self._force_wager_relogin = False
            self.logger.info("Login Successful")
            # Ensure Angular sports shell is loaded before offering navigation.
            self.driver.get(self.sport_url)
            try:
                self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "app-sports, #gamesAccordion, a.sportIcon")
                    )
                )
            except Exception:
                self.logger.warning("Sports shell slow after login; offering nav will retry")
            return True

        except Exception as e:
            self.logger.error(f"Login Failed: {e}")
            with open(debug_filepath("debug_login_betamapola_FAIL"), "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self._safe_send_monitoring_alert(e)
            raise

    def __inject_mutation_observer(self):
        from utils.odds_observer import install_mutation_observer
        self.logger.info("Injecting MutationObserver on game lines (JS)")
        install_mutation_observer(self.driver, self.ODDS_OBSERVER_SELECTORS, self.logger)

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

    # ===================================================================
    # Direct API access (GetSportOffering) - the reliable path
    # Uses the exact endpoint + payload discovered via browser inspection.
    # Executed inside the authenticated Selenium session so cookies, JWT,
    # origin, and the BrightData proxy are all inherited automatically.
    # ===================================================================
    @staticmethod
    def _normalize_us_odds(odds) -> str:
        """Normalize -101.0 / '-101' / +100.0 to comparable integer strings."""
        try:
            val = int(float(odds))
            return f"+{val}" if val > 0 else str(val)
        except (TypeError, ValueError):
            text = str(odds).strip()
            return text if text.startswith(("+", "-")) else f"+{text}"

    def _odds_text_matches(self, displayed: str, expected) -> bool:
        tolerance = getattr(self, "_odds_tolerance", 0) or 0
        if tolerance > 0 and arb_moneyline_odds_acceptable(expected, displayed, tolerance):
            return True
        disp = self._normalize_us_odds((displayed or "").strip())
        exp = self._normalize_us_odds(expected)
        if disp == exp:
            return True
        raw = (displayed or "").strip()
        return exp in raw or raw == str(expected).strip()

    @staticmethod
    def _team_name_matches(candidate: str, expected: str) -> bool:
        return teams_same(candidate, expected)

    def _find_game_line_by_teams(self, api_lines, team_name: str, team_1: str = None, team_2: str = None):
        for gl in api_lines or []:
            if gl.get("PeriodNumber") != 0:
                continue
            for team_no, field in ((1, "Team1ID"), (2, "Team2ID")):
                if self._team_name_matches(gl.get(field), team_name):
                    self.logger.info(
                        f"Resolved game by team name fallback: {gl.get(field)} "
                        f"(GameNum={gl.get('GameNum')}, rot={gl.get('Team1RotNum')}-{gl.get('Team2RotNum')})"
                    )
                    return gl, team_no
            if team_1 and team_2:
                gl_t1 = gl.get("Team1ID")
                gl_t2 = gl.get("Team2ID")
                aligned = teams_same(gl_t1, team_1) and teams_same(gl_t2, team_2)
                flipped = teams_same(gl_t1, team_2) and teams_same(gl_t2, team_1)
                if aligned or flipped:
                    if self._team_name_matches(gl_t1, team_name):
                        return gl, 1
                    if self._team_name_matches(gl_t2, team_name):
                        return gl, 2
        return None, None

    def _lookup_game_line_from_api(
        self,
        game_id: str,
        team_name: str,
        team_1: str = None,
        team_2: str = None,
        moneyline_odd: str | None = None,
    ):
        """Resolve a live API game line by rotation id, then team-name fallback."""
        api_lines = self._fetch_game_lines_via_api()
        if not api_lines:
            return None, None

        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        if len(rotations) >= 2:
            rot1, rot2 = rotations[0], rotations[1]
            matches = []
            for gl in api_lines:
                if gl.get("PeriodNumber") != 0:
                    continue
                gl_rot1 = str(gl.get("Team1RotNum") or "").strip()
                gl_rot2 = str(gl.get("Team2RotNum") or "").strip()
                if gl_rot1 != rot1 or gl_rot2 != rot2:
                    continue

                team_no = None
                if self._team_name_matches(gl.get("Team1ID"), team_name):
                    team_no = 1
                elif self._team_name_matches(gl.get("Team2ID"), team_name):
                    team_no = 2

                if team_no is not None:
                    matches.append((gl, team_no))

            if matches:
                if moneyline_odd:
                    for gl, team_no in matches:
                        live_odds = gl.get(f"MoneyLine{team_no}")
                        if live_odds is not None and self._odds_text_matches(
                            str(live_odds), moneyline_odd
                        ):
                            return gl, team_no
                return matches[0]

        return self._find_game_line_by_teams(api_lines, team_name, team_1=team_1, team_2=team_2)

    def _trigger_angular_offering_select(self) -> bool:
        """Select the active sport/league in the Angular SPA (same UI path users take)."""
        if self.sport_name == "NBA":
            label_for = "gl_Basketball_NBA_G"
        else:
            label_for = "gl_Baseball_MLB_G"

        try:
            result = self.driver.execute_script("""
                var labelFor = arguments[0];
                var label = document.querySelector('label[for="' + labelFor + '"]');
                if (!label) return false;

                label.click();

                try {
                    var scope = angular.element(label).scope();
                    if (scope && scope.Events && scope.sport && scope.sub) {
                        if (scope.ClearFilter) scope.ClearFilter();
                        scope.Events.ToggleOffering(scope.sport, scope.sub, true);
                        if (scope.$apply) scope.$apply();
                    }
                } catch (e) {}

                return true;
            """, label_for)
            if result:
                self.logger.info(f"Triggered Angular offering select for {self.sport_name}")
            else:
                self.logger.warning(f"Could not find Angular offering label for {self.sport_name}")
            return bool(result)
        except Exception as e:
            self.logger.warning(f"Angular offering select failed: {e}")
            return False

    def _wait_for_moneyline_button(self, game_num, team_no: int, timeout: int = 25):
        """Wait for M{team}_{GameNum}_0 button (TicoSports DOM id format)."""
        selector = f"button#M{team_no}_{game_num}_0, #M{team_no}_{game_num}_0"
        end = time.time() + timeout
        while time.time() < end:
            elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if elems:
                return elems[0]
            time.sleep(0.5)
        return None

    def _betslip_text(self) -> str:
        try:
            return (self.driver.find_element(By.ID, "betSlipDiv").text or "").strip()
        except Exception:
            return ""

    def _betslip_wager_count(self) -> int:
        return betamapola_wager_item_count(self.driver)

    def _betslip_is_empty(self) -> bool:
        return betamapola_betslip_is_empty(
            self._betslip_text(), self._betslip_wager_count()
        )

    def _betslip_has_spread_pick(self, team_name: str) -> bool:
        if self._betslip_is_empty():
            return False
        slip = self._betslip_text().lower()
        if "spread" not in slip:
            return False
        if team_name.lower() in slip:
            return True
        last_word = team_name.strip().split()[-1].lower() if team_name.strip() else ""
        return bool(last_word and last_word in slip)

    def _betslip_stake_inputs_visible(self) -> bool:
        from utils.stake_entry import (
            DEFAULT_RISK_SELECTORS,
            DEFAULT_WIN_SELECTORS,
            _find_stake_input,
        )

        scope = "#betSlipBody"
        return bool(
            _find_stake_input(self.driver, DEFAULT_RISK_SELECTORS, scope)
            or _find_stake_input(self.driver, DEFAULT_WIN_SELECTORS, scope)
        )

    def _betslip_has_team(self, team_name: str) -> bool:
        if self._betslip_is_empty():
            return False
        slip = self._betslip_text().lower()
        if team_name.lower() in slip:
            return True
        last_word = team_name.strip().split()[-1].lower() if team_name.strip() else ""
        return bool(last_word and last_word in slip)

    def _betslip_has_pick(self, team_name: str, bet_type: str = "moneyline") -> bool:
        if bet_type == "spread":
            return self._betslip_has_spread_pick(team_name)
        return self._betslip_has_team(team_name)

    def _wait_for_betslip_team(
        self, team_name: str, timeout: int = 8, bet_type: str = "moneyline"
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._betslip_wager_count() > 0 and self._betslip_has_pick(team_name, bet_type):
                return True
            time.sleep(0.4)
        return False

    def _prepare_bet_slip_for_wager(self):
        """Clear stale picks so GameLineAction adds to an empty straight bet slip."""
        if self._betslip_is_empty():
            return
        try:
            cleared = self.driver.execute_script("""
                function betamapolaBetSlipScope() {
                    var root = document.getElementById('betSlipDiv');
                    if (!root || typeof angular === 'undefined') return null;
                    var scope = angular.element(root).scope();
                    while (scope) {
                        if (typeof scope.CancelAction === 'function') return scope;
                        scope = scope.$parent;
                    }
                    return null;
                }
                var scope = betamapolaBetSlipScope();
                if (scope) {
                    scope.CancelAction(true);
                    if (scope.$apply) scope.$apply();
                    return 'CancelAction';
                }
                var btn = document.querySelector(
                    "#betSlipDiv button[ng-click*='CancelAction'], #betSlipDiv .btn-cancelbet"
                );
                if (btn) { btn.click(); return 'cancel_button'; }
                return null;
            """)
            if cleared:
                self.logger.info(f"Cleared existing bet slip picks ({cleared})")
                time.sleep(0.5)
        except Exception as e:
            self.logger.warning(f"Could not clear bet slip: {e}")

    def _add_moneyline_to_slip(
        self, game_line: dict, team_no: int, team_name: str,
        moneyline_elem=None,
    ) -> bool:
        """Click DOM and/or Angular until the bet slip actually contains the team."""
        self._prepare_bet_slip_for_wager()
        game_num = game_line.get("GameNum")

        if not moneyline_elem and game_num and team_no:
            moneyline_elem = self._wait_for_moneyline_button(game_num, team_no, timeout=10)

        if moneyline_elem:
            self.logger.info(f"Moneyline element located: {moneyline_elem.get_attribute('id')}")
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", moneyline_elem)
            time.sleep(0.4)
            self.driver.execute_script("arguments[0].click();", moneyline_elem)
            self.logger.info("Moneyline element clicked")
            if self._wait_for_betslip_team(team_name, timeout=5, bet_type="moneyline"):
                return True
            self.logger.warning(
                f"DOM click on M{team_no}_{game_num}_0 did not populate bet slip; trying Angular"
            )

        if self._click_moneyline_via_angular(game_line, team_no):
            if self._wait_for_betslip_team(team_name, timeout=6, bet_type="moneyline"):
                return True
            self.logger.warning("Angular GameLineAction did not populate bet slip")

        return False

    def _click_moneyline_via_angular(self, game_line: dict, team_no: int) -> bool:
        """Add moneyline pick via Angular GameLineAction when DOM buttons are not clickable yet."""
        try:
            result = self.driver.execute_script("""
                var gameNum = arguments[0];
                var rot1 = String(arguments[1]);
                var rot2 = String(arguments[2]);
                var teamNo = arguments[3];

                function invoke(scope) {
                    if (!scope || !scope.GameLineAction) return false;
                    var lines = scope.sortedGameLines || scope.GameLines || [];
                    for (var i = 0; i < lines.length; i++) {
                        var gl = lines[i];
                        if (!gl || gl.IsTitle) continue;
                        var match = String(gl.GameNum) === String(gameNum)
                            || (String(gl.Team1RotNum) === rot1 && String(gl.Team2RotNum) === rot2);
                        if (!match) continue;
                        scope.GameLineAction(gl, 'M', teamNo);
                        if (scope.$apply) scope.$apply();
                        return true;
                    }
                    return false;
                }

                var root = document.getElementById('GameLinesCtrl')
                    || document.querySelector('#gamesAccordion')
                    || document.querySelector('app-sports');
                if (!root || typeof angular === 'undefined') return false;

                var scope = angular.element(root).scope();
                if (invoke(scope)) return true;

                var child = scope && scope.$$childHead;
                while (child) {
                    if (invoke(child)) return true;
                    child = child.$$nextSibling;
                }
                return false;
            """, game_line.get("GameNum"), game_line.get("Team1RotNum"),
                 game_line.get("Team2RotNum"), team_no)
            if result:
                self.logger.info(
                    f"Added moneyline via Angular GameLineAction "
                    f"(GameNum={game_line.get('GameNum')}, team={team_no})"
                )
            return bool(result)
        except Exception as e:
            self.logger.warning(f"Angular GameLineAction failed: {e}")
            return False

    def _wait_for_spread_button(self, game_num, team_no: int, timeout: int = 25):
        """Wait for S{team}_{GameNum}_0 button (TicoSports DOM id format)."""
        selector = f"button#S{team_no}_{game_num}_0, #S{team_no}_{game_num}_0"
        end = time.time() + timeout
        while time.time() < end:
            elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if elems:
                return elems[0]
            time.sleep(0.5)
        return None

    def _find_spread_element(
        self,
        game_id: str,
        team_name: str,
        wager_odds,
        game_line: dict = None,
        team_no: int = None,
        spread_line: float | None = None,
    ):
        game_num = (game_line or {}).get("GameNum")
        period = (game_line or {}).get("PeriodNumber", 0)
        team_lower = team_name.lower()

        if game_num is not None and team_no in (1, 2):
            for selector in (
                f"button#S{team_no}_{game_num}_{period}",
                f"#S{team_no}_{game_num}_{period}",
            ):
                candidates = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if candidates:
                    txt = (candidates[0].text or candidates[0].get_attribute("innerText") or "").strip()
                    if self._odds_text_matches(txt, wager_odds):
                        return candidates[0]
                    if spread_line is not None:
                        spread_txt = re.search(r"([+-]?\d+(?:\.\d+)?)", txt)
                        if spread_txt and spread_values_match(spread_txt.group(1), spread_line):
                            self.logger.info(
                                f"Using spread button {candidates[0].get_attribute('id')} "
                                f"with live odds {txt} (arb {wager_odds})"
                            )
                            return candidates[0]

        if game_num is not None:
            for prefix in ("S1_", "S2_"):
                candidates = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    f"button[id^='{prefix}'][id*='_{game_num}_'], span[id^='{prefix}'][id*='_{game_num}_']",
                )
                for cand in candidates:
                    txt = (cand.text or cand.get_attribute("innerText") or "").strip()
                    if not self._odds_text_matches(txt, wager_odds):
                        continue
                    if spread_line is not None:
                        spread_txt = re.search(r"([+-]?\d+(?:\.\d+)?)", txt)
                        if spread_txt and not spread_values_match(spread_txt.group(1), spread_line):
                            continue
                    return cand

        for cand in self.driver.find_elements(
            By.CSS_SELECTOR, "button[id^='S1_'], button[id^='S2_'], span[id^='S1_'], span[id^='S2_']"
        ):
            try:
                row = cand.find_element(
                    By.XPATH,
                    "./ancestor::*[contains(@class,'game') or contains(@class,'line') or contains(@class,'betting')][1]",
                )
                row_text = (row.text or "").lower()
            except Exception:
                row_text = (cand.text or "").lower()

            txt = (cand.text or cand.get_attribute("innerText") or "").strip()
            if team_lower in row_text and self._odds_text_matches(txt, wager_odds):
                return cand

        return None

    def _add_spread_to_slip(
        self,
        game_line: dict,
        team_no: int,
        team_name: str,
        spread_elem=None,
    ) -> bool:
        """Click DOM and/or Angular until the bet slip contains the spread pick."""
        self._prepare_bet_slip_for_wager()
        game_num = game_line.get("GameNum")

        if spread_elem:
            self.logger.info(f"Spread element located: {spread_elem.get_attribute('id')}")
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", spread_elem)
            time.sleep(0.4)
            self.driver.execute_script("arguments[0].click();", spread_elem)
            self.logger.info("Spread element clicked")
            if self._wait_for_betslip_team(team_name, timeout=5, bet_type="spread"):
                return True
            self.logger.warning(
                f"DOM click on S{team_no}_{game_num}_0 did not populate bet slip; trying Angular"
            )

        if click_line_via_angular(self.driver, game_line, team_no, "S"):
            if self._wait_for_betslip_team(team_name, timeout=6, bet_type="spread"):
                return True
            self.logger.warning("Angular GameLineAction (spread) did not populate bet slip")

        btn = self._wait_for_spread_button(game_num, team_no, timeout=3)
        if btn:
            self.driver.execute_script("arguments[0].click();", btn)
            if self._wait_for_betslip_team(team_name, timeout=4, bet_type="spread"):
                return True

        return False

    def _get_api_payload(self):
        if self.sport_name == "NBA":
            return {
                "sportType": "Basketball",
                "sportSubType": "NBA",
                "wagerType": "Straight Bet",
                "hoursAdjustment": 0,
                "periodNumber": None,
                "gameNum": None,
                "parentGameNum": None,
                "teaserName": "",
                "requestMode": None,
            }
        return {
            "sportType": "Baseball",
            "sportSubType": "MLB",
            "wagerType": "Straight Bet",
            "hoursAdjustment": 0,
            "periodNumber": None,
            "gameNum": None,
            "parentGameNum": None,
            "teaserName": "",
            "requestMode": None,
        }

    def _dismiss_blocking_overlays(self) -> None:
        try:
            self.driver.execute_script("""
                document.querySelectorAll(
                    '.swal2-container button.swal2-close, .swal2-container button.swal2-confirm'
                ).forEach(function(btn) { try { btn.click(); } catch (e) {} });
            """)
        except Exception:
            pass

    def __ensure_sport_offering_loaded(self, game_num=None, team_no: int = None, fast: bool = False) -> bool:
        """Navigate the SPA to the active sport (MLB/NBA) so game lines are in the DOM."""
        if fast and self._is_on_sport_page_with_games():
            api_lines = self._fetch_game_lines_via_api()
            if api_lines and len(api_lines) >= self._min_expected_full_game_lines():
                if game_num and team_no and self._wait_for_moneyline_button(game_num, team_no, timeout=8):
                    self.logger.info(
                        f"Fast path: target moneyline M{team_no}_{game_num}_0 already in DOM"
                    )
                    return True
                self.logger.info(f"Fast path: API has {len(api_lines)} lines; skipping full reload")
                return True

        self.logger.info(f"Ensuring {self.sport_name} offering is loaded in the SPA...")

        for nav_attempt in range(1, 4):
            if nav_attempt > 1:
                self.logger.warning(
                    f"Retrying {self.sport_name} SPA navigation (attempt {nav_attempt}/3)"
                )
                try:
                    self.driver.refresh()
                except Exception:
                    pass
                time.sleep(3)

            self.driver.get(self.sport_url)
            self._dismiss_blocking_overlays()
            time.sleep(2)

            try:
                self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "#gamesAccordion, .sport-lines-container, app-sports")
                    )
                )
            except Exception:
                self.logger.warning("Main content containers not found quickly.")

            sidebar_ready = False
            try:
                self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "a.sportIcon, a#img_Baseball, a#img_Basketball")
                    )
                )
                sidebar_ready = True
            except Exception:
                self.logger.warning("Sports sidebar icons did not appear quickly.")

            if not sidebar_ready:
                continue

            if self.sport_name == "NBA":
                sport_selectors = [
                    "a#img_Basketball",
                    "a[data-target='#sp_Basketball']",
                    "#gl_Basketball_NBA_G",
                    "label[for='gl_Basketball_NBA_G']",
                ]
            else:
                sport_selectors = [
                    "a#img_Baseball",
                    "a[data-target='#sp_Baseball']",
                    "#gl_Baseball_MLB_G",
                    "label[for='gl_Baseball_MLB_G']",
                ]

            try:
                if self.sport_name == "MLB":
                    baseball_link = self.driver.find_element(
                        By.CSS_SELECTOR, "a#img_Baseball, a[data-target='#sp_Baseball']"
                    )
                    self.driver.execute_script("arguments[0].click();", baseball_link)
                    time.sleep(1)
            except Exception as e:
                self.logger.warning(f"Could not expand sport section: {e}")
                continue

            selected = False
            for selector in sport_selectors[2:]:
                try:
                    elem = self.driver.find_element(By.CSS_SELECTOR, selector)
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
                    self.driver.execute_script("arguments[0].click();", elem)
                    selected = True
                    self.logger.info(f"Selected {self.sport_name} via {selector}")
                    break
                except Exception:
                    continue

            if not selected:
                try:
                    result = self.driver.execute_script("""
                        var selectors = arguments[0];
                        for (var i = 0; i < selectors.length; i++) {
                            var el = document.querySelector(selectors[i]);
                            if (el) { el.click(); return selectors[i]; }
                        }
                        var label = document.querySelector('label[for="gl_Baseball_MLB_G"]')
                            || document.querySelector('label[for="gl_Basketball_NBA_G"]');
                        if (label) { label.click(); return label.getAttribute('for'); }
                        return null;
                    """, sport_selectors[2:])
                    if result:
                        selected = True
                        self.logger.info(f"Selected {self.sport_name} via JS click ({result})")
                except Exception as e:
                    self.logger.warning(f"JS sport selection failed: {e}")

            if not selected:
                continue

            self._trigger_angular_offering_select()
            time.sleep(3)

            api_lines = self._fetch_game_lines_via_api()
            if api_lines:
                break

        api_lines = api_lines if "api_lines" in locals() else []
        api_has_lines = bool(api_lines)
        if api_has_lines:
            self.logger.info(f"API confirms {len(api_lines)} {self.sport_name} lines are available")

        lines_ready = False
        if game_num and team_no:
            if self._wait_for_moneyline_button(game_num, team_no, timeout=20):
                lines_ready = True
                self.logger.info(f"Target moneyline button M{team_no}_{game_num}_0 is in DOM")

        if not lines_ready:
            for _ in range(20):
                if self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "button[id^='M1_'], button[id^='M2_'], span[id^='M1_'], span[id^='M2_'], span.line-rot-num",
                ):
                    lines_ready = True
                    break
                time.sleep(1)

        if lines_ready:
            self.logger.info(f"{self.sport_name} lines detected in DOM")
        elif api_has_lines:
            self.logger.warning(
                f"{self.sport_name} lines exist in API but not yet rendered in DOM; "
                "will use GameNum selectors / Angular GameLineAction"
            )
            lines_ready = True
        else:
            self.logger.warning(f"{self.sport_name} lines not visible in DOM or API after navigation")
            raise RuntimeError(f"{self.sport_name} offering failed to load after SPA navigation retries")

        self._ensure_odds_mutation_observer()
        return lines_ready

    def _min_expected_full_game_lines(self) -> int:
        """Minimum full-game lines before we treat the SPA offering as degraded."""
        if self.sport_name == "NBA":
            return 2
        return int(os.getenv("BETAMAPOLA_MIN_EXPECTED_MLB_LINES", "3"))

    @staticmethod
    def _parse_get_sport_offering_response(result) -> tuple[list, bool]:
        return parse_get_sport_offering_response(result)

    def _fetch_game_lines_via_api(self):
        """POST to GetSportOffering for current sport Straight Bet lines (all periods)."""
        self.logger.info("Fetching via GetSportOffering API (browser context)...")

        payload = self._get_api_payload()

        script = """
            const p = arguments[0];
            return fetch('/sports/Api/Offering.asmx/GetSportOffering', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*'
                },
                credentials: 'include',
                body: JSON.stringify(p)
            }).then(r => r.json());
        """

        try:
            result = self.driver.execute_script(script, payload)
            lines, payload_ok = self._parse_get_sport_offering_response(result)
            if not payload_ok:
                self._api_offering_failures += 1
                snippet = str(result)[:500] if result is not None else "null"
                self.logger.warning(
                    f"GetSportOffering invalid payload (fail #{self._api_offering_failures}): "
                    f"{snippet}"
                )
                if result and isinstance(result, dict) and result.get("d") is None:
                    self._invalidate_wager_session()
                return []

            limits = []
            if isinstance(result, dict):
                inner = result.get("d") or {}
                if isinstance(inner, dict):
                    data = inner.get("Data") or {}
                    if isinstance(data, dict):
                        limits = data.get("SportLimits") or []

            self._api_offering_failures = 0
            self.logger.info(
                f"API success: {len(lines)} GameLines, {len(limits)} SportLimits entries"
            )
            return lines
        except Exception as e:
            self._api_offering_failures += 1
            self.logger.error(f"Browser-context API call failed: {e}")
            return []

    def _recover_degraded_offering(self, *, parsed_games: int, force_scan: bool) -> None:
        """Reload SPA or re-login when GetSportOffering returns too few games."""
        min_expected = self._min_expected_full_game_lines()
        prev = self._last_full_game_line_count

        thin = parsed_games < min_expected
        sharp_drop = prev >= min_expected and parsed_games < min_expected
        bad_payload = self._api_offering_failures >= 2

        if not force_scan and not thin and not bad_payload:
            return
        if not thin and not sharp_drop and not bad_payload:
            return

        reason = []
        if bad_payload:
            reason.append(f"api_failures={self._api_offering_failures}")
        if thin:
            reason.append(f"parsed={parsed_games}<{min_expected}")
        if sharp_drop:
            reason.append(f"drop_from={prev}")
        self.logger.warning(
            f"Degraded {self.sport_name} offering ({', '.join(reason)}); reloading SPA"
        )

        try:
            self.__ensure_sport_offering_loaded()
            self._api_offering_failures = 0
        except Exception as e:
            self.logger.warning(f"SPA reload failed: {e}")

        if bad_payload or sharp_drop:
            self.logger.warning("Offering still degraded after SPA reload; performing full re-login")
            try:
                self.__login()
                self.__ensure_sport_offering_loaded()
                self._api_offering_failures = 0
            except Exception as e:
                self.logger.error(f"Full re-login after thin offering failed: {e}")

    def _parse_api_game_lines(self, game_lines):
        """Convert raw GetSportOffering GameLines into the internal games format used by cache/storage."""
        games = []
        for gl in game_lines:
            # Primary full-game lines (PeriodNumber 0). 1H = 1, etc.
            if gl.get("PeriodNumber") != 0:
                continue

            team1 = gl.get("Team1ID")
            team2 = gl.get("Team2ID")
            rot1 = gl.get("Team1RotNum")
            rot2 = gl.get("Team2RotNum")
            ml1 = gl.get("MoneyLine1")
            ml2 = gl.get("MoneyLine2")

            if not team1 or not team2 or ml1 is None or ml2 is None:
                continue

            game_id = f"{rot1}-{rot2}"
            game_dt = gl.get("GameDateTimeString") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            spread_val = gl.get("Spread")
            spread_a1 = gl.get("SpreadAdj1")
            spread_a2 = gl.get("SpreadAdj2")
            team_1_spread, team_2_spread = resolve_ticosports_spread_lines(spread_val, ml1, ml2)

            total_val = gl.get("TotalPoints")
            ttl_a1 = gl.get("TtlPtsAdj1")
            ttl_a2 = gl.get("TtlPtsAdj2")

            # Always normalize to %Y-%m-%d %H:%M:%S so parse_odds + cross-book group-by on game_datetime succeed
            # Pass game_tz so times from API (in book-specific TZ) are converted to UTC for cross-book matching
            normalized_dt = parse_to_mysql_datetime(game_dt, tz_name=self.game_tz) or game_dt
            if not isinstance(normalized_dt, str) or not normalized_dt[4:5] == "-":
                # last resort ensure format for strptime in parse_odds
                try:
                    if isinstance(game_dt, str) and len(game_dt) >= 10:
                        # still normalize the raw date str using tz
                        normalized_dt = parse_to_mysql_datetime(game_dt, tz_name=self.game_tz)
                    else:
                        normalized_dt = parse_to_mysql_datetime(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tz_name=self.game_tz)
                except Exception:
                    normalized_dt = parse_to_mysql_datetime(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tz_name=self.game_tz)

            game = {
                "bookmaker": self.bookmaker,
                "sport": self.sport_name,
                "league": self.league,
                "game_id": game_id,
                "game_datetime": normalized_dt,
                "match": f"{team1} vs {team2}",
                "team_1": team1,
                "team_2": team2,
                "moneyline": {
                    "team_1": str(ml1) if ml1 is not None else None,
                    "team_2": str(ml2) if ml2 is not None else None
                },
                "spread": {
                    "team_1_spread": team_1_spread,
                    "team_2_spread": team_2_spread,
                    "team_1_odds": spread_a1,
                    "team_2_odds": spread_a2
                },
                "total": {
                    "over_total": total_val,
                    "under_total": total_val,
                    "over_odds": ttl_a1,
                    "under_odds": ttl_a2
                },
                "status": gl.get("Status"),
                "pitcher1": gl.get("ListedPitcher1"),
                "pitcher2": gl.get("ListedPitcher2"),
                "comments": gl.get("Comments"),
                "game_num": gl.get("GameNum"),
                "period": gl.get("PeriodNumber"),
            }
            games.append(game)

        self.logger.info(f"Parsed {len(games)} full-game (Period 0) lines from API")
        return games

    # ===================================================================
    # Odds watch (persistent session + GetSportOffering API poll)
    # ===================================================================
    def _fetch_games_for_odds(self, allow_dom_fallback: bool = False):
        api_lines = self._fetch_game_lines_via_api()
        if api_lines:
            games = self._parse_api_game_lines(api_lines)
            if games:
                return games, "api"

        use_dom = allow_dom_fallback or self._sport_games_present()
        if use_dom and self._sport_games_present():
            html = self.driver.page_source
            soup = BeautifulSoup(html, "html.parser")
            games = []
            for game in soup.select("div.sports-league-game"):
                try:
                    game_id = game.get("idgame")
                    if not game_id:
                        continue
                    mline1 = game.select_one(".mline-1 label.bet-indicator")
                    mline2 = game.select_one(".mline-2 label.bet-indicator")
                    if not mline1 or not mline2:
                        continue
                    team_1, ml1 = self._extract_team_odds_from_dom_label(mline1)
                    team_2, ml2 = self._extract_team_odds_from_dom_label(mline2)
                    if not team_1 or not team_2 or not ml1 or not ml2:
                        continue
                    games.append({
                        "bookmaker": self.bookmaker,
                        "sport": self.sport_name,
                        "league": self.league,
                        "game_id": game_id,
                        "game_datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "match": f"{team_1} vs {team_2}",
                        "team_1": team_1,
                        "team_2": team_2,
                        "moneyline": {"team_1": ml1, "team_2": ml2},
                        "spread": {"team_1_spread": None, "team_2_spread": None,
                                    "team_1_odds": None, "team_2_odds": None},
                        "total": {"over_total": None, "under_total": None,
                                  "over_odds": None, "under_odds": None},
                    })
                except Exception:
                    continue
            if games:
                return games, "dom"

        return [], "none"

    @staticmethod
    def _extract_team_odds_from_dom_label(label):
        title = (label.get("title") or label.text or "").strip()
        match = re.match(r"^(.+?)\s+([+-]?\d+)", title)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        text = label.text.strip()
        match = re.match(r"^(.+?)\s+([+-]?\d+)", text)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        return None, None

    def _poll_odds_watch_once(self, force_scan: bool = False, source: str = "watch", **kwargs) -> int:
        if not hasattr(self, "_last_saved_ml"):
            self._last_saved_ml = {}
        games, src = self._fetch_games_for_odds(allow_dom_fallback=force_scan)
        min_expected = self._min_expected_full_game_lines()
        if force_scan and (not games or len(games) < min_expected):
            should_recover = (
                not games
                or self._api_offering_failures >= 2
                or self._last_full_game_line_count >= min_expected
                or not self._thin_offering_recovery_attempted
            )
            if should_recover:
                self._thin_offering_recovery_attempted = True
                self._recover_degraded_offering(
                    parsed_games=len(games), force_scan=force_scan
                )
                games, src = self._fetch_games_for_odds(allow_dom_fallback=True)
        label = f"{source}/{src}" if src != "none" else source
        if not games and force_scan:
            self.logger.warning(f"No {self.sport_name} lines from API or DOM on force scan")
        elif games:
            self._last_full_game_line_count = len(games)
            if len(games) >= min_expected:
                self._thin_offering_recovery_attempted = False
        return persist_moneyline_games(
            self.cache,
            self.storage,
            self.logger,
            games,
            self.sport_name,
            self.league,
            self._last_saved_ml,
            source=label,
        )

    def _maybe_poll_odds_while_idle(self):
        """DOM-triggered or timed ML + spread poll while betting loop is idle."""
        if not hasattr(self, "_last_odds_force_scan"):
            self._last_odds_force_scan = 0.0
        if not self._is_session_valid():
            self.logger.warning("Session invalid during idle odds poll; re-establishing")
            try:
                self._ensure_betting_session()
            except Exception as e:
                self.logger.warning(f"Idle session refresh failed: {e}")
            return
        try:
            self._last_odds_force_scan, processed = self._tick_odds_on_idle(
                self._last_odds_force_scan,
                idle_label="betting-idle",
            )
            if not processed:
                return
        except Exception as e:
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
            f"API poll {poll_interval}s, force scan {force_scan_interval}s =========="
        )

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()
        self._cleanup_stale_temp_dirs()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                self._ensure_betting_session()
                setup_ok = True
                break
            except Exception as e:
                self.logger.error(f"Odds watch setup failed (attempt {attempt}/5): {e}")
                self._recover_driver()
                time.sleep(5)

        if not setup_ok:
            self.logger.error("Could not start Betamapola odds watch")
            return

        last_force_scan = 0.0
        consecutive_recoveries = 0

        try:
            while True:
                watchdog.beat()
                try:
                    current_url = self.driver.current_url
                except Exception as e:
                    self.logger.error(f"Odds watch driver error: {e}")
                    self._recover_driver()
                    if self._relogin_after_recovery():
                        self._ensure_betting_session()
                    time.sleep(5)
                    continue

                if "/sports" not in (current_url or ""):
                    self.logger.warning(f"Odds watch off sport page ({current_url}); recovering")
                    self._recover_driver()
                    if self._relogin_after_recovery():
                        self._ensure_betting_session()
                    time.sleep(3)
                    continue

                consecutive_recoveries = 0
                now = time.monotonic()
                last_force_scan, processed = self._tick_odds_on_idle(
                    last_force_scan, idle_label="watch"
                )
                if not processed:
                    time.sleep(poll_interval)

        except KeyboardInterrupt:
            self.logger.info("Betamapola odds watch stopped by user")
        except Exception as e:
            self.logger.error(f"Fatal Betamapola odds watch error: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            self.logger.info(f"========== Odds Watch ({self.sport_name}) (END) ==========")

    # ===================================================================
    # Selenium fetch_odds (now prefers the direct GetSportOffering API)
    # ===================================================================
    @time_it
    def fetch_odds(self, refresh_interval=10):
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self.logger.info(f"========== Fetching Odds ({self.sport_name}) via Selenium (START) ==========")
        prune_debug_files()

        try:
            # Ensure we are logged in using the existing Selenium driver
            self.__login()
            self.__ensure_sport_offering_loaded()

            # ------------------------------------------------------------------
            # Preferred path: direct API call (GetSportOffering) - fast & reliable
            # ------------------------------------------------------------------
            games = []
            api_lines = self._fetch_game_lines_via_api()
            if api_lines:
                games = self._parse_api_game_lines(api_lines)
                self.logger.info(f"Using API data: {len(games)} full-game lines (skipping DOM scrape)")
            else:
                self.logger.warning("API returned no lines — falling back to legacy DOM scraping + long waits")

            if not games:
                # Good compromise wait (only needed for fallback DOM path):
                # 1. Wait until we see at least 1 rotation number
                # 2. Then do an additional fixed wait to let more content populate
                self.logger.info("Waiting for game schedule to start rendering (waiting for first rotation number)...")

                max_wait_for_first = 60   # max seconds to wait for the first rotation number
                additional_wait = 10      # extra seconds after seeing the first one

                first_rotation_found = False
                start_time = time.time()

                while time.time() - start_time < max_wait_for_first:
                    try:
                        rot_elements = self.driver.find_elements(By.CSS_SELECTOR, "span.line-rot-num")
                        if len(rot_elements) >= 1:
                            first_rotation_found = True
                            break
                    except Exception:
                        pass
                    time.sleep(1)

                if first_rotation_found:
                    self.logger.info(f"First rotation number detected. Waiting additional {additional_wait}s for full content...")
                    time.sleep(additional_wait)
                else:
                    self.logger.warning(f"No rotation numbers found after {max_wait_for_first}s. Proceeding anyway.")

            # Capture network requests made during the load (useful for both paths)
            try:
                import json
                performance_logs = self.driver.get_log("performance")
                network_requests = []
                for entry in performance_logs:
                    try:
                        msg = json.loads(entry["message"])["message"]
                        if msg["method"] == "Network.responseReceived":
                            resp = msg["params"]["response"]
                            if resp.get("url"):
                                network_requests.append({
                                    "url": resp["url"],
                                    "status": resp.get("status"),
                                    "mimeType": resp.get("mimeType"),
                                    "requestId": msg["params"].get("requestId")
                                })
                    except Exception:
                        pass

                # Log interesting requests (XHR/Fetch or anything game/schedule related)
                interesting = [r for r in network_requests if any(k in r["url"].lower() for k in ["game", "schedule", "odds", "line", "mlb", "sport", "api"])]
                if interesting:
                    self.logger.info(f"Network requests captured during load: {len(interesting)} interesting")
                    for req in interesting[:15]:  # limit output
                        self.logger.info(f"  {req['status']} | {req['url'][:180]}")
                else:
                    self.logger.info("No obviously relevant network requests found during the load window.")

                # Save full captured requests for later analysis
                net_file = os.path.join(
                    get_debug_dir(),
                    f"network_betamapola_{self.sport_name.lower()}_{int(time.time())}.json",
                )
                with open(net_file, "w", encoding="utf-8") as f:
                    json.dump(network_requests, f, indent=2)
                self.logger.info(f"💾 Saved full network log: {net_file}")
            except Exception as e:
                self.logger.warning(f"Failed to capture performance logs: {e}")

            # Save debug HTML after waiting (always useful for diagnostics)
            debug_file = debug_filepath(f"debug_betamapola_{self.sport_name.lower()}")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.info(f"💾 Saved debug HTML: {debug_file}")

            # ------------------------------------------------------------------
            # Legacy DOM scraping path (only when API gave us nothing)
            # ------------------------------------------------------------------
            if not games:
                html = self.driver.page_source
                soup = BeautifulSoup(html, "html.parser")
                games = []

                # Primary containers observed: #gamesAccordion, .sport-lines-container, ul.bettinglines, .gameLineInfo
                containers = soup.select("#gamesAccordion, .sport-lines-container, ul.bettinglines, .gameLineInfo, div[id*='subSport_']")
                if not containers:
                    containers = [soup]  # fallback whole doc

                for container in containers:
                    # Look for rotation numbers + team text blocks
                    rot_spans = container.select("span.line-rot-num")
                    if not rot_spans:
                        continue

                    # Group by pairs (home/away rotation)
                    for i in range(0, len(rot_spans) - 1, 2):
                        try:
                            rot1 = rot_spans[i].get_text(strip=True)
                            rot2 = rot_spans[i + 1].get_text(strip=True) if i + 1 < len(rot_spans) else ""

                            # Find nearest game line info ancestor
                            parent = rot_spans[i].find_parent(class_=re.compile(r"gameLine|game-line|bettinglines"))
                            if not parent:
                                parent = rot_spans[i].find_parent("ul") or rot_spans[i].find_parent("div")

                            text = parent.get_text(" ", strip=True) if parent else ""

                            # Extract teams (common patterns: "Toronto Blue Jays at Baltimore Orioles" or "Team1 vs Team2")
                            teams_match = re.search(r"(\d{2,4})\s+([A-Za-z][A-Za-z\s\.]+?)\s+(at|vs|VS)\s+([A-Za-z][A-Za-z\s\.]+?)(?:\s*[-–]|$|\d)", text)
                            if not teams_match:
                                # Fallback looser
                                teams_match = re.search(r"(\d{2,4})\s+([A-Z][A-Za-z\s]+?)\s+([A-Z][A-Za-z\s]+?)(?:\s*-|\s+\d)", text)

                            if teams_match:
                                team_1 = teams_match.group(2).strip()
                                team_2 = teams_match.group(4).strip() if teams_match.lastindex >= 4 else teams_match.group(3).strip()
                            else:
                                # Last resort: split on " at "
                                if " at " in text:
                                    parts = text.split(" at ", 1)
                                    team_1 = parts[0].split()[-2:] if len(parts) > 0 else "T1"
                                    team_1 = " ".join(team_1) if isinstance(team_1, list) else team_1
                                    team_2 = parts[1].split()[0:3] if len(parts) > 1 else "T2"
                                    team_2 = " ".join(team_2) if isinstance(team_2, list) else team_2
                                else:
                                    continue

                            # Extract moneyline using the M1_/M2_ span ids or +/- patterns near rotation
                            def find_ml_for_rot(rot):
                                # Try exact id pattern first (M1_xxxx_0 or M2_)
                                span = soup.select_one(f"span[id^='M'][id*='_{rot}_']")
                                if span:
                                    val = span.get_text(strip=True)
                                    m = re.search(r'([+-]?\d+)', val)
                                    if m:
                                        return m.group(1)
                                # Fallback: look in text near the rotation number
                                m = re.search(rf"{rot}[^+-]*?([+-]\d{{2,4}})", text)
                                if m:
                                    return m.group(1)
                                return None

                            ml1 = find_ml_for_rot(rot1)
                            ml2 = find_ml_for_rot(rot2)

                            if not ml1 or not ml2:
                                continue

                            game_id = f"{rot1}-{rot2}"  # stable composite using rotations
                            game_datetime_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                            games.append({
                                "bookmaker": self.bookmaker,
                                "sport": self.sport_name,
                                "league": self.league,
                                "game_id": game_id,
                                "game_datetime": game_datetime_str,
                                "match": f"{team_1} vs {team_2}",
                                "team_1": team_1,
                                "team_2": team_2,
                                "moneyline": {"team_1": ml1, "team_2": ml2},
                                "spread": {"team_1_spread": None, "team_2_spread": None, "team_1_odds": None, "team_2_odds": None},
                                "total": {"over_total": None, "under_total": None, "over_odds": None, "under_odds": None}
                            })
                        except Exception as e:
                            self.logger.error(f"Error parsing game block: {e}")
                            continue

            source = "API" if api_lines else "Selenium DOM"
            self.logger.info(f"Extracted {len(games)} {self.sport_name} matches via {source}")

            if len(games) == 0:
                self.logger.warning(
                    f"No games found for {self.sport_name}. "
                    f"Inspect the debug file: {debug_file}. "
                    "The site structure may have changed or additional waits/selectors are needed."
                )

            odds_data = {
                "sport": self.sport_name,
                "league": self.league,
                "total_matches": len(games),
                "matches": games,
                "timestamp": datetime.now().isoformat()
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
                source=source,
            )

        except Exception as e:
            self.logger.error(f"Selenium fetch_odds failed: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            self._quit_driver()
            self.logger.info(f"========= Fetching Odds ({self.sport_name}) via Selenium (END) ==========")

    def _find_moneyline_element(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd,
        game_line: dict = None,
        team_no: int = None,
    ):
        """Locate clickable moneyline button/span for team/odds on the loaded offering page."""
        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        team_lower = team_name.lower()
        moneyline_elem = None
        game_num = (game_line or {}).get("GameNum")
        period = (game_line or {}).get("PeriodNumber", 0)

        # Strategy 1: TicoSports id format is M{teamNo}_{GameNum}_{PeriodNumber} on <button>
        if game_num is not None and team_no in (1, 2):
            for selector in (
                f"button#M{team_no}_{game_num}_{period}",
                f"#M{team_no}_{game_num}_{period}",
                f"button[id='M{team_no}_{game_num}_{period}']",
            ):
                candidates = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if candidates:
                    moneyline_elem = candidates[0]
                    break

        # Strategy 2: GameNum embedded in any M1_/M2_ button id + odds text match
        if not moneyline_elem and game_num is not None:
            for prefix in ("M1_", "M2_"):
                candidates = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    f"button[id^='{prefix}'][id*='_{game_num}_'], span[id^='{prefix}'][id*='_{game_num}_']",
                )
                for cand in candidates:
                    txt = (cand.text or cand.get_attribute("innerText") or "").strip()
                    if self._odds_text_matches(txt, moneyline_odd):
                        moneyline_elem = cand
                        break
                if moneyline_elem:
                    break

        # Strategy 3: team name in row + matching odds (buttons first, then spans)
        if not moneyline_elem:
            for cand in self.driver.find_elements(
                By.CSS_SELECTOR, "button[id^='M1_'], button[id^='M2_'], span[id^='M1_'], span[id^='M2_']"
            ):
                try:
                    row = cand.find_element(
                        By.XPATH,
                        "./ancestor::*[contains(@class,'game') or contains(@class,'line') or contains(@class,'betting')][1]",
                    )
                    row_text = (row.text or "").lower()
                except Exception:
                    row_text = (cand.text or "").lower()

                txt = (cand.text or cand.get_attribute("innerText") or "").strip()
                if team_lower in row_text and self._odds_text_matches(txt, moneyline_odd):
                    moneyline_elem = cand
                    break

        # Strategy 4: rotation numbers visible in row + matching odds
        if not moneyline_elem:
            for row in self.driver.find_elements(
                By.CSS_SELECTOR,
                "ul.bettinglines, .gameLineInfo, .sport-lines-container div, #gamesAccordion div",
            ):
                row_text = (row.text or "").lower()
                if team_lower not in row_text:
                    continue
                if rotations and not all(rot in row_text for rot in rotations):
                    continue
                for cand in row.find_elements(
                    By.CSS_SELECTOR,
                    "button[id^='M1_'], button[id^='M2_'], span[id^='M1_'], span[id^='M2_'], span.text-black",
                ):
                    txt = (cand.text or "").strip()
                    if self._odds_text_matches(txt, moneyline_odd):
                        moneyline_elem = cand
                        break
                if moneyline_elem:
                    break

        return moneyline_elem

    def _fetch_open_wagers_via_api(self):
        script = """
            return fetch('/sports/Api/Betting.asmx/GetWagerPicks', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*'
                },
                credentials: 'include',
                body: JSON.stringify({})
            }).then(r => r.json());
        """
        try:
            result = self.driver.execute_script(script)
            if not result:
                return []
            data = (result.get("d") or {}).get("Data") or result.get("d") or {}
            if isinstance(data, list):
                return data
            for key in ("WagerPicks", "Picks", "OpenWagers", "Items"):
                if isinstance(data.get(key), list):
                    return data[key]
            return []
        except Exception as e:
            self.logger.warning(f"GetWagerPicks failed: {e}")
            return []

    def _open_bets_url(self) -> str:
        base = (self.driver.current_url or self.sport_url or self.dashboard_url or "").split("#")[0].rstrip("/")
        if not base:
            base = f"https://{self.website}/sports"
        return f"{base}#/openBets"

    def _load_open_bets_page_text(self) -> str:
        """Navigate to the Open Bets SPA route and return visible page text."""
        sport_url = self.driver.current_url
        try:
            self.driver.get(self._open_bets_url())
            time.sleep(2.5)
            try:
                WebDriverWait(self.driver, 8).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "table, .open-bets, .openBets, main, #content")
                    )
                )
            except Exception:
                pass
            return (self.driver.find_element(By.TAG_NAME, "body").text or "").strip()
        finally:
            if sport_url:
                try:
                    self.driver.get(sport_url)
                    time.sleep(1.0)
                except Exception:
                    pass

    @staticmethod
    def _open_bets_text_has_wager(
        page_text: str,
        team_name: str,
        team_1: str,
        team_2: str,
        ticket_number: int | str | None = None,
    ) -> bool:
        page_l = (page_text or "").lower()
        if not page_l or "open bets" not in page_l:
            return False
        if ticket_number is not None:
            ticket_s = str(ticket_number).strip()
            if ticket_s and ticket_s in page_text:
                return True
        team_l = (team_name or "").lower()
        if not team_l or team_l not in page_l:
            return False
        t1 = (team_1 or "").lower()
        t2 = (team_2 or "").lower()
        matchup_ok = True
        if t1 and t2:
            matchup_ok = t1 in page_l or t2 in page_l
        wager_markers = ("money line", "risk", "win", "tick#", "accepted date")
        return matchup_ok and any(marker in page_l for marker in wager_markers)

    def _verify_open_bet_on_open_bets_page(
        self,
        team_name: str,
        team_1: str,
        team_2: str,
        ticket_number: int | str | None = None,
        timeout: int = 20,
    ) -> tuple[bool, str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            page_text = self._load_open_bets_page_text()
            if self._open_bets_text_has_wager(
                page_text, team_name, team_1, team_2, ticket_number=ticket_number
            ):
                detail = f"Open bet verified on open-bets page for {team_name}"
                if ticket_number:
                    detail += f" (ticket {ticket_number})"
                return True, detail
            time.sleep(1.5)
        return False, "Bet not found on open-bets page"

    def _notify_open_bets_screenshot(
        self,
        team_name: str,
        team_1: str,
        team_2: str,
        game_id: str,
        ticket_number: int | str | None = None,
    ) -> str | None:
        """Capture open-bets confirmation for the screenshots channel (sent by finalize_confirmed_bet)."""
        path = bet_screenshot_path(self.bookmaker, game_id)
        shot = capture_betamapola_confirmation(
            self.driver,
            path,
            self.logger,
            team_name=team_name,
            team_1=team_1,
            team_2=team_2,
        )
        if not shot:
            self.logger.warning("Open-bets screenshot capture failed")
            return None
        self._last_screenshot_path = shot
        return shot

    @staticmethod
    def _pick_looks_like_open_wager(pick) -> bool:
        return pick_looks_like_open_wager(pick)

    def _pick_matches_open_wager(self, pick, team_name: str, team_1: str, team_2: str) -> bool:
        if not self._pick_looks_like_open_wager(pick):
            return False
        text = json.dumps(pick).lower()
        return (
            team_name.lower() in text
            and team_1.lower() in text
            and team_2.lower() in text
        )

    def _has_existing_open_bet(self, team_name: str, team_1: str, team_2: str) -> bool:
        cache_key = f"{team_name}:{team_1}:{team_2}".lower()
        now = time.time()
        cached = self._pending_check_cache.get(cache_key)
        if cached and (now - cached["ts"]) < self.PENDING_CHECK_CACHE_TTL:
            return cached["found"]

        found = False
        try:
            page_text = self._load_open_bets_page_text()
            found = self._open_bets_text_has_wager(page_text, team_name, team_1, team_2)
            if not found:
                for pick in self._fetch_open_wagers_via_api():
                    if self._pick_matches_open_wager(pick, team_name, team_1, team_2):
                        found = True
                        break
        except Exception as e:
            self.logger.warning(f"Could not check existing open bets: {e}")
            return False

        if found:
            self.logger.info(
                f"Open bet already on open-bets page for {team_name} ({team_1} vs {team_2})"
            )
        self._pending_check_cache[cache_key] = {"found": found, "ts": now}
        return found

    def _message_requires_relogin(self, message: str) -> bool:
        msg_l = (message or "").lower()
        return any(marker in msg_l for marker in self.WAGER_SESSION_EXPIRED_MARKERS)

    def _invalidate_wager_session(self):
        self._force_wager_relogin = True

    def _page_has_login_required_marker(self) -> bool:
        try:
            for elem in self.driver.find_elements(
                By.CSS_SELECTOR, "#betSlipDiv, .alert, .modal-body, .login-form"
            ):
                text = (elem.text or "").lower()
                if any(marker in text for marker in self.WAGER_SESSION_EXPIRED_MARKERS):
                    return True
        except Exception:
            pass
        return False

    def _is_session_valid(self) -> bool:
        try:
            if self._force_wager_relogin:
                return False
            if self._page_has_login_required_marker():
                return False
            url = (self.driver.current_url or "").lower()
            if self.website.lower() not in url:
                return False
            if "/sports" not in url:
                return False
            account_fields = self.driver.find_elements(By.ID, "account")
            return not account_fields or not account_fields[0].is_displayed()
        except Exception:
            return False

    def _sport_games_present(self) -> bool:
        try:
            return bool(
                self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "button[id^='M1_'], button[id^='M2_'], span.line-rot-num",
                )
            )
        except Exception:
            return False

    def _is_on_sport_page_with_games(self) -> bool:
        try:
            url = (self.driver.current_url or "").lower()
            if "/sports" not in url:
                return False
            return self._sport_games_present()
        except Exception:
            return False

    def _return_to_sport_page(self):
        try:
            if self._is_on_sport_page_with_games():
                return
            self.__ensure_sport_offering_loaded()
        except Exception as e:
            self.logger.warning(f"Could not return to {self.sport_name} page: {e}")

    def _ensure_betting_session(self):
        """Login only when the browser session is missing or invalid."""
        if self._force_wager_relogin:
            self.logger.info("Wager session flagged invalid; performing full login")
            self.__login()
            self.__ensure_sport_offering_loaded()
            return

        if self._is_session_valid() and self._is_on_sport_page_with_games():
            self.logger.info(
                "Session valid on sport page with games loaded; skipping login"
            )
            return

        if self._is_session_valid():
            self.logger.info("Session valid but sport offering not loaded; navigating only")
            self.__ensure_sport_offering_loaded()
            return

        self.logger.info("Session invalid; performing full login")
        self.__login()
        self.__ensure_sport_offering_loaded()

    def _refresh_session_before_wager(self):
        if self._force_wager_relogin:
            self.logger.info("Wager session flagged invalid; performing full login before placement")
            self.__login()
            self.__ensure_sport_offering_loaded()
            return

        if self._is_session_valid() and self._is_on_sport_page_with_games():
            self.logger.info(
                "Session valid on sport page with games loaded; skipping login refresh"
            )
            return

        if self._is_session_valid():
            self.logger.info("Session valid but off sport page; navigating to sport page only")
            self._return_to_sport_page()
            return

        self.logger.info("Session invalid; performing full login before wager placement")
        self.__login()
        self.__ensure_sport_offering_loaded()

    def _install_wager_network_hook(self):
        self.driver.execute_script("""
            window.__wagerResponses = [];
            if (window.__wagerHookInstalled) return;
            window.__wagerHookInstalled = true;
            const capture = (url, body) => {
                if (!url) return;
                const u = String(url).toLowerCase();
                if (u.includes('wager') || u.includes('bet') || u.includes('ticket')
                    || u.includes('process') || u.includes('pick')) {
                    window.__wagerResponses.push({url: String(url), body: String(body || '').slice(0, 4000)});
                }
            };
            const origFetch = window.fetch;
            window.fetch = function(...args) {
                const reqUrl = args[0];
                return origFetch.apply(this, args).then(resp => {
                    resp.clone().text().then(t => capture(reqUrl, t)).catch(() => {});
                    return resp;
                });
            };
            const origOpen = XMLHttpRequest.prototype.open;
            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                this.__arbUrl = url;
                return origOpen.call(this, method, url, ...rest);
            };
            XMLHttpRequest.prototype.send = function(...args) {
                this.addEventListener('load', function() {
                    capture(this.__arbUrl, this.responseText);
                });
                return origSend.apply(this, args);
            };
        """)

    def _get_wager_network_log(self):
        try:
            return self.driver.execute_script("return window.__wagerResponses || []") or []
        except Exception:
            return []

    def _scan_rejection_ui(self):
        reject_markers = (
            "error", "rejected", "not accepted", "another user has taken",
            "insufficient funds", "insufficient balance", "failed to place",
            "wager declined", "unable to place", "line changed", "odds changed",
            "session expired", "logged out",
        )
        try:
            slip = self._betslip_text().lower()
            for marker in reject_markers:
                if marker in slip:
                    return True, f"Bet slip rejection: {self._betslip_text()[:300]}"
        except Exception:
            pass

        try:
            page_l = (self.driver.page_source or "").lower()
            page_markers = tuple(m for m in reject_markers if m != "error")
            for marker in page_markers:
                if marker in page_l:
                    return True, f"Page rejection marker: {marker}"
        except Exception:
            pass
        return False, ""

    def _accept_line_changes(self):
        accepted = False
        try:
            for cb in self.driver.find_elements(
                By.CSS_SELECTOR, "#betSlipDiv input[type='checkbox'], #betSlipBody input[type='checkbox']"
            ):
                label_bits = " ".join(
                    filter(
                        None,
                        (
                            cb.get_attribute("id") or "",
                            cb.get_attribute("class") or "",
                            cb.get_attribute("ng-model") or "",
                        ),
                    )
                ).lower()
                nearby = ""
                try:
                    parent = cb.find_element(By.XPATH, "./..")
                    nearby = (parent.text or "").lower()
                except Exception:
                    pass
                if "accept" in label_bits or "accept" in nearby or "auto accept" in nearby:
                    if not cb.is_selected():
                        self.driver.execute_script("arguments[0].click();", cb)
                        accepted = True
        except Exception:
            pass

        for btn in self.driver.find_elements(By.CSS_SELECTOR, "#betSlipDiv button, #betSlipDiv a"):
            try:
                text = (btn.text or "").strip().lower()
                if text in ("accept", "accept changes", "ok", "continue"):
                    self.driver.execute_script("arguments[0].click();", btn)
                    accepted = True
                    time.sleep(0.5)
            except Exception:
                continue

        if accepted:
            self.logger.info("Accepted line changes / odds update prompts")
        return accepted

    def _fill_betamapola_stake(self, stake_plan) -> bool:
        if self._betslip_is_empty():
            self.logger.warning("Cannot enter stake: bet slip has no wager items")
            return False

        filled = fill_betslip_stake_input(
            self.driver,
            stake_plan,
            self.logger,
            scope_css="#betSlipBody",
        )
        if filled:
            time.sleep(0.3)
            sync_betamapola_stake_models(
                self.driver,
                stake_plan.risk,
                stake_plan.to_win,
                stake_plan.entry_field,
            )
            return True

        self.logger.warning(
            "Bet slip stake inputs not found in DOM; trying Angular ticket models"
        )
        return sync_betamapola_stake_models(
            self.driver,
            stake_plan.risk,
            stake_plan.to_win,
            stake_plan.entry_field,
        )

    def _click_process_ticket(self) -> bool:
        self._accept_line_changes()
        submitted = invoke_betamapola_process_ticket(self.driver)
        if submitted == "ProcessTicket":
            self.logger.info("ProcessTicket invoked via Angular betSlipController")
            return True
        if submitted == "unsafe":
            self.logger.warning(
                "ProcessTicket blocked: IsSafeToPostTicket() is false after stake entry"
            )

        place_btn = None
        for b in self.driver.find_elements(By.CSS_SELECTOR, "#betSlipDiv button, #betSlipDiv a"):
            ng_click = (b.get_attribute("ng-click") or "").lower()
            if "processticket" in ng_click or "place bet" in (b.text or "").lower():
                place_btn = b
                break
        if not place_btn:
            return False

        btn_class = (place_btn.get_attribute("class") or "").lower()
        if "btn-disabled" in btn_class or place_btn.get_attribute("disabled"):
            self.logger.warning("Place Bet button is disabled")
            return False

        self.driver.execute_script("arguments[0].click();", place_btn)
        self.logger.info("Place Bet clicked (DOM fallback)")
        return True

    def _betslip_shows_wager_confirmed(self) -> bool:
        return betslip_text_confirms_wager(self._betslip_text())

    def _betslip_awaiting_place_bet(self) -> bool:
        try:
            for btn in self.driver.find_elements(
                By.CSS_SELECTOR, "#betSlipDiv button, #betSlipDiv a"
            ):
                if "place bet" in (btn.text or "").lower() and btn.is_displayed():
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _wager_network_entry_confirms(entry: dict) -> bool:
        return wager_network_entry_confirms(entry)

    def _confirm_bet_accepted(self, team_name: str, team_1: str, team_2: str, stake: float, timeout: int = 25):
        deadline = time.time() + timeout
        success_markers = (
            "wager accepted", "bet accepted", "ticket accepted",
            "successfully placed", "your wager has been accepted",
            "wager(s) confirmed", "wagers confirmed",
        )

        while time.time() < deadline:
            rejected, reject_msg = self._scan_rejection_ui()
            if rejected:
                if self._message_requires_relogin(reject_msg):
                    self._invalidate_wager_session()
                self.logger.error(f"Bet rejected by bookmaker UI: {reject_msg}")
                return False, reject_msg

            if self._betslip_shows_wager_confirmed():
                return True, "Bet slip shows wager confirmed"

            page_l = (self.driver.page_source or "").lower()
            for marker in success_markers:
                if marker in page_l:
                    return True, marker

            for entry in self._get_wager_network_log():
                if self._wager_network_entry_confirms(entry):
                    self.logger.info(
                        f"Wager API success ({entry.get('url')}): "
                        f"{(entry.get('body') or '')[:300]}"
                    )
                    if self._betslip_awaiting_place_bet() and not self._betslip_shows_wager_confirmed():
                        continue
                    return True, "Wager post API confirmed"

            for pick in self._fetch_open_wagers_via_api():
                if self._pick_matches_open_wager(pick, team_name, team_1, team_2):
                    return True, "Open wager found via GetWagerPicks"

            time.sleep(1)

        self.logger.warning(
            f"Bet not confirmed within {timeout}s; final GetWagerPicks retries"
        )
        for attempt in range(1, 4):
            if self._betslip_shows_wager_confirmed():
                return True, "Bet slip shows wager confirmed (retry)"
            for pick in self._fetch_open_wagers_via_api():
                if self._pick_matches_open_wager(pick, team_name, team_1, team_2):
                    return True, "Open wager found via GetWagerPicks (retry)"
            if attempt < 3:
                time.sleep(5)
        if self._betslip_awaiting_place_bet():
            return False, "Place Bet still visible — wager not submitted"
        return False, "Bet not confirmed by bookmaker"

    def _confirm_bet_accepted_fast(
        self,
        team_name: str,
        team_1: str,
        team_2: str,
        process_data: dict | None = None,
        timeout: int = 12,
    ):
        """Confirm via ProcessTicket ticket number, then open-bets page (source of truth)."""
        ticket_number = None
        if process_data:
            ok, ticket_number, msg = parse_process_ticket_response(process_data)
            if ok and ticket_number and int(ticket_number) > 0:
                verified, verify_msg = self._verify_open_bet_on_open_bets_page(
                    team_name,
                    team_1,
                    team_2,
                    ticket_number=ticket_number,
                    timeout=min(timeout + 8, 25),
                )
                if verified:
                    return True, verify_msg
                return True, msg

        deadline = time.time() + timeout
        while time.time() < deadline:
            rejected, reject_msg = self._scan_rejection_ui()
            if rejected:
                if self._message_requires_relogin(reject_msg):
                    self._invalidate_wager_session()
                return False, reject_msg

            page_text = self._load_open_bets_page_text()
            if self._open_bets_text_has_wager(
                page_text, team_name, team_1, team_2, ticket_number=ticket_number
            ):
                return True, "Open wager verified on open-bets page"

            if self._betslip_shows_wager_confirmed():
                return True, "Bet slip shows wager confirmed"

            for pick in self._fetch_open_wagers_via_api():
                if self._pick_matches_open_wager(pick, team_name, team_1, team_2):
                    return True, "Open wager found via GetWagerPicks"

            time.sleep(0.5)

        verified, verify_msg = self._verify_open_bet_on_open_bets_page(
            team_name, team_1, team_2, ticket_number=ticket_number, timeout=12
        )
        if verified:
            return True, verify_msg
        return False, "Bet not confirmed on open-bets page"

    def _execute_bet_attempt_api(
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
        """Fast path: API line lookup, Angular pick/stake, ProcessTicket HTTP."""
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)
        market_label = (
            f"spread {spread_line:+.1f}" if bet_type == "spread" and spread_line is not None else bet_type
        )
        self.logger.info(
            f"API placement | Game ID: {game_id} | Team: {team_name} | "
            f"Market: {market_label} | Odds: {moneyline_odd} | {format_base_amount_stake(stake_plan)}"
        )

        self._refresh_session_before_wager()

        game_line, team_no = self._lookup_game_line_from_api(
            game_id, team_name, team_1=team_1, team_2=team_2, moneyline_odd=moneyline_odd
        )
        if not game_line:
            self.__ensure_sport_offering_loaded(fast=True)
            game_line, team_no = self._lookup_game_line_from_api(
                game_id, team_name, team_1=team_1, team_2=team_2, moneyline_odd=moneyline_odd
            )
        if not game_line:
            raise Exception(
                f"Game {game_id} ({team_name}) not found in live {self.sport_name} API offering"
            )

        matchup_1 = str(game_line.get("Team1ID") or team_1 or "")
        matchup_2 = str(game_line.get("Team2ID") or team_2 or "")
        if self._has_existing_open_bet(team_name, matchup_1, matchup_2):
            raise Exception(f"Open bet already exists for {team_name}; skipping duplicate placement")

        game_num = game_line.get("GameNum")
        self.logger.info(
            f"API resolved GameNum={game_num}, team_no={team_no}, "
            f"rot={game_line.get('Team1RotNum')}-{game_line.get('Team2RotNum')}"
        )

        if bet_type == "spread":
            live_odds = game_line.get(f"SpreadAdj{team_no}")
            if live_odds is not None and not self._odds_text_matches(str(live_odds), moneyline_odd):
                raise Exception(
                    f"Line moved: live spread odds {live_odds} differ from arb odds {moneyline_odd}"
                )
            if spread_line is not None:
                spread_val = game_line.get("Spread")
                team_1_spread, team_2_spread = resolve_ticosports_spread_lines(
                    spread_val, game_line.get("MoneyLine1"), game_line.get("MoneyLine2")
                )
                live_spread = team_1_spread if team_no == 1 else team_2_spread
                if live_spread is not None and not spread_values_match(live_spread, spread_line):
                    raise Exception(
                        f"Spread line moved: live {live_spread} differs from arb {spread_line}"
                    )

        dom_ready = False
        if bet_type == "moneyline" and game_num and team_no:
            dom_ready = bool(self._wait_for_moneyline_button(game_num, team_no, timeout=3))
        elif bet_type == "spread" and game_num and team_no:
            dom_ready = bool(self._wait_for_spread_button(game_num, team_no, timeout=3))

        angular_ready = wait_for_angular_game_lines(self.driver, timeout=4)
        if not angular_ready and not dom_ready:
            self.logger.info("Pick paths not ready; loading full sport offering")
            self.__ensure_sport_offering_loaded(game_num=game_num, team_no=team_no)
            angular_ready = wait_for_angular_game_lines(self.driver, timeout=10)
            if bet_type == "moneyline" and game_num and team_no:
                dom_ready = bool(self._wait_for_moneyline_button(game_num, team_no, timeout=5))
            elif bet_type == "spread" and game_num and team_no:
                dom_ready = bool(self._wait_for_spread_button(game_num, team_no, timeout=5))

        if not angular_ready and not dom_ready:
            raise Exception("Neither Angular GameLineAction nor DOM line button is available")

        self._prepare_bet_slip_for_wager()
        if bet_type == "spread":
            if not self._add_spread_to_slip(game_line, team_no, team_name):
                raise Exception(f"Could not add spread pick for {team_name} (GameNum={game_num})")
        elif not self._add_moneyline_to_slip(game_line, team_no, team_name):
            raise Exception(f"Could not add moneyline pick for {team_name} (GameNum={game_num})")

        wager_count = wait_for_betamapola_wager_items(self.driver, min_count=1, timeout=3.0)
        if wager_count < 1:
            raise Exception(
                f"Bet slip has no wager items after pick for {team_name}"
            )

        if not self._fill_betamapola_stake(stake_plan):
            raise Exception("Could not set stake on bet slip")

        if not ensure_betamapola_stake_ready(
            self.driver,
            stake_plan.risk,
            stake_plan.to_win,
            stake_plan.entry_field,
            timeout=3.0,
        ):
            self.logger.warning("Angular stake models not confirmed safe; retrying after line accept")

        if accept_betamapola_line_changes(self.driver):
            self.logger.info("Accepted line changes after stake entry")
            self._fill_betamapola_stake(stake_plan)
            ensure_betamapola_stake_ready(
                self.driver,
                stake_plan.risk,
                stake_plan.to_win,
                stake_plan.entry_field,
                timeout=3.0,
            )

        if self._page_has_login_required_marker():
            self._invalidate_wager_session()
            raise Exception("Rejection marker on page: please log in")

        posted, process_data, post_msg, submit_mode = betamapola_process_ticket_via_api(
            self.driver, password=self.password
        )
        if not posted:
            raise Exception(post_msg or "ProcessTicket API failed")

        self._betamapola_ticket_submitted = True
        self.logger.info(f"ProcessTicket submitted via {submit_mode}: {post_msg}")
        process_payload = None
        if isinstance(process_data, dict):
            process_payload = process_data

        matchup_1 = team_1 or game_line.get("Team1ID") or ""
        matchup_2 = team_2 or game_line.get("Team2ID") or ""
        confirmed, message = self._confirm_bet_accepted_fast(
            team_name,
            matchup_1,
            matchup_2,
            process_data=process_payload,
        )
        if not confirmed and self._betamapola_ticket_submitted and process_payload:
            ok, ticket_number, msg = parse_process_ticket_response(process_payload)
            if ok and ticket_number:
                confirmed, message = True, msg

        if not confirmed and self._betamapola_ticket_submitted:
            recovered, recover_msg = self._verify_open_bet_on_open_bets_page(
                team_name, matchup_1, matchup_2, timeout=15
            )
            if recovered:
                confirmed, message = True, recover_msg

        if not confirmed:
            raise Exception(message or "Bet not accepted by bookmaker")

        ticket_number = None
        if process_payload:
            ok, ticket_number, _ = parse_process_ticket_response(process_payload)
            if not ok:
                ticket_number = None

        self.logger.info(f"Bet accepted by bookmaker (API path): {message}")
        self._last_ticket_number = ticket_number
        self._notify_open_bets_screenshot(
            team_name,
            matchup_1,
            matchup_2,
            game_id,
            ticket_number=ticket_number,
        )
        cache_key = f"{team_name}:{matchup_1}:{matchup_2}".lower()
        self._pending_check_cache[cache_key] = {"found": True, "ts": time.time()}
        return True, stake_plan

    # --------------------------------------------------------
    # Execute Bet (adapted for Betamapola /sports#/ SPA + betSlipDiv)
    # --------------------------------------------------------
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
        self._betamapola_ticket_submitted = False
        blocked = block_real_money_bet(
            self.logger, stake, bet_type=bet_type, bookmaker=self.bookmaker
        )
        if blocked is not None:
            self._last_bet_error = REAL_MONEY_BETTING_PAUSED_MSG
            return blocked
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)

        try:
            for attempt in range(1, 3):
                try:
                    if attempt > 1:
                        self.logger.info(f"Retrying wager after re-login (attempt {attempt}/2)")
                    if self.API_PLACEMENT_ENABLED:
                        return self._execute_bet_attempt_api(
                            game_id, team_name, moneyline_odd, stake,
                            team_1=team_1, team_2=team_2,
                            bet_type=bet_type,
                            spread_line=spread_line,
                        )
                    return self._execute_bet_attempt(
                        game_id, team_name, moneyline_odd, stake,
                        team_1=team_1, team_2=team_2,
                        bet_type=bet_type,
                        spread_line=spread_line,
                    )
                except Exception as e:
                    err_text = str(e)
                    if attempt == 1 and self._betamapola_ticket_submitted:
                        matchup_1 = team_1 or ""
                        matchup_2 = team_2 or ""
                        recovered, recover_msg = self._verify_open_bet_on_open_bets_page(
                            team_name, matchup_1, matchup_2, timeout=12
                        )
                        if recovered:
                            self.logger.warning(
                                f"ProcessTicket likely succeeded despite error ({err_text}); "
                                f"{recover_msg} — not retrying"
                            )
                            return True, stake_plan
                    if attempt == 1 and "open bet already exists" in err_text.lower():
                        raise
                    if attempt == 1 and self._message_requires_relogin(err_text):
                        self.logger.warning(
                            f"Wager blocked by expired session ({e}); forcing re-login and retry"
                        )
                        self._invalidate_wager_session()
                        self.__login()
                        self.__ensure_sport_offering_loaded()
                        continue
                    raise
            return False, stake

        except Exception as e:
            self._last_bet_error = str(e)
            self.logger.error(f"Place Bet failed: {e}", exc_info=True)
            asyncio.run(send_monitoring_alert(self.website, self.account_id, e, TELEGRAM.get('arbitrage_monitoring')))
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
        try:
            stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)
            market_label = (
                f"spread {spread_line:+.1f}" if bet_type == "spread" and spread_line is not None else bet_type
            )
            self.logger.info(
                f"Placing Bet | Game ID: {game_id} | Team: {team_name} | "
                f"Market: {market_label} | Odds: {moneyline_odd} | {format_base_amount_stake(stake_plan)}"
            )

            self._refresh_session_before_wager()

            game_line, team_no = self._lookup_game_line_from_api(
                game_id, team_name, team_1=team_1, team_2=team_2
            )
            if not game_line:
                self.logger.warning(
                    f"Game {game_id} not found in live API on first pass; refreshing offering"
                )
                self.__ensure_sport_offering_loaded(fast=True)
                game_line, team_no = self._lookup_game_line_from_api(
                    game_id, team_name, team_1=team_1, team_2=team_2
                )
            if not game_line:
                raise Exception(
                    f"Game {game_id} ({team_name}) not found in live {self.sport_name} API offering"
                )

            game_num = game_line.get("GameNum")
            self.logger.info(
                f"API resolved GameNum={game_num}, team_no={team_no}, "
                f"rot={game_line.get('Team1RotNum')}-{game_line.get('Team2RotNum')}"
            )

            if bet_type == "spread":
                live_odds = game_line.get(f"SpreadAdj{team_no}")
                if live_odds is not None and not self._odds_text_matches(str(live_odds), moneyline_odd):
                    raise Exception(
                        f"Line moved: live spread odds {live_odds} differ from arb odds {moneyline_odd}"
                    )
                if spread_line is not None:
                    spread_val = game_line.get("Spread")
                    team_1_spread, team_2_spread = resolve_ticosports_spread_lines(
                        spread_val, game_line.get("MoneyLine1"), game_line.get("MoneyLine2")
                    )
                    live_spread = team_1_spread if team_no == 1 else team_2_spread
                    if live_spread is not None and not spread_values_match(live_spread, spread_line):
                        raise Exception(
                            f"Spread line moved: live {live_spread} differs from arb {spread_line}"
                        )

            if not self.__ensure_sport_offering_loaded(
                game_num=game_num, team_no=team_no, fast=True
            ):
                self.__ensure_sport_offering_loaded(game_num=game_num, team_no=team_no)

            if bet_type == "spread":
                spread_elem = self._find_spread_element(
                    game_id,
                    team_name,
                    moneyline_odd,
                    game_line=game_line,
                    team_no=team_no,
                    spread_line=spread_line,
                )
                if not spread_elem:
                    self.logger.warning(
                        f"Spread element not found on first pass for {team_name} @ {moneyline_odd}, re-navigating"
                    )
                    self.__ensure_sport_offering_loaded(game_num=game_num, team_no=team_no)
                    spread_elem = self._find_spread_element(
                        game_id,
                        team_name,
                        moneyline_odd,
                        game_line=game_line,
                        team_no=team_no,
                        spread_line=spread_line,
                    )
                add_to_slip = lambda: self._add_spread_to_slip(
                    game_line, team_no, team_name, spread_elem=spread_elem
                )
                missing_line_msg = f"Spread not found for {team_name} @ {moneyline_odd}"
            else:
                moneyline_elem = self._find_moneyline_element(
                    game_id, team_name, moneyline_odd, game_line=game_line, team_no=team_no
                )
                if not moneyline_elem:
                    self.logger.warning(
                        f"Moneyline element not found on first pass for {team_name} @ {moneyline_odd}, re-navigating"
                    )
                    self.__ensure_sport_offering_loaded(game_num=game_num, team_no=team_no)
                    moneyline_elem = self._find_moneyline_element(
                        game_id, team_name, moneyline_odd, game_line=game_line, team_no=team_no
                    )
                add_to_slip = lambda: self._add_moneyline_to_slip(
                    game_line, team_no, team_name, moneyline_elem=moneyline_elem
                )
                missing_line_msg = f"Moneyline not found for {team_name} @ {moneyline_odd}"

            self.wait.until(EC.presence_of_element_located((By.ID, "betSlipDiv")))
            self.logger.info("Bet slip appeared")

            if not add_to_slip():
                time.sleep(1.0)
                if self._betslip_has_pick(team_name, bet_type):
                    self.logger.info(f"{bet_type.title()} pick present in bet slip after delayed render")
                else:
                    slip_preview = self._betslip_text()[:200]
                    raise Exception(
                        f"Bet slip still empty after click attempts for {team_name} "
                        f"(GameNum={game_num}): {slip_preview or missing_line_msg}"
                    )

            if not self._betslip_has_pick(team_name, bet_type):
                raise Exception(
                    f"Bet slip missing {bet_type} pick for {team_name}: "
                    f"{self._betslip_text()[:200]}"
                )

            limits_text = self._betslip_text()
            self.logger.info(f"Bet slip populated: {limits_text[:200]}")

            if not self._fill_betamapola_stake(stake_plan):
                raise Exception("Could not locate bet slip stake input for base amount")

            self._accept_line_changes()

            if self._page_has_login_required_marker():
                self._invalidate_wager_session()
                raise Exception("Rejection marker on page: please log in")

            self._install_wager_network_hook()
            if not self._click_process_ticket():
                raise Exception("Place Bet button not found or disabled")
            network_log = self._get_wager_network_log()
            if network_log:
                self.logger.info(f"Wager network activity after click: {network_log[-3:]}")
            else:
                self.logger.warning("No wager network activity detected immediately after Place Bet click")

            matchup_1 = team_1 or game_line.get("Team1ID") or ""
            matchup_2 = team_2 or game_line.get("Team2ID") or ""
            confirmed, message = self._confirm_bet_accepted(team_name, matchup_1, matchup_2, stake_plan)
            if not confirmed:
                raise Exception(message or "Bet not accepted by bookmaker")

            self.logger.info(f"Bet accepted by bookmaker: {message}")
            return True, stake_plan

        except Exception:
            raise

    def _quit_driver(self):
        """Safely terminate only this controller's WebDriver session."""
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
        """Kill only Chrome/chromedriver processes owned by this controller instance."""
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
        """Remove old temp profile/extension dirs without killing live browser processes."""
        cleanup_stale_temp_dirs(
            active_dirs=(
                getattr(self, "user_data_dir", None),
                getattr(self, "proxy_extension_dir", None),
            ),
            max_age_seconds=max_age_seconds,
            logger=self.logger,
        )

    def _recover_driver(self):
        """Attempt to recover from driver crash by killing processes, removing stale temps,
        and creating a completely fresh driver + extension.
        """
        self.logger.info("Recovering from Chrome driver crash...")
        owned_profile = getattr(self, "user_data_dir", None)
        self._cleanup_owned_chrome()

        # Remove this run's temp dirs (extension and user data) so the next create is clean
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

        # Fresh everything
        self._create_driver()

    # --------------------------------------------------------
    # Place Bet
    # --------------------------------------------------------
    def place_bet(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd: str,
        stake: float = 1.0
    ):
        self.logger = Logger.get_logger(f"{self.bookmaker}-bet")
        self.storage = Storage(self.logger)

        self.logger.info("==================== Place Bet (START) ====================")

        # Step 1: Login
        self.__login()

        # Step 2: Load sport offering (same path as odds fetch / betting loop)
        self.__ensure_sport_offering_loaded()

        # Step 3: Place Bet
        self.__execute_bet(game_id, team_name, moneyline_odd, stake, team_1=team_1, team_2=team_2)

        self.logger.info("==================== Place Bet (END) ====================")

    # --------------------------------------------------------
    # Betting (arbitrage loop) - identical high-level flow
    # --------------------------------------------------------
    def betting(
        self,
        stake: float = 1.0
    ):
        self.logger = Logger.get_logger(f"{self.bookmaker}-betting")
        self.storage = Storage(self.logger)

        self.logger.info("==================== Betting (START) ====================")

        self._api_offering_failures = 0
        self._last_full_game_line_count = 0
        self._thin_offering_recovery_attempted = False

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()

        # Clean only stale temp dirs; never pkill all Chrome (other jobs may be running).
        self._cleanup_stale_temp_dirs()

        # Initial driver is created in __init__, but under systemd + BrightData extension
        # the session can be dead within seconds even if webdriver.Chrome() "succeeded".
        # Wrap first login + nav in recovery retries so we don't lose the whole process.
        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                # Step 1: Ensure logged-in session (login only when invalid)
                self._ensure_betting_session()

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
        self._exposure_cleanup_at = 0.0
        last_idle_poll_at = 0.0
        while True:
            watchdog.beat()
            self._exposure_cleanup_at = tick_exposure_cleanup(
                self.cache, self.logger, self._exposure_cleanup_at
            )

            try:
                current_url = self.driver.current_url
            except Exception as e:
                self.logger.error(f"Driver error getting current URL: {e}. Attempting recovery...")
                self._recover_driver()
                consecutive_recoveries += 1
                if consecutive_recoveries >= 3:
                    backoff = min(60, 10 * consecutive_recoveries)
                    self.logger.warning(f"Multiple recoveries ({consecutive_recoveries}). Backing off {backoff}s.")
                    time.sleep(backoff)
                    consecutive_recoveries = 0
                if not self._relogin_after_recovery():
                    time.sleep(8)
                continue

            if "/sports" not in current_url:
                self.logger.warning(f"Unexpected URL detected ({current_url}). Re-establishing session...")
                self._recover_driver()
                consecutive_recoveries += 1
                if consecutive_recoveries >= 3:
                    backoff = min(60, 10 * consecutive_recoveries)
                    self.logger.warning(f"Multiple recoveries ({consecutive_recoveries}). Backing off {backoff}s.")
                    time.sleep(backoff)
                    consecutive_recoveries = 0
                if not self._relogin_after_recovery():
                    time.sleep(8)
                continue

            consecutive_recoveries = 0
            arbs = get_arbitrage_for_placement(self.cache, self.bookmaker)
            if not arbs:
                _, last_idle_poll_at = wait_for_arb_or_idle(
                    self.cache,
                    self.bookmaker,
                    idle_poll_fn=self._maybe_poll_odds_while_idle,
                    last_idle_poll_at=last_idle_poll_at,
                )
                self.logger.info("Waiting for Arbitrage")
                continue

            self.logger.info(f"Arbitrage opportunities: {len(arbs)} — pausing odds scan for placement")

            for arb in arbs:
                sport = arb.get('sport')
                league = arb.get('league')
                game_datetime = arb.get('game_datetime')
                bet_type = arb.get('bet_type', 'moneyline')

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

                self.logger.info(f"Arbitrage | Match: {team_1} vs {team_2}")

                book_1 = arb.get("team_1_bookmaker")
                book_2 = arb.get("team_2_bookmaker")

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
                        f"skipping duplicate placement (arb scan lock requires bookmaker confirmation)"
                    )
                    continue

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
                    err = (self._last_bet_error or "").lower()
                    if "not found in live" in err and "api offering" in err:
                        self.logger.warning(
                            f"Game gone from live {self.sport_name} offering; "
                            f"removing arb from cache | {team_1} vs {team_2}"
                        )
                        self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                        continue
                if bet_placed:
                    self.logger.info("Bet Placement Completed")
                    acknowledge_placed_leg(
                        self.cache,
                        self.logger,
                        arb,
                        self.bookmaker,
                        game_id,
                        team_name=team_name,
                    )
                    screenshot_path = getattr(self, "_last_screenshot_path", None)
                    if not screenshot_path or not os.path.isfile(screenshot_path):
                        screenshot_path = capture_bet_screenshot_for_alert(
                            self.logger,
                            self.bookmaker,
                            arb,
                            team_name,
                            game_id,
                            stake_used,
                            wager_odds,
                            driver=self.driver,
                            ticket_number=getattr(self, "_last_ticket_number", None),
                        )
                    self._last_screenshot_path = None
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
                        screenshot_path=screenshot_path,
                        ticket_number=getattr(self, "_last_ticket_number", None),
                        leg_already_acknowledged=True,
                    )
                    self.logger.info("Re-establishing sport offering before next arbitrage")
                    self.__ensure_sport_offering_loaded()

        # The main arbitrage/betting loop above runs until the process is terminated.
        # Explicit returns in the setup phase or unrecoverable errors will end here.
        self.logger.info("==================== Betting (END) ====================")


# Quick self-test entrypoint (uses provided credentials)
def main():
    from database.models.Accounts import Accounts
    from utils.config import BETAMAPOLA, BETAMAPOLA_ACCOUNT, BETAMAPOLA_PASSWORD

    if not BETAMAPOLA_ACCOUNT or not BETAMAPOLA_PASSWORD:
        raise ValueError("BETAMAPOLA_ACCOUNT and BETAMAPOLA_PASSWORD must be set in .env")

    account = Accounts(
        account=BETAMAPOLA_ACCOUNT,
        password=BETAMAPOLA_PASSWORD,
        label='Reader-30K',
    )
    controller = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    # Only fetch odds for testing (betting requires live arb cache)
    controller.fetch_odds()


if __name__ == "__main__":
    main()
