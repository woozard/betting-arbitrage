import time
import json
import asyncio
import re
from decimal import Decimal
from datetime import datetime
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from utils.config import PROXY1, PROXY2, TELEGRAM
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import parse_to_mysql_datetime, parse_odds, currency_to_float, send_telegram_alert, send_monitoring_alert, send_testing_alert
from utils.timing import time_it
from cache.arbitrage_cache import ArbitrageCache

class Sports411Controller:
    def __init__(self, account, site):

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

        # Set URLs for various API endpoints
        self.base_url = f"https://www.{self.website}"
        self.login_url = f"{self.base_url}/"
        self.dashboard_url = f"https://be.{self.website}/en/sports/"
        self.basketball_url = f"https://be.{self.website}/en/sports/basketball/nba/game-lines/"

        self.sport = "Basketball"
        self.league = "NBA"

        # Proxy
        proxy_host = PROXY1['host']
        proxy_port = PROXY1['port']
        proxy_url = f"http://{proxy_host}:{proxy_port}"

        # proxy_host = PROXY2['host']
        # proxy_port = PROXY2['port']
        # proxy_username = PROXY2['username']
        # proxy_password = PROXY2['password']
        # proxy_url = f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}"

        # Chrome options
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"--proxy-server={proxy_url}")

        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 30)

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
            self.logger.info("💾 Saved debug_login_sports411_*.html — inspect for current form fields!")

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
                By.CSS_SELECTOR, "input[type='\''submit'\'']\.login"
            )
            login_btn.click()

            self.wait.until(EC.url_contains("/en/sports/"))
            self.logger.info("Login Successful")
        except Exception as e:
            self.logger.error(f"Login Failed: {e}")
            with open(f"debug_login_sports411_FAIL_{int(time.time())}.html", "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
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
    # Fetch Odds
    # --------------------------------------------------------
    @time_it
    def fetch_odds(self, refresh_interval=10):
        start = time.perf_counter()

        """
        Continuously fetch NBA odds by refreshing the page every `refresh_interval` seconds.
        Login happens only once. The browser remains open.
        """

        # Logger & Storage
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)

        self.logger.info("========== Fetching Odds (START) ==========")
        try:
            # Step 1: Login
            self.__login()

            # Step 2: Go to basketball page
            self.logger.info(f"Navigating to NBA page: {self.basketball_url}")
            self.driver.get(self.basketball_url)
            time.sleep(5)  # Wait for initial load

             # Step 3: Inject MutationObserver (JS) once after page load
            self.__inject_mutation_observer()

            # Step 4: Initialize BEFORE loop
            first_run = True
            last_scan_time = time.monotonic()
            FORCE_SCAN_INTERVAL = 30

            while True:
                current_url = self.driver.current_url

                # Ensure still on NBA page
                if not current_url.startswith(self.basketball_url):
                    error_msg = f"Terminating Process - Unexpected URL detected ({current_url})."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                now = time.monotonic()
                time_since_last_scan = now - last_scan_time

                # -------------------------------
                # FORCE SCAN every 30 seconds
                # -------------------------------
                if first_run or time_since_last_scan >= FORCE_SCAN_INTERVAL:
                    self.logger.info("DOM Updates - FORCE SCAN triggered")

                    updates = [self.driver.execute_script("""
                        return document.body.innerHTML;
                    """)]

                    last_scan_time = now
                    first_run = False

                else:
                    # normal MutationObserver buffer
                    updates = self.driver.execute_script("""
                        const data = window.oddsBuffer || [];
                        window.oddsBuffer = [];
                        return data;
                    """)

                    if not updates:
                        self.logger.info("DOM Updates - Waiting")
                        time.sleep(1)
                        continue

                try:

                    for inner_html in updates:
                        soup = BeautifulSoup(inner_html, "html.parser")

                        games = []

                        # ----- Parse all games -----
                        for game in soup.select("div.sports-league-game"):
                            try:
                                # Game ID
                                game_id = game.get("idgame")

                                # Date & Time
                                time_block = game.select_one(".game-time")
                                if not time_block:
                                    self.logger.warning(f"No time block found for game {game_id}")
                                    continue

                                date_spans = time_block.select("span")
                                if len(date_spans) < 2:
                                    self.logger.warning(f"Could not parse datetime for game {game_id}")
                                    continue

                                game_date = date_spans[0].get_text(strip=True)
                                game_time = date_spans[1].get_text(strip=True)
                                game_datetime = parse_to_mysql_datetime(game_date, game_time)

                                # ---------- TEAMS ----------
                                team_2_elem = game.select_one(".teams .visitor span")
                                team_1_elem = game.select_one(".teams .home span")

                                if not team_1_elem or not team_2_elem:
                                    self.logger.warning(f"Could not find teams for game {game_id}")
                                    continue

                                team_1 = team_1_elem.get_text(strip=True)
                                team_2 = team_2_elem.get_text(strip=True)

                                # ---------- MONEYLINE ----------
                                team_2_ml_elem = game.select_one(".mline-1 .odds span")
                                team_1_ml_elem = game.select_one(".mline-2 .odds span")

                                team_1_ml = None
                                team_2_ml = None

                                if team_2_ml_elem:
                                    lock_icon = team_2_ml_elem.find("i")
                                    if lock_icon and "fa-lock" in lock_icon.get("class", ""):
                                        team_2_ml = "LOCKED"
                                    else:
                                        team_2_ml = team_2_ml_elem.get_text(strip=True)

                                if team_1_ml_elem:
                                    lock_icon = team_1_ml_elem.find("i")
                                    if lock_icon and "fa-lock" in lock_icon.get("class", ""):
                                        team_1_ml = "LOCKED"
                                    else:
                                        team_1_ml = team_1_ml_elem.get_text(strip=True)

                                # ---------- SPREADS ----------
                                spread_divs = game.select(".hdp-home-away .hdp")

                                team_1_spread = None
                                team_2_spread = None
                                team_1_spread_odds = None
                                team_2_spread_odds = None

                                if len(spread_divs) >= 2:
                                    # Team 2 spread (visitor)
                                    team_2_label = spread_divs[0].select_one(".bet-indicator")
                                    if team_2_label:
                                        points_elem = team_2_label.select_one(".points-line span")
                                        odds_elem = team_2_label.select_one(".odds span")

                                        if points_elem:
                                            team_2_spread = points_elem.get_text(strip=True)
                                        elif team_2_label.select_one(".odds i.fa-lock"):
                                            team_2_spread = "LOCKED"

                                        if odds_elem:
                                            lock_icon = odds_elem.find("i")
                                            if lock_icon and "fa-lock" in lock_icon.get("class", ""):
                                                team_2_spread_odds = "LOCKED"
                                            else:
                                                team_2_spread_odds = odds_elem.get_text(strip=True)

                                    # Team 1 spread (home)
                                    team_1_label = spread_divs[1].select_one(".bet-indicator")
                                    if team_1_label:
                                        points_elem = team_1_label.select_one(".points-line span")
                                        odds_elem = team_1_label.select_one(".odds span")

                                        if points_elem:
                                            team_1_spread = points_elem.get_text(strip=True)
                                        elif team_1_label.select_one(".odds i.fa-lock"):
                                            team_1_spread = "LOCKED"

                                        if odds_elem:
                                            lock_icon = odds_elem.find("i")
                                            if lock_icon and "fa-lock" in lock_icon.get("class", ""):
                                                team_1_spread_odds = "LOCKED"
                                            else:
                                                team_1_spread_odds = odds_elem.get_text(strip=True)

                                # ---------- TOTALS ----------
                                total_divs = game.select(".ou-total .ou")

                                over_total = None
                                under_total = None
                                over_odds = None
                                under_odds = None

                                if len(total_divs) >= 2:
                                    # Over
                                    over_label = total_divs[0].select_one(".bet-indicator")
                                    if over_label:
                                        points_elem = over_label.select_one(".points-line span")
                                        odds_elem = over_label.select_one(".odds span")

                                        if points_elem:
                                            txt = points_elem.get_text(strip=True)
                                            if txt.lower().startswith("o"):
                                                over_total = txt[1:]
                                        elif over_label.select_one(".odds i.fa-lock"):
                                            over_total = "LOCKED"

                                        if odds_elem:
                                            lock_icon = odds_elem.find("i")
                                            if lock_icon and "fa-lock" in lock_icon.get("class", ""):
                                                over_odds = "LOCKED"
                                            else:
                                                over_odds = odds_elem.get_text(strip=True)

                                    # Under
                                    under_label = total_divs[1].select_one(".bet-indicator")
                                    if under_label:
                                        points_elem = under_label.select_one(".points-line span")
                                        odds_elem = under_label.select_one(".odds span")

                                        if points_elem:
                                            txt = points_elem.get_text(strip=True)
                                            if txt.lower().startswith("u"):
                                                under_total = txt[1:]
                                        elif under_label.select_one(".odds i.fa-lock"):
                                            under_total = "LOCKED"

                                        if odds_elem:
                                            lock_icon = odds_elem.find("i")
                                            if lock_icon and "fa-lock" in lock_icon.get("class", ""):
                                                under_odds = "LOCKED"
                                            else:
                                                under_odds = odds_elem.get_text(strip=True)

                                games.append({
                                    "bookmaker": self.bookmaker,
                                    "sport": self.sport,
                                    "league": self.league,

                                    "game_id": game_id,
                                    "game_datetime": game_datetime,
                                    "match": f"{team_1} vs {team_2}",

                                    "team_1": team_1,
                                    "team_2": team_2,

                                    "moneyline": {
                                        "team_1": team_1_ml,
                                        "team_2": team_2_ml
                                    },
                                    "spread": {
                                        "team_1_spread": team_1_spread,
                                        "team_2_spread": team_2_spread,
                                        "team_1_odds": team_1_spread_odds,
                                        "team_2_odds": team_2_spread_odds,
                                    },
                                    "total": {
                                        "over_total": over_total,
                                        "under_total": under_total,
                                        "over_odds": over_odds,
                                        "under_odds": under_odds,
                                    }
                                })

                            except Exception as e:
                                self.logger.error(f"Error parsing game: {e}", exc_info=True)
                                continue

                        self.logger.info(f"Extracted {len(games)} NBA matches")
                        # Parse odds and save
                        odds_data = {
                            "sport": self.sport,
                            "league": self.league,
                            "total_matches": len(games),
                            "matches": games,
                            "timestamp": datetime.now().isoformat()
                        }

                        parsed_odds = parse_odds(odds_data)

                        for idx, odd_row in enumerate(parsed_odds, start=1):
                            self.logger.info(f"========== Parsed Odds #{idx}  ==========")

                            # Cache: Add Odds
                            if odd_row.get('bet_type') == 'moneyline':
                                self.cache.add_odds(odd_row)

                            # Save Odds in DB
                            saved_odds = self.storage.save_odds(odd_row)
                            if saved_odds:      

                                # Send Telegram Alert
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
                                    # f"Spread: {odd_row.get('spread_team_1')} {odd_row.get('spread_team_2')}\n"
                                    # f"Spread Value: {odd_row.get('spread_value')}\n"
                                    # f"Total: {odd_row.get('over_odds')} {odd_row.get('under_odds')}\n"
                                    # f"Total Points: {odd_row.get('total_points')}\n"
                                )
                                
                                self.logger.info(f"========== Alert ==========")
                                self.logger.info(alert)
                                self.logger.info(f"========== Alert ==========")

                                asyncio.run(send_telegram_alert(alert, TELEGRAM['arbitrage_monitoring']))


                except Exception as e:
                    self.logger.error(f"Error during odds fetch: {e}", exc_info=True)
                    asyncio.run(send_monitoring_alert(self.website, self.account_id, e))
                    time.sleep(refresh_interval)

        except KeyboardInterrupt:
            self.logger.info("Stopped live NBA odds fetching by user.")

        except Exception as e:
            self.logger.error(f"Fatal exception: {e}", exc_info=True)
            asyncio.run(send_monitoring_alert(self.website, self.account_id, e, TELEGRAM['arbitrage_monitoring']))

        finally:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.logger.info("========= Fetching Odds (END) ==========")
    

    # --------------------------------------------------------
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
            




