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
from utils.helpers import parse_to_mysql_datetime, parse_odds, currency_to_float, send_telegram_alert, send_monitoring_alert, send_testing_alert
from utils.timing import time_it
from cache.arbitrage_cache import ArbitrageCache


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
        options.add_argument("--no-zygote")
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

        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 30)

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

            with open(f"debug_login_betamapola_{int(time.time())}.html", "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.info("[SAVED] debug_login_betamapola_*.html")

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
            with open(f"debug_login_betamapola_FAIL_{int(time.time())}.html", "w", encoding="utf-8") as f:
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
    def _fetch_game_lines_via_api(self):
        """POST to GetSportOffering for MLB Straight Bet lines (all periods)."""
        self.logger.info("Fetching via GetSportOffering API (browser context)...")

        payload = {
            "sportType": "Baseball",
            "sportSubType": "MLB",
            "wagerType": "Straight Bet",
            "hoursAdjustment": 0,
            "periodNumber": None,
            "gameNum": None,
            "parentGameNum": None,
            "teaserName": "",
            "requestMode": None
        }

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

        try:
            # Ensure we are logged in using the existing Selenium driver
            self.__login()

            self.logger.info(f"Navigating to {self.sport_url}")
            self.driver.get(self.sport_url)

            # Wait for main content containers to appear
            try:
                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#gamesAccordion, .sport-lines-container, app-sports")))
            except Exception:
                self.logger.warning("Main content containers not found quickly.")

            # Wait for the sports sidebar icons to load (they are async)
            try:
                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a.sportIcon, a#img_Baseball")))
                self.logger.info("Sports sidebar loaded.")
            except Exception:
                self.logger.warning("Sports sidebar icons did not appear quickly.")

            # Expand the Baseball section (contains MLB games)
            try:
                baseball_link = self.driver.find_element(
                    By.CSS_SELECTOR, 
                    "a#img_Baseball, a[data-target='#sp_Baseball'], a.sportIcon"
                )
                self.driver.execute_script("arguments[0].click();", baseball_link)
                self.logger.info("Clicked to expand Baseball/MLB section.")
                time.sleep(2)  # Allow expansion animation
            except Exception as e:
                self.logger.warning(f"Could not click to expand Baseball section: {e}")

            # Directly trigger Angular to select the MLB league (more reliable than click)
            try:
                result = self.driver.execute_script("""
                    var label = document.querySelector('label[for="gl_Baseball_MLB_G"]');
                    if (label) {
                        var scope = angular.element(label).scope();
                        if (scope && scope.Events && scope.sport && scope.sub) {
                            scope.ClearFilter && scope.ClearFilter();
                            scope.Events.ToggleOffering(scope.sport, scope.sub, true);
                            if (scope.$apply) scope.$apply();
                            return true;
                        }
                    }
                    return false;
                """)
                if result:
                    self.logger.info("Directly triggered Angular ToggleOffering for MLB.")
                else:
                    self.logger.warning("Could not access Angular scope for MLB item.")
                time.sleep(4)  # Allow games to load after toggling
            except Exception as e:
                self.logger.warning(f"Failed to trigger Angular MLB selection via scope: {e}")

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
                net_file = f"network_betamapola_{self.sport_name.lower()}_{int(time.time())}.json"
                with open(net_file, "w", encoding="utf-8") as f:
                    json.dump(network_requests, f, indent=2)
                self.logger.info(f"💾 Saved full network log: {net_file}")
            except Exception as e:
                self.logger.warning(f"Failed to capture performance logs: {e}")

            # Save debug HTML after waiting (always useful for diagnostics)
            debug_file = f"debug_betamapola_{self.sport_name.lower()}_{int(time.time())}.html"
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
            self.logger.info(f"========= Fetching Odds ({self.sport_name}) via Selenium (END) ==========")

    # --------------------------------------------------------
    # Execute Bet (adapted for Betamapola /sports#/ SPA + betSlipDiv)
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

            # Find the moneyline span by rotation or by searching text (M1_/M2_ ids contain the composite game_id)
            moneyline_span = None

            # Strategy 1: id contains the game_id parts (rotations)
            for prefix in ["M1_", "M2_"]:
                candidates = self.driver.find_elements(
                    By.CSS_SELECTOR, f"span[id^='{prefix}'][id*='{game_id.split('-')[0]}']"
                )
                for cand in candidates:
                    txt = (cand.text or cand.get_attribute("innerText") or "").strip()
                    if moneyline_odd in txt or txt == moneyline_odd:
                        moneyline_span = cand
                        break
                if moneyline_span:
                    break

            # Strategy 2: search all M* spans + match team name + odd in nearby text
            if not moneyline_span:
                all_ml = self.driver.find_elements(By.CSS_SELECTOR, "span[id^='M1_'], span[id^='M2_']")
                for cand in all_ml:
                    parent_text = (cand.find_element(By.XPATH, "./ancestor::div[1]").text if cand.find_elements(By.XPATH, "./ancestor::div[1]") else cand.text) or ""
                    parent_text = parent_text.lower()
                    if team_name.lower() in parent_text and moneyline_odd in (cand.text or ""):
                        moneyline_span = cand
                        break

            if not moneyline_span:
                # Last resort: broad search
                all_spans = self.driver.find_elements(By.CSS_SELECTOR, "span.text-black, span.ng-binding")
                for cand in all_spans:
                    if (cand.text or "").strip() == moneyline_odd:
                        # verify team context
                        ctx = cand.find_element(By.XPATH, "./ancestor::*[contains(@class,'game') or contains(@class,'line')]").text.lower() if cand.find_elements(By.XPATH, "./ancestor::*[contains(@class,'game') or contains(@class,'line')]") else ""
                        if team_name.lower() in ctx:
                            moneyline_span = cand
                            break

            if not moneyline_span:
                raise Exception(f"Moneyline span not found for team '{team_name}' @ {moneyline_odd}")

            self.logger.info(f"Moneyline span located: {moneyline_span.get_attribute('id')}")

            # Click the odds span (adds to bet slip)
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", moneyline_span)
            time.sleep(0.4)
            self.driver.execute_script("arguments[0].click();", moneyline_span)
            self.logger.info("Moneyline span clicked")

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

            if place_btn:
                self.driver.execute_script("arguments[0].click();", place_btn)
                self.logger.info("Place Bet clicked")
            else:
                self.logger.warning("Place Bet button not auto-detected - manual intervention may be required")

            time.sleep(3)
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
        self.logger = Logger.get_logger(f"{self.bookmaker}-bet")
        self.storage = Storage(self.logger)

        self.logger.info("==================== Place Bet (START) ====================")

        # Step 1: Login
        self.__login()

        # Step 2: Go to sports SPA
        self.logger.info(f"Navigating to sports: {self.sport_url}")
        self.driver.get(self.sport_url)
        time.sleep(3)

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

        try:
            # Step 1: Login
            self.__login()

            # Step 2: Go to sports page
            self.logger.info(f"Navigating to sports: {self.sport_url}")
            self.driver.get(self.sport_url)
            time.sleep(3)

            while True:
                time.sleep(2)

                current_url = self.driver.current_url
                if "/sports" not in current_url:
                    error_msg = f"Terminating Process - Unexpected URL detected ({current_url})."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                arbs = self.cache.get_arbitrage(bookmaker=self.bookmaker, bet_type='moneyline')
                if not arbs:
                    self.logger.info("Waiting for Arbitrage")
                    continue

                self.logger.info(f"Arbitrage opportunities: {len(arbs)}")

                for arb in arbs:
                    sport = arb.get('sport')
                    league = arb.get('league')
                    game_date = arb.get('game_date')
                    bet_type = arb.get('bet_type')
                    team_1 = arb.get("team_1")
                    team_2 = arb.get("team_2")

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

                    # Place Bet
                    bet_placed, stake = self.__execute_bet(game_id, team_name, moneyline_odd, stake)
                    if bet_placed:
                        self.logger.info("Bet Placement Completed")

                        self.cache.remove_arbitrage(bookmaker=self.bookmaker, bet_type='moneyline', game_id=game_id)

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
                        )

                        self.logger.info("========== Alert ==========")
                        self.logger.info(alert)
                        self.logger.info("========== Alert ==========")

                        asyncio.run(send_telegram_alert(alert, TELEGRAM['arbitrage']))

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

                        self.logger.info("Refreshing page before next arbitrage")
                        self.driver.refresh()
                        time.sleep(3)

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


# Quick self-test entrypoint (uses provided credentials)
def main():
    from database.models.Accounts import Accounts
    from utils.config import BETAMAPOLA

    account = Accounts(account='PC8396', password='SUN87', label='Reader-30K')
    controller = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    # Only fetch odds for testing (betting requires live arb cache)
    controller.fetch_odds()


if __name__ == "__main__":
    main()
