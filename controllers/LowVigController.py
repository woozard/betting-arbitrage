import os
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver import Remote
from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection
from selenium.webdriver.support.ui import WebDriverWait

from controllers.BetamapolaController import BetamapolaController
from utils.config import brightdata_selenium_endpoint, lowvig_proxy_settings
from utils.helpers import debug_filepath


class LowVigController(BetamapolaController):
    """
    lowvig.ag — BetOnline-family SPA (GetSportOffering + TicoSports bet slip).
    Login uses OpenID at account.lowvig.ag; sports UI at sports.lowvig.ag/sportsbook.
    """

    ODDS_WATCH_POLL_SECONDS = float(os.getenv("LOWVIG_ODDS_POLL_SEC", "1.5"))
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("LOWVIG_ODDS_FORCE_SCAN_SEC", "5"))
    ODDS_IDLE_POLL_SECONDS = float(os.getenv("LOWVIG_ODDS_IDLE_POLL_SEC", "2"))
    LOGIN_WAIT_SECONDS = int(os.getenv("LOWVIG_LOGIN_WAIT_SEC", "180"))

    SPORTS_HOST = os.getenv("LOWVIG_SPORTS_HOST", "sports.lowvig.ag")
    AUTH_LOGIN_URL = os.getenv(
        "LOWVIG_AUTH_LOGIN_URL",
        "https://account.lowvig.ag/Login/AuthenticationUser",
    )

    USER_FIELD_SELECTORS = (
        "#CustomerID",
        "#customerID",
        "input[name='CustomerID']",
        "#username",
        "#account",
    )
    PASS_FIELD_SELECTORS = (
        "#Password",
        "#password",
        "input[type='password']",
    )
    SUBMIT_SELECTORS = (
        "#btnLogin",
        "button[type='submit']",
        "input[type='submit']",
        "button.btn-primary",
        "#LogInAccount",
    )

    def __init__(self, account, site, sport="baseball"):
        self._proxy = lowvig_proxy_settings()
        self._use_scraping_browser = bool(
            not self._proxy
            and os.getenv("LOWVIG_USE_SCRAPING_BROWSER", "1") == "1"
            and brightdata_selenium_endpoint()
        )
        super().__init__(account, site, sport=sport)
        self.login_url = os.getenv("LOWVIG_LOGIN_URL", f"https://www.{self.website}")
        self.dashboard_url = f"https://{self.SPORTS_HOST}/sportsbook"
        self.sport_url = self.dashboard_url

    def _create_driver_with_proxy(self, proxy: dict):
        """Local Chrome + authenticated HTTP proxy (IPRoyal residential/ISP)."""
        self.proxy_extension_dir = self._create_proxy_extension(
            proxy["host"],
            proxy["port"],
            proxy["username"],
            proxy["password"],
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
        options.add_argument(f"--load-extension={self.proxy_extension_dir}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions-except=" + self.proxy_extension_dir)
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        )
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        self.user_data_dir = tempfile.mkdtemp(prefix="chrome_user_data_")
        options.add_argument(f"--user-data-dir={self.user_data_dir}")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.driver = webdriver.Chrome(options=options)
                try:
                    _ = self.driver.current_url
                except Exception as ve:
                    self.logger.warning(
                        f"Chrome created on attempt {attempt + 1} but session is dead: {ve}"
                    )
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(5)
                    continue
                break
            except Exception as e:
                self.logger.warning(
                    f"Chrome driver start attempt {attempt + 1}/{max_retries} failed: {e}"
                )
                if attempt == max_retries - 1:
                    raise
                time.sleep(5)

        self.wait = WebDriverWait(self.driver, 30)
        time.sleep(2)

    def _create_driver(self):
        """IPRoyal/local proxy, else Bright Data Browser API, else default Bright Data proxy."""
        if self._proxy:
            self.logger.info(
                f"LowVig using HTTP proxy {self._proxy['host']}:{self._proxy['port']} "
                f"(user={self._proxy['username']})"
            )
            self._create_driver_with_proxy(self._proxy)
            return

        endpoint = brightdata_selenium_endpoint()
        if self._use_scraping_browser and endpoint:
            self.logger.info(f"LowVig using Bright Data Browser API ({endpoint.split('@')[-1]})")
            options = ChromeOptions()
            options.add_argument("--ignore-certificate-errors")
            connection = ChromiumRemoteConnection(endpoint, "goog", "chrome")
            self.driver = Remote(connection, options=options)
            self.wait = WebDriverWait(self.driver, 30)
            time.sleep(2)
            return
        super()._create_driver()

    def _page_is_cloudflare(self) -> bool:
        try:
            src = (self.driver.page_source or "").lower()
            title = (self.driver.title or "").lower()
            return "just a moment" in src or "just a moment" in title or "attention required" in src
        except Exception:
            return True

    def _find_visible(self, selectors: tuple):
        for sel in selectors:
            try:
                for elem in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if elem.is_displayed():
                        return elem, sel
            except Exception:
                continue
        return None, None

    def _navigate_to_auth_login(self):
        self.logger.info("Opening LowVig home page")
        self.driver.get(self.login_url)
        time.sleep(6)

        login_debug = debug_filepath("debug_login_lowvig")
        with open(login_debug, "w", encoding="utf-8") as f:
            f.write(self.driver.page_source)
        self.logger.info(f"[SAVED] {login_debug}")

        if self._page_is_cloudflare():
            self.logger.warning("LowVig home page behind Cloudflare challenge")

        try:
            btn = self.driver.find_element(By.CSS_SELECTOR, "#lvbtn")
            self.driver.execute_script("arguments[0].click();", btn)
            self.logger.info("Clicked LowVig Log In button")
            time.sleep(4)
        except Exception:
            self.logger.info(f"Log In button not found; opening {self.AUTH_LOGIN_URL}")
            self.driver.get(self.AUTH_LOGIN_URL)
            time.sleep(6)

    def _fill_input(self, elem, value: str):
        """Fill form fields; Bright Data Browser API forbids JS password injection."""
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});",
            elem,
        )
        ActionChains(self.driver).move_to_element(elem).click().perform()
        time.sleep(0.2)
        elem.send_keys(Keys.CONTROL, "a")
        elem.send_keys(Keys.BACKSPACE)
        elem.send_keys(value)

    def _perform_login(self):
        """LowVig OpenID login at account.lowvig.ag (not Betamapola #account form)."""
        try:
            self.logger.info(f"Account: {self.account_id}")
            self.logger.info(f"Label: {self.label}")

            self._navigate_to_auth_login()

            deadline = time.monotonic() + self.LOGIN_WAIT_SECONDS
            user_el = None
            pass_el = None
            while time.monotonic() < deadline:
                user_el, user_sel = self._find_visible(self.USER_FIELD_SELECTORS)
                pass_el, pass_sel = self._find_visible(self.PASS_FIELD_SELECTORS)
                if user_el and pass_el:
                    self.logger.info(f"Login form ready ({user_sel}, {pass_sel})")
                    break
                if self._page_is_cloudflare():
                    self.logger.info("Waiting for Cloudflare challenge to clear...")
                time.sleep(3)

            if not user_el or not pass_el:
                fail_debug = debug_filepath("debug_login_lowvig_FAIL")
                with open(fail_debug, "w", encoding="utf-8") as f:
                    f.write(self.driver.page_source)
                raise RuntimeError(
                    f"LowVig login form not available after {self.LOGIN_WAIT_SECONDS}s "
                    f"(likely Cloudflare on account.lowvig.ag). Saved {fail_debug}"
                )

            self.logger.info("Filling username")
            self._fill_input(user_el, self.account_id)
            self.logger.info("Filling password")
            self._fill_input(pass_el, self.password)

            submit_el, submit_sel = self._find_visible(self.SUBMIT_SELECTORS)
            if not submit_el:
                raise RuntimeError("LowVig login submit button not found")
            self.driver.execute_script("arguments[0].click();", submit_el)
            self.logger.info(f"Submitted LowVig login via {submit_sel}")

            time.sleep(8)
            self.driver.get(self.sport_url)
            time.sleep(6)

            if self._page_is_cloudflare():
                raise RuntimeError("Cloudflare still blocking sports.lowvig.ag after login")

            self._force_wager_relogin = False
            self.logger.info(f"Login Successful (url={self.driver.current_url})")
            return True

        except Exception as e:
            self.logger.error(f"Login Failed: {e}")
            fail_debug = debug_filepath("debug_login_lowvig_FAIL")
            try:
                with open(fail_debug, "w", encoding="utf-8") as f:
                    f.write(self.driver.page_source)
            except Exception:
                pass
            self._safe_send_monitoring_alert(e)
            raise

    def _ensure_betting_session(self):
        if self._force_wager_relogin:
            self.logger.info("Wager session flagged invalid; performing full login")
            self._perform_login()
            self._BetamapolaController__ensure_sport_offering_loaded()
            return
        if self._is_session_valid() and self._is_on_sport_page_with_games():
            self.logger.info("Session valid on sport page with games loaded; skipping login")
            return
        if self._is_session_valid():
            self.logger.info("Session valid but sport offering not loaded; navigating only")
            self._BetamapolaController__ensure_sport_offering_loaded()
            return
        self.logger.info("Session invalid; performing full login")
        self._perform_login()
        self._BetamapolaController__ensure_sport_offering_loaded()

    def _relogin_after_recovery(self) -> bool:
        try:
            self._perform_login()
            self._BetamapolaController__ensure_sport_offering_loaded()
            return True
        except Exception as e:
            self.logger.error(f"Re-login after recovery failed: {e}")
            return False

    def _refresh_session_before_wager(self):
        if self._force_wager_relogin:
            self.logger.info("Wager session flagged invalid; performing full login before placement")
            self._perform_login()
            self._BetamapolaController__ensure_sport_offering_loaded()
            return
        if not self._is_session_valid():
            self.logger.info("Session invalid before wager; re-login")
            self._perform_login()
            self._BetamapolaController__ensure_sport_offering_loaded()

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
            if account_fields and account_fields[0].is_displayed():
                return False
            customer_fields = self.driver.find_elements(By.CSS_SELECTOR, "#CustomerID, #customerID")
            if customer_fields and customer_fields[0].is_displayed():
                return False
            return self._sport_games_present() or bool(self._fetch_game_lines_via_api())
        except Exception:
            return False
