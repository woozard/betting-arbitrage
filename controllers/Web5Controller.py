import traceback
import re
import random
from bs4 import BeautifulSoup
from datetime import datetime
import time
import asyncio
import json

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains 
from selenium.common.exceptions import TimeoutException
import undetected_chromedriver as uc

from utils.config import PROXY1 ,PROXY2, TELEGRAM
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import currency_to_float, determine_wager_on_spread, send_telegram_alert, send_monitoring_alert, send_testing_alert, epoch_to_mysql_datetime, parse_odds
from utils.helpers import detect_odds_type, decimal_to_american, american_to_decimal, odds_equal
from cache.arbitrage_cache import ArbitrageCache

class Web5Controller:
    def __init__(self, account, site):

         # Credentials
        self.account_id = account.account
        self.password = account.password
        self.label = account.label if account.label is not None else "N/A"
        
        # Bookmaker 
        self.bookmaker = site['bookmaker']

        # Logger & Storage
        self.logger = Logger.get_logger(site['bookmaker'])
        self.storage = Storage(self.logger)

        # Cache
        self.cache = ArbitrageCache()
        
        # Set URLs for various API endpoints
        self.website = site['website']
        self.base_url = site['url']
        self.login_url = f"{self.base_url}/en"
        self.dashboard_url = f"{self.base_url}/en/sports/soccer"
        self.pending_wagers_url = f"{self.base_url}/en/account/my-bets-full"
        self.basketball_url = f"{self.base_url}/en/sports/basketball"
        self.basketball_api_url = f"{self.base_url}/sports-service/sv/compact/favourite-events?_g=0&btg=1&c=&cl=100&d=&ec=&ev=&g=QQ%3D%3D&hle=false&l=100&lg=487&lv=&me=0&mk=3&more=false&o=1&ot=0&pa=0&pimo=&pn=-1&sp=4&tm=0&v=0&wm=&locale=en_US&_=1765914560489&withCredentials=true"

        # Proxy settings
        proxy_host = PROXY1['host']
        proxy_port = PROXY1['port']
        proxy_url = f"http://{proxy_host}:{proxy_port}"

        # proxy_host = PROXY2['host']
        # proxy_port = PROXY2['port']
        # proxy_url = f"http://{proxy_host}:{proxy_port}"

        # proxy_host = PROXY2['host']
        # proxy_port = PROXY2['port']
        # proxy_username = PROXY2['username']
        # proxy_password = f"{PROXY2['password']}_country-us_city-newyorkcity"
        # proxy_url = f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}"
        
        # Initialize Chrome WebDriver with proxy options
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument(f'--proxy-server={proxy_url}')

        # Add these additional arguments to appear more like a real browser
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
        
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 30)  # Increased timeout duration

        # Initialize Chrome WebDriver with proxy options
        # options = uc.ChromeOptions()
        # options.headless = True  # Runs the browser in headless mode for deployment
        # options.add_argument(f'--proxy-server={proxy_url}')
        # self.driver = uc.Chrome(version_main=126, use_subprocess=False, options=options)
        # self.wait = WebDriverWait(self.driver, 30)  # Increased timeout duration

    # --------------------------------------------------------
    # Login
    # --------------------------------------------------------
    def __login(self):
        try:
            
            self.logger.info(f"account_id: {self.account_id}")
            self.logger.info(f"label: {self.label}")

        
            # Step 1: Go to the sign-in page
            self.driver.get(self.login_url)
            time.sleep(5)  # Allow time for the page to load
            self.logger.info("Opened Login Page")
            self.logger.info(f"Current URL: {self.driver.current_url}")

            # Print the login page HTML content
            # print("Login Page HTML Content:")
            # print(driver.page_source)  # Print the HTML of the login page
            
            # Step 2: Find and fill the username and password fields
            self.wait.until(
                EC.presence_of_element_located((By.NAME, "loginId"))
            )
            username_input = self.driver.find_element(By.NAME, 'loginId')
            password_input = self.driver.find_element(By.NAME, 'pass')

            username_input.send_keys(self.account_id)  # Enter username
            password_input.send_keys(self.password)  # Enter password
            self.logger.info("Filled Login Form")

            # Step 3: Submit the form by pressing ENTER
            password_input.send_keys(Keys.RETURN)
            time.sleep(3)  # Allow time for the page to process the login
            self.logger.info("Submitted Login Form")
            
            # Redirect to dashboard page (if login redirects automatically, this may not be needed)
            self.wait.until(EC.url_contains(self.dashboard_url))
            self.logger.info("Login Passed")
        except Exception as e:
            self.logger.error(f"Login Failed - Reason: {e}")
            raise Exception(f"Login Failed - Reason: {e}") from e
    
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
        # Split the "Detail" column into parts and handle cases where it might be incomplete
        detail_parts = row.get('detail', '').split('|')
        
        # Check for expected parts in "Detail" and assign only if available
        row['book_ticket_id'] = detail_parts[0] if len(detail_parts) > 0 else None
        row['sport'] = detail_parts[1] if len(detail_parts) > 1 else None
        row['datetime'] = detail_parts[-1] if len(detail_parts) > 2 else None

        # Split the "Selection" column into parts and handle cases where it might be incomplete
        selection_parts = row.get('selection', '').split('|')
        print(selection_parts)
        
        row['wager_on'] = selection_parts[0] if len(selection_parts) > 0 else None
        # Validate and set the spread only if it matches the pattern
        if len(selection_parts) > 1 and re.match(r'^[+-]?\d+(\.\d+)?$', selection_parts[1].strip()):
            row['spread'] = selection_parts[1].strip()
        else:
            row['spread'] = None  # Set spread to None if not a valid number

        # For team_1 and team_2, check if selection_parts contains '-vs-'
        if len(selection_parts) > 4:
            # Find the index of '-vs-' if it exists in the list
            vs_index = selection_parts.index('-vs-') if '-vs-' in selection_parts else -1

            if vs_index != -1:
                # Ensure there are enough elements before and after '-vs-'
                if vs_index > 0 and vs_index < len(selection_parts) - 1:
                    row['team_1'] = selection_parts[vs_index - 1].strip()  # Team 1 is the part before '-vs-'
                    row['team_2'] = selection_parts[vs_index + 1].strip()  # Team 2 is the part after '-vs-'
                else:
                    row['team_1'] = None
                    row['team_2'] = None
            else:
                row['team_1'] = None
                row['team_2'] = None
        else:
            row['team_1'] = None
            row['team_2'] = None


        # Clean the "Odds" column by removing "|A" if present
        row['odds'] = row.get('odds', '').split('|')[0]
        
        # Extract only the Risk amount from "Stake (USD)" column
        stake_parts = row.get('stake', '').split('|')
        row['risk'] = stake_parts[1] if len(stake_parts) > 1 else None
        
        # Remove the original 'Detail' column as it's split into separate columns
        # if 'Detail' in row:
        #     del row['Detail']
        
        return row

    # --------------------------------------------------------
    # Pending Wagers / Open Bets
    # --------------------------------------------------------
    def pending_wagers(self):
        self.logger.info(f"==================== Pending Wagers (START) ====================")
       
        try:

            self.__login()
            
            # Step 4: Redirect to the open bets page
            self.driver.get(self.pending_wagers_url)
            self.logger.info("Opened Pending Bets Page")
            self.logger.info(f"Current URL: {self.driver.current_url}")
            
            # Wait until the table loads on the page
            self.wait.until(
                EC.presence_of_element_located((By.CLASS_NAME, "info-div-table"))
            )

            time.sleep(10)  # Allow time for the page to load rows in table
            
            # Step 5: Get the page source and parse it with BeautifulSoup
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            
            # Find the table
            table = soup.find("table", {"class": "info-div-table"})
            
            # Check if the table exists
            if table is None:
                self.logger.info("Table with class 'info-div-table' not found.")
            else:
                # Define the new column names
                headers = ["srno", "product", "detail", "selection", "odds", "stake", "win", "status"]

                # Extract rows of data with the new headers
                rows_data = []
                for row in table.find_all("tr")[1:-1]:  # Skip header row
                    cells = row.find_all("td")
                    # Map each cell to the corresponding new header
                    row_data = {headers[i]: self.__get_text_with_separator(cells[i]) for i in range(len(cells))}
                    rows_data.append(row_data)

                # Apply the formatting to each row in rows_data
                formatted_rows_data = [self.__format_row_data(row) for row in rows_data]

                # Step 4: self.logger.info each formatted row
                for row in formatted_rows_data:
                    self.logger.info(f"========== Row ==========")
                    self.logger.info(row)
                    self.logger.info(f"========== Row ==========")
                    
                    # Retrieve the values for the current row
                    game_info = row.get('selection', 'N/A')
                    sport = row.get('sport', 'N/A')

                    # Skip rows where sport is "Politics"
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

                    # Exception 01 - Skip saving and sending alerts
                    if self.account_id == "PWF2804090" and risk < 1000:
                        self.logger.info(f"Skipping alert for account_id: {self.account_id} as risk ({risk}) is less than 1000")
                        continue

                    # Exception 02 - Skip saving and sending alerts
                    if self.account_id == "PWF2804094" and 'LIVE' in game_info:
                        self.logger.info(f"Skipping alert for account_id: {self.account_id} as  game_info contains LIVE")
                        continue

                    # Determine the value of wager_on using spread
                    spread, wager_on = determine_wager_on_spread(spread, wager_on)
                    
                    is_send_alert = self.storage.save_telegram_alert(book_ticket_id,self.account_id,self.website,self.account_id,"alert",str(book_ticket_id),wager_on,team_1,team_2,bet_type,odds,spread,"no",wager_on,risk,win,status,sport,date_time,created_at,updated_at)
                    # is_send_alert = True
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
            asyncio.run(send_monitoring_alert(self.website, self.account_id, e))
        finally:
            self.driver.quit()

    def __inject_mutation_observer(self):

        self.logger.info(f"Injecting Mutation Observer (JS)")
        script = """
        if (!window.oddsObserverInstalled) {
            window.oddsObserverInstalled = true;
            window.oddsBuffer = [];
            
            // Fix: Use class selector with dot (.) for class names
            const target = document.querySelector('.odds-container-nolive');
            
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
            
    def __check_signed_out_popup(self):
        try:
            return self.driver.execute_script("""
                const el = document.querySelector('.alert-overlay .alert-message');
                if (!el) return false;
                return el.innerText.includes('signed out due to multiple logins');
            """)
        except:
            return False

    # --------------------------------------------------------
    # Fetch Odds
    # --------------------------------------------------------
    def fetch_odds(self):

        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)

        self.logger.info("==================== Fetching Odds (START) ====================")

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
            login_time = time.time()
            MAX_RUNTIME = 60 * 60  # 60 minutes
            first_run = True
            last_scan_time = time.monotonic()
            FORCE_SCAN_INTERVAL = 30

            while True:

                # Kill after 60 minutes
                if time.time() - login_time > MAX_RUNTIME:
                    error_msg = "Terminating Process - Max runtime (60 minutes) reached."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                # Check logout popup FIRST
                if self.__check_signed_out_popup():
                    error_msg = "Terminating Process - Detected multiple login logout popup."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                # Ensure still on NBA page
                current_url = self.driver.current_url
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
                    
                    self.logger.info(f"DOM Updates - Detected - {len(updates)}")

                try:

                    # Calling Basketball NBA API
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

                    # ---------- SPORT ----------
                    sport = None
                    league = None
                    matches_data = []

                    sports = data.get("n", [])

                    if not sports:
                        raise ValueError("No sports data found in response")

                    sport_block = sports[0]
                    sport = sport_block[1]  # "Basketball"

                    leagues = sport_block[2]
                    if not leagues:
                        raise ValueError("No leagues found under sport")


                    # Iterate leagues (NBA, etc.)
                    for league_block in leagues:
                        try:
                            league = league_block[1]          # NBA
                            matches_data = league_block[2]    # Matches list
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

                            # Market data
                            if '0' not in match_info[8]:
                                self.logger.warning(f"No moneyline data for match: {team_1} vs {team_2}")
                                continue

                            markets = match_info[8]
                            market_0 = markets.get("0")
                            if not market_0:
                                continue

                            # ---------- SPREADS ----------
                            spreads = []
                            for s in market_0[0]:
                                spreads.append({
                                    "handicap": float(s[2]),
                                    "team_1_spread": float(s[0]),
                                    "team_2_spread": float(s[1]),
                                    "team_1_odds": s[3],
                                    "team_2_odds": s[4],
                                })

                            # ---------- TOTALS ----------
                            totals = []
                            for t in market_0[1]:
                                totals.append({
                                    "total": float(t[1]),
                                    "over_odds": t[2],
                                    "under_odds": t[3],
                                })

                            # ---------- MONEYLINE ----------
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

                    # print(json.dumps(odds_data, indent=2))
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
                        self.logger.info(f"========== Parsed Odds #{idx}  ==========")

                except Exception as e:
                    self.logger.error(f"Exception: {e}")
                    self.logger.error("Trace:", exc_info=True)
                    asyncio.run(send_monitoring_alert(self.website, self.account_id, e))
                    return None

        except Exception as e:
            self.logger.error(f"Exception: {e}", exc_info=True)
            asyncio.run(send_monitoring_alert(self.website, self.account_id, e, TELEGRAM['arbitrage_monitoring']))

        finally:
            try:
                self.driver.quit()
            except:
                pass

            self.logger.info("==================== Fetching Odds (END) ====================")
    
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
        """
        Place a MONEYLINE bet by selecting the odds from NBA page and placing in bet slip
        """

        self.logger.info("========== Execute Bet (START) ==========")
        
        try:

            self.logger.info(
                f"Placing Bet | Game ID: {game_id} | Team: {team_name} | Odds: {moneyline_odd} | Stake: {stake}"
            )
            
            # -----------------------------------
            # PRINT ALL TABLE IDS INSIDE ODDS CONTAINER
            # -----------------------------------
            tables = self.driver.find_elements(
                By.CSS_SELECTOR,
                "div.odds-container-nolive div.odds-container table"
            )

            self.logger.info(f"Total tables found: {len(tables)}")

            for table in tables:
                table_id = table.get_attribute("id")
                if table_id:
                    self.logger.info(f"Table ID: {table_id}")

            # -----------------------------------
            # FIND GAME TABLE BY GAME ID
            # -----------------------------------
            game_table = None
            try:
                # Find table with the specific game ID
                game_table = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, f"table.events[id='e{game_id}']")
                    )
                )
                self.logger.info(f"Found game table for ID: {game_id}")
            except:
                # Alternative: find by data-event-id attribute
                try:
                    # First find the row with the data-eid
                    game_row = self.wait.until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, f"tr[data-eid='{game_id}']")
                        )
                    )
                    # Then get its parent table
                    game_table = game_row.find_element(By.XPATH, "./ancestor::table")
                    self.logger.info(f"Found game table by data-eid: {game_id}")
                except Exception as e:
                    self.logger.error(f"Could not find game table: {e}")
                    raise Exception(f"Game table for ID {game_id} not found")

            # -----------------------------------
            # FIND MONEYLINE ODDS ELEMENT
            # -----------------------------------
            moneyline_element = None

            rows = game_table.find_elements(By.CSS_SELECTOR, "tr.odd, tr.even")

            for row in rows:
                try:
                    # Skip more bets
                    if row.find_elements(By.CSS_SELECTOR, "td.more-bets[colspan]"):
                        continue

                    # Full game moneyline cell
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
                                american_odds = decimal_to_american(decimal_odds)  # safer, previously called american_to_decimal incorrectly

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

            # -----------------------------------
            # CLICK ON MONEYLINE ODDS
            # -----------------------------------
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});", 
                moneyline_element
            )
            time.sleep(1)
            
            # Click using JavaScript to avoid interception
            self.driver.execute_script("arguments[0].click();", moneyline_element)
            self.logger.info("Clicked on moneyline odds")
            
            # -----------------------------------
            # WAIT FOR BETSLIP TO APPEAR
            # -----------------------------------
            time.sleep(2)
            
            # Check if bet slip appears
            try:
                betslip_container = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".BetslipComponent, .betslip-container, [class*='betslip']")
                    )
                )
                self.logger.info("Betslip container found")
            except:
                # Try alternative selectors
                betslip_container = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".SingleBetComponent, .bet-body, .bet-footer")
                    )
                )
                self.logger.info("Betslip found via alternative selector")

            # -----------------------------------
            # VERIFY BET SLIP CONTENT
            # -----------------------------------
            time.sleep(1)
            
            # Check if our selection appears in bet slip
            try:
                bet_slip_team = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".bet-event span.team1, .selection")
                    )
                ).text.strip()
                
                if team_name.lower() not in bet_slip_team.lower():
                    self.logger.warning(f"Betslip team mismatch: Expected {team_name}, found {bet_slip_team}")
                    # Continue anyway as it might be a formatting difference
            except:
                self.logger.warning("Could not verify bet slip team")

            # -----------------------------------
            # ENTER STAKE IN BASE INPUT FIELD
            # -----------------------------------
            try:
                # Find base stake input field
                base_input = self.wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[name='base'][class*='stake'], input.stake.base")
                    )
                )
                
                # Clear and enter stake
                base_input.clear()
                base_input.send_keys(str(stake))
                self.logger.info(f"Entered stake in base field: {stake}")
                
                # Trigger change event if needed
                self.driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", base_input)
                
            except Exception as e:
                self.logger.warning(f"Could not find base input: {e}")
                
                # Try risk input as fallback
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

            # -----------------------------------
            # CHECK FOR ERRORS
            # -----------------------------------
            time.sleep(1)
            
            # Check for error messages
            error_elements = self.driver.find_elements(
                By.CSS_SELECTOR, ".error-message, .ERROR, .BetItemError, .attention.ERROR"
            )
            
            for error in error_elements:
                error_text = error.text.strip()
                if error_text and "unavailable" not in error_text.lower():
                    self.logger.warning(f"Bet slip error detected: {error_text}")
                    # Don't raise here, just log - we'll check if bet can be placed

            # -----------------------------------
            # ACCEPT BETTER ODDS (IF PRESENT)
            # -----------------------------------
            try:
                better_odds_checkbox = self.driver.find_element(
                    By.CSS_SELECTOR, "input#betterOddsCheckbox, input[name='betterOdds']"
                )
                
                if better_odds_checkbox.is_displayed() and not better_odds_checkbox.is_selected():
                    self.driver.execute_script("arguments[0].click();", better_odds_checkbox)
                    self.logger.info("Checked 'Accept Better Odds'")
            except:
                self.logger.info("'Accept Better Odds' checkbox not found or already checked")

            # -----------------------------------
            # LOG MIN / MAX BET LIMITS
            # -----------------------------------
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

                # Validate that the stake is within limits
                if stake < min_bet:
                    raise Exception(
                        f"Stake {stake} is below minimum bet {min_bet}"
                    )

                if max_bet > 0 and stake > max_bet:
                    raise Exception(
                        f"Stake {stake} exceeds maximum bet {max_bet}"
                    )
                
                # Set the stake directly via JS
                # self.driver.execute_script("""
                # arguments[0].value = arguments[1];
                # arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
                # arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
                # """, base_input, stake)

                self.logger.info(f"Re-entered stake in base field using JS: {stake}")

            except Exception as e:
                self.logger.warning(f"Could not read min/max bet limits: {e}")

            # -----------------------------------
            # CHECK IF PLACE BET BUTTON IS ENABLED
            # -----------------------------------
            place_bet_btn = self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".place-bet-btn, button[class*='place-bet']")
                )
            )
            
            btn_classes = place_bet_btn.get_attribute("class")
            is_disabled = "disabled" in btn_classes or place_bet_btn.get_attribute("disabled")
            
            if is_disabled:
                # Check why it's disabled
                disabled_reason = "Button disabled"
                
                # Check for error messages
                error_msg = self.driver.find_elements(By.CSS_SELECTOR, ".error-message")
                if error_msg:
                    disabled_reason = error_msg[0].text.strip()
                
                raise Exception(f"Cannot place bet - {disabled_reason}")
            
            # -----------------------------------
            # CLICK PLACE BET BUTTON
            # -----------------------------------
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", 
                place_bet_btn
            )
            time.sleep(0.5)
            
            self.driver.execute_script("arguments[0].click();", place_bet_btn)
            self.logger.info("Clicked 'Place Bet' button")

            # -----------------------------------
            # HANDLE ALERT MODALS AFTER PLACE BET
            # -----------------------------------
            try:
                # Wait a short time for any alert to appear
                alert_overlay = WebDriverWait(self.driver, 3).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".alert-overlay"))
                )

                # Get alert message and classes
                alert_message = alert_overlay.find_element(By.CSS_SELECTOR, ".alert-message").text.strip()
                alert_classes = alert_overlay.find_element(By.CSS_SELECTOR, ".AlertComponent").get_attribute("class")

                # Warning / failure alert
                if "confirm-alert" not in alert_classes:
                    self.logger.error(f"Bet rejected by alert: {alert_message}")
                    try:
                        ok_btn = alert_overlay.find_element(By.CSS_SELECTOR, "button.okBtn")
                        self.driver.execute_script("arguments[0].click();", ok_btn)
                        self.logger.info("Closed warning alert modal")
                    except:
                        self.logger.warning("Alert OK button not found")
                    return False  # Bet failed

                # Confirmation alert
                else:
                    self.logger.info(f"Confirmation alert appeared: {alert_message}")
                    try:
                        ok_btn = alert_overlay.find_element(By.CSS_SELECTOR, "button.okBtn")
                        self.driver.execute_script("arguments[0].click();", ok_btn)
                        self.logger.info("Confirmed bet by clicking OK")
                    except:
                        self.logger.error("Could not find OK button on confirmation alert")
                        return False

                    # Optional: wait a bit to ensure bet is placed
                    time.sleep(2)

                    self.logger.info("Bet placement process completed")
                    return True, stake  # Bet successfully placed

            except TimeoutException:
                # No alert appeared → assume bet placed successfully
                self.logger.info("No alert appeared after place bet → assuming success")
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

            # Step 3: Initialize BEFORE loop
            login_time = time.time()
            MAX_RUNTIME = 60 * 60  # 60 minutes

            while True:
                time.sleep(2)

                # Kill after 60 minutes
                if time.time() - login_time > MAX_RUNTIME:
                    error_msg = "Terminating Process - Max runtime (60 minutes) reached."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                # Check logout popup FIRST
                if self.__check_signed_out_popup():
                    error_msg = "Terminating Process - Detected multiple login logout popup."
                    self.logger.error(error_msg)
                    raise Exception(error_msg)

                # Ensure still on NBA page
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
                    # bet_placed = True
                    if bet_placed:

                        self.logger.info("Bet Placed")

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
