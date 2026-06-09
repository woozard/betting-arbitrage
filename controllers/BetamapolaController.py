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

from utils.config import PROXY1, PROXY2, TELEGRAM, ZENROWS_API_KEY
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import parse_to_mysql_datetime, parse_odds, currency_to_float, send_telegram_alert, send_monitoring_alert, send_testing_alert, is_game_pregame, debug_filepath, prune_debug_files, get_debug_dir
from utils.bet_placement import finalize_confirmed_bet
from utils.timing import time_it
from cache.arbitrage_cache import ArbitrageCache

# Use a project-local temporary directory to avoid FileNotFoundError on /tmp
# (very common when running under systemd with PrivateTmp, small tmpfs, or
# restricted service environments). We create 'tmp/' next to the project root
# and force tempfile + Chrome to use it.
PROJECT_TMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tmp'))
os.makedirs(PROJECT_TMP_DIR, exist_ok=True)
tempfile.tempdir = PROJECT_TMP_DIR


class BetamapolaController:
    # ===================================================================
    # Betamapola.com - uses identical browser/scraping stack as Sports411
    # (Selenium + BrightData proxy extension + ZenRows for odds polling)
    # ===================================================================
    def __init__(self, account, site, sport="baseball"):  # MLB primary for this book

        # Credentials
        self.account_id = account.account
        self.password = account.password
        self.label = account.label if account.label else "N/A"

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
            self.logger.info("Login Successful")
            return True

        except Exception as e:
            self.logger.error(f"Login Failed: {e}")
            with open(debug_filepath("debug_login_betamapola_FAIL"), "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self._safe_send_monitoring_alert(e)
            raise

    def __inject_mutation_observer(self):
        # Kept for parity (not strictly required for current SPA)
        self.logger.info("Injecting Mutation Observer (JS)")
        script = """
        if (!window.oddsObserverInstalled) {
            window.oddsObserverInstalled = true;
            window.oddsBuffer = [];
            const target = document.getElementById('GameLines') || document.getElementById('gamesAccordion');
            if (!target) { console.log("Observer: target not found"); return; }
            const observer = new MutationObserver((mutations) => {
                window.oddsBuffer.push(target.innerHTML);
            });
            observer.observe(target, { childList: true, subtree: true, characterData: true });
            console.log("MutationObserver installed");
        }
        """
        self.driver.execute_script(script)

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
        disp = self._normalize_us_odds((displayed or "").strip())
        exp = self._normalize_us_odds(expected)
        if disp == exp:
            return True
        raw = (displayed or "").strip()
        return exp in raw or raw == str(expected).strip()

    @staticmethod
    def _team_name_matches(candidate: str, expected: str) -> bool:
        cand = (candidate or "").strip().lower()
        exp = (expected or "").strip().lower()
        if not cand or not exp:
            return False
        return cand == exp or exp in cand or cand in exp

    def _lookup_game_line_from_api(self, game_id: str, team_name: str):
        """Resolve a live API game line by rotation game_id + team name."""
        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        if len(rotations) < 2:
            return None, None

        rot1, rot2 = rotations[0], rotations[1]
        api_lines = self._fetch_game_lines_via_api()
        if not api_lines:
            return None, None

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

            if team_no is None:
                continue
            return gl, team_no

        return None, None

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

    def __ensure_sport_offering_loaded(self, game_num=None, team_no: int = None) -> bool:
        """Navigate the SPA to the active sport (MLB/NBA) so game lines are in the DOM."""
        self.logger.info(f"Ensuring {self.sport_name} offering is loaded in the SPA...")

        self.driver.get(self.sport_url)

        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "#gamesAccordion, .sport-lines-container, app-sports")
                )
            )
        except Exception:
            self.logger.warning("Main content containers not found quickly.")

        try:
            self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a.sportIcon, a#img_Baseball, a#img_Basketball"))
            )
        except Exception:
            self.logger.warning("Sports sidebar icons did not appear quickly.")

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

        self._trigger_angular_offering_select()
        time.sleep(3)

        api_lines = self._fetch_game_lines_via_api()
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

        return lines_ready

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
            if not result:
                self.logger.warning("API returned empty response")
                return []

            data = result.get("d", {}).get("Data", {})
            lines = data.get("GameLines", [])
            limits = data.get("SportLimits", [])

            self.logger.info(f"API success: {len(lines)} GameLines, {len(limits)} SportLimits entries")
            return lines
        except Exception as e:
            self.logger.error(f"Browser-context API call failed: {e}")
            return []

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
                    "team_1_spread": spread_val,
                    "team_2_spread": -spread_val if isinstance(spread_val, (int, float)) else None,
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

    def _has_existing_open_bet(self, team_name: str, team_1: str, team_2: str) -> bool:
        team_l = team_name.lower()
        for pick in self._fetch_open_wagers_via_api():
            text = json.dumps(pick).lower()
            if team_l in text and team_1.lower() in text and team_2.lower() in text:
                return True
        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            return team_l in body and team_1.lower() in body and team_2.lower() in body
        except Exception:
            return False

    def _confirm_bet_accepted(self, team_name: str, team_1: str, team_2: str, stake: float):
        time.sleep(2)
        page = (self.driver.page_source or "").lower()
        reject_markers = (
            "rejected", "not accepted", "insufficient funds", "insufficient balance",
            "failed to place", "wager declined", "unable to place",
        )
        for marker in reject_markers:
            if marker in page:
                return False, f"Bet rejected ({marker})"

        success_markers = (
            "wager accepted", "bet accepted", "ticket accepted",
            "successfully placed", "your wager has been accepted",
        )
        for marker in success_markers:
            if marker in page:
                return True, marker

        team_l = team_name.lower()
        for pick in self._fetch_open_wagers_via_api():
            text = json.dumps(pick).lower()
            if team_l in text:
                return True, "Open wager found via GetWagerPicks"

        try:
            for tab in self.driver.find_elements(
                By.XPATH,
                "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'open bet')]",
            ):
                self.driver.execute_script("arguments[0].click();", tab)
                time.sleep(2)
                break
        except Exception:
            pass

        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            if team_l in body and team_1.lower() in body and team_2.lower() in body:
                return True, "Open bet visible on page"
        except Exception:
            pass

        return False, "Bet not confirmed by bookmaker"

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
    ):
        self.logger.info("========== Execute Bet (START) ==========")

        try:
            self.logger.info(
                f"Placing Bet | Game ID: {game_id} | Team: {team_name} | Odds: {moneyline_odd} | Stake: {stake}"
            )

            game_line, team_no = self._lookup_game_line_from_api(game_id, team_name)
            if not game_line:
                raise Exception(
                    f"Game {game_id} ({team_name}) not found in live {self.sport_name} API offering"
                )

            game_num = game_line.get("GameNum")
            self.logger.info(
                f"API resolved GameNum={game_num}, team_no={team_no}, "
                f"rot={game_line.get('Team1RotNum')}-{game_line.get('Team2RotNum')}"
            )

            if not self.__ensure_sport_offering_loaded(game_num=game_num, team_no=team_no):
                raise Exception(f"{self.sport_name} lines not loaded before bet placement")

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

            added_to_slip = False
            if moneyline_elem:
                self.logger.info(f"Moneyline element located: {moneyline_elem.get_attribute('id')}")
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", moneyline_elem)
                time.sleep(0.4)
                self.driver.execute_script("arguments[0].click();", moneyline_elem)
                self.logger.info("Moneyline element clicked")
                added_to_slip = True
            elif self._click_moneyline_via_angular(game_line, team_no):
                added_to_slip = True
            else:
                raise Exception(
                    f"Moneyline not found for team '{team_name}' @ {moneyline_odd} "
                    f"(GameNum={game_num})"
                )

            if not added_to_slip:
                raise Exception(f"Failed to add {team_name} moneyline to bet slip")

            # Wait for Bet Slip (id="betSlipDiv")
            self.wait.until(EC.presence_of_element_located((By.ID, "betSlipDiv")))
            self.logger.info("Bet slip appeared")

            # Verify team in bet slip (safety)
            try:
                slip_text = self.driver.find_element(By.ID, "betSlipDiv").text.lower()
                if team_name.lower() not in slip_text:
                    self.logger.warning(f"Betslip team verification: expected '{team_name}' in slip text")
            except Exception:
                pass

            # Read limits if present (reuse pattern)
            try:
                limits_text = self.driver.find_element(By.ID, "betSlipDiv").text
                # Betamapola shows limits in header area; parse if visible
                self.logger.info(f"Bet slip context (limits may appear): {limits_text[:200]}")
            except Exception:
                pass

            # ENTER STAKE - look for common risk/stake inputs (TicoSports style often uses input[id^=risk_] or ng-model risk)
            stake_input = None
            for selector in [
                "input[id^='risk_']",
                "input[name*='risk']",
                "input[ng-model*='risk']",
                "input[ng-model*='stake']",
                "#betSlipDiv input[type='text']",
                "#betSlipDiv input[type='number']"
            ]:
                try:
                    elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if elems:
                        stake_input = elems[0]
                        break
                except:
                    continue

            if not stake_input:
                # Fallback: any visible text input in the bet slip area
                for inp in self.driver.find_elements(By.CSS_SELECTOR, "#betSlipDiv input"):
                    if inp.is_displayed():
                        stake_input = inp
                        break

            if stake_input:
                stake_input.clear()
                stake_input.send_keys(f"{stake:.2f}")
                self.logger.info(f"Stake entered: {stake:.2f}")
            else:
                self.logger.warning("Could not locate stake input - bet may use default")

            # Accept line changes if checkbox/prompt appears (common)
            try:
                for cb in self.driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
                    if "accept" in (cb.get_attribute("id") or "").lower() or "accept" in (cb.get_attribute("class") or "").lower():
                        if not cb.is_selected():
                            self.driver.execute_script("arguments[0].click();", cb)
                            self.logger.info("Accepted line changes")
            except Exception:
                pass

            # Click Place Bet (ng-click=ProcessTicket() or text)
            place_btn = None
            for b in self.driver.find_elements(By.CSS_SELECTOR, "#betSlipDiv button, #betSlipDiv a"):
                if "place bet" in b.text.lower() or "process" in (b.get_attribute("ng-click") or "").lower():
                    place_btn = b
                    break

            if not place_btn:
                raise Exception("Place Bet button not found")

            self.driver.execute_script("arguments[0].click();", place_btn)
            self.logger.info("Place Bet clicked")

            matchup_1 = team_1 or game_line.get("Team1ID") or ""
            matchup_2 = team_2 or game_line.get("Team2ID") or ""
            confirmed, message = self._confirm_bet_accepted(team_name, matchup_1, matchup_2, stake)
            if not confirmed:
                raise Exception(message or "Bet not accepted by bookmaker")

            self.logger.info(f"Bet accepted by bookmaker: {message}")
            return True, stake

        except Exception as e:
            self.logger.error(f"Place Bet failed: {e}", exc_info=True)
            asyncio.run(send_monitoring_alert(self.website, self.account_id, e, TELEGRAM.get('arbitrage_monitoring')))
            return False, stake
        finally:
            self.logger.info("========== Execute Bet (END) ==========")

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
        import shutil
        import glob

        active_dirs = {
            d for d in (
                getattr(self, "user_data_dir", None),
                getattr(self, "proxy_extension_dir", None),
            )
            if d
        }

        try:
            now = time.time()
            for pat in ("brightdata_proxy_*", "chrome_user_data_*"):
                for d in glob.glob(os.path.join(PROJECT_TMP_DIR, pat)):
                    if d in active_dirs:
                        continue
                    try:
                        if now - os.path.getmtime(d) < max_age_seconds:
                            continue
                        shutil.rmtree(d, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass

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

        # Clean only stale temp dirs; never pkill all Chrome (other jobs may be running).
        self._cleanup_stale_temp_dirs()

        # Initial driver is created in __init__, but under systemd + BrightData extension
        # the session can be dead within seconds even if webdriver.Chrome() "succeeded".
        # Wrap first login + nav in recovery retries so we don't lose the whole process.
        setup_ok = False
        for attempt in range(1, 6):
            try:
                # Step 1: Login (may raise if driver session is already gone)
                self.__login()

                # Step 2: Go to sports page and load MLB offering
                self.__ensure_sport_offering_loaded()

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
            time.sleep(2)

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
                bet_type = arb.get('bet_type')
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

                if self.cache.is_leg_placed(self.bookmaker, "moneyline", game_id):
                    self.logger.info(
                        f"Skipping — leg already confirmed on {self.bookmaker} | "
                        f"{team_name} | {team_1} vs {team_2}"
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                if self._has_existing_open_bet(team_name, team_1, team_2):
                    self.logger.warning(
                        f"Open bet already exists for {team_name} on {self.bookmaker}; "
                        f"marking leg placed and stopping new arb scans"
                    )
                    self.cache.mark_leg_placed(self.bookmaker, "moneyline", game_id)
                    self.cache.lock_arb_scan(team_1, team_2, book_1, book_2, game_date)
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                bet_placed, stake = self.__execute_bet(
                    game_id, team_name, moneyline_odd, stake, team_1=team_1, team_2=team_2
                )
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
