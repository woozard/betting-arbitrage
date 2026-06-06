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
import sqlalchemy.exc   # ← NEW: for explicit table-missing error handling

from utils.config import PROXY1, PROXY2, TELEGRAM, ZENROWS_API_KEY
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import parse_to_mysql_datetime, parse_odds, currency_to_float, send_telegram_alert, send_monitoring_alert, send_testing_alert
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
        options.add_argument("--no-zygote")
        options.add_argument("--single-process")  # Can help in some container/service envs, remove if issues
        options.add_argument(f'--load-extension={self.proxy_extension_dir}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-extensions-except=' + self.proxy_extension_dir)
        options.add_argument(
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
        )

        # Use a unique user data dir to avoid profile conflicts in service restarts
        self.user_data_dir = tempfile.mkdtemp(prefix="chrome_user_data_")
        options.add_argument(f'--user-data-dir={self.user_data_dir}')

        # Retry driver creation - Chrome can be flaky under systemd/service on servers
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.driver = webdriver.Chrome(options=options)
                break
            except Exception as e:
                self.logger.warning(f"Chrome driver start attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(5)

        self.wait = WebDriverWait(self.driver, 30)

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
            time.sleep(8)

            with open(f"debug_login_sports411_{int(time.time())}.html", "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.info("💾 Saved debug_login_sports411_*.html")

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
            with open(f"debug_login_sports411_FAIL_{int(time.time())}.html", "w", encoding="utf-8") as f:
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
            debug_file = f"debug_sports411_{self.sport_name.lower()}_{int(time.time())}.html"
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
            self.logger.info(f"========= Fetching Odds ({self.sport_name}) via Selenium (END) ==========")
    # END CHANGE

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

            # -----------------------------------
            # FIND GAME CONTAINER
            # -----------------------------------
            game_container = self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, f"div.sports-league-game[idgame='{game_id}']")
                )
            )

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

            # -----------------------------------
            # ACCEPT LINE CHANGES
            # -----------------------------------
            try:
                accept_all = self.driver.find_element(By.ID, "accept_all")
                if not accept_all.is_selected():
                    self.driver.execute_script("arguments[0].click();", accept_all)
                    self.logger.info("Accepted line changes")
            except Exception:
                self.logger.warning("Accept line changes option not available")

            # -----------------------------------
            # WAIT UNTIL PLACE BET ENABLED
            # -----------------------------------
            place_bet_btn = self.wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, ".place-bet-container button.btn-primary:not([disabled])")
                )
            )

            self.driver.execute_script("arguments[0].click();", place_bet_btn)
            self.logger.info("Place Bet button clicked")

            time.sleep(2)
            self.logger.info("Bet placement attempted successfully")
            return True, stake

        except Exception as e:
            self.logger.error(f"Place Bet failed: {e}", exc_info=True)
            asyncio.run(send_monitoring_alert(self.website, self.account_id, e, TELEGRAM['arbitrage']))
            return False, stake
        finally:
            self.logger.info("========== Execute Bet (END) ==========")

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
        self.logger = Logger.get_logger(f"{self.bookmaker}-betting")
        self.storage = Storage(self.logger)

        self.logger.info("==================== Betting (START) ====================")

        try:

            # Step 1: Login
            self.__login()

            # Step 2: Go to basketball page
            self.logger.info(f"Navigating to NBA page: {self.basketball_url}")
            self.driver.get(self.basketball_url)
            time.sleep(2)  # Wait for initial load

            while True:
                time.sleep(2)

                current_url = self.driver.current_url

                # Ensure still on NBA page
                if not current_url.startswith(self.basketball_url):
                    error_msg = f"Terminating Process - Unexpected URL detected ({current_url})."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)
                
                arbs = self.cache.get_arbitrage(bookmaker=self.bookmaker, bet_type='moneyline')
                if not arbs:
                    self.logger.info("Waiting for Arbitrage")
                    continue
                
                self.logger.info(f"Arbitrage: {len(arbs)}")

                for arb in arbs:

                    sport = arb.get('sport')
                    league = arb.get('league')
                    game_date = arb.get('game_date')
                    bet_type = arb.get('bet_type')
                    team_1 = arb.get("team_1")
                    team_2 = arb.get("team_2")

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

                    # Place Bet
                    bet_placed, stake = self.__execute_bet(game_id, team_name, moneyline_odd, stake)
                    if bet_placed:

                        self.logger.info("Bet Placement Completed")
                        
                        # Cache: Remove Arbitrage
                        self.cache.remove_arbitrage(bookmaker=self.bookmaker, bet_type='moneyline', game_id=game_id)

                        # Send Telegram Alert
                        alert = (
                            f"===== Moneyline Bet =====\n"
                            f"Sport: {sport}\n"
                            f"League: {league}\n"
                            f"Date: {game_date}\n"
                            f"Match: {team_1} vs {team_2}\n"
                            f"Bet Type: {bet_type}\n"
                            f"Team No: {team_no}\n"
                            f"Team: {team_name}\n"
                            f"Bookmaker: {self.bookmaker}\n"
                            f"Odds: {moneyline_odd}\n"
                            f"Stake: {stake}\n"
                            # f"Attempts: {attempts}\n"
                        )

                        self.logger.info(f"========== Alert ==========")
                        self.logger.info(alert)
                        self.logger.info(f"========== Alert ==========")

                        asyncio.run(send_telegram_alert(alert, TELEGRAM['arbitrage']))

                        # ------------------------------
                        # Save Bet
                        # ------------------------------
                        bet_data = {
                            "sport": sport,
                            "league": league,
                            "game_id": game_id,
                            "game_datetime": game_date,

                            "team_1": team_1,
                            "team_2": team_2,

                            "bookmaker": self.bookmaker,
                            "bet_type": bet_type,

                            "team_no": team_no,
                            "team_name": team_name,

                            "odds": moneyline_odd,
                            "stake": stake
                        }

                        saved_bet = self.storage.save_bet(bet_data)

                        if saved_bet:
                            self.logger.info("DB - Bet Saved")
                        else:
                            self.logger.warning("DB - Bet Not Saved")

                        # Refreshing page before processing the next arbitrage
                        self.logger.info("Refreshing page before processing the next arbitrage")
                        self.driver.refresh()
                        time.sleep(2)  # wait for page reload


        except Exception as e:
            self.logger.error(f"Bet Place Failed: {e}", exc_info=True)
            asyncio.run(send_monitoring_alert(self.website, self.account_id, e, TELEGRAM['arbitrage_monitoring']))
            return None
        finally:
            try:
                self.driver.quit()
            except Exception:
                pass
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




