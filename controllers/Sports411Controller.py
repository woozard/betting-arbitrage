import time
import json
import asyncio
import re
import tempfile
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import sqlalchemy.exc   # ← NEW: for explicit table-missing error handling

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

class Sports411Controller:
    # ===================================================================
    # Multi-sport support (NBA + MLB) + remove duplicate sport override
    # ===================================================================
    def __init__(self, account, site, sport="basketball"):

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

        # === Multi-sport configuration (exactly like Web5Controller) ===
        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_url = f"https://be.{self.website}/en/sports/basketball/nba/game-lines/"
            self.sport_name = "NBA"
            self.league = "NBA"
        elif self.sport in ["baseball", "mlb"]:
            self.sport_url = f"https://be.{self.website}/en/sports/baseball/mlb/game-lines/"
            self.sport_name = "MLB"
            self.league = "MLB"
        else:
            raise ValueError(f"Unsupported sport: {sport}. Use 'basketball'/'nba' or 'baseball'/'mlb'.")

        # Timezone for game times returned by this book's page.
        # All game_datetimes are normalized to UTC via pytz for consistent matching
        # across bookmakers that may display times in ET vs PT etc.
        self.game_tz = 'US/Pacific'

        # Set URLs
        self.base_url = f"https://www.{self.website}"
        self.login_url = f"{self.base_url}/"
        self.dashboard_url = f"https://be.{self.website}/en/sports/"
        self.basketball_url = self.sport_url
        if self.sport in ["basketball", "nba"]:
            self.game_lines_path = "/basketball/nba/game-lines"
        else:
            self.game_lines_path = "/baseball/mlb/game-lines"

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
        # === BrightData Proxy Extension (same as Web5) ===
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
            self.logger.info(f"Navigating to {self.sport_name} page after recovery: {self.sport_url}")
            self.driver.get(self.sport_url)
            time.sleep(5)
            return True
        except Exception as e:
            self.logger.error(f"Re-login and navigation after driver recovery failed: {e}")
            return False

    # === helper methods from Web5Controller ===
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

        self.logger.info(f"✅ BrightData proxy extension created at: {ext_dir}")
        return ext_dir

    def _zenrows_get(self, url: str, js_render: bool = True, wait: int = 20000):
        """Zenrows helper – same as Web5Controller"""
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
                self.logger.info(f"✅ Zenrows request successful for {url}")
                return resp.text
            except Exception as e:
                self.logger.error(f"Zenrows request failed (attempt {attempt + 1}): {e}")
                if attempt == 2:
                    raise
                time.sleep(5)
        raise Exception("Zenrows failed after 3 attempts")

    def _safe_send_monitoring_alert(self, ex):
        """Safe version - does NOT crash if token is missing (same as Web5)"""
        try:
            if TELEGRAM.get('bot_token'):
                asyncio.run(
                    send_monitoring_alert(self.website, self.account_id, ex, TELEGRAM.get('arbitrage_monitoring')))
            else:
                self.logger.warning("TELEGRAM bot_token missing - skipping alert")
        except Exception as alert_err:
            self.logger.error(f"Failed to send monitoring alert: {alert_err}")

    # --------------------------------------------------------
    # Login
    # --------------------------------------------------------
    # Improved login with debug HTML dump + longer waits
    def __login(self):
        try:
            self.logger.info(f"Account: {self.account_id}")
            self.logger.info(f"Label: {self.label}")

            self.logger.info("Opening Login Page")
            self.driver.get(self.login_url)
            self._wait_for_login_page_or_sports()

            login_debug = debug_filepath("debug_login_sports411")
            with open(login_debug, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.info(f"💾 Saved {login_debug}")

            if self._is_already_logged_in():
                self.logger.info("Already logged in; skipping credential entry")
                return True

            # Hard block detection
            page_source_lower = self.driver.page_source.lower()
            if "sorry, you have been blocked" in page_source_lower or "attention required" in page_source_lower:
                self.logger.error("❌ HARD CLOUDFLARE BLOCK DETECTED – SWITCHING TO ZENROWS")
                self.logger.info("🔄 Using Zenrows for login...")
                html = self._zenrows_get(self.login_url)
                self.logger.info("✅ Zenrows login page retrieved successfully")
                # TODO: Full Zenrows login form submission can be added later if needed
                return True

            # Normal Selenium login (updated selectors may be needed)
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

            login_btn = self.driver.find_element(
                By.CSS_SELECTOR, "input[type='submit'].login"
            )
            login_btn.click()

            self.wait.until(EC.url_contains("/en/sports/"))
            self.logger.info("Login Successful")
            return True

        except Exception as e:
            self.logger.error(f"Login Failed: {e}")
            with open(debug_filepath("debug_login_sports411_FAIL"), "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self._safe_send_monitoring_alert(e)  # <-- safe version
            raise


    def __inject_mutation_observer(self):

        self.logger.info(f"Injecting Mutation Observer (JS)")
        script = """
        if (!window.oddsObserverInstalled) {
            window.oddsObserverInstalled = true;
            window.oddsBuffer = [];
            
            const target = document.querySelector('app-american-schedule');
            
            if (!target) {
                console.log("Observer: target not found");
                return;
            }

            const observer = new MutationObserver((mutations) => {
                // Push updated HTML snapshot
                window.oddsBuffer.push(target.innerHTML);
            });

            observer.observe(target, {
                childList: true,
                subtree: true,
                characterData: true
            });

            console.log("MutationObserver installed");
        }
        """
        self.driver.execute_script(script)

    # --------------------------------------------------------
    # Game datetime extraction (critical for cross-book matching on game_datetime)
    # --------------------------------------------------------
    def _extract_game_datetime(self, game_soup):
        """Best-effort extraction of scheduled game start time from the sports-league-game element.
        Returns string in %Y-%m-%d %H:%M:%S using today's date + found time, or None.
        """
        candidates = []
        # Try common time-related selectors that appear in betting schedule UIs
        for selector in [
            ".game-time", ".time", "time", ".match-time", ".game-start",
            "[data-time]", "[class*='time']", "[class*='start']",
            ".game-info", ".header", "span.time", ".game-header"
        ]:
            try:
                els = game_soup.select(selector) or []
                for el in els:
                    t = ""
                    try:
                        t = (el.get_text(" ", strip=True) or "").strip()
                    except Exception:
                        pass
                    if not t:
                        t = (el.get("data-time") or el.get("title") or el.get("data-start") or "").strip()
                    if t:
                        candidates.append(t)
            except Exception:
                pass

        # Also scan the full text of this game block (most reliable fallback)
        try:
            full_text = game_soup.get_text(" ", strip=True)
            if full_text:
                candidates.append(full_text)
        except Exception:
            pass

        # Search for time patterns in order of preference (with am/pm first)
        time_patterns = [
            r'(\d{1,2}:\d{2}(?::\d{2})?\s*[APap][Mm])',  # 7:10 PM, 19:10 pm
            r'(\d{1,2}:\d{2}(?::\d{2})?)',                # 19:10 or 7:10
        ]
        for cand in candidates:
            for pat in time_patterns:
                m = re.search(pat, cand)
                if m:
                    time_part = m.group(1)
                    return self._combine_date_with_time(time_part)

        return None

    def _combine_date_with_time(self, time_str: str) -> str:
        """ '7:10 PM' or '19:10' or '19:10:00' -> '2026-06-01 19:10:00' (today's date)
        The resulting string is passed through parse_to_mysql_datetime with this book's
        game_tz so that it gets localized and converted to UTC. This ensures game_datetime
        strings are consistent for cross-book matching regardless of what TZ each bookmaker
        uses to display times.
        """
        try:
            time_str = time_str.strip()
            is_pm = bool(re.search(r'pm', time_str, re.I))
            is_am = bool(re.search(r'am', time_str, re.I))
            clean = re.sub(r'\s*[APap][Mm]', '', time_str).strip()
            tparts = clean.split(':')
            hour = int(tparts[0])
            minute = int(tparts[1]) if len(tparts) > 1 else 0
            if is_pm and hour != 12:
                hour += 12
            elif is_am and hour == 12:
                hour = 0
            tz = pytz.timezone(self.game_tz)
            today = datetime.now(tz).date()
            dt = datetime(today.year, today.month, today.day, hour, minute, 0)
            local_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            # Normalize using this book's TZ (set in __init__) to UTC
            return parse_to_mysql_datetime(local_str, tz_name=self.game_tz)
        except Exception:
            # fallback: normalize server now() as if in game tz (rare case)
            local_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return parse_to_mysql_datetime(local_str, tz_name=self.game_tz)

    def _zenrows_get(self, url: str, js_render: bool = True, wait: int = 15000):
        """Zenrows helper – fast and reliable for odds fetching"""
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
                self.logger.info(f"✅ Zenrows fetched {url} successfully")
                return resp.text
            except Exception as e:
                self.logger.error(f"Zenrows attempt {attempt + 1} failed: {e}")
                if attempt == 2:
                    raise
                time.sleep(5)
        raise Exception("Zenrows failed after 3 attempts")

    # ===================================================================
    # ZenRows fetch_odds
    # ===================================================================
    @time_it
    def fetch_odds(self, refresh_interval=10):
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self.logger.info(f"========== Fetching Odds ({self.sport_name}) via Selenium (START) ==========")
        prune_debug_files()

        try:
            # Use the existing authenticated Selenium driver (with BrightData proxy)
            self.__login()

            self.logger.info(f"Navigating to {self.sport_url}")
            self.driver.get(self.sport_url)

            # Wait for the main schedule component
            try:
                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "app-american-schedule")))
            except Exception:
                self.logger.warning("Schedule component not found quickly.")

            # Wait for the loading spinner to disappear (up to 25 seconds)
            try:
                spinner_locator = (By.CSS_SELECTOR, "div.component-loader, .fa-spinner-third")
                WebDriverWait(self.driver, 25).until(
                    EC.invisibility_of_element_located(spinner_locator)
                )
                self.logger.info("Loading spinner disappeared.")
            except Exception:
                self.logger.warning("Spinner did not disappear within 25s timeout.")

            # Additional patient wait for actual game data to populate (up to ~20s)
            self.logger.info("Waiting for game data to load into the DOM...")
            game_content_found = False
            for _ in range(20):
                time.sleep(1)
                page_text = self.driver.page_source
                # Look for rotation numbers + team names (very common pattern in this book)
                if re.search(r'\b[0-9]{3,4}\s+[A-Z][A-Za-z]', page_text):
                    game_content_found = True
                    break

            if not game_content_found:
                self.logger.warning("Game content may still be loading after waiting.")

            # Save debug HTML after waiting attempts
            debug_file = debug_filepath(f"debug_sports411_{self.sport_name.lower()}")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.info(f"💾 Saved debug HTML: {debug_file}")

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

                    def extract_team_odds(label):
                        title = (label.get("title") or label.text or "").strip()
                        match = re.match(r"^(.+?)\s+([+-]?\d+)", title)
                        if match:
                            return match.group(1).strip(), match.group(2).strip()
                        text = label.text.strip()
                        match = re.match(r"^(.+?)\s+([+-]?\d+)", text)
                        if match:
                            return match.group(1).strip(), match.group(2).strip()
                        return None, None

                    team_1, team_1_ml = extract_team_odds(mline1)
                    team_2, team_2_ml = extract_team_odds(mline2)
                    if not team_1 or not team_2 or not team_1_ml or not team_2_ml:
                        continue

                    game_datetime_str = self._extract_game_datetime(game)
                    if not game_datetime_str:
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
                        "moneyline": {"team_1": team_1_ml, "team_2": team_2_ml},
                        "spread": {"team_1_spread": None, "team_2_spread": None, "team_1_odds": None,
                                   "team_2_odds": None},
                        "total": {"over_total": None, "under_total": None, "over_odds": None, "under_odds": None}
                    })
                except Exception as e:
                    self.logger.error(f"Error parsing game: {e}", exc_info=True)
                    continue

            self.logger.info(f"Extracted {len(games)} {self.sport_name} matches via Selenium")

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
                        self.logger.warning("⚠️ Table 'arbitrage_odds' issue - continuing")
                    else:
                        self.logger.warning(f"DB save failed: {db_err}")

        except Exception as e:
            self.logger.error(f"Selenium fetch_odds failed: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            self._quit_driver()
            self.logger.info(f"========= Fetching Odds ({self.sport_name}) via Selenium (END) ==========")
    # END CHANGE

    def _wait_for_login_page_or_sports(self, timeout=15):
        def ready(driver):
            url = (driver.current_url or "").lower()
            if f"be.{self.website}" in url and "/en/sports/" in url:
                return True
            account_fields = driver.find_elements(By.ID, "account")
            return bool(account_fields) and account_fields[0].is_displayed()

        WebDriverWait(self.driver, timeout).until(ready)

    def _is_already_logged_in(self) -> bool:
        try:
            url = (self.driver.current_url or "").lower()
            if f"be.{self.website}" not in url or "/en/sports/" not in url:
                return False
            account_fields = self.driver.find_elements(By.ID, "account")
            return not account_fields or not account_fields[0].is_displayed()
        except Exception:
            return False

    def _sport_games_present(self) -> bool:
        try:
            return bool(
                self.driver.find_elements(By.CSS_SELECTOR, "div.sports-league-game")
            )
        except Exception:
            return False

    def _wait_for_sport_games_loaded(self, timeout=15):
        WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.sports-league-game"))
        )

    def _is_session_valid(self) -> bool:
        try:
            url = (self.driver.current_url or "").lower()
            if f"be.{self.website}" not in url:
                return False
            if "/en/sports/" not in url and "/en/account/" not in url:
                return False
            account_fields = self.driver.find_elements(By.ID, "account")
            return not account_fields or not account_fields[0].is_displayed()
        except Exception:
            return False

    def _is_on_sport_page_with_games(self) -> bool:
        try:
            if self.game_lines_path not in (self.driver.current_url or ""):
                return False
            return self._sport_games_present()
        except Exception:
            return False

    def _return_to_sport_page(self):
        try:
            if (
                self.game_lines_path in (self.driver.current_url or "")
                and self._sport_games_present()
            ):
                return
            self.driver.get(self.sport_url)
            self._wait_for_sport_games_loaded()
        except Exception as e:
            self.logger.warning(f"Could not return to {self.sport_name} page: {e}")

    def _is_off_sport_page(self, url: str) -> bool:
        return self.game_lines_path not in (url or "")

    def _should_soft_navigate_back(self, url: str) -> bool:
        url_l = (url or "").lower()
        return any(
            marker in url_l
            for marker in ("/account/pending", "/account/", "/logout", "/en/sports/")
        ) and self.game_lines_path not in url_l

    def _has_existing_open_bet(self, team_name: str, team_1: str, team_2: str) -> bool:
        try:
            pending_url = f"https://be.{self.website}/en/account/pending"
            self.driver.get(pending_url)
            time.sleep(2)
            page = self.driver.page_source.lower()
            team_l = team_name.lower()
            teams_present = (
                team_l in page
                and team_1.lower() in page
                and team_2.lower() in page
            )
            if not teams_present:
                return False
            wager_context = any(
                marker in page
                for marker in (
                    "risk:", "to win:", "pending wager", "open bet",
                    "wager #", "ticket #", "straight bet",
                )
            )
            return wager_context
        except Exception as e:
            self.logger.warning(f"Could not check existing open bets: {e}")
            return False
        finally:
            self._return_to_sport_page()

    def _refresh_session_before_wager(self):
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
        self._return_to_sport_page()

    def _install_wager_network_hook(self):
        self.driver.execute_script("""
            window.__wagerResponses = [];
            if (window.__wagerHookInstalled) return;
            window.__wagerHookInstalled = true;
            const capture = (url, body) => {
                if (!url) return;
                const u = String(url).toLowerCase();
                if (u.includes('wager') || u.includes('bet') || u.includes('ticket')
                    || u.includes('place') || u.includes('pending')) {
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
            "rejected", "not accepted", "line changed", "line has changed",
            "odds changed", "limit exceeded", "maximum bet", "minimum bet",
            "insufficient funds", "insufficient balance", "session expired",
            "logged out", "please log in", "unable to place", "wager declined",
        )
        try:
            for overlay in self.driver.find_elements(By.CSS_SELECTOR, ".alert-overlay"):
                try:
                    message = overlay.find_element(By.CSS_SELECTOR, ".alert-message").text.strip()
                except Exception:
                    message = (overlay.text or "").strip()
                if not message:
                    continue
                classes = ""
                try:
                    classes = overlay.find_element(By.CSS_SELECTOR, ".AlertComponent").get_attribute("class") or ""
                except Exception:
                    pass
                msg_l = message.lower()
                if "confirm-alert" not in classes or any(m in msg_l for m in reject_markers):
                    try:
                        ok_btn = overlay.find_element(By.CSS_SELECTOR, "button.okBtn")
                        self.driver.execute_script("arguments[0].click();", ok_btn)
                    except Exception:
                        pass
                    return True, message
        except Exception:
            pass

        try:
            page_l = (self.driver.page_source or "").lower()
            for marker in reject_markers:
                if marker in page_l:
                    return True, f"Rejection marker on page: {marker}"
        except Exception:
            pass
        return False, ""

    def _accept_line_changes(self):
        accepted = False
        try:
            accept_all = self.driver.find_element(By.ID, "accept_all")
            if not accept_all.is_selected():
                self.driver.execute_script("arguments[0].click();", accept_all)
                accepted = True
        except Exception:
            pass

        for selector in (
            "input[id*='accept']",
            "label[for='accept_all']",
            ".accept-changes input[type='checkbox']",
        ):
            try:
                for elem in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if elem.tag_name.lower() == "input" and not elem.is_selected():
                        self.driver.execute_script("arguments[0].click();", elem)
                        accepted = True
            except Exception:
                continue

        for btn in self.driver.find_elements(By.CSS_SELECTOR, "button, a"):
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

    def _verify_open_bet_on_pending(self, team_name: str, stake: float):
        try:
            pending_url = f"https://be.{self.website}/en/account/pending"
            self.driver.get(pending_url)
            time.sleep(3)
            page = self.driver.page_source.lower()
            team_l = team_name.lower()
            if team_l not in page:
                return False, "Bet not found on pending page"
            stake_hits = (
                f"{stake:.2f}" in page
                or f"${stake:.0f}" in page
                or f"risk: ${stake:.2f}" in page
            )
            if stake_hits:
                return True, "Open bet found on pending page"
            return False, "Team found on pending page but stake not verified"
        except Exception as e:
            return False, f"Could not verify open bet: {e}"
        finally:
            self._return_to_sport_page()

    def _confirm_bet_accepted(self, team_name: str, stake: float, timeout: int = 25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rejected, reject_msg = self._scan_rejection_ui()
            if rejected:
                self.logger.error(f"Bet rejected by bookmaker UI: {reject_msg}")
                return False, reject_msg or "Bet rejected"

            for overlay in self.driver.find_elements(By.CSS_SELECTOR, ".alert-overlay"):
                try:
                    alert_message = overlay.find_element(By.CSS_SELECTOR, ".alert-message").text.strip()
                    alert_classes = overlay.find_element(
                        By.CSS_SELECTOR, ".AlertComponent"
                    ).get_attribute("class") or ""
                    try:
                        ok_btn = overlay.find_element(By.CSS_SELECTOR, "button.okBtn")
                        self.driver.execute_script("arguments[0].click();", ok_btn)
                    except Exception:
                        pass
                    if "confirm-alert" in alert_classes:
                        self.logger.info(f"Bet confirmed by alert: {alert_message}")
                        return True, alert_message or "Bet confirmed"
                    self.logger.error(f"Bet rejected by bookmaker alert: {alert_message}")
                    return False, alert_message or "Bet rejected"
                except Exception:
                    continue

            for entry in self._get_wager_network_log():
                body_l = (entry.get("body") or "").lower()
                url = entry.get("url") or ""
                if any(m in body_l for m in ("rejected", "declined", "error", "failed")):
                    self.logger.error(f"Wager API rejection ({url}): {entry.get('body', '')[:500]}")
                    return False, f"Wager API rejected: {url}"
                if any(m in body_l for m in ("accepted", "confirmed", "success", "ticket")):
                    self.logger.info(f"Wager API success ({url}): {entry.get('body', '')[:300]}")
                    return True, "Wager API confirmed"

            time.sleep(1)

        self.logger.warning(
            f"No confirmation alert within {timeout}s; checking pending wagers with retries"
        )
        for attempt in range(1, 4):
            confirmed, message = self._verify_open_bet_on_pending(team_name, stake)
            if confirmed:
                return True, message
            if attempt < 3:
                self.logger.warning(f"Pending check attempt {attempt}/3 failed; retrying in 5s")
                time.sleep(5)
        return False, message

    # Execute Bet
    # --------------------------------------------------------
    def __execute_bet(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd: str,
        stake: float = 1.0
    ):
        self.logger.info("========== Execute Bet (START) ==========")

        try:

            self.logger.info(
                f"Placing Bet | Game ID: {game_id} | Team: {team_name} | Odds: {moneyline_odd} | Stake: {stake}"
            )

            self._refresh_session_before_wager()

            if self._is_off_sport_page(self.driver.current_url):
                self.logger.info(f"Navigating to {self.sport_name} page before bet placement")
                self._return_to_sport_page()

            # -----------------------------------
            # FIND GAME CONTAINER
            # -----------------------------------
            game_container = None
            try:
                game_container = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, f"div.sports-league-game[idgame='{game_id}']")
                    )
                )
            except TimeoutException:
                self.logger.warning(
                    f"Game container not found for {game_id}, reloading {self.sport_name} page and retrying once"
                )
                self._return_to_sport_page()
                try:
                    game_container = self.wait.until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, f"div.sports-league-game[idgame='{game_id}']")
                        )
                    )
                except TimeoutException:
                    raise  # will be caught by outer except, send to monitoring only

            # -----------------------------------
            # FIND MONEYLINE LABEL (TEAM + ODDS)
            # -----------------------------------
            moneyline_label = None

            labels = game_container.find_elements(
                By.CSS_SELECTOR,
                ".mline-1 label.bet-indicator, .mline-2 label.bet-indicator"
            )

            for idx, label in enumerate(labels):
                title = (label.get_attribute("title") or "").strip()

                # -----------------------------
                # TEAM NAME FROM TITLE
                # -----------------------------
                team = ""
                odds_text = ""

                if title:
                    # Example: "Philadelphia 76ers -106"
                    match = re.match(r"(.+?)\s([+-]\d+)", title)
                    if match:
                        team = match.group(1).strip()
                        odds_text = match.group(2).strip()

                # FALLBACK: read odds from DOM if needed
                if not odds_text:
                    try:
                        odds_text = label.find_element(By.CSS_SELECTOR, ".odds span").text.strip()
                    except Exception:
                        odds_text = ""

                # LOG
                self.logger.info(
                    f"Moneyline [{idx}] | Team: '{team}' | Odds: '{odds_text}' | Title: '{title}'"
                )

                # MATCH TEAM + ODDS
                if team.lower() == team_name.lower() and int(odds_text) == int(moneyline_odd):
                    moneyline_label = label
                    self.logger.info(
                        f"Matched Moneyline | Index: {idx} | Team: {team} | Odds: {odds_text}"
                    )
                    break


            if not moneyline_label:
                raise Exception("Moneyline label not found for given team & odds")

            self.logger.info(f"Moneyline Label: {moneyline_label}")

            # -----------------------------------
            # CLICK MONEYLINE (ANGULAR SAFE)
            # -----------------------------------
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", moneyline_label
            )
            time.sleep(0.4)
            self.driver.execute_script("arguments[0].click();", moneyline_label)

            self.logger.info("Moneyline label clicked")

            # -----------------------------------
            # WAIT FOR BETSLIP
            # -----------------------------------
            self.wait.until(EC.presence_of_element_located((By.ID, "betslip")))

            # -----------------------------------
            # VERIFY BETSLIP TEAM (SAFETY)
            # -----------------------------------
            betslip_team = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#betslip .team-name"))
            ).text.strip()

            if betslip_team.lower() != team_name.lower():
                raise Exception(
                    f"Betslip team mismatch | Expected: {team_name} | Found: {betslip_team}"
                )

            self.logger.info(f"Betslip verified | Team: {betslip_team}")

            # -----------------------------------
            # READ MIN / MAX BET LIMITS
            # -----------------------------------
            try:
                bet_limits = self.wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#betslip .bet-limits"))
                )

                amounts = bet_limits.find_elements(By.CSS_SELECTOR, "span.amount")
                min_bet = currency_to_float(amounts[0].text.strip()) if len(amounts) > 0 else "N/A"
                max_bet = currency_to_float(amounts[1].text.strip()) if len(amounts) > 1 else "N/A"

                self.logger.info(
                    f"Bet Limits | Min Bet: {min_bet} | Max Bet: {max_bet} | Stake: {stake}"
                )

                # Validate that the stake is within limits
                if stake < min_bet:
                    raise Exception(
                        f"Stake {stake} is below minimum bet {min_bet}"
                    )

                if max_bet > 0 and stake > max_bet:
                    raise Exception(
                        f"Stake {stake} exceeds maximum bet {max_bet}"
                    )

            except Exception as e:
                self.logger.warning(f"Bet limits could not be determined: {e}")

            # -----------------------------------
            # ENTER STAKE
            # -----------------------------------
            stake_input = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[id^='risk_']"))
            )

            stake_input.clear()
            stake_input.send_keys(f"{stake:.2f}")
            self.logger.info(f"Stake entered: {stake:.2f}")

            self._accept_line_changes()

            # -----------------------------------
            # WAIT UNTIL PLACE BET ENABLED
            # -----------------------------------
            place_bet_btn = self.wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, ".place-bet-container button.btn-primary:not([disabled])")
                )
            )

            self._install_wager_network_hook()
            self._accept_line_changes()
            self.driver.execute_script("arguments[0].click();", place_bet_btn)
            self.logger.info("Place Bet button clicked")
            network_log = self._get_wager_network_log()
            if network_log:
                self.logger.info(f"Wager network activity after click: {network_log[-3:]}")
            else:
                self.logger.warning("No wager network activity detected immediately after Place Bet click")

            confirmed, message = self._confirm_bet_accepted(team_name, stake)
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

        # Logger & Storage
        self.logger = Logger.get_logger(f"{self.bookmaker}-bet")
        self.storage = Storage(self.logger)

        self.logger.info("==================== Place Bet (START) ====================")

        # Step 1: Login
        self.__login()

        # Step 2: Go to basketball page
        self.logger.info(f"Navigating to NBA page: {self.basketball_url}")
        self.driver.get(self.basketball_url)
        time.sleep(2)  # Wait for initial load

        # Step 3: Place Bet
        self.__execute_bet(game_id, team_name, moneyline_odd, stake)            
            
        self.logger.info("==================== Place Bet (END) ====================")
    
    # --------------------------------------------------------
    # Betting
    # --------------------------------------------------------
    def betting(
        self,
        stake: float = 1.0
    ):
        # Logger & Storage
        self.logger = Logger.get_logger(f"{self.bookmaker}-betting-{self.sport_name.lower()}")
        self.storage = Storage(self.logger)

        self.logger.info(f"==================== Betting ({self.sport_name}) (START) ====================")

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

                # Step 2: Go to sport-specific game-lines page
                self.logger.info(f"Navigating to {self.sport_name} page: {self.sport_url}")
                self.driver.get(self.sport_url)
                time.sleep(2)  # Wait for initial load

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

            # Ensure still on the sport-specific game-lines page.
            # The site often redirects to the parent /sports page (be.sports411.ag/) or www root
            # after login, idle periods, or page interactions. Use a tolerant path check instead of
            # exact startswith on the long URL.
            if self._is_off_sport_page(current_url):
                if self._should_soft_navigate_back(current_url):
                    self.logger.warning(
                        f"Off {self.sport_name} page ({current_url}); navigating back without driver reset"
                    )
                    self._return_to_sport_page()
                    continue
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
            matching_arbs = [
                arb for arb in arbs
                if arb.get("sport") == self.sport_name and arb.get("league") == self.league
            ]
            if not matching_arbs:
                self.logger.info("Waiting for Arbitrage")
                continue

            self.logger.info(f"Arbitrage: {len(matching_arbs)}")

            for arb in matching_arbs:

                sport = arb.get('sport')
                league = arb.get('league')
                game_date = arb.get('game_date')
                game_datetime = arb.get('game_datetime')
                bet_type = arb.get('bet_type')
                team_1 = arb.get("team_1")
                team_2 = arb.get("team_2")

                if not is_game_pregame(game_datetime):
                    self.logger.info(
                        f"Skipping arb (game started) | Match: {team_1} vs {team_2}"
                    )
                    continue

                self.logger.info(
                    f"Arbitrage | Match: {team_1} vs {team_2}"
                )
                
                if arb.get("team_1_bookmaker") == self.bookmaker:
                    team_no = 1
                    game_id = arb.get("team_1_game_id")
                    team_name = team_1
                    moneyline_odd = arb.get("team_1_odds")
                    attempts = arb.get("team_1_bet_placed_attempts", 0) + 1
                elif arb.get("team_2_bookmaker") == self.bookmaker:
                    team_no = 2
                    game_id = arb.get("team_2_game_id")
                    team_name = team_2
                    moneyline_odd = arb.get("team_2_odds")
                    attempts = arb.get("team_2_bet_placed_attempts", 0) + 1
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
                        f"Open wager detected on pending page for {team_name} on {self.bookmaker}; "
                        f"skipping duplicate placement (arb scan lock requires bookmaker confirmation)"
                    )
                    continue

                bet_placed, stake = self.__execute_bet(game_id, team_name, moneyline_odd, stake)
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
                    self.logger.info("Returning to sport page before next arbitrage")
                    self.driver.get(self.sport_url)
                    time.sleep(2)

        # The main arbitrage/betting loop above runs until the process is terminated.
        # Explicit returns in the setup phase or unrecoverable errors will end here.
        self.logger.info("==================== Betting (END) ====================")


# Quick self-test entrypoint (matches sports411_odds.py behavior)
def main():
    from database.models.Accounts import Accounts
    from utils.config import SPORTS411

    account = Accounts(
        account = '8715',
        password = 'eqr0mjx-MXY*rcn1ana',
        label = 'Reader'
    )

    # === FETCH BOTH NBA AND MLB MONEYLINE ===
    print("=== Fetching NBA Moneyline ===")
    controller_nba = Sports411Controller(account, SPORTS411, sport="basketball")
    controller_nba.fetch_odds()

    print("\n=== Fetching MLB Moneyline ===")
    controller_mlb = Sports411Controller(account, SPORTS411, sport="baseball")
    controller_mlb.fetch_odds()

    print("\n✅ Finished fetching NBA + MLB moneyline odds")


if __name__ == "__main__":
    main()




