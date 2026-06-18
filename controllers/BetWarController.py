import time
import json
import asyncio
import re
import tempfile
import os
import requests
from decimal import Decimal, InvalidOperation
from bs4 import BeautifulSoup
from datetime import datetime
import pytz

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import sqlalchemy.exc

from utils.config import PROXY1, PROXY2, TELEGRAM, ZENROWS_API_KEY
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import parse_to_mysql_datetime, parse_odds, currency_to_float, send_telegram_alert, send_monitoring_alert, send_testing_alert, is_game_pregame, debug_filepath, prune_debug_files, get_debug_dir
from utils.bet_placement import finalize_confirmed_bet
from utils.timing import time_it
from utils.chrome_temp import cleanup_stale_temp_dirs, handle_init_driver_failure
from cache.arbitrage_cache import ArbitrageCache


class BetWarController:
    WAGER_SESSION_EXPIRED_MARKERS = (
        "please log in",
        "session expired",
        "logged out",
    )

    # ===================================================================
    # BetWar.com - Player portal (ASP.NET main.aspx sidebar + bet slip)
    # (Selenium + BrightData proxy extension)
    # ===================================================================
    def __init__(self, account, site, sport="baseball"):  # MLB primary for this book

        # Credentials
        self.account_id = account.account
        self.password = account.password
        self.label = account.label if account.label else "N/A"
        self._force_wager_relogin = False
        self._last_bet_error = None

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

        # BetWar Player portal (ASP.NET postbacks on main.aspx — not the /sports#/ SPA)
        self.base_url = f"https://www.{self.website}"
        self.login_url = self.base_url
        self.dashboard_url = f"{self.base_url}/Player/main.aspx"
        self.sport_url = self.dashboard_url

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
    # Login (BetWar — TicoSports-like but not identical to Betamapola)
    # --------------------------------------------------------
    def _login_urls_to_try(self):
        host = self.website
        return (
            f"https://www.{host}",
            f"https://{host}",
            f"https://www.{host}/Login.aspx",
            f"https://{host}/Login.aspx",
        )

    def _is_already_logged_in(self) -> bool:
        try:
            url = (self.driver.current_url or "").lower()
            if "/player/" in url or "player/main.aspx" in url:
                return True
            if "/sports" in url or "wagering" in url or "default.aspx" in url:
                if not self._login_form_visible():
                    return True
            if self.driver.find_elements(
                By.CSS_SELECTOR,
                "#linkSports, #div-sportsSidebar, #div-betSlip, #gamesAccordion, #betSlipDiv",
            ):
                return not self._login_form_visible()
        except Exception:
            pass
        return False

    def _click_login_entrypoint_if_needed(self):
        selectors = (
            "#LogInAccount",
            "a[href*='login']",
            "button[data-action='login']",
            ".login-form button",
            "a.login",
            "button.login",
        )
        for selector in selectors:
            for elem in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if not elem.is_displayed():
                        continue
                    text = (elem.text or elem.get_attribute("value") or "").strip().lower()
                    if selector == "#LogInAccount" or "log" in text or "sign" in text:
                        self.driver.execute_script("arguments[0].click();", elem)
                        self.logger.info(f"Clicked login entrypoint via {selector}")
                        time.sleep(2)
                        return
                except Exception:
                    continue

    def _find_login_inputs(self, timeout: int = 45):
        # BetWar uses ASP.NET Login.aspx (not TicoSports id=account like Betamapola)
        account_selectors = (
            (By.ID, "txtAccessOfCode"),
            (By.NAME, "txtAccessOfCode"),
            (By.ID, "account"),
            (By.NAME, "account"),
            (By.CSS_SELECTOR, "input[name='loginId']"),
            (By.CSS_SELECTOR, "input[name='username']"),
            (By.CSS_SELECTOR, "form.login-form input[type='text']"),
        )
        password_selectors = (
            (By.ID, "txtAccessOfPassword"),
            (By.NAME, "txtAccessOfPassword"),
            (By.ID, "password"),
            (By.NAME, "password"),
            (By.CSS_SELECTOR, "input[type='password']"),
        )
        deadline = time.time() + timeout
        last_error = "login fields not found"

        while time.time() < deadline:
            contexts = [None]
            try:
                frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
                contexts.extend(frames)
            except Exception:
                frames = []

            for frame in contexts:
                try:
                    self.driver.switch_to.default_content()
                    if frame is not None:
                        self.driver.switch_to.frame(frame)

                    account_input = None
                    password_input = None
                    for by, selector in account_selectors:
                        elems = self.driver.find_elements(by, selector)
                        if elems and elems[0].is_displayed():
                            account_input = elems[0]
                            break
                    for by, selector in password_selectors:
                        elems = self.driver.find_elements(by, selector)
                        if elems and elems[0].is_displayed():
                            password_input = elems[0]
                            break

                    if account_input and password_input:
                        ctx = "iframe" if frame is not None else "main document"
                        self.logger.info(f"Found login fields in {ctx}")
                        return account_input, password_input
                except Exception as e:
                    last_error = str(e)
                finally:
                    try:
                        self.driver.switch_to.default_content()
                    except Exception:
                        pass

            self._click_login_entrypoint_if_needed()
            time.sleep(1)

        raise TimeoutException(last_error)

    def _login_form_visible(self) -> bool:
        try:
            elems = self.driver.find_elements(By.ID, "txtAccessOfCode")
            return bool(elems) and elems[0].is_displayed()
        except Exception:
            return False

    def _page_has_login_error(self) -> str:
        """Only match visible login-page rejection text, not validation attrs on other pages."""
        if self._is_already_logged_in():
            return ""
        try:
            for elem in self.driver.find_elements(
                By.CSS_SELECTOR, ".error, .alert, .mlogin, #form1, .validation-summary-errors"
            ):
                text = (elem.text or "").strip().lower()
                if not text:
                    continue
                markers = (
                    "invalid username",
                    "invalid password",
                    "incorrect username",
                    "incorrect password",
                    "login failed",
                    "access denied",
                    "account is locked",
                    "account locked",
                    "suspended",
                )
                for marker in markers:
                    if marker in text:
                        return marker
        except Exception:
            pass
        return ""

    def _click_login_submit(self, password_input=None):
        try:
            form = self.driver.find_element(By.ID, "form1")
            self.driver.execute_script("arguments[0].submit();", form)
            self.logger.info("Submitted login via form1")
            return
        except Exception:
            pass

        for selector in (
            "input.btn01[type='submit']",
            "#form1 input[type='submit']",
            "#LogInAccount",
            "button[data-action='login']",
            "form.login-form button[type='submit']",
            "input[type='submit']",
            "button[type='submit']",
        ):
            elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
            for elem in elems:
                try:
                    if elem.is_displayed():
                        self.driver.execute_script("arguments[0].click();", elem)
                        self.logger.info(f"Clicked login submit via {selector}")
                        return
                except Exception:
                    continue

        if password_input is not None:
            password_input.send_keys(Keys.ENTER)
            self.logger.info("Submitted login via Enter key on password field")
            return

        raise Exception("Login submit button not found")

    def _wait_for_post_login(self, start_url: str, timeout: int = 45):
        start_url_l = (start_url or "").lower()

        def login_resolved(driver):
            if self._is_already_logged_in():
                return True
            err = self._page_has_login_error()
            if err:
                return True
            if not self._login_form_visible():
                return True
            url = (driver.current_url or "").lower()
            if url and url != start_url_l and "login.aspx" not in url and "/logins/" not in url:
                return True
            return False

        WebDriverWait(self.driver, timeout).until(login_resolved)

        err = self._page_has_login_error()
        if err:
            raise Exception(f"Login rejected by bookmaker page: {err}")

        if self._login_form_visible():
            raise Exception(
                f"Login form still visible after submit | url={self.driver.current_url}"
            )

    def _save_login_debug(self, label: str):
        path = debug_filepath(f"debug_login_betwar_{label}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.driver.page_source)
        self.logger.info(
            f"[SAVED] {path} | url={self.driver.current_url} | title={self.driver.title}"
        )

    def __login(self):
        try:
            self.logger.info(f"Account: {self.account_id}")
            self.logger.info(f"Label: {self.label}")

            opened = False
            for url in self._login_urls_to_try():
                self.logger.info(f"Opening Login Page: {url}")
                self.driver.get(url)
                time.sleep(4)
                if self._is_already_logged_in():
                    self._force_wager_relogin = False
                    self.logger.info("Already logged in; skipping credential entry")
                    return True
                self._click_login_entrypoint_if_needed()
                try:
                    self._find_login_inputs(timeout=8)
                    self.login_url = url
                    opened = True
                    break
                except TimeoutException:
                    continue

            if not opened:
                self.logger.info(f"Retrying default login URL: {self.login_url}")
                self.driver.get(self.login_url)
                time.sleep(6)

            self._save_login_debug("precheck")

            page_source_lower = (self.driver.page_source or "").lower()
            if "sorry, you have been blocked" in page_source_lower or "attention required" in page_source_lower:
                self.logger.error("[BLOCK] HARD BLOCK DETECTED - SWITCHING TO ZENROWS")
                self._zenrows_get(self.login_url)
                self.logger.info("[OK] Zenrows login page retrieved successfully")
                return True

            account_input, password_input = self._find_login_inputs(timeout=45)
            start_url = self.driver.current_url
            account_input.clear()
            account_input.send_keys(self.account_id)
            password_input.clear()
            password_input.send_keys(self.password)
            self._click_login_submit(password_input=password_input)
            self._wait_for_post_login(start_url=start_url)
            self._save_login_debug("post_submit")
            self._force_wager_relogin = False
            self.logger.info(f"Login Successful | url={self.driver.current_url}")
            return True

        except Exception as e:
            self.logger.error(f"Login Failed: {e}")
            try:
                self._save_login_debug("FAIL")
            except Exception:
                pass
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

    def _betslip_text(self) -> str:
        for selector in ("#div-betSlip", "#betSlipDiv", "#div-betSlipCart"):
            try:
                text = (self.driver.find_element(By.CSS_SELECTOR, selector).text or "").strip()
                if text:
                    return text
            except Exception:
                continue
        return ""

    def _betslip_has_team(self, team_name: str) -> bool:
        slip = self._betslip_text().lower()
        if not slip or "bet slip is empty" in slip:
            return False
        return team_name.lower() in slip

    def _wait_for_betslip_team(self, team_name: str, timeout: int = 8) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._betslip_has_team(team_name):
                return True
            time.sleep(0.4)
        return False

    def _add_moneyline_to_slip(
        self, game_line: dict, team_no: int, team_name: str,
        moneyline_elem=None,
    ) -> bool:
        """Click DOM and/or Angular until the bet slip actually contains the team."""
        game_num = game_line.get("GameNum")

        if moneyline_elem:
            self.logger.info(f"Moneyline element located: {moneyline_elem.get_attribute('id')}")
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", moneyline_elem)
            time.sleep(0.4)
            self.driver.execute_script("arguments[0].click();", moneyline_elem)
            self.logger.info("Moneyline element clicked")
            if self._wait_for_betslip_team(team_name, timeout=4):
                return True
            self.logger.warning(
                f"DOM click on M{team_no}_{game_num}_0 did not populate bet slip; trying Angular"
            )

        if self._click_moneyline_via_angular(game_line, team_no):
            if self._wait_for_betslip_team(team_name, timeout=6):
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

    # ------------------------------------------------------------------
    # BetWar Player portal navigation (main.aspx sidebar + postbacks)
    # ------------------------------------------------------------------
    def _player_portal_url(self) -> str:
        return f"{self.base_url}/Player/main.aspx"

    def _league_keywords(self) -> tuple:
        if self.sport_name == "NBA":
            return ("nba",)
        return ("mlb", "major league")

    def _sidebar_sport_name(self) -> str:
        return "Basketball" if self.sport_name == "NBA" else "Baseball"

    def _ensure_player_portal(self):
        url = (self.driver.current_url or "").lower()
        if "/player/" not in url:
            self.driver.get(self._player_portal_url())
            time.sleep(3)

    def _portal_click(self, elem):
        """Click via Selenium first so ASP.NET postback handlers fire."""
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        time.sleep(0.3)
        try:
            elem.click()
            return
        except Exception:
            pass
        self.driver.execute_script("arguments[0].click();", elem)

    def _sports_view_ready(self) -> bool:
        try:
            link = self.driver.find_element(By.ID, "linkSports")
            if (link.get_attribute("aria-expanded") or "").lower() != "true":
                return False
            if not self.driver.find_elements(
                By.CSS_SELECTOR,
                "#div-sportsSidebar.show, #div-sportsSidebar.collapse.show",
            ):
                return False
            return bool(self.driver.find_elements(By.CSS_SELECTOR, ".sport-sfl-item"))
        except Exception:
            return False

    def _open_sports_sidebar(self):
        self._ensure_player_portal()
        if self._sports_view_ready():
            self.logger.info("Sports sidebar already active")
            return

        for selector in ("#linkSports", "a#linkSports"):
            elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if elems:
                self._portal_click(elems[0])
                self.logger.info("Opened sports sidebar via linkSports")
                break
        else:
            self.logger.warning("linkSports not found")
            return

        try:
            WebDriverWait(self.driver, 15).until(lambda _: self._sports_view_ready())
        except TimeoutException:
            self.logger.warning("Sports sidebar did not become active after linkSports click")

    def _sport_expanded(self, sport_name: str) -> bool:
        elems = self.driver.find_elements(
            By.CSS_SELECTOR, f'.sport-sfl-item[data-sportname="{sport_name}"]'
        )
        if not elems:
            return False
        is_open = (elems[0].get_attribute("data-is-open") or "").lower() == "true"
        has_stl = bool(self._find_sport_stl_items(sport_name))
        return is_open and has_stl

    def _find_sport_stl_items(self, sport_name: str = None):
        sport_name = sport_name or self._sidebar_sport_name()
        xpath = (
            f"//div[contains(@class,'sport-sfl-item') and @data-sportname='{sport_name}']"
            f"/following-sibling::div[contains(@class,'sport-tl-menu')]"
            f"//div[contains(@class,'sport-stl-item')]"
        )
        return self.driver.find_elements(By.XPATH, xpath)

    def _click_sidebar_sport(self):
        sport_name = self._sidebar_sport_name()
        selector = f'.sport-sfl-item[data-sportname="{sport_name}"]'
        elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
        if not elems:
            raise Exception(f"Sport sidebar item not found for {sport_name}")

        if self._sport_expanded(sport_name):
            self.logger.info(f"Sport {sport_name} already expanded with sub-menu")
            return

        self._portal_click(elems[0])
        self.logger.info(f"Clicked sport sidebar: {sport_name}")
        try:
            WebDriverWait(self.driver, 15).until(lambda _: self._sport_expanded(sport_name))
        except TimeoutException:
            self.logger.warning(f"Sport {sport_name} sub-menu did not appear after click")

    def _click_sport_ssl_league(self) -> bool:
        """Select MLB/NBA under the expanded sport (sport-ssl-item), not the sport header."""
        keywords = self._league_keywords()

        for elem in self.driver.find_elements(By.CSS_SELECTOR, ".sport-ssl-item"):
            label = (elem.get_attribute("data-sportname") or elem.text or "").strip().lower()
            if label and any(kw in label for kw in keywords):
                self._portal_click(elem)
                self.logger.info(f"Clicked sport-ssl-item league: {(elem.text or label)[:80]}")
                self._wait_for_postback()
                self._wait_for_loading_hidden()
                return True

        for span in self.driver.find_elements(By.CSS_SELECTOR, "span.lblLeagueName, p.lblLeagueName"):
            text = (span.text or "").strip()
            if not text:
                continue
            text_l = text.lower()
            if keywords and not any(kw in text_l for kw in keywords):
                continue
            clickable = span
            for xpath in (
                "./ancestor::div[contains(@class,'sport-ssl-item')][1]",
                "./ancestor::div[contains(@class,'sport-sfl-item')][1]",
            ):
                try:
                    clickable = span.find_element(By.XPATH, xpath)
                    break
                except Exception:
                    continue
            self._portal_click(clickable)
            self.logger.info(f"Clicked lblLeagueName league: {text[:80]}")
            self._wait_for_postback()
            self._wait_for_loading_hidden()
            return True
        return False

    def _click_game_subleague_item(self) -> bool:
        """Click the Game/Full Game period under the selected league."""
        sport_name = self._sidebar_sport_name()
        stl_items = self._find_sport_stl_items(sport_name)
        if not stl_items:
            stl_items = self.driver.find_elements(By.CSS_SELECTOR, ".sport-stl-item")

        target = None
        for elem in stl_items:
            label = (elem.text or "").strip().lower()
            if label in ("game", "games", "full game") or "game" in label:
                target = elem
                break
        if target is None and stl_items:
            target = stl_items[0]

        if not target:
            self.logger.warning(f"No sport-stl-item found under {sport_name}")
            return False

        self._portal_click(target)
        self.logger.info(f"Clicked sport-stl-item: {(target.text or '')[:80]}")
        self._wait_for_postback()
        self._wait_for_loading_hidden()
        return True

    def _click_league_or_subleague(self) -> bool:
        """Select league (sport-ssl-item) then Game period (sport-stl-item)."""
        for elem in self.driver.find_elements(By.CSS_SELECTOR, ".league-sfl-item"):
            name = (elem.get_attribute("data-leaguename") or elem.text or "").strip().lower()
            if name and any(kw in name for kw in self._league_keywords()):
                self._portal_click(elem)
                self.logger.info(f"Clicked league-sfl-item: {(elem.text or name)[:80]}")
                self._wait_for_postback()
                break

        if not self._click_sport_ssl_league():
            self.logger.warning(
                f"No {self.sport_name} league item found in sidebar; lines may not load"
            )
            return False

        return self._click_game_subleague_item()

    def _straights_tab_active(self) -> bool:
        for elem in self.driver.find_elements(By.CSS_SELECTOR, "#btnStraights"):
            classes = (elem.get_attribute("class") or "").lower()
            return "active" in classes or "btn-danger" in classes
        return False

    def _ensure_straights_tab(self):
        if self._straights_tab_active():
            self.logger.info("Straights tab already active; skipping submit click")
            return

        elems = self.driver.find_elements(By.CSS_SELECTOR, "#btnStraights")
        if elems:
            self._portal_click(elems[0])
            self._wait_for_postback()

    def _wait_for_postback(self, timeout: int = 20):
        """BetWar sidebar clicks trigger ASP.NET postbacks; wait for DOM to settle."""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass
        time.sleep(2)

    def _wait_for_loading_hidden(self, timeout: int = 45):
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.invisibility_of_element_located((By.ID, "divLoading"))
            )
        except TimeoutException:
            pass

    @staticmethod
    def _normalize_ml_odds(value) -> str:
        """Normalize BetWar moneyline values to Decimal-parseable American odds."""
        if value is None:
            return None

        if isinstance(value, dict):
            for key in ("odds", "line", "display", "value", "american"):
                nested = value.get(key)
                if nested not in (None, ""):
                    normalized = BetWarController._normalize_ml_odds(nested)
                    if normalized is not None:
                        return normalized
            return None

        text = str(value).strip()
        if not text:
            return None

        text = (
            text.replace("\u00a0", " ")
            .replace("\u2212", "-")
            .replace("\u2013", "-")
            .replace("\u2014", "-")
            .strip()
        )

        lowered = re.sub(r"[^a-z]", "", text.lower())
        if lowered in ("even", "ev", "pk", "pick", "pickem"):
            return BetWarController._normalize_us_odds(100)

        if lowered in ("off", "na", "none", "closed", "unavailable"):
            return None

        paren = re.match(r"^\(\s*([-+]?\d+(?:\.\d+)?)\s*\)$", text)
        if paren:
            text = f"-{paren.group(1).lstrip('+-')}"

        try:
            return BetWarController._normalize_us_odds(int(float(text)))
        except (TypeError, ValueError):
            pass

        num_match = re.search(r"[-+]?\d+", text)
        if num_match:
            try:
                return BetWarController._normalize_us_odds(int(num_match.group(0)))
            except (TypeError, ValueError):
                pass

        return None

    def _get_side_moneyline_odds(self, side: dict) -> str:
        """Extract moneyline odds from a GetLines side object."""
        ml_obj = side.get("moneyline") or {}
        for key in ("odds", "line", "display", "value", "american"):
            val = ml_obj.get(key)
            if val not in (None, ""):
                normalized = self._normalize_ml_odds(val)
                if normalized is not None:
                    return normalized

        for key in ("moneyline", "ml", "MoneyLine"):
            val = side.get(key)
            if val not in (None, ""):
                normalized = self._normalize_ml_odds(val)
                if normalized is not None:
                    return normalized
        return None

    def _filter_games_with_valid_moneylines(self, games: list) -> list:
        """Drop games whose moneyline values cannot be stored as DECIMAL odds."""
        valid = []
        for match in games:
            ml = match.get("moneyline") or {}
            t1, t2 = ml.get("team_1"), ml.get("team_2")
            if not t1 or not t2:
                self.logger.warning(
                    f"Dropping game {match.get('game_id')}: missing moneyline ({t1!r} / {t2!r})"
                )
                continue
            try:
                Decimal(str(t1))
                Decimal(str(t2))
            except InvalidOperation:
                self.logger.warning(
                    f"Dropping game {match.get('game_id')} "
                    f"({match.get('team_1')} vs {match.get('team_2')}): "
                    f"unparseable ML {t1!r} / {t2!r}"
                )
                continue
            valid.append(match)
        return valid

    def _parse_betwar_game_datetime(self, raw: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            return parse_to_mysql_datetime(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                tz_name=self.game_tz,
            )

        tz = pytz.timezone(self.game_tz)
        year = datetime.now(tz).year
        match = re.search(
            r"([A-Za-z]{3})\s+(\d{1,2})\s*-\s*(\d{1,2}:\d{2}\s*[APap][Mm])",
            raw,
        )
        if match:
            try:
                local_dt = datetime.strptime(
                    f"{match.group(1)} {match.group(2)} {year} {match.group(3)}",
                    "%b %d %Y %I:%M %p",
                )
                utc_dt = tz.localize(local_dt).astimezone(pytz.utc)
                return utc_dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        normalized = parse_to_mysql_datetime(raw, tz_name=self.game_tz)
        return normalized or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    def _page_indicates_no_lines(self) -> bool:
        try:
            page_l = (self.driver.page_source or "").lower()
            markers = (
                "no games available",
                "no lines available",
                "no contests available",
                "there are no",
                "currently no",
            )
            return any(marker in page_l for marker in markers)
        except Exception:
            return False

    def _player_lines_populated(self) -> bool:
        try:
            if self.driver.find_elements(By.CSS_SELECTOR, ".btnMLLine, .divMLLine"):
                team_spans = self.driver.find_elements(By.CSS_SELECTOR, "span.lblTeamName")
                rot_spans = self.driver.find_elements(By.CSS_SELECTOR, "span.lblRotation")
                has_team = any((s.text or "").strip() for s in team_spans)
                has_rot = any((s.text or "").strip() for s in rot_spans)
                if has_team and has_rot:
                    return True
            game_content = self.driver.find_elements(By.CSS_SELECTOR, "#divGameContent .divTeamContent")
            if len(game_content) >= 2:
                return True
        except Exception:
            pass
        return False

    def _wait_for_player_game_lines(self, timeout: int = 45) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._player_lines_populated():
                return True
            time.sleep(1)
        return self._player_lines_populated()

    def _extract_team_row(self, row) -> dict:
        rot = ""
        team = ""
        ml = None

        rot_elem = row.select_one("span.lblRotation")
        if rot_elem:
            rot = rot_elem.get_text(strip=True)

        team_elem = row.select_one("span.lblTeamName")
        if team_elem:
            team = team_elem.get_text(strip=True)

        ml_btn = row.select_one(".btnMLLine, .divMLLine")
        if ml_btn:
            ml = self._normalize_ml_odds(ml_btn.get_text(strip=True))

        if not ml:
            for odds_elem in row.select("span.odds, .odds, [class*='odds']"):
                txt = odds_elem.get_text(strip=True)
                if re.search(r"^[+-]?\d+$", txt) or txt.lower() == "even":
                    ml = self._normalize_ml_odds(txt)
                    break

        return {"rotation": rot, "team": team, "moneyline": ml}

    def _fetch_lines_via_getlines_api(self) -> list:
        """Call linesAJX.aspx/GetLines inside the authenticated browser session."""
        script = """
            const callback = arguments[arguments.length - 1];
            (async () => {
                try {
                    const sigEl = document.getElementById('_hiddenAppSignature');
                    const sig = sigEl ? sigEl.value : '';
                    const challenge = (typeof generateJsChallenge === 'function')
                        ? generateJsChallenge() : '';

                    let menuItems = [];
                    if (typeof menuItemsSelected !== 'undefined' && menuItemsSelected.length) {
                        menuItems = menuItemsSelected;
                    } else {
                        const active = document.querySelector(
                            '.sport-ssl-item.active, .sport-stl-item.active'
                        );
                        if (active) {
                            menuItems = [{
                                idSport: active.getAttribute('data-value'),
                                name: active.getAttribute('data-sportname') || '',
                                token: active.getAttribute('data-token') || ''
                            }];
                        }
                    }

                    if (!menuItems.length) {
                        callback({error: 'no menuItemsSelected'});
                        return;
                    }

                    const payload = {
                        menuItems: menuItems,
                        iscontest: false,
                        wagerTypeInfo: (typeof wagerTypeSelected !== 'undefined'
                            ? wagerTypeSelected : '1'),
                        isRefresh: false,
                        contestOrderBy: 0,
                        isContestRelated: false,
                        specialEvent: null,
                        getOnlyPeriods: false
                    };

                    const resp = await fetch(
                        '/Player/app/services/linesAJX.aspx/GetLines',
                        {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json; charset=utf-8',
                                'X-App-Signature': sig,
                                'X-Js-Challenge': challenge,
                                'X-Requested-With': 'XMLHttpRequest'
                            },
                            body: JSON.stringify(payload)
                        }
                    );
                    const body = await resp.json();
                    callback({ok: true, status: resp.status, body: body});
                } catch (e) {
                    callback({error: String(e)});
                }
            })();
        """
        try:
            result = self.driver.execute_async_script(script)
        except Exception as e:
            self.logger.warning(f"GetLines browser API call failed: {e}")
            return []

        if not result or result.get("error"):
            self.logger.warning(f"GetLines API unavailable: {result}")
            return []

        return self._parse_getlines_response(result.get("body") or {})

    def _parse_getlines_response(self, body: dict) -> list:
        raw = (body or {}).get("d") or ""
        if not raw:
            return []

        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError) as e:
            self.logger.warning(f"GetLines response JSON parse failed: {e}")
            return []

        events = []
        for grp in data.get("groups") or []:
            events.extend(grp.get("lines") or [])
        if not events:
            events = data.get("lines") or []

        games = []
        for ev in events:
            if ev.get("eventtype") != 2:
                continue
            sides = ev.get("sides") or []
            if len(sides) < 2:
                continue

            s1, s2 = sides[0], sides[1]
            rot1 = str(s1.get("rotation") or "").strip()
            rot2 = str(s2.get("rotation") or "").strip()
            team1 = (s1.get("name") or "").strip()
            team2 = (s2.get("name") or "").strip()
            ml1 = self._get_side_moneyline_odds(s1)
            ml2 = self._get_side_moneyline_odds(s2)

            if not rot1 or not rot2 or not team1 or not team2 or not ml1 or not ml2:
                continue

            normalized_dt = self._parse_betwar_game_datetime(ev.get("dateandtime"))

            games.append({
                "bookmaker": self.bookmaker,
                "sport": self.sport_name,
                "league": self.league,
                "game_id": f"{rot1}-{rot2}",
                "game_datetime": normalized_dt,
                "match": f"{team1} vs {team2}",
                "team_1": team1,
                "team_2": team2,
                "moneyline": {"team_1": ml1, "team_2": ml2},
                "spread": {
                    "team_1_spread": None, "team_2_spread": None,
                    "team_1_odds": None, "team_2_odds": None,
                },
                "total": {
                    "over_total": None, "under_total": None,
                    "over_odds": None, "under_odds": None,
                },
            })

        self.logger.info(f"Parsed {len(games)} games from GetLines API")
        return games

    def _parse_player_portal_dom(self, html: str) -> list:
        """Parse game lines from BetWar Player portal DOM (divGameTeam rows)."""
        soup = BeautifulSoup(html, "html.parser")
        games = []
        rows = []

        for row in soup.select("div.divTeamContent, div.divGameTeam"):
            parsed = self._extract_team_row(row)
            if parsed["team"] and parsed["rotation"]:
                rows.append(parsed)

        if not rows:
            for team_elem in soup.select("span.lblTeamName"):
                team = team_elem.get_text(strip=True)
                if not team:
                    continue
                parent = team_elem.find_parent("div", class_=re.compile(r"divGameTeam|game", re.I))
                if not parent:
                    parent = team_elem.find_parent("div")
                if parent:
                    parsed = self._extract_team_row(parent)
                    if parsed["team"] and parsed["rotation"]:
                        rows.append(parsed)

        i = 0
        while i < len(rows) - 1:
            r1, r2 = rows[i], rows[i + 1]
            if not r1["moneyline"] or not r2["moneyline"]:
                i += 1
                continue

            game_id = f"{r1['rotation']}-{r2['rotation']}"
            game_datetime_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            normalized_dt = parse_to_mysql_datetime(game_datetime_str, tz_name=self.game_tz) or game_datetime_str

            games.append({
                "bookmaker": self.bookmaker,
                "sport": self.sport_name,
                "league": self.league,
                "game_id": game_id,
                "game_datetime": normalized_dt,
                "match": f"{r1['team']} vs {r2['team']}",
                "team_1": r1["team"],
                "team_2": r2["team"],
                "moneyline": {"team_1": r1["moneyline"], "team_2": r2["moneyline"]},
                "spread": {
                    "team_1_spread": None, "team_2_spread": None,
                    "team_1_odds": None, "team_2_odds": None,
                },
                "total": {
                    "over_total": None, "under_total": None,
                    "over_odds": None, "under_odds": None,
                },
            })
            i += 2

        self.logger.info(f"Parsed {len(games)} games from Player portal DOM")
        return games

    def __ensure_sport_offering_loaded(self, game_num=None, team_no: int = None) -> bool:
        """Navigate Player portal sidebar to the active sport/league and wait for lines."""
        self.logger.info(f"Ensuring {self.sport_name} offering is loaded in Player portal...")

        self._ensure_player_portal()
        self._open_sports_sidebar()
        self._click_sidebar_sport()
        self._click_league_or_subleague()
        # Straights is usually already active; clicking the submit input resets the board.
        self._ensure_straights_tab()

        lines_ready = self._wait_for_player_game_lines(timeout=60)
        if lines_ready:
            self.logger.info(f"{self.sport_name} lines detected in Player portal DOM")
        elif self._page_indicates_no_lines():
            self.logger.warning(
                f"{self.sport_name} board loaded but bookmaker reports no available lines"
            )
        else:
            debug_file = debug_filepath(f"debug_betwar_{self.sport_name.lower()}_nav_fail")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.warning(
                f"{self.sport_name} lines not populated after navigation; saved {debug_file}"
            )

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
                    "team_1": self._normalize_ml_odds(ml1),
                    "team_2": self._normalize_ml_odds(ml2),
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

    def _ensure_odds_session(self):
        """Login for odds fetch only when the browser session is missing or invalid."""
        if self._force_wager_relogin:
            self.logger.info("Session flagged invalid; performing full login for odds fetch")
            self.__login()
            return

        if self._is_session_valid() and self._is_on_sport_page_with_games():
            self.logger.info(
                "Session valid on sport page with games loaded; skipping login for odds fetch"
            )
            return

        if self._is_session_valid():
            self.logger.info("Session valid for odds fetch; reloading sport offering only")
            self.__ensure_sport_offering_loaded()
            return

        self.logger.info("Session invalid for odds fetch; performing full login")
        self.__login()

    @time_it
    def fetch_odds(self, refresh_interval=10, quit_driver=True):
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self.logger.info(f"========== Fetching Odds ({self.sport_name}) via Selenium (START) ==========")
        prune_debug_files()

        try:
            self._ensure_odds_session()
            self.__ensure_sport_offering_loaded()

            odds_source = "unknown"
            games = self._fetch_lines_via_getlines_api()
            if games:
                odds_source = "GetLines API"
                self.logger.info(f"Using GetLines API: {len(games)} full-game lines")
            else:
                api_lines = self._fetch_game_lines_via_api()
                if api_lines:
                    games = self._parse_api_game_lines(api_lines)
                    odds_source = "GetSportOffering API"
                    self.logger.info(
                        f"Using GetSportOffering API: {len(games)} full-game lines"
                    )

            if not games:
                if not self._player_lines_populated():
                    self.logger.info("Waiting for Player portal game lines to populate...")
                    self._wait_for_player_game_lines(timeout=30)
                    self._wait_for_loading_hidden(timeout=15)
                    time.sleep(2)

                games = self._parse_player_portal_dom(self.driver.page_source)
                odds_source = "Player portal DOM"
                if not games:
                    self.logger.warning(
                        "Player portal scrape returned no games; "
                        "account may not offer this league or lines are still loading"
                    )

            if games:
                before = len(games)
                games = self._filter_games_with_valid_moneylines(games)
                if len(games) < before:
                    self.logger.warning(
                        f"Filtered {before - len(games)} games with invalid moneyline odds"
                    )

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
                    f"network_betwar_{self.sport_name.lower()}_{int(time.time())}.json",
                )
                with open(net_file, "w", encoding="utf-8") as f:
                    json.dump(network_requests, f, indent=2)
                self.logger.info(f"💾 Saved full network log: {net_file}")
            except Exception as e:
                self.logger.warning(f"Failed to capture performance logs: {e}")

            # Save debug HTML after waiting (always useful for diagnostics)
            debug_file = debug_filepath(f"debug_betwar_{self.sport_name.lower()}")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.info(f"💾 Saved debug HTML: {debug_file}")

            self.logger.info(
                f"Extracted {len(games)} {self.sport_name} matches via {odds_source}"
            )

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
            if quit_driver:
                self._quit_driver()
            self.logger.info(f"========= Fetching Odds ({self.sport_name}) via Selenium (END) ==========")

    def _find_player_moneyline_element(self, game_id: str, team_name: str, moneyline_odd):
        """Locate clickable moneyline odds in BetWar Player portal divGameTeam rows."""
        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        team_lower = team_name.lower()

        for row in self.driver.find_elements(By.CSS_SELECTOR, "div.divGameTeam"):
            row_text = (row.text or "").lower()
            if team_lower not in row_text:
                continue

            rot_spans = row.find_elements(By.CSS_SELECTOR, "span.lblRotation")
            rot_text = (rot_spans[0].text if rot_spans else "").strip()
            if rotations and rot_text and rot_text not in rotations:
                continue

            for odds_elem in row.find_elements(
                By.CSS_SELECTOR, "span.odds, .odds, [class*='odds']"
            ):
                txt = (odds_elem.text or "").strip()
                if self._odds_text_matches(txt, moneyline_odd):
                    return odds_elem

        return None

    def _find_moneyline_element(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd,
        game_line: dict = None,
        team_no: int = None,
    ):
        """Locate clickable moneyline button/span for team/odds on the loaded offering page."""
        player_elem = self._find_player_moneyline_element(game_id, team_name, moneyline_odd)
        if player_elem:
            return player_elem

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

    @staticmethod
    def _pick_looks_like_open_wager(pick) -> bool:
        if not isinstance(pick, dict):
            return False
        text = json.dumps(pick).lower()
        wager_markers = (
            "amount", "risk", "towin", "wageramount", "wageramt", "pickamount",
            "ticketnumber", "wagernumber", "confirmation", "pickid", "wagerstatus",
        )
        return any(marker in text for marker in wager_markers)

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
        for pick in self._fetch_open_wagers_via_api():
            if self._pick_matches_open_wager(pick, team_name, team_1, team_2):
                return True
        return False

    def _message_requires_relogin(self, message: str) -> bool:
        msg_l = (message or "").lower()
        return any(marker in msg_l for marker in self.WAGER_SESSION_EXPIRED_MARKERS)

    def _invalidate_wager_session(self):
        self._force_wager_relogin = True

    def _page_has_login_required_marker(self) -> bool:
        try:
            for elem in self.driver.find_elements(
                By.CSS_SELECTOR, "#div-betSlip, #betSlipDiv, .alert, .modal-body, .login-form"
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
            if "/player/" not in url:
                return False
            if self._login_form_visible():
                return False
            return bool(
                self.driver.find_elements(
                    By.CSS_SELECTOR, "#linkSports, #div-sportsSidebar, #div-betSlip"
                )
            )
        except Exception:
            return False

    def _sport_games_present(self) -> bool:
        return self._player_lines_populated()

    def _is_on_sport_page_with_games(self) -> bool:
        try:
            url = (self.driver.current_url or "").lower()
            if "/player/" not in url:
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
            for cb in self.driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
                cb_id = (cb.get_attribute("id") or "").lower()
                cb_class = (cb.get_attribute("class") or "").lower()
                if "accept" in cb_id or "accept" in cb_class:
                    if not cb.is_selected():
                        self.driver.execute_script("arguments[0].click();", cb)
                        accepted = True
        except Exception:
            pass

        for btn in self.driver.find_elements(
            By.CSS_SELECTOR, "#div-betSlip button, #div-betSlip a, #betSlipDiv button, #betSlipDiv a"
        ):
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

    def _confirm_bet_accepted(self, team_name: str, team_1: str, team_2: str, stake: float, timeout: int = 25):
        deadline = time.time() + timeout
        success_markers = (
            "wager accepted", "bet accepted", "ticket accepted",
            "successfully placed", "your wager has been accepted",
        )

        while time.time() < deadline:
            rejected, reject_msg = self._scan_rejection_ui()
            if rejected:
                if self._message_requires_relogin(reject_msg):
                    self._invalidate_wager_session()
                self.logger.error(f"Bet rejected by bookmaker UI: {reject_msg}")
                return False, reject_msg

            page_l = (self.driver.page_source or "").lower()
            for marker in success_markers:
                if marker in page_l:
                    return True, marker

            for entry in self._get_wager_network_log():
                body_l = (entry.get("body") or "").lower()
                url = entry.get("url") or ""
                if any(m in body_l for m in ("rejected", "declined", "error", "another user")):
                    self.logger.error(f"Wager API rejection ({url}): {entry.get('body', '')[:500]}")
                    return False, f"Wager API rejected: {url}"
                if any(m in body_l for m in ("accepted", "confirmed", "success", "ticket")):
                    self.logger.info(f"Wager API success ({url}): {entry.get('body', '')[:300]}")
                    return True, "Wager API confirmed"

            for pick in self._fetch_open_wagers_via_api():
                if self._pick_matches_open_wager(pick, team_name, team_1, team_2):
                    return True, "Open wager found via GetWagerPicks"

            time.sleep(1)

        self.logger.warning(
            f"Bet not confirmed within {timeout}s; final GetWagerPicks retries"
        )
        for attempt in range(1, 4):
            for pick in self._fetch_open_wagers_via_api():
                if self._pick_matches_open_wager(pick, team_name, team_1, team_2):
                    return True, "Open wager found via GetWagerPicks (retry)"
            if attempt < 3:
                time.sleep(5)
        return False, "Bet not confirmed by bookmaker"

    # --------------------------------------------------------
    # Execute Bet (adapted for BetWar /sports#/ SPA + betSlipDiv)
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
    ):
        try:
            self.logger.info(
                f"Placing Bet | Game ID: {game_id} | Team: {team_name} | Odds: {moneyline_odd} | Stake: {stake}"
            )

            self._refresh_session_before_wager()

            if not self.__ensure_sport_offering_loaded():
                raise Exception(f"{self.sport_name} lines not loaded before bet placement")

            moneyline_elem = self._find_moneyline_element(
                game_id, team_name, moneyline_odd
            )

            if not moneyline_elem:
                self.logger.warning(
                    f"Moneyline element not found on first pass for {team_name} @ {moneyline_odd}, re-navigating"
                )
                self.__ensure_sport_offering_loaded()
                moneyline_elem = self._find_moneyline_element(
                    game_id, team_name, moneyline_odd
                )

            if not moneyline_elem:
                raise Exception(
                    f"Moneyline element not found for {team_name} @ {moneyline_odd} (game_id={game_id})"
                )

            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", moneyline_elem
            )
            time.sleep(0.4)
            self.driver.execute_script("arguments[0].click();", moneyline_elem)
            self.logger.info("Player portal moneyline odds clicked")

            if not self._wait_for_betslip_team(team_name, timeout=8):
                slip_preview = self._betslip_text()[:200]
                raise Exception(
                    f"Bet slip still empty after click for {team_name} (game_id={game_id}): {slip_preview}"
                )

            limits_text = self._betslip_text()
            self.logger.info(f"Bet slip populated: {limits_text[:200]}")

            # ENTER STAKE - look for common risk/stake inputs (TicoSports style often uses input[id^=risk_] or ng-model risk)
            stake_input = None
            for selector in [
                "input[id^='risk_']",
                "input[name*='risk']",
                "input[ng-model*='risk']",
                "input[ng-model*='stake']",
                "#div-betSlip input[type='text']",
                "#div-betSlip input[type='number']",
                "#betSlipDiv input[type='text']",
                "#betSlipDiv input[type='number']",
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
                for inp in self.driver.find_elements(
                    By.CSS_SELECTOR, "#div-betSlip input, #betSlipDiv input"
                ):
                    if inp.is_displayed():
                        stake_input = inp
                        break

            if stake_input:
                stake_input.clear()
                stake_input.send_keys(f"{stake:.2f}")
                self.logger.info(f"Stake entered: {stake:.2f}")
            else:
                self.logger.warning("Could not locate stake input - bet may use default")

            self._accept_line_changes()

            # Click Place Bet (ng-click=ProcessTicket() or text)
            place_btn = None
            for b in self.driver.find_elements(
                By.CSS_SELECTOR,
                "#div-betSlip button, #div-betSlip a, #btnBetSlipCart, #betSlipDiv button, #betSlipDiv a",
            ):
                btn_text = (b.text or "").lower()
                ng_click = (b.get_attribute("ng-click") or "").lower()
                if "place bet" in btn_text or "place wager" in btn_text or "process" in ng_click:
                    place_btn = b
                    break

            if not place_btn:
                raise Exception("Place Bet button not found")

            if self._page_has_login_required_marker():
                self._invalidate_wager_session()
                raise Exception("Rejection marker on page: please log in")

            self._install_wager_network_hook()
            self._accept_line_changes()
            self.driver.execute_script("arguments[0].click();", place_btn)
            self.logger.info("Place Bet clicked")
            network_log = self._get_wager_network_log()
            if network_log:
                self.logger.info(f"Wager network activity after click: {network_log[-3:]}")
            else:
                self.logger.warning("No wager network activity detected immediately after Place Bet click")

            matchup_1 = team_1 or ""
            matchup_2 = team_2 or ""
            if not matchup_1 or not matchup_2:
                for game in self._parse_player_portal_dom(self.driver.page_source):
                    if game.get("game_id") == game_id:
                        matchup_1 = game.get("team_1") or matchup_1
                        matchup_2 = game.get("team_2") or matchup_2
                        break
            confirmed, message = self._confirm_bet_accepted(team_name, matchup_1, matchup_2, stake)
            if not confirmed:
                raise Exception(message or "Bet not accepted by bookmaker")

            self.logger.info(f"Bet accepted by bookmaker: {message}")
            return True, stake

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
        self.__execute_bet(game_id, team_name, moneyline_odd, stake)

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

            if "/player/" not in (current_url or "").lower():
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
                        f"skipping duplicate placement (arb scan lock requires bookmaker confirmation)"
                    )
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


# Quick self-test entrypoint (credentials from .env)
def main():
    from database.models.Accounts import Accounts
    from utils.config import BETWAR, BETWAR_ACCOUNT, BETWAR_PASSWORD, BETWAR_LABEL

    if not BETWAR_ACCOUNT or not BETWAR_PASSWORD:
        raise ValueError("BETWAR_ACCOUNT and BETWAR_PASSWORD must be set in .env")

    account = Accounts(
        account=BETWAR_ACCOUNT,
        password=BETWAR_PASSWORD,
        label=BETWAR_LABEL,
    )
    controller = BetWarController(account, BETWAR, sport="baseball")
    # Only fetch odds for testing (betting requires live arb cache)
    controller.fetch_odds()


if __name__ == "__main__":
    main()
