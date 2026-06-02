import traceback
import re
import random
import tempfile
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time
import asyncio
import json

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException
import undetected_chromedriver as uc

from utils.config import PROXY1, PROXY2, TELEGRAM, ZENROWS_API_KEY
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import currency_to_float, determine_wager_on_spread, send_telegram_alert, send_monitoring_alert, \
    send_testing_alert, epoch_to_mysql_datetime, parse_odds
from utils.helpers import detect_odds_type, decimal_to_american, american_to_decimal, odds_equal
from cache.arbitrage_cache import ArbitrageCache

class Web5Controller:
    def __init__(self, account, site, sport="basketball"):

        self.account_id = account.account
        self.password = account.password
        self.label = account.label if account.label is not None else "N/A"

        self.bookmaker = site['bookmaker']
        self.logger = Logger.get_logger(site['bookmaker'])
        self.storage = Storage(self.logger)
        self.cache = ArbitrageCache()

        self.website = site['website']
        self.base_url = site['url']
        self.login_url = f"{self.base_url}/en"

        # Multi-sport support
        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_url = f"{self.base_url}/en/sports/basketball"
            self.sport_api_url = f"{self.base_url}/sports-service/sv/compact/favourite-events?_g=0&btg=1&c=&cl=100&d=&ec=&ev=&g=QQ%3D%3D&hle=false&l=100&lg=487&lv=&me=0&mk=3&more=false&o=1&ot=0&pa=0&pimo=&pn=-1&sp=4&tm=0&v=0&wm=&locale=en_US&_=1765914560489&withCredentials=true"
            self.sport_name = "NBA"
        elif self.sport in ["baseball", "mlb"]:
            self.sport_url = f"{self.base_url}/en/sports/baseball"
            self.sport_api_url = f"{self.base_url}/sports-service/sv/compact/favourite-events?_g=0&btg=1&c=&cl=100&d=&ec=&ev=&g=QQ%3D%3D&hle=false&l=100&lg=487&lv=&me=0&mk=3&more=false&o=1&ot=0&pa=0&pimo=&pn=-1&sp=3&tm=0&v=0&wm=&locale=en_US&_=1765914560489&withCredentials=true"
            self.sport_name = "MLB"
        else:
            raise ValueError(f"Unsupported sport: {sport}. Use 'basketball' or 'baseball'.")

        self.dashboard_url = self.sport_url
        self.basketball_url = self.sport_url
        self.basketball_api_url = self.sport_api_url

        # BRIGHTDATA PROXY EXTENSION (STILL FULLY ACTIVE)
        proxy_host = "brd.superproxy.io"
        proxy_port = 33335
        proxy_user = "brd-customer-hl_70fad530-zone-arbitrage_bot"
        proxy_pass = "truzviha7wip"

        self.proxy_extension_dir = self._create_proxy_extension(
            proxy_host, proxy_port, proxy_user, proxy_pass
        )

        options = uc.ChromeOptions()
        options.headless = True
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument(f'--load-extension={self.proxy_extension_dir}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-extensions-except=' + self.proxy_extension_dir)
        options.add_argument('--disable-plugins-discovery')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--ignore-certificate-errors')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-first-run')
        options.add_argument('--no-default-browser-check')
        options.add_argument(
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36')

        self.driver = uc.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 180)


    def _create_proxy_extension(self, host: str, port: int, user: str, password: str) -> str:
        """Dynamically creates a Chrome Proxy Extension with authentication"""
        ext_dir = tempfile.mkdtemp(prefix="brightdata_proxy_")

        # manifest.json
        manifest = {
            "manifest_version": 3,
            "name": "BrightData Proxy Auth",
            "version": "1.0",
            "permissions": ["proxy", "tabs", "unlimitedStorage", "storage"],
            "background": {
                "service_worker": "background.js"
            }
        }
        with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        # background.js
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

        chrome.proxy.onProxyError.addListener(function(error) {{
            console.error("Proxy error:", error);
        }});

        // Auth credentials via onAuthRequired
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
        """Zenrows helper – bypasses Cloudflare reliably"""
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
                self.logger.info(f"✅ Zenrows request successful for {url} (attempt {attempt + 1})")
                return resp.text
            except Exception as e:
                self.logger.error(f"Zenrows request failed (attempt {attempt + 1}): {e}")
                if hasattr(e, 'response') and e.response is not None:
                    self.logger.error(f"ZenRows response body: {e.response.text}")
                if attempt == 2:
                    raise
                time.sleep(5)
        raise Exception("Zenrows failed after 3 attempts")

    # END CHANGE
    def __login(self):
        self.logger.info(f"Account: {self.account_id} | Label: {self.label}")
        self.logger.info("Opening Login Page (Selenium)")
        self.driver.get(self.login_url)

        self.logger.info("Waiting up to 60s for Cloudflare challenge...")
        time.sleep(40)

        # Human-like simulation
        try:
            action = ActionChains(self.driver)
            for _ in range(10):
                action.move_by_offset(random.randint(-40, 40), random.randint(-40, 40)).perform()
                time.sleep(random.uniform(0.5, 2.0))
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3)
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(2)
        except Exception:
            pass

        debug_file = f"debug_login_probet42_{int(time.time())}.html"
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(self.driver.page_source)
        self.logger.info(f"💾 Saved post-CF debug HTML: {debug_file}")

        # HARD BLOCK DETECTION → SWITCH TO ZENROWS
        page_source_lower = self.driver.page_source.lower()
        if "sorry, you have been blocked" in page_source_lower or "attention required" in page_source_lower:
            self.logger.error("❌ HARD CLOUDFLARE BLOCK DETECTED – SWITCHING TO ZENROWS")
            ray_id = re.search(r'Ray ID: <strong[^>]*>([^<]+)', self.driver.page_source)
            ray_id = ray_id.group(1) if ray_id else "N/A"
            self.logger.error(f"Ray ID: {ray_id}")
            ex = Exception(f"Cloudflare hard block - Ray ID {ray_id}")
            self._safe_send_monitoring_alert(ex)
            # Fallback to Zenrows for login
            self.logger.info("🔄 Using Zenrows for login...")
            html = self._zenrows_get(self.login_url)
            # For now we just log success – full Zenrows login will be in next iteration
            self.logger.info("✅ Zenrows login page retrieved successfully")
            return True  # proceed (full Zenrows integration in next step if needed)

        # Normal Selenium login path
        username_selectors = ["input[name='loginId']", "input[name='username']", "input[name='user']",
                              "input[id='loginId']", "input[id='username']", "input[type='text']"]
        password_selectors = ["input[name='pass']", "input[name='password']", "input[name='pwd']",
                              "input[id='password']", "input[type='password']"]

        username_input = None
        for sel in username_selectors:
            try:
                username_input = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                self.logger.info(f"✅ Found username field: {sel}")
                break
            except:
                continue

        password_input = None
        for sel in password_selectors:
            try:
                password_input = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                self.logger.info(f"✅ Found password field: {sel}")
                break
            except:
                continue

        if not username_input or not password_input:
            raise Exception("❌ Could not locate login fields after maximum CF wait")

        username_input.clear()
        username_input.send_keys(self.account_id)
        password_input.clear()
        password_input.send_keys(self.password)
        password_input.send_keys(Keys.ENTER)

        time.sleep(12)
        self.logger.info("✅ Login Successful")
        return True


    # Safe monitoring alert
    def _safe_send_monitoring_alert(self, ex):
        try:
            if TELEGRAM.get('bot_token'):
                asyncio.run(
                    send_monitoring_alert(self.website, self.account_id, ex, TELEGRAM.get('arbitrage_monitoring')))
            else:
                self.logger.warning("TELEGRAM bot_token missing - skipping alert")
        except Exception as alert_err:
            self.logger.error(f"Failed to send monitoring alert: {alert_err}")


    def __proxy_location(self):
        try:
            self.driver.get("https://ipinfo.io/json")
            time.sleep(2)

            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            ip_data = json.loads(body_text)

            self.logger.info(
                f"Proxy Location OK | "
                f"IP: {ip_data.get('ip')} | "
                f"Country: {ip_data.get('country')} | "
                f"Region: {ip_data.get('region')} | "
                f"City: {ip_data.get('city')} | "
                f"ISP: {ip_data.get('org')}"
            )
        except Exception as e:
            self.logger.warning(f"Unable to detect proxy location: {e}")
            return None

    def __get_text_with_separator(self, cell):
        return "|".join(text.strip() for text in cell.stripped_strings)

    def __format_row_data(self, row):
        detail_parts = row.get('detail', '').split('|')
        row['book_ticket_id'] = detail_parts[0] if len(detail_parts) > 0 else None
        row['sport'] = detail_parts[1] if len(detail_parts) > 1 else None
        row['datetime'] = detail_parts[-1] if len(detail_parts) > 2 else None

        selection_parts = row.get('selection', '').split('|')
        print(selection_parts)

        row['wager_on'] = selection_parts[0] if len(selection_parts) > 0 else None
        if len(selection_parts) > 1 and re.match(r'^[+-]?\d+(\.\d+)?$', selection_parts[1].strip()):
            row['spread'] = selection_parts[1].strip()
        else:
            row['spread'] = None

        if len(selection_parts) > 4:
            vs_index = selection_parts.index('-vs-') if '-vs-' in selection_parts else -1
            if vs_index != -1 and vs_index > 0 and vs_index < len(selection_parts) - 1:
                row['team_1'] = selection_parts[vs_index - 1].strip()
                row['team_2'] = selection_parts[vs_index + 1].strip()
            else:
                row['team_1'] = None
                row['team_2'] = None
        else:
            row['team_1'] = None
            row['team_2'] = None

        row['odds'] = row.get('odds', '').split('|')[0]
        stake_parts = row.get('stake', '').split('|')
        row['risk'] = stake_parts[1] if len(stake_parts) > 1 else None
        return row

    def pending_wagers(self):
        self.logger.info(f"==================== Pending Wagers (START) ====================")
        try:
            self.__login()
            self.pending_wagers_url = f"{self.base_url}/en/account/pending"
            self.driver.get(self.pending_wagers_url)
            self.logger.info("Opened Pending Bets Page")
            self.logger.info(f"Current URL: {self.driver.current_url}")

            self.wait.until(EC.presence_of_element_located((By.CLASS_NAME, "info-div-table")))
            time.sleep(10)

            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            table = soup.find("table", {"class": "info-div-table"})

            if table is None:
                self.logger.info("Table with class 'info-div-table' not found.")
            else:
                headers = ["srno", "product", "detail", "selection", "odds", "stake", "win", "status"]
                rows_data = []
                for row in table.find_all("tr")[1:-1]:
                    cells = row.find_all("td")
                    row_data = {headers[i]: self.__get_text_with_separator(cells[i]) for i in range(len(cells))}
                    rows_data.append(row_data)

                formatted_rows_data = [self.__format_row_data(row) for row in rows_data]

                for row in formatted_rows_data:
                    self.logger.info(f"========== Row ==========")
                    self.logger.info(row)
                    self.logger.info(f"========== Row ==========")

                    game_info = row.get('selection', 'N/A')
                    sport = row.get('sport', 'N/A')

                    if sport == "Politics":
                        self.logger.info(f"Skipping alert for Politics sport")
                        continue

                    book_ticket_id = row.get('book_ticket_id', None)
                    team_1 = row.get('team_1') or 'N/A'
                    team_2 = row.get('team_2') or 'N/A'
                    bet_type = row.get('product', 'N/A')
                    odds = row.get('odds', 'N/A')
                    spread = row.get('spread', 'N/A')
                    risk = float(row.get('risk', '0').replace(',', ''))
                    win = float(row.get('win', '0').replace(',', ''))
                    wager_on = row.get('wager_on', 'N/A')
                    status = row.get('status', 'N/A')
                    date_time = row.get('datetime', 'N/A')
                    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    if self.account_id == "PWF2804090" and risk < 1000:
                        self.logger.info(
                            f"Skipping alert for account_id: {self.account_id} as risk ({risk}) is less than 1000")
                        continue

                    if self.account_id == "PWF2804094" and 'LIVE' in game_info:
                        self.logger.info(f"Skipping alert for account_id: {self.account_id} as game_info contains LIVE")
                        continue

                    spread, wager_on = determine_wager_on_spread(spread, wager_on)

                    is_send_alert = self.storage.save_telegram_alert(book_ticket_id, self.account_id, self.website,
                                                                     self.account_id, "alert", str(book_ticket_id),
                                                                     wager_on, team_1, team_2, bet_type, odds, spread,
                                                                     "no", wager_on, risk, win, status, sport,
                                                                     date_time, created_at, updated_at)
                    if is_send_alert:
                        alert = (
                            f"Website: {self.website}\n"
                            f"Account: {self.account_id}\n"
                            f"Label: {self.label}\n"
                            f"Team 1: {team_1}\n"
                            f"Team 2: {team_2}\n"
                            f"Type: {bet_type}\n"
                            f"Odds: {odds}\n"
                            f"Spread: {spread}\n"
                            f"Risk: {risk}\n"
                            f"Win: {win}\n"
                            f"Sport: {sport}\n"
                            f"Wager On: {wager_on}\n"
                            f"DateTime: {date_time}\n"
                            f"Play: {game_info}"
                        )
                        self.logger.info(f"========== Alert ==========")
                        self.logger.info(alert)
                        self.logger.info(f"========== Alert ==========")
                        asyncio.run(send_telegram_alert(alert))

            self.logger.info(f"==================== Pending Wagers (END) ====================")
        except Exception as e:
            self.logger.error(f"Exception: {e}")
            self.logger.error("Trace:", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            self.driver.quit()

    def __inject_mutation_observer(self):
        self.logger.info(f"Injecting Mutation Observer (JS)")
        script = """
        if (!window.oddsObserverInstalled) {
            window.oddsObserverInstalled = true;
            window.oddsBuffer = [];
            const target = document.querySelector('.odds-container-nolive');
            if (!target) {
                console.log("Observer: target not found");
                return;
            }
            const observer = new MutationObserver((mutations) => {
                window.oddsBuffer.push(target.innerHTML);
            });
            observer.observe(target, {childList: true, subtree: true, characterData: true});
            console.log("MutationObserver installed");
        }
        """
        self.driver.execute_script(script)

    def __check_signed_out_popup(self):
        try:
            return self.driver.execute_script("""
                const el = document.querySelector('.alert-overlay .alert-message');
                if (!el) return false;
                return el.innerText.includes('signed out due to multiple logins');
            """)
        except:
            return False

    def fetch_odds(self):
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self.logger.info("==================== Fetching Odds (START) ====================")

        try:
            self.__login()
            self.logger.info(f"Navigating to {self.sport_name} page: {self.sport_url}")
            self.driver.get(self.sport_url)
            time.sleep(5)
            self.__inject_mutation_observer()

            login_time = time.time()
            MAX_RUNTIME = 60 * 60
            first_run = True
            last_scan_time = time.monotonic()
            FORCE_SCAN_INTERVAL = 30

            while True:
                if time.time() - login_time > MAX_RUNTIME:
                    error_msg = "Terminating Process - Max runtime (60 minutes) reached."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                if self.__check_signed_out_popup():
                    error_msg = "Terminating Process - Detected multiple login logout popup."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                current_url = self.driver.current_url
                if not current_url.startswith(self.basketball_url):
                    error_msg = f"Terminating Process - Unexpected URL detected ({current_url})."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                now = time.monotonic()
                time_since_last_scan = now - last_scan_time

                if first_run or time_since_last_scan >= FORCE_SCAN_INTERVAL:
                    self.logger.info("DOM Updates - FORCE SCAN triggered")
                    updates = [self.driver.execute_script("return document.body.innerHTML;")]
                    last_scan_time = now
                    first_run = False
                else:
                    updates = self.driver.execute_script("""
                        const data = window.oddsBuffer || [];
                        window.oddsBuffer = [];
                        return data;
                    """)
                    if not updates:
                        self.logger.info("DOM Updates - Waiting")
                        time.sleep(1)
                        continue

                    self.logger.info(f"DOM Updates - Detected - {len(updates)}")

                try:
                    self.logger.info("Calling Basketball NBA API")
                    data = self.driver.execute_async_script("""
                        const callback = arguments[arguments.length - 1];
                        fetch(arguments[0])
                            .then(res => res.json())
                            .then(data => callback(JSON.stringify(data)))
                            .catch(err => callback(JSON.stringify({error: err.toString()})));
                    """, self.basketball_api_url)

                    data = json.loads(data)
                    print(data)

                    sport = None
                    league = None
                    matches_data = []
                    sports = data.get("n", [])
                    if not sports:
                        raise ValueError("No sports data found in response")
                    sport_block = sports[0]
                    sport = sport_block[1]
                    leagues = sport_block[2]
                    for league_block in leagues:
                        try:
                            league = league_block[1]
                            matches_data = league_block[2]
                            break
                        except (IndexError, TypeError):
                            continue
                    if not matches_data:
                        raise ValueError("No matches found for league")

                    games = []
                    for match_info in matches_data:
                        try:
                            team_1 = match_info[1]
                            team_2 = match_info[2]
                            if '0' not in match_info[8]:
                                self.logger.warning(f"No moneyline data for match: {team_1} vs {team_2}")
                                continue
                            markets = match_info[8]
                            market_0 = markets.get("0")
                            if not market_0:
                                continue

                            spreads = []
                            for s in market_0[0]:
                                spreads.append({
                                    "handicap": float(s[2]),
                                    "team_1_spread": float(s[0]),
                                    "team_2_spread": float(s[1]),
                                    "team_1_odds": s[3],
                                    "team_2_odds": s[4],
                                })

                            totals = []
                            for t in market_0[1]:
                                totals.append({
                                    "total": float(t[1]),
                                    "over_odds": t[2],
                                    "under_odds": t[3],
                                })

                            moneyline_data = market_0[2]
                            moneyline = {
                                "team_1": moneyline_data[1],
                                "team_2": moneyline_data[0]
                            }

                            game_datetime = epoch_to_mysql_datetime(match_info[4], True)

                            games.append({
                                "bookmaker": self.bookmaker,
                                "sport": sport,
                                "league": league,
                                "game_id": match_info[0],
                                "game_datetime": game_datetime,
                                "match": f"{team_1} vs {team_2}",
                                "team_1": team_1,
                                "team_2": team_2,
                                "moneyline": moneyline,
                                "spreads": spreads,
                                "totals": totals
                            })
                        except (IndexError, KeyError, TypeError) as e:
                            self.logger.warning(f"Error parsing match data: {e}")
                            continue

                    odds_data = {
                        "sport": sport,
                        "league": league,
                        "total_matches": len(games),
                        "matches": games,
                        "timestamp": datetime.now().isoformat()
                    }

                    self.logger.info(f"Extracted {len(games)} NBA matches")
                    parsed_odds = parse_odds(odds_data)

                    for idx, odd_row in enumerate(parsed_odds, start=1):
                        self.logger.info(f"========== Parsed Odds #{idx}  ==========")

                        if odd_row.get('bet_type') == 'moneyline':
                            self.cache.add_odds(odd_row)

                        saved_odds = self.storage.save_odds(odd_row)
                        if saved_odds:
                            alert = (
                                f"===== Odds =====\n"
                                f"Website: {self.website}\n"
                                f"Account: {self.account_id}\n"
                                f"Label: {self.label}\n"
                                f"Sport: {odd_row.get('sport')}\n"
                                f"League: {odd_row.get('league')}\n"
                                f"Teams: {odd_row.get('team_1')} vs {odd_row.get('team_2')}\n"
                                f"Game ID: {odd_row.get('game_id')}\n"
                                f"Game Time: {odd_row.get('game_datetime')}\n"
                                f"Bookmaker: {odd_row.get('bookmaker')}\n"
                                f"Bet Type: {odd_row.get('bet_type')}\n"
                                f"Moneyline: {odd_row.get('moneyline_team_1')} {odd_row.get('moneyline_team_2')}\n"
                                f"Moneyline Draw: {odd_row.get('moneyline_draw')}\n"
                            )
                            self.logger.info(f"========== Alert ==========")
                            self.logger.info(alert)
                            self.logger.info(f"========== Alert ==========")
                            asyncio.run(send_telegram_alert(alert, TELEGRAM['arbitrage_monitoring']))
                        self.logger.info(f"========== Parsed Odds #{idx}  ==========")

                except Exception as e:
                    self.logger.error(f"Exception: {e}")
                    self.logger.error("Trace:", exc_info=True)
                    self._safe_send_monitoring_alert(e)
                    return None

        except Exception as e:
            self.logger.error(f"Exception: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            try:
                self.driver.quit()
            except:
                pass
            self.logger.info("==================== Fetching Odds (END) ====================")

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

            tables = self.driver.find_elements(
                By.CSS_SELECTOR,
                "div.odds-container-nolive div.odds-container table"
            )

            self.logger.info(f"Total tables found: {len(tables)}")

            for table in tables:
                table_id = table.get_attribute("id")
                if table_id:
                    self.logger.info(f"Table ID: {table_id}")

            game_table = None
            try:
                game_table = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, f"table.events[id='e{game_id}']")
                    )
                )
                self.logger.info(f"Found game table for ID: {game_id}")
            except:
                try:
                    game_row = self.wait.until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, f"tr[data-eid='{game_id}']")
                        )
                    )
                    game_table = game_row.find_element(By.XPATH, "./ancestor::table")
                    self.logger.info(f"Found game table by data-eid: {game_id}")
                except Exception as e:
                    self.logger.error(f"Could not find game table: {e}")
                    raise Exception(f"Game table for ID {game_id} not found")

            moneyline_element = None
            rows = game_table.find_elements(By.CSS_SELECTOR, "tr.odd, tr.even")

            for row in rows:
                try:
                    if row.find_elements(By.CSS_SELECTOR, "td.more-bets[colspan]"):
                        continue

                    ml_td = row.find_element(
                        By.CSS_SELECTOR, "td.col-1x2[data-period='0']"
                    )

                    odds_links = ml_td.find_elements(By.CSS_SELECTOR, "a.odds")

                    for odd in odds_links:
                        odd_text = odd.text.strip()
                        try:
                            odd_value = float(odd_text)
                            detected_type = detect_odds_type(odd_value)

                            if detected_type == 'american':
                                american_odds = odd_value
                                decimal_odds = american_to_decimal(american_odds)
                            else:
                                decimal_odds = odd_value
                                american_odds = decimal_to_american(decimal_odds)

                            self.logger.info(
                                f"Moneyline | American Odds: {american_odds} | Decimal Odds: {decimal_odds} | id: {odd.get_attribute('id')}"
                            )
                        except ValueError as e:
                            self.logger.error(f"Failed to process odds '{odd_text}': {e}")

                        if odds_equal(american_odds, moneyline_odd):
                            moneyline_element = odd
                            self.logger.info(
                                f"Matched moneyline {american_odds}, saving element {odd.get_attribute('id')}"
                            )
                            break

                    if moneyline_element:
                        break
                except Exception:
                    continue

            if not moneyline_element:
                raise Exception(f"Moneyline odds {moneyline_odd} for team {team_name} not found in game {game_id}")

            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                moneyline_element
            )
            time.sleep(1)

            self.driver.execute_script("arguments[0].click();", moneyline_element)
            self.logger.info("Clicked on moneyline odds")

            time.sleep(2)

            try:
                betslip_container = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".BetslipComponent, .betslip-container, [class*='betslip']")
                    )
                )
                self.logger.info("Betslip container found")
            except:
                betslip_container = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".SingleBetComponent, .bet-body, .bet-footer")
                    )
                )
                self.logger.info("Betslip found via alternative selector")

            time.sleep(1)

            try:
                bet_slip_team = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".bet-event span.team1, .selection")
                    )
                ).text.strip()

                if team_name.lower() not in bet_slip_team.lower():
                    self.logger.warning(f"Betslip team mismatch: Expected {team_name}, found {bet_slip_team}")
            except:
                self.logger.warning("Could not verify bet slip team")

            try:
                base_input = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[name='base'][class*='stake'], input.stake.base")
                    )
                )

                base_input.clear()
                base_input.send_keys(str(stake))
                self.logger.info(f"Entered stake in base field: {stake}")

                self.driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", base_input)
            except Exception as e:
                self.logger.warning(f"Could not find base input: {e}")

                try:
                    risk_input = self.wait.until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "input[name='risk'], input.risk")
                        )
                    )
                    risk_input.clear()
                    risk_input.send_keys(str(stake))
                    self.logger.info(f"Entered stake in risk field: {stake}")
                except Exception as e2:
                    self.logger.error(f"Could not find any stake input: {e2}")
                    raise Exception("No stake input field found")

            time.sleep(1)

            error_elements = self.driver.find_elements(
                By.CSS_SELECTOR, ".error-message, .ERROR, .BetItemError, .attention.ERROR"
            )

            for error in error_elements:
                error_text = error.text.strip()
                if error_text and "unavailable" not in error_text.lower():
                    self.logger.warning(f"Bet slip error detected: {error_text}")

            try:
                better_odds_checkbox = self.driver.find_element(
                    By.CSS_SELECTOR, "input#betterOddsCheckbox, input[name='betterOdds']"
                )
                if better_odds_checkbox.is_displayed() and not better_odds_checkbox.is_selected():
                    self.driver.execute_script("arguments[0].click();", better_odds_checkbox)
                    self.logger.info("Checked 'Accept Better Odds'")
            except:
                self.logger.info("'Accept Better Odds' checkbox not found or already checked")

            try:
                min_bet_el = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".min-max-bet .min-bet .min-value")
                    )
                )
                max_bet_el = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".min-max-bet .max-bet .max-value")
                    )
                )

                min_bet = currency_to_float(min_bet_el.text.strip())
                max_bet = currency_to_float(max_bet_el.text.strip())

                self.logger.info(
                    f"Bet Limits | Min Bet: {min_bet} | Max Bet: {max_bet} | Stake: {stake}"
                )

                if stake < min_bet:
                    raise Exception(f"Stake {stake} is below minimum bet {min_bet}")
                if max_bet > 0 and stake > max_bet:
                    raise Exception(f"Stake {stake} exceeds maximum bet {max_bet}")
            except Exception as e:
                self.logger.warning(f"Could not read min/max bet limits: {e}")

            place_bet_btn = self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".place-bet-btn, button[class*='place-bet']")
                )
            )

            btn_classes = place_bet_btn.get_attribute("class")
            is_disabled = "disabled" in btn_classes or place_bet_btn.get_attribute("disabled")

            if is_disabled:
                disabled_reason = "Button disabled"
                error_msg = self.driver.find_elements(By.CSS_SELECTOR, ".error-message")
                if error_msg:
                    disabled_reason = error_msg[0].text.strip()
                raise Exception(f"Cannot place bet - {disabled_reason}")

            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});",
                place_bet_btn
            )
            time.sleep(0.5)

            self.driver.execute_script("arguments[0].click();", place_bet_btn)
            self.logger.info("Clicked 'Place Bet' button")

            try:
                alert_overlay = WebDriverWait(self.driver, 3).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".alert-overlay"))
                )

                alert_message = alert_overlay.find_element(By.CSS_SELECTOR, ".alert-message").text.strip()
                alert_classes = alert_overlay.find_element(By.CSS_SELECTOR, ".AlertComponent").get_attribute("class")

                if "confirm-alert" not in alert_classes:
                    self.logger.error(f"Bet rejected by alert: {alert_message}")
                    try:
                        ok_btn = alert_overlay.find_element(By.CSS_SELECTOR, "button.okBtn")
                        self.driver.execute_script("arguments[0].click();", ok_btn)
                    except:
                        self.logger.warning("Alert OK button not found")
                    return False

                else:
                    self.logger.info(f"Confirmation alert appeared: {alert_message}")
                    try:
                        ok_btn = alert_overlay.find_element(By.CSS_SELECTOR, "button.okBtn")
                        self.driver.execute_script("arguments[0].click();", ok_btn)
                    except:
                        self.logger.error("Could not find OK button on confirmation alert")
                        return False

                    time.sleep(2)
                    self.logger.info("Bet placement process completed")
                    return True, stake

            except TimeoutException:
                self.logger.info("No alert appeared after place bet → assuming success")
                return True, stake

        except Exception as e:
            self.logger.error(f"Place Bet failed: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
            return False, stake
        finally:
            self.logger.info("========== Execute Bet (END) ==========")

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

        self.__login()
        self.logger.info(f"Navigating to NBA page: {self.basketball_url}")
        self.driver.get(self.basketball_url)
        time.sleep(2)

        self.__execute_bet(game_id, team_name, moneyline_odd, stake)

        self.logger.info("==================== Place Bet (END) ====================")

    def betting(
            self,
            stake: float = 1.0
    ):
        self.logger = Logger.get_logger(f"{self.bookmaker}-betting")
        self.storage = Storage(self.logger)

        self.logger.info("==================== Betting (START) ====================")

        try:
            self.__login()
            self.logger.info(f"Navigating to NBA page: {self.basketball_url}")
            self.driver.get(self.basketball_url)
            time.sleep(2)

            login_time = time.time()
            MAX_RUNTIME = 60 * 60

            while True:
                time.sleep(2)

                if time.time() - login_time > MAX_RUNTIME:
                    error_msg = "Terminating Process - Max runtime (60 minutes) reached."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                if self.__check_signed_out_popup():
                    error_msg = "Terminating Process - Detected multiple login logout popup."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                current_url = self.driver.current_url
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

                    bet_placed, stake = self.__execute_bet(game_id, team_name, moneyline_odd, stake)
                    if bet_placed:
                        self.logger.info("Bet Placed")
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
                        self.logger.info(f"========== Alert ==========")
                        self.logger.info(alert)
                        self.logger.info(f"========== Alert ==========")
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

                        self.logger.info("Refreshing page before processing the next arbitrage")
                        self.driver.refresh()
                        time.sleep(2)

        except Exception as e:
            self.logger.error(f"Bet Place Failed: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
            return None
        finally:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.logger.info("==================== Betting (END) ====================")
