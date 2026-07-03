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

from utils.config import PROXY1, PROXY2, TELEGRAM, ZENROWS_API_KEY, is_active_arb_pair
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import parse_to_mysql_datetime, parse_odds, currency_to_float, send_telegram_alert, send_monitoring_alert, send_testing_alert, is_game_pregame, debug_filepath, prune_debug_files, get_debug_dir, arb_live_odds_acceptable, teams_same, resolve_ticosports_spread_lines, spread_values_match
from utils.arb_placement import get_arbitrage_for_placement, arb_leg_for_book
from utils.ticosports_wager import click_line_via_angular
from utils.team_registry import standard_team_name
from utils.bet_placement import (
    REAL_MONEY_BETTING_PAUSED_MSG,
    block_real_money_bet,
    finalize_confirmed_bet,
    capture_bet_screenshot_for_alert,
    format_bet_failure_reason,
    maybe_notify_partial_arb_exposure,
    should_defer_for_sequential_first_leg,
    should_notify_failed_bet,
    should_pause_first_leg_for_exposure,
    odds_tolerance_for_placement,
    should_skip_spread_arb_for_placement,
)
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.betting_watchdog import (
    BettingLoopWatchdog,
    OddsScanHealthWatchdog,
    SessionUnauthorizedError,
)
from utils.stake_sizing import (
    BaseAmountStake,
    base_amount_stake_from_odds,
    format_base_amount_stake,
    stake_matches_verification_amount,
)
from utils.stake_entry import fill_betslip_stake_input
from utils.odds_watch import persist_moneyline_games
from utils.timing import time_it
from utils.chrome_temp import cleanup_stale_temp_dirs, handle_init_driver_failure
from cache.arbitrage_cache import ArbitrageCache


class BetWarController:
    # Alternate-period markers in bet slip / My Bets (F5, halves, etc.) — not full-game ML.
    _NON_FULL_GAME_ML_MARKERS = (
        "(1st5)",
        "1st5",
        "1st half",
        "2nd half",
        "first 5",
        "f5 innings",
        "(1h)",
        "(2h)",
        " 1h ",
        " 2h ",
    )
    WAGER_SESSION_EXPIRED_MARKERS = (
        "please log in",
        "session expired",
        "logged out",
    )
    ODDS_WATCH_POLL_SECONDS = float(os.getenv("BETWAR_ODDS_POLL_SEC", "5"))
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("BETWAR_ODDS_FORCE_SCAN_SEC", "5"))
    ODDS_IDLE_POLL_SECONDS = float(os.getenv("BETWAR_ODDS_IDLE_POLL_SEC", "5"))
    ODDS_OBSERVER_SELECTORS = ["#GameLines", "#gamesAccordion", ".btnPSLine", "body"]
    GETLINES_MIN_GAMES = int(os.getenv("BETWAR_GETLINES_MIN_GAMES", "1"))
    GETLINES_HEALTH_WINDOW_SEC = float(os.getenv("BETWAR_GETLINES_HEALTH_SEC", "45"))
    GETLINES_SOFT_RETRIES = int(os.getenv("BETWAR_GETLINES_SOFT_RETRIES", "2"))

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
        self._last_getlines_success_at = 0.0
        self._last_getlines_success_count = 0

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
        from utils.odds_observer import install_mutation_observer
        self.logger.info("Injecting MutationObserver on player game lines (JS)")
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

    def _standardize_team_name(self, raw: str) -> str:
        return standard_team_name(raw, sport=self.sport_name, league=self.league)

    def _odds_text_matches(self, displayed: str, expected) -> bool:
        tolerance = getattr(self, "_odds_tolerance", 0) or 0
        if tolerance > 0 and arb_live_odds_acceptable(expected, displayed, tolerance):
            return True
        disp = self._normalize_us_odds((displayed or "").strip())
        exp = self._normalize_us_odds(expected)
        if disp == exp:
            return True
        raw = (displayed or "").strip()
        if exp in raw or raw == str(expected).strip():
            return True
        # BetWar Player portal often shows favorites as bare "105" without a minus sign.
        try:
            exp_val = int(float(str(expected)))
            num_match = re.search(r"[-+]?\d+", raw.replace("\u2212", "-"))
            if num_match and exp_val < 0:
                disp_val = int(num_match.group(0).lstrip("+"))
                if disp_val == abs(exp_val):
                    return True
        except (TypeError, ValueError):
            pass
        return False

    def _game_rotations(self, game_id: str) -> list[str]:
        return [p.strip() for p in str(game_id).split("-") if p.strip()]

    def _target_rotation(self, game_id: str, team_name: str, team_no: int | None = None) -> str | None:
        rotations = self._game_rotations(game_id)
        if team_no == 1 and rotations:
            return rotations[0]
        if team_no == 2 and len(rotations) > 1:
            return rotations[1]
        return None

    def _rotation_on_board(self, game_id: str) -> bool:
        """True when at least one rotation number for this game is visible in the DOM."""
        rotations = set(self._game_rotations(game_id))
        if not rotations:
            return False
        for span in self.driver.find_elements(By.CSS_SELECTOR, "span.lblRotation"):
            if (span.text or "").strip() in rotations:
                return True
        return False

    def _board_rotation_numbers(self) -> list[str]:
        return [
            (span.text or "").strip()
            for span in self.driver.find_elements(By.CSS_SELECTOR, "span.lblRotation")
            if (span.text or "").strip()
        ]

    def _lookup_side_from_getlines(self, game_id: str, team_name: str):
        """Resolve team_no and BetWar display name from GetLines (no GetSportOffering)."""
        try:
            games = self._fetch_getlines_games()
        except SessionUnauthorizedError:
            raise
        except Exception as e:
            self.logger.warning(f"GetLines lookup during bet failed: {e}")
            return None, None, None

        for game in games:
            if str(game.get("game_id")) != str(game_id):
                continue
            t1 = game.get("team_1") or ""
            t2 = game.get("team_2") or ""
            if self._team_name_matches(t1, team_name):
                return 1, t1, (game.get("moneyline") or {}).get("team_1")
            if self._team_name_matches(t2, team_name):
                return 2, t2, (game.get("moneyline") or {}).get("team_2")
        return None, None, None

    def _save_bet_board_debug(self, game_id: str, tag: str):
        try:
            rots = self._board_rotation_numbers()
            debug_file = debug_filepath(f"debug_betwar_bet_{tag}_{self.sport_name.lower()}")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.warning(
                f"Saved bet board debug: {debug_file} | target={game_id} | "
                f"visible_rotations={rots[:20]}"
            )
        except Exception as e:
            self.logger.warning(f"Could not save bet board debug: {e}")

    def _pick_moneyline_click_target(self, ml_card):
        linekey = (ml_card.get_attribute("data-linekey") or "").strip()
        if linekey:
            if not self._linekey_is_full_game_moneyline(linekey):
                return None
            return ml_card
        if ml_card.get_attribute("onclick"):
            try:
                ancestor = ml_card.find_element(
                    By.XPATH, "./ancestor-or-self::*[@data-linekey][1]"
                )
                lk = (ancestor.get_attribute("data-linekey") or "").strip()
                if lk and not self._linekey_is_full_game_moneyline(lk):
                    return None
            except Exception:
                pass
            return ml_card
        try:
            ancestor = ml_card.find_element(
                By.XPATH, "./ancestor-or-self::*[@data-linekey][1]"
            )
            lk = (ancestor.get_attribute("data-linekey") or "").strip()
            if lk and self._linekey_is_full_game_moneyline(lk):
                return ancestor
        except Exception:
            pass
        try:
            onclick_parent = ml_card.find_element(
                By.XPATH, "./ancestor-or-self::*[@onclick][1]"
            )
            lk = self._extract_data_linekey(onclick_parent)
            if lk and not self._linekey_is_full_game_moneyline(lk):
                return None
            return onclick_parent
        except Exception:
            return None

    @staticmethod
    def _linekey_team_segment(linekey: str) -> int | None:
        parts = (linekey or "").split("-")
        if len(parts) >= 3 and parts[2] in ("1", "2"):
            return int(parts[2])
        return None

    @staticmethod
    def _linekey_is_full_game_moneyline(linekey: str) -> bool:
        """Full-game ML keys use {GameNum}-1-{team}-1-{period}; F5 uses -8- in segment 2."""
        parts = (linekey or "").split("-")
        return len(parts) >= 5 and parts[1] == "1" and parts[3] == "1"

    @staticmethod
    def _linekey_is_full_game_spread(linekey: str) -> bool:
        """Full-game spread keys use {GameNum}-1-{team}-2-{period}; alt periods use -8- etc."""
        parts = (linekey or "").split("-")
        return len(parts) >= 5 and parts[1] == "1" and parts[3] == "2"

    def _element_is_full_game_moneyline(self, elem) -> bool:
        """True only when the DOM target is a full-game moneyline (not F5 / half / alt period)."""
        if not elem:
            return False
        linekey = self._extract_data_linekey(elem)
        if linekey:
            return self._linekey_is_full_game_moneyline(linekey)
        elem_id = (elem.get_attribute("id") or "").strip()
        match = re.match(r"M(\d)_(\d+)_(\d+)", elem_id, re.I)
        if match:
            return int(match.group(3)) == 0
        return False

    @classmethod
    def _text_indicates_non_full_game_ml(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._NON_FULL_GAME_ML_MARKERS)

    def _assert_betslip_is_full_game_moneyline(self, team_name: str) -> None:
        """Abort before wagering if the slip shows an alternate-period market."""
        slip = self._betslip_text()
        if not slip:
            return
        if self._text_indicates_non_full_game_ml(slip):
            raise Exception(
                f"Bet slip shows alternate-period market (not full-game ML) for {team_name}: "
                f"{slip[:250]}"
            )

    def _extract_data_linekey(self, elem):
        if not elem:
            return None
        lk = (elem.get_attribute("data-linekey") or "").strip()
        if lk:
            return lk
        try:
            for child in elem.find_elements(By.CSS_SELECTOR, "[data-linekey]"):
                lk = (child.get_attribute("data-linekey") or "").strip()
                if lk:
                    return lk
        except Exception:
            pass
        try:
            parent = elem.find_element(By.XPATH, "./ancestor-or-self::*[@data-linekey][1]")
            return (parent.get_attribute("data-linekey") or "").strip() or None
        except Exception:
            return None

    def _parse_game_line_from_linekey(self, linekey: str, rotations: list | None = None):
        parts = (linekey or "").split("-")
        if not parts or not str(parts[0]).isdigit():
            return None, None
        rot1 = rotations[0] if rotations and len(rotations) > 0 else ""
        rot2 = rotations[1] if rotations and len(rotations) > 1 else ""
        team_no = self._linekey_team_segment(linekey)
        game_line = {
            "GameNum": int(parts[0]),
            "PeriodNumber": int(parts[-1]) if str(parts[-1]).isdigit() else 0,
            "Team1RotNum": rot1,
            "Team2RotNum": rot2,
            "LineKey": linekey,
        }
        return game_line, team_no

    def _invoke_add_line_to_bet_slip(self, elem) -> bool:
        """Modern BetWar portal adds picks via global addLineToBetSlip(el)."""
        self._open_bet_slip_tab()
        self._ensure_betslip_expanded()
        try:
            return bool(self.driver.execute_script("""
                var el = arguments[0];
                if (!el) return false;
                if (typeof addLineToBetSlip === 'function') {
                    addLineToBetSlip(el);
                    return true;
                }
                if (typeof el.onclick === 'function') {
                    el.onclick.call(el);
                    return true;
                }
                el.click();
                return true;
            """, elem))
        except Exception as e:
            self.logger.warning(f"addLineToBetSlip invoke failed: {e}")
            return False

    def _find_ml_element_by_linekey(self, game_num, team_no: int):
        if not game_num or team_no not in (1, 2):
            return None
        needle = f"{game_num}-1-{team_no}-1-"
        for elem in self.driver.find_elements(
            By.CSS_SELECTOR, "div.btnMLLine[data-linekey], div.gc-line.btnMLLine[data-linekey]"
        ):
            lk = (elem.get_attribute("data-linekey") or "").strip()
            if lk.startswith(str(game_num)) and needle in lk:
                return elem
        return None

    def _find_spread_element_by_linekey(self, game_num, team_no: int):
        if not game_num or team_no not in (1, 2):
            return None
        needle = f"{game_num}-1-{team_no}-2-"
        for elem in self.driver.find_elements(
            By.CSS_SELECTOR,
            "div.btnPSLine[data-linekey], div.gc-line.btnPSLine[data-linekey], "
            ".btnPSLine.modern-bet-card[data-linekey]",
        ):
            lk = (elem.get_attribute("data-linekey") or "").strip()
            if lk.startswith(str(game_num)) and needle in lk:
                return elem
        return None

    def _element_is_full_game_spread(self, elem) -> bool:
        if not elem:
            return False
        linekey = self._extract_data_linekey(elem)
        if linekey:
            return self._linekey_is_full_game_spread(linekey)
        elem_id = (elem.get_attribute("id") or "").strip()
        match = re.match(r"S(\d)_(\d+)_(\d+)", elem_id, re.I)
        if match:
            return int(match.group(3)) == 0
        return False

    def _moneyline_cards_for_rotation_row(self, rot_span):
        team_row = rot_span.find_element(
            By.XPATH,
            "./ancestor::div[contains(@class,'row')][.//div[contains(@class,'divLineContainer')]][1]",
        )
        return team_row.find_elements(
            By.CSS_SELECTOR,
            "div.btnMLLine, div.gc-line.btnMLLine, div.divMLLine .gc-line",
        )

    def _spread_cards_for_rotation_row(self, rot_span):
        team_row = rot_span.find_element(
            By.XPATH,
            "./ancestor::div[contains(@class,'row')][.//span[contains(@class,'lblRotation')]][1]",
        )
        return team_row.find_elements(
            By.CSS_SELECTOR,
            "div.btnPSLine[data-linekey], div.gc-line.btnPSLine[data-linekey], "
            ".btnPSLine.modern-bet-card[data-linekey]",
        )

    def _find_moneyline_by_rotation(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd,
        team_no: int | None = None,
    ):
        """Find moneyline by rotation + team row; prefer odds match but accept team row ML when tolerance applies."""
        rotations = set(self._game_rotations(game_id))
        if not rotations:
            return None

        target_rot = self._target_rotation(game_id, team_name, team_no)
        fallback = None

        for rot_span in self.driver.find_elements(By.CSS_SELECTOR, "span.lblRotation"):
            rot_text = (rot_span.text or "").strip()
            if rot_text not in rotations:
                continue
            if target_rot and rot_text != target_rot:
                continue

            try:
                team_block = rot_span.find_element(
                    By.XPATH, "./ancestor::div[contains(@class,'divGameTeam')][1]"
                )
                team_label = team_block.find_element(By.CSS_SELECTOR, "span.lblTeamName")
                label_text = (team_label.text or "").strip()
            except Exception:
                continue

            if (
                target_rot
                and rot_text == target_rot
                and team_no in (1, 2)
            ):
                pass  # rotation + team_no is enough (e.g. TOR Blue Jays vs Toronto Blue Jays)
            elif not self._team_name_matches(label_text, team_name):
                continue

            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", rot_span
            )
            time.sleep(0.3)

            for ml_card in self._moneyline_cards_for_rotation_row(rot_span):
                linekey = (ml_card.get_attribute("data-linekey") or "").strip()
                if linekey and not self._linekey_is_full_game_moneyline(linekey):
                    continue
                if team_no in (1, 2):
                    seg = self._linekey_team_segment(linekey)
                    if seg is not None and seg != team_no:
                        continue
                txt = (ml_card.text or "").strip()
                target = self._pick_moneyline_click_target(ml_card)
                if target and self._odds_text_matches(txt, moneyline_odd):
                    return target
                if fallback is None and target and (linekey or ml_card.get_attribute("onclick")):
                    fallback = target

            if fallback is not None and (
                getattr(self, "_odds_tolerance", 0) or not moneyline_odd
            ):
                self.logger.info(
                    f"Using rotation-row moneyline for {label_text} @ {rot_text} "
                    f"(expected {moneyline_odd}, tolerance active)"
                )
                return fallback

        return None

    def _find_spread_by_rotation(
        self,
        game_id: str,
        team_name: str,
        wager_odds,
        team_no: int | None = None,
        spread_line: float | None = None,
    ):
        """Find full-game spread/run-line by rotation row (modern BetWar portal layout)."""
        rotations = set(self._game_rotations(game_id))
        if not rotations:
            return None

        target_rot = self._target_rotation(game_id, team_name, team_no)
        fallback = None

        for rot_span in self.driver.find_elements(By.CSS_SELECTOR, "span.lblRotation"):
            rot_text = (rot_span.text or "").strip()
            if rot_text not in rotations:
                continue
            if target_rot and rot_text != target_rot:
                continue

            try:
                team_block = rot_span.find_element(
                    By.XPATH,
                    "./ancestor::div[contains(@class,'row')][.//span[contains(@class,'lblRotation')]][1]",
                )
                team_label = team_block.find_element(By.CSS_SELECTOR, "span.lblTeamName")
                label_text = (team_label.text or "").strip()
            except Exception:
                label_text = ""

            if target_rot and rot_text == target_rot and team_no in (1, 2):
                pass
            elif label_text and not self._team_name_matches(label_text, team_name):
                continue

            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", rot_span
            )
            time.sleep(0.3)

            for ps_card in self._spread_cards_for_rotation_row(rot_span):
                linekey = (ps_card.get_attribute("data-linekey") or "").strip()
                if linekey and not self._linekey_is_full_game_spread(linekey):
                    continue
                if team_no in (1, 2):
                    seg = self._linekey_team_segment(linekey)
                    if seg is not None and seg != team_no:
                        continue
                txt = (ps_card.text or ps_card.get_attribute("innerText") or "").strip()
                spread, odds = self._parse_betwar_ps_line_text(txt)
                if spread_line is not None and spread is not None:
                    if not spread_values_match(spread, spread_line):
                        continue
                if odds and self._odds_text_matches(odds, wager_odds):
                    return ps_card
                if spread_line is not None and spread is not None:
                    if spread_values_match(spread, spread_line):
                        self.logger.info(
                            f"Using spread line {spread} @ {odds} (arb odds {wager_odds})"
                        )
                        return ps_card
                if fallback is None and (linekey or ps_card.get_attribute("onclick")):
                    fallback = ps_card

            if fallback is not None and (
                getattr(self, "_odds_tolerance", 0) or spread_line is not None
            ):
                self.logger.info(
                    f"Using rotation-row spread for {label_text or team_name} @ {rot_text} "
                    f"(expected {wager_odds}, tolerance active)"
                )
                return fallback

        return None

    def _find_spread_by_linekey_first(
        self,
        game_id: str,
        team_name: str,
        wager_odds,
        team_no: int | None = None,
        spread_line: float | None = None,
    ):
        """Prefer exact full-game spread linekey match when GameNum is known."""
        try:
            api_gl, api_team_no = None, None
            try:
                api_gl, api_team_no = self._lookup_game_line_from_api(game_id, team_name)
            except SessionUnauthorizedError:
                pass
            except Exception as e:
                self.logger.debug(f"GetSportOffering spread linekey lookup skipped: {e}")

            rots = self._game_rotations(game_id)
            game_num = (api_gl or {}).get("GameNum")
            if game_num is None and len(rots) >= 2:
                game_num = self._resolve_game_num_from_dom_rotations(rots[0], rots[1])
            tn = team_no if team_no in (1, 2) else api_team_no
            if game_num is None or tn not in (1, 2):
                return None
            keyed = self._find_spread_element_by_linekey(game_num, tn)
            if not keyed:
                return None
            txt = (keyed.text or keyed.get_attribute("innerText") or "").strip()
            spread, odds = self._parse_betwar_ps_line_text(txt)
            if spread_line is not None and spread is not None:
                if not spread_values_match(spread, spread_line):
                    return None
            tol = getattr(self, "_odds_tolerance", 0) or 0
            if odds and (self._odds_text_matches(odds, wager_odds) or tol > 0 or spread_line is not None):
                self.logger.info(
                    f"Full-game spread via linekey {self._extract_data_linekey(keyed)} "
                    f"for {team_name} @ {wager_odds}"
                )
                return keyed
        except Exception as e:
            self.logger.debug(f"Linekey-first spread lookup failed: {e}")
        return None

    @staticmethod
    def _team_name_matches(candidate: str, expected: str) -> bool:
        if not (candidate or "").strip() or not (expected or "").strip():
            return False
        return teams_same(candidate, expected)

    def _lookup_game_line_from_api(self, game_id: str, team_name: str):
        """Resolve a live API game line by rotation game_id + team name."""
        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        if len(rotations) < 2:
            return None, None

        rot1, rot2 = rotations[0], rotations[1]
        try:
            api_lines = self._fetch_game_lines_via_api(
                raise_session_error=not self._getlines_recently_healthy()
            )
        except SessionUnauthorizedError:
            if self._getlines_recently_healthy():
                api_lines = []
            else:
                raise
        if not api_lines:
            if self._page_has_login_required_marker():
                raise SessionUnauthorizedError("GetSportOffering returned no lines (login required)")
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

    def _minimal_game_line_from_rotations(self, rotations: list) -> dict:
        rot1 = rotations[0] if len(rotations) > 0 else ""
        rot2 = rotations[1] if len(rotations) > 1 else ""
        return {
            "GameNum": None,
            "PeriodNumber": 0,
            "Team1RotNum": rot1,
            "Team2RotNum": rot2,
        }

    def _fallback_game_line_from_rotations(self, game_id: str) -> dict | None:
        """Build a minimal GameLine when GetSportOffering is unavailable."""
        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        if len(rotations) < 2:
            return None
        game_line = self._minimal_game_line_from_rotations(rotations)
        game_num = self._resolve_game_num_from_dom_rotations(rotations[0], rotations[1])
        if game_num is not None:
            game_line["GameNum"] = game_num
        return game_line

    def _resolve_game_num_from_dom_rotations(self, rot1: str, rot2: str) -> int | None:
        """Parse GameNum from a full-game linekey on the rotation row in the DOM."""
        target_rots = {str(rot1), str(rot2)}
        try:
            for rot_span in self.driver.find_elements(By.CSS_SELECTOR, "span.lblRotation"):
                rot_text = (rot_span.text or "").strip()
                if rot_text not in target_rots:
                    continue
                for ml_card in self._moneyline_cards_for_rotation_row(rot_span):
                    linekey = self._extract_data_linekey(ml_card)
                    if not linekey or not self._linekey_is_full_game_moneyline(linekey):
                        continue
                    parts = linekey.split("-")
                    if parts and str(parts[0]).isdigit():
                        return int(parts[0])
                for ps_card in self._spread_cards_for_rotation_row(rot_span):
                    linekey = self._extract_data_linekey(ps_card)
                    if not linekey or not self._linekey_is_full_game_spread(linekey):
                        continue
                    parts = linekey.split("-")
                    if parts and str(parts[0]).isdigit():
                        return int(parts[0])
        except Exception:
            pass
        return None

    def _parse_game_line_from_button(self, elem, game_id: str):
        """Extract GameNum/team_no from portal moneyline element (linekey or legacy id)."""
        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        linekey = self._extract_data_linekey(elem)
        if linekey and not self._linekey_is_full_game_moneyline(linekey):
            linekey = None
        if linekey:
            game_line, team_no = self._parse_game_line_from_linekey(linekey, rotations)
            if game_line:
                return game_line, team_no

        elem_id = (elem.get_attribute("id") or "").strip()
        match = re.match(r"M(\d)_(\d+)_(\d+)", elem_id, re.I)
        if match:
            team_no = int(match.group(1))
            return {
                "GameNum": int(match.group(2)),
                "PeriodNumber": int(match.group(3)),
                "Team1RotNum": rotations[0] if len(rotations) > 0 else "",
                "Team2RotNum": rotations[1] if len(rotations) > 1 else "",
            }, team_no
        return self._minimal_game_line_from_rotations(rotations), None

    def _parse_game_line_from_spread_button(self, elem, game_id: str):
        """Extract GameNum/team_no from portal spread element (linekey or legacy id)."""
        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        linekey = self._extract_data_linekey(elem)
        if linekey and not self._linekey_is_full_game_spread(linekey):
            linekey = None
        if linekey:
            game_line, team_no = self._parse_game_line_from_linekey(linekey, rotations)
            if game_line:
                return game_line, team_no

        elem_id = (elem.get_attribute("id") or "").strip()
        match = re.match(r"S(\d)_(\d+)_(\d+)", elem_id, re.I)
        if match:
            team_no = int(match.group(1))
            return {
                "GameNum": int(match.group(2)),
                "PeriodNumber": int(match.group(3)),
                "Team1RotNum": rotations[0] if len(rotations) > 0 else "",
                "Team2RotNum": rotations[1] if len(rotations) > 1 else "",
            }, team_no
        return self._minimal_game_line_from_rotations(rotations), None

    def _resolve_game_line_for_bet(
        self, game_id: str, team_name: str, moneyline_elem, team_no: int | None = None,
    ):
        """Build a GameLine dict with GameNum for bet-slip handlers."""
        rotations = [p.strip() for p in str(game_id).split("-") if p.strip()]
        game_line = None
        parsed_team_no = None

        if moneyline_elem and self._element_is_full_game_moneyline(moneyline_elem):
            game_line, parsed_team_no = self._parse_game_line_from_button(
                moneyline_elem, game_id
            )
            if team_no is None:
                team_no = parsed_team_no
            if game_line and game_line.get("GameNum") is not None:
                return game_line, team_no
        elif moneyline_elem:
            lk = self._extract_data_linekey(moneyline_elem)
            self.logger.warning(
                f"Ignoring non-full-game moneyline element (linekey={lk}) for {team_name}; "
                "resolving full-game line via API/linekey lookup"
            )

        try:
            api_gl, api_team_no = self._lookup_game_line_from_api(game_id, team_name)
            if api_gl and api_gl.get("GameNum") is not None:
                if team_no is None:
                    team_no = api_team_no
                return api_gl, team_no
        except SessionUnauthorizedError:
            raise
        except Exception as e:
            self.logger.warning(f"GetSportOffering game-line lookup failed: {e}")

        if game_line and game_line.get("GameNum") and team_no in (1, 2):
            keyed_elem = self._find_ml_element_by_linekey(game_line["GameNum"], team_no)
            if keyed_elem:
                lk = self._extract_data_linekey(keyed_elem)
                gl, tn = self._parse_game_line_from_linekey(lk, rotations)
                if gl:
                    if team_no is None:
                        team_no = tn
                    return gl, team_no

        if game_line:
            return game_line, team_no
        return self._minimal_game_line_from_rotations(rotations), team_no

    def _find_moneyline_by_linekey_first(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd,
        team_no: int | None = None,
    ):
        """Prefer exact full-game linekey match (safest for ML placement)."""
        try:
            api_gl, api_team_no = self._lookup_game_line_from_api(game_id, team_name)
            if not api_gl or api_gl.get("GameNum") is None:
                return None
            tn = team_no if team_no in (1, 2) else api_team_no
            if tn not in (1, 2):
                return None
            keyed = self._find_ml_element_by_linekey(api_gl["GameNum"], tn)
            if not keyed:
                return None
            txt = (keyed.text or "").strip()
            tol = getattr(self, "_odds_tolerance", 0) or 0
            if self._odds_text_matches(txt, moneyline_odd) or tol > 0:
                self.logger.info(
                    f"Full-game ML via linekey {self._extract_data_linekey(keyed)} "
                    f"for {team_name} @ {moneyline_odd}"
                )
                return keyed
        except SessionUnauthorizedError:
            raise
        except Exception as e:
            self.logger.debug(f"Linekey-first moneyline lookup failed: {e}")
        return None

    def _find_moneyline_on_board(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd,
        team_no: int | None = None,
    ):
        """Locate a clickable full-game moneyline on the loaded Player portal DOM."""
        elem = self._find_moneyline_by_linekey_first(
            game_id, team_name, moneyline_odd, team_no=team_no
        )
        if elem:
            return elem
        elem = self._find_moneyline_by_rotation(
            game_id, team_name, moneyline_odd, team_no=team_no
        )
        if elem and self._element_is_full_game_moneyline(elem):
            return elem
        elem = self._find_player_moneyline_element(game_id, team_name, moneyline_odd)
        if elem and self._element_is_full_game_moneyline(elem):
            return elem
        elem = self._find_moneyline_element(
            game_id, team_name, moneyline_odd, team_no=team_no
        )
        if elem and self._element_is_full_game_moneyline(elem):
            return elem
        return None

    def _infer_team_no(self, team_name: str, team_1: str = None, team_2: str = None) -> int | None:
        if team_1 and self._team_name_matches(team_1, team_name):
            return 1
        if team_2 and self._team_name_matches(team_2, team_name):
            return 2
        return None

    def _ensure_bet_board_ready(self, game_id: str | None = None) -> bool:
        """Ensure lines are visible for clicking; reload when target rotations are missing."""
        if (
            game_id
            and self._is_on_sport_page_with_games()
            and self._rotation_on_board(game_id)
        ):
            self.logger.info(
                f"Bet board loaded and target game {game_id} visible; "
                "skipping full sport navigation"
            )
            self._ensure_straights_tab()
            return True

        if game_id and self._is_on_sport_page_with_games():
            visible = self._board_rotation_numbers()
            self.logger.info(
                f"Target rotations {game_id} not on board (visible: {visible[:15]}); "
                "reloading sport offering"
            )
        return self.__ensure_sport_offering_loaded()

    def _scroll_board_until_rotation(self, game_id: str, max_scrolls: int = 10) -> bool:
        """Scroll the lines panel until target rotations render (late games are off-screen)."""
        if self._rotation_on_board(game_id):
            return True
        for _ in range(max_scrolls):
            try:
                self.driver.execute_script("window.scrollBy(0, 900);")
            except Exception:
                pass
            time.sleep(0.4)
            if self._rotation_on_board(game_id):
                return True
        return self._rotation_on_board(game_id)

    def _refresh_bet_board_for_game(self, game_id: str) -> bool:
        """Try progressively stronger board refreshes until target rotations appear."""
        if self._rotation_on_board(game_id):
            return True

        self.logger.info(f"Soft-refreshing bet board for missing game {game_id}")
        self._soft_refresh_lines_context()
        time.sleep(1.0)
        if self._rotation_on_board(game_id):
            return True

        self.logger.info(f"Scrolling bet board for missing game {game_id}")
        if self._scroll_board_until_rotation(game_id):
            return True

        self.logger.info(f"Full sport navigation for missing game {game_id}")
        if not self.__ensure_sport_offering_loaded():
            return False
        time.sleep(0.5)
        if self._rotation_on_board(game_id):
            return True
        return self._scroll_board_until_rotation(game_id)

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

    def _open_bet_slip_tab(self):
        """Switch sidebar from My Bets back to Bet Slip (required before addLineToBetSlip)."""
        try:
            tab = self.driver.find_element(By.CSS_SELECTOR, "#pillBetslipTab")
            selected = (tab.get_attribute("aria-selected") or "").lower() == "true"
            if not selected:
                self.driver.execute_script("arguments[0].click();", tab)
                time.sleep(0.4)
            pane = self.driver.find_element(By.CSS_SELECTOR, "#pills-betslip")
            pane_class = pane.get_attribute("class") or ""
            if "active" not in pane_class and "show" not in pane_class:
                self.driver.execute_script(
                    "arguments[0].classList.add('show', 'active');", pane
                )
        except Exception as e:
            self.logger.warning(f"Could not open Bet Slip tab: {e}")

    def _clear_bet_slip(self):
        """Remove any stale picks so a new arb leg starts from an empty slip."""
        try:
            self._open_bet_slip_tab()
            removed = self.driver.execute_script("""
                var removed = 0;
                var root = document.getElementById('pills-betslip') || document.getElementById('div-betSlip');
                if (!root) return 0;
                root.querySelectorAll('.btnBSRemove, [onclick*="removeWager"]').forEach(function(btn) {
                    try { btn.click(); removed++; } catch (e) {}
                });
                return removed;
            """)
            if removed:
                self.logger.info(f"Cleared {removed} existing pick(s) from bet slip")
                time.sleep(0.5)
        except Exception as e:
            self.logger.warning(f"Could not clear bet slip: {e}")

    def _prepare_bet_slip_for_wager(self):
        """Bet slip must be expanded and on the Bet Slip tab (not My Bets) before adding a line."""
        self._open_bet_slip_tab()
        self._ensure_betslip_expanded()
        self._clear_bet_slip()

    def _betslip_text(self) -> str:
        for selector in ("#pills-betslip", "#div-betSlip", "#betSlipDiv", "#div-betSlipCart"):
            try:
                text = (self.driver.find_element(By.CSS_SELECTOR, selector).text or "").strip()
                if text:
                    return text
            except Exception:
                continue
        return ""

    def _betslip_stake_inputs_visible(self) -> bool:
        from utils.stake_entry import (
            DEFAULT_RISK_SELECTORS,
            DEFAULT_WIN_SELECTORS,
            _find_stake_input,
        )

        for scope in ("#divBetSlip", "#pills-betslip", "#div-betSlip"):
            if _find_stake_input(self.driver, DEFAULT_RISK_SELECTORS, scope):
                return True
            if _find_stake_input(self.driver, DEFAULT_WIN_SELECTORS, scope):
                return True
        return False

    def _betslip_has_team(self, team_name: str) -> bool:
        slip = self._betslip_text()
        slip_l = slip.lower()
        if not slip or "bet slip is empty" in slip_l or "no bets" in slip_l:
            return False
        if team_name.lower() in slip_l:
            return True
        last_word = team_name.strip().split()[-1].lower() if team_name.strip() else ""
        if last_word and last_word in slip_l:
            return True
        return False

    def _wait_for_betslip_team(
        self, team_name: str, timeout: int = 8, require_stake_inputs: bool = False
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._betslip_has_team(team_name):
                if not require_stake_inputs or self._betslip_stake_inputs_visible():
                    return True
            time.sleep(0.4)
        return False

    def _ensure_betslip_expanded(self):
        """BetWar hides the slip in a Bootstrap collapse panel on smaller viewports."""
        self._open_bet_slip_tab()
        try:
            slip = self.driver.find_element(By.CSS_SELECTOR, "#div-betSlip")
            slip_class = slip.get_attribute("class") or ""
            if "collapse" in slip_class and "show" not in slip_class:
                for selector in ("#btnBetSlipCart", "[data-target='#div-betSlip']", ".btnBetSlipCart"):
                    try:
                        btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                        if btn.is_displayed():
                            self.driver.execute_script("arguments[0].click();", btn)
                            time.sleep(0.5)
                            break
                    except Exception:
                        continue
                self.driver.execute_script(
                    "var el = document.getElementById('div-betSlip');"
                    "if (el) { el.classList.add('show'); el.style.display = 'block'; }"
                )
        except Exception:
            pass

    def _fill_wager_password_if_required(self) -> bool:
        """BetWar requires login password in #txtPassword before Place Bets."""
        self._ensure_betslip_expanded()
        pwd_input = None
        deadline = time.time() + 8
        while time.time() < deadline:
            for inp in self.driver.find_elements(
                By.CSS_SELECTOR,
                "#div-betSlip #txtPassword, #pills-betslip #txtPassword, #txtPassword",
            ):
                if inp.is_displayed() and inp.is_enabled():
                    pwd_input = inp
                    break
            if pwd_input:
                break
            time.sleep(0.3)

        if not pwd_input:
            return True  # field not shown on this account/view

        password = self.password or ""
        if not password:
            self.logger.error("BetWar confirm password required but account password is empty")
            return False

        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", pwd_input
        )
        try:
            pwd_input.click()
            pwd_input.clear()
            pwd_input.send_keys(password)
        except Exception:
            pass

        self.driver.execute_script(
            """
            var el = arguments[0];
            var val = arguments[1];
            el.focus();
            el.value = val;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur', {bubbles: true}));
            """,
            pwd_input,
            password,
        )
        time.sleep(0.3)

        filled_len = len(pwd_input.get_attribute("value") or "")
        if filled_len < len(password):
            self.logger.error(
                f"BetWar confirm password field not populated (len={filled_len})"
            )
            return False

        self.logger.info("Wager password confirmation entered")
        return True

    def _betslip_shows_wager_confirmed(self) -> bool:
        slip = self._betslip_text()
        slip_l = slip.lower()
        if any(
            marker in slip_l
            for marker in (
                "wager(s) confirmed",
                "wagers confirmed",
                "your selections are now active",
            )
        ):
            return True
        # Do not match bare "REFERENCE ID" placeholder; require an actual ticket number.
        return bool(re.search(r"reference\s*id\s*#?\s*\d+", slip_l, re.I))

    def _parse_my_bets_rows(self, text: str) -> list:
        """Parse BetWar pending rows like '2.00 / 2.82' then 'NY Mets ML +141'."""
        rows = []
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        skip = {"description", "risk / win", "my bets", "loading", "loading..."}
        i = 0
        while i < len(lines):
            ln = lines[i]
            if ln.lower() in skip:
                i += 1
                continue
            if re.match(r"^\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?$", ln.replace(",", "")):
                parts = [p.strip() for p in ln.replace(",", "").split("/")]
                risk_raw = parts[0]
                win_raw = parts[1] if len(parts) > 1 else ""
                desc = lines[i + 1] if i + 1 < len(lines) else ""
                if desc.lower() not in skip:
                    rows.append({
                        "risk_raw": risk_raw,
                        "win_raw": win_raw,
                        "description": desc,
                        "risk_win": ln,
                    })
                    i += 2
                    continue
            i += 1
        return rows

    def _my_bets_tab_has_payload(self, text: str) -> bool:
        """True when My Bets finished loading (wager rows or explicit empty state)."""
        text_l = (text or "").lower().strip()
        if not text_l or text_l in ("my bets", "loading", "loading..."):
            return False
        if any(m in text_l for m in ("no pending", "no open", "no wagers", "you have no")):
            return True
        return bool(self._parse_my_bets_rows(text))

    def _my_bets_row_matches_stake(self, row: dict, stake) -> bool:
        try:
            if stake_matches_verification_amount(stake, row["risk_raw"]):
                return True
            win_raw = row.get("win_raw")
            if win_raw and stake_matches_verification_amount(stake, win_raw):
                return True
        except (TypeError, ValueError):
            pass
        return False

    @staticmethod
    def _my_bets_row_matches_team(description: str, team_name: str) -> bool:
        desc = (description or "").strip()
        if not desc or not team_name:
            return False
        if team_name.lower() in desc.lower():
            return True
        if teams_same(desc, team_name):
            return True
        last_word = team_name.strip().split()[-1].lower()
        return bool(last_word and last_word in desc.lower())

    def _my_bets_has_wager(self, team_name: str, stake) -> bool:
        text = self._my_bets_tab_text(timeout=12)
        for row in self._parse_my_bets_rows(text):
            if not self._my_bets_row_matches_team(row.get("description", ""), team_name):
                continue
            if self._my_bets_row_matches_stake(row, stake):
                return True
        return False

    def _my_bets_has_team_wager(self, team_name: str) -> bool:
        text = self._my_bets_tab_text(timeout=12)
        for row in self._parse_my_bets_rows(text):
            if self._my_bets_row_matches_team(row.get("description", ""), team_name):
                return True
        return False

    def _betslip_shows_insufficient_available(self) -> bool:
        return "insufficient available" in self._betslip_text().lower()

    def _prepare_betslip_for_submit(self, stake_plan: BaseAmountStake | None = None) -> None:
        """Settle bet slip UI before Place Bets (reduces transient 'Insufficient available')."""
        self._ensure_betslip_expanded()
        self._accept_line_changes()
        if stake_plan is not None:
            fill_betslip_stake_input(
                self.driver,
                stake_plan,
                self.logger,
                scope_css="#divBetSlip, #pills-betslip",
            )
        self._fill_wager_password_if_required()
        time.sleep(0.6)

    def _submit_place_bets_with_retries(
        self, max_attempts: int = 10, stake_plan: BaseAmountStake | None = None,
    ) -> bool:
        """
        BetWar often shows transient 'Insufficient available' on the first Place Bets click.
        Re-settle the slip and retry until confirmed or a hard rejection.
        """
        last_slip = ""
        for attempt in range(1, max_attempts + 1):
            self._prepare_betslip_for_submit(
                stake_plan=stake_plan if attempt > 1 else None
            )
            if attempt == 1:
                self.logger.info(
                    "Submitting Place Bets (retry if 'Insufficient available' appears)"
                )

            if not self._click_place_bets_button():
                raise Exception("Place Bets button not found")

            time.sleep(1.2)

            if self._betslip_shows_wager_confirmed():
                self.logger.info(f"Wager confirmed in bet slip (attempt {attempt}/{max_attempts})")
                return True

            last_slip = self._betslip_text()
            if self._betslip_shows_insufficient_available():
                self.logger.warning(
                    f"BetWar 'Insufficient available' on attempt {attempt}/{max_attempts}; "
                    "re-settling slip and retrying Place Bets"
                )
                continue

            rejected, reject_msg = self._scan_hard_rejection_ui()
            if rejected:
                raise Exception(reject_msg)

            time.sleep(1.0)
            if self._betslip_shows_wager_confirmed():
                self.logger.info(
                    f"Wager confirmed in bet slip after wait (attempt {attempt}/{max_attempts})"
                )
                return True

        if self._betslip_shows_insufficient_available():
            raise Exception(
                "BetWar Insufficient available persisted after Place Bets retries: "
                f"{last_slip[:200]}"
            )
        return False

    def _click_favorites_mlb(self) -> bool:
        """Load MLB via the Favorites shortcut when visible (faster than full sidebar nav)."""
        if self.sport_name != "MLB":
            return False

        self._ensure_player_portal()

        for fav in self.driver.find_elements(By.CSS_SELECTOR, ".sport-favorites"):
            if (fav.get_attribute("data-is-open") or "").lower() != "true":
                self._portal_click(fav)
                time.sleep(0.5)

        keywords = self._league_keywords()
        for menu in self.driver.find_elements(By.CSS_SELECTOR, ".divSportFavoritesMenu"):
            if "d-none" in (menu.get_attribute("class") or ""):
                continue
            for elem in menu.find_elements(By.CSS_SELECTOR, ".sport-ssl-item"):
                label = (elem.get_attribute("data-sportname") or "").strip().lower()
                spans = elem.find_elements(By.CSS_SELECTOR, "span.lblLeagueName")
                if spans:
                    label = (spans[0].text or "").strip().lower()
                if not label or not any(kw in label for kw in keywords):
                    continue
            self._portal_click(elem)
            self.logger.info("Clicked MLB in Favorites shortcut")
            self._wait_for_postback()
            self._wait_for_loading_hidden()
            self._ensure_straights_tab()
            if self._wait_for_player_game_lines(timeout=15):
                self.logger.info("MLB lines visible after Favorites shortcut")
                return True
            if self._click_game_subleague_item():
                return True
            # Favorites often sets menuItemsSelected even when the period sub-menu is absent.
            try:
                preview = self._fetch_lines_via_getlines_api()
                if preview:
                    self.logger.info(
                        f"GetLines returned {len(preview)} games after Favorites MLB click"
                    )
                    return True
            except Exception as e:
                self.logger.debug(f"GetLines preview after Favorites failed: {e}")
            return False
        return False

    def _scan_hard_rejection_ui(self):
        reject_markers = (
            "error", "rejected", "not accepted", "another user has taken",
            "insufficient funds", "insufficient balance",
            "failed to place", "wager declined", "unable to place",
            "line changed", "odds changed", "session expired", "logged out",
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

    def _click_place_bets_button(self):
        for selector in ("#btnBSSaveWagers", "button.btnBSSaveSelections"):
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed():
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    time.sleep(0.2)
                    submitted = self.driver.execute_script("""
                        if (typeof processWagers === 'function') {
                            processWagers();
                            return 'processWagers';
                        }
                        arguments[0].click();
                        return 'click';
                    """, btn)
                    self.logger.info(f"Place Bets clicked ({submitted})")
                    return True
            except Exception:
                continue

        for btn in self.driver.find_elements(
            By.CSS_SELECTOR, "#div-betSlip button, #pills-betslip button"
        ):
            onclick = (btn.get_attribute("onclick") or "").lower()
            btn_text = (btn.text or "").lower()
            if "processwagers" in onclick or "place bet" in btn_text or "submit" in btn_text:
                if btn.is_displayed():
                    self.driver.execute_script("""
                        if (typeof processWagers === 'function') { processWagers(); }
                        else { arguments[0].click(); }
                    """, btn)
                    self.logger.info("Place Bets clicked (fallback)")
                    return True
        return False

    def _add_moneyline_to_slip(
        self, game_line: dict, team_no: int, team_name: str,
        moneyline_elem=None,
    ) -> bool:
        """Click DOM and/or Angular until the bet slip actually contains the team."""
        self._prepare_bet_slip_for_wager()
        game_num = game_line.get("GameNum")
        if moneyline_elem and not self._element_is_full_game_moneyline(moneyline_elem):
            lk = self._extract_data_linekey(moneyline_elem)
            self.logger.warning(
                f"Rejecting non-full-game moneyline element (linekey={lk}); "
                f"using full-game linekey lookup for GameNum={game_num}"
            )
            moneyline_elem = None

        if game_num and team_no in (1, 2) and not moneyline_elem:
            moneyline_elem = self._find_ml_element_by_linekey(game_num, team_no)
            if moneyline_elem:
                self.logger.info(
                    f"Using full-game linekey element "
                    f"{self._extract_data_linekey(moneyline_elem)}"
                )

        linekey = self._extract_data_linekey(moneyline_elem) if moneyline_elem else None

        if moneyline_elem:
            elem_label = (
                moneyline_elem.get_attribute("id")
                or linekey
                or moneyline_elem.get_attribute("class")
            )
            self.logger.info(f"Moneyline element located: {elem_label}")
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", moneyline_elem
            )
            time.sleep(0.4)

            if linekey or moneyline_elem.get_attribute("onclick"):
                self.logger.info(
                    f"Invoking addLineToBetSlip (linekey={linekey or 'onclick'})"
                )
                if self._invoke_add_line_to_bet_slip(moneyline_elem):
                    if self._wait_for_betslip_team(team_name, timeout=5):
                        return True
                self.logger.warning(
                    f"addLineToBetSlip did not populate bet slip for {linekey or elem_label}"
                )

            self.driver.execute_script("arguments[0].click();", moneyline_elem)
            self.logger.info("Moneyline element clicked")
            if self._wait_for_betslip_team(team_name, timeout=4):
                return True
            self.logger.warning(
                f"DOM click on M{team_no}_{game_num}_0 did not populate bet slip; trying fallbacks"
            )

        if game_num and team_no in (1, 2):
            keyed_elem = self._find_ml_element_by_linekey(game_num, team_no)
            if keyed_elem and keyed_elem != moneyline_elem:
                lk = self._extract_data_linekey(keyed_elem)
                self.logger.info(f"Retrying addLineToBetSlip via linekey match {lk}")
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", keyed_elem
                )
                time.sleep(0.3)
                if self._invoke_add_line_to_bet_slip(keyed_elem):
                    if self._wait_for_betslip_team(team_name, timeout=5):
                        return True

        if game_num is not None and team_no in (1, 2):
            btn = self._wait_for_moneyline_button(game_num, team_no, timeout=3)
            if btn:
                self.driver.execute_script("arguments[0].click();", btn)
                if self._wait_for_betslip_team(team_name, timeout=4):
                    return True

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
                        if (gl.PeriodNumber !== 0 && gl.PeriodNumber !== '0') continue;
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

    def _find_spread_on_board(
        self,
        game_id: str,
        team_name: str,
        wager_odds,
        team_no: int | None = None,
        spread_line: float | None = None,
        game_line: dict | None = None,
    ):
        """Locate a clickable full-game spread/run-line on the BetWar board."""
        elem = self._find_spread_by_linekey_first(
            game_id,
            team_name,
            wager_odds,
            team_no=team_no,
            spread_line=spread_line,
        )
        if elem:
            return elem

        elem = self._find_spread_by_rotation(
            game_id,
            team_name,
            wager_odds,
            team_no=team_no,
            spread_line=spread_line,
        )
        if elem and self._element_is_full_game_spread(elem):
            return elem

        game_num = (game_line or {}).get("GameNum")
        if game_num is not None and team_no in (1, 2):
            keyed = self._find_spread_element_by_linekey(game_num, team_no)
            if keyed:
                txt = (keyed.text or keyed.get_attribute("innerText") or "").strip()
                spread, odds = self._parse_betwar_ps_line_text(txt)
                if spread_line is None or (
                    spread is not None and spread_values_match(spread, spread_line)
                ):
                    if odds and self._odds_text_matches(odds, wager_odds):
                        return keyed
                    if spread_line is not None and spread is not None:
                        return keyed

        for selector_tpl in (
            "button#S{team}_{game}_0",
            "#S{team}_{game}_0",
        ):
            if game_num is None or team_no not in (1, 2):
                break
            selector = selector_tpl.format(team=team_no, game=game_num)
            candidates = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if not candidates:
                continue
            txt = (candidates[0].text or candidates[0].get_attribute("innerText") or "").strip()
            if self._odds_text_matches(txt, wager_odds):
                return candidates[0]
            if spread_line is not None:
                spread, odds = self._parse_betwar_ps_line_text(txt)
                if spread is not None and spread_values_match(spread, spread_line):
                    self.logger.info(
                        f"Using legacy spread button {selector} live {txt} (arb {wager_odds})"
                    )
                    return candidates[0]
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
        linekey = self._extract_data_linekey(spread_elem) if spread_elem else None

        if spread_elem:
            elem_label = (
                spread_elem.get_attribute("id")
                or linekey
                or spread_elem.get_attribute("class")
            )
            self.logger.info(f"Spread element located: {elem_label}")
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", spread_elem
            )
            time.sleep(0.4)
            if linekey or spread_elem.get_attribute("onclick"):
                if self._invoke_add_line_to_bet_slip(spread_elem):
                    if self._wait_for_betslip_team(team_name, timeout=5):
                        return True
            self.driver.execute_script("arguments[0].click();", spread_elem)
            if self._wait_for_betslip_team(team_name, timeout=4):
                return True

        if click_line_via_angular(self.driver, game_line, team_no, "S"):
            if self._wait_for_betslip_team(team_name, timeout=6):
                return True
            self.logger.warning("Angular GameLineAction (spread) did not populate bet slip")

        btn = self._wait_for_spread_button(game_num, team_no, timeout=3)
        if btn:
            self.driver.execute_script("arguments[0].click();", btn)
            if self._wait_for_betslip_team(team_name, timeout=4):
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

        def _is_full_game_elem(elem) -> bool:
            label = (elem.text or "").strip().lower()
            data_val = (elem.get_attribute("data-value") or "").strip()
            if label in ("game", "games", "full game"):
                return True
            if re.search(r"/0,1(?:\b|$)", data_val):
                return True
            return False

        target = None
        for elem in stl_items:
            if _is_full_game_elem(elem):
                target = elem
                break

        if target is None:
            for elem in self.driver.find_elements(By.CSS_SELECTOR, ".sport-stl-item"):
                if _is_full_game_elem(elem):
                    target = elem
                    break

        if not target:
            for elem in stl_items:
                label = (elem.text or "").strip()
                if label:
                    target = elem
                    self.logger.warning(
                        f"Full game period unavailable; loading lines via {label[:40]}"
                    )
                    break

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

    @staticmethod
    def _parse_betwar_spread_handicap(text: str) -> float | None:
        raw = (text or "").strip().replace("½", ".5").replace(" ", "")
        if not raw:
            return None
        if raw in (".5", "+.5"):
            return 0.5
        if raw == "-.5":
            return -0.5
        try:
            spread = float(raw)
            if spread == 0.0:
                return None
            return spread
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_betwar_spread_odds_token(text: str) -> str | None:
        token = (text or "").strip()
        if not token:
            return None
        lowered = token.lower()
        if lowered in ("even", "ev", "pk", "pick", "pickem"):
            return "+100"
        if re.match(r"^[+-]?\d+$", token):
            return token if token.startswith(("+", "-")) else f"+{token}"
        return None

    @staticmethod
    def _parse_betwar_ps_line_text(text: str) -> tuple[float | None, str | None]:
        """Parse BetWar point-spread/run-line text like '+1½\\n-230' or '+1.5 -110'."""
        raw = (text or "").strip()
        if not raw:
            return None, None

        lines = [ln.strip() for ln in raw.replace("\r", "").split("\n") if ln.strip()]
        if len(lines) >= 2:
            spread = BetWarController._parse_betwar_spread_handicap(lines[0])
            odds = BetWarController._parse_betwar_spread_odds_token(lines[1])
            if spread is not None and odds:
                return spread, odds

        normalized = raw.replace("½", ".5").replace(" ", "").replace("\n", "").replace("\r", "")
        match = re.match(
            r"^([+-]?(?:\d+(?:\.\d+)?|\.?\d+))([+-]?\d+|Even|EV|Pk|Pick)$",
            normalized,
            re.I,
        )
        if match:
            spread = BetWarController._parse_betwar_spread_handicap(match.group(1))
            odds = BetWarController._parse_betwar_spread_odds_token(match.group(2))
            if spread is not None and odds:
                return spread, odds

        return None, None

    def _get_side_spread_from_getlines(self, side: dict) -> tuple[float | None, str | None]:
        """Extract run-line handicap + American odds from a GetLines side object."""
        sp_obj = side.get("spread") or {}
        line_txt = (sp_obj.get("line") or "").strip()
        if line_txt:
            spread, odds = self._parse_betwar_ps_line_text(line_txt)
            if spread is not None and odds:
                normalized = self._normalize_ml_odds(odds)
                if normalized is not None:
                    return spread, normalized

        odds_val = (sp_obj.get("odds") or {}).get("OddsValue")
        if odds_val in (None, "", 0):
            return None, None
        # Odds without handicap text are ambiguous (often means RL not posted).
        return None, None

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

    def _api_response_requires_relogin(self, message: str) -> bool:
        msg_l = (message or "").lower()
        return any(
            marker in msg_l
            for marker in (
                "unexpected token '<'",
                "<!doctype",
                "http 401",
                " 401",
                "unauthorized",
                "please log in",
                "session expired",
            )
        )

    def _record_getlines_success(self, count: int):
        self._last_getlines_success_at = time.time()
        self._last_getlines_success_count = count

    def _getlines_recently_healthy(self, min_games: int | None = None) -> bool:
        min_games = self.GETLINES_MIN_GAMES if min_games is None else min_games
        if not self._last_getlines_success_at:
            return False
        age = time.time() - self._last_getlines_success_at
        return (
            age <= self.GETLINES_HEALTH_WINDOW_SEC
            and self._last_getlines_success_count >= min_games
        )

    def _soft_refresh_lines_context(self) -> bool:
        """Refresh the lines board without a full logout/login cycle."""
        try:
            if not self._is_session_valid():
                return False
            self.logger.info("Soft-refreshing BetWar lines context (no full re-login)")
            if self._is_on_sport_page_with_games():
                self._ensure_straights_tab()
                time.sleep(0.8)
                return True
            return bool(self.__ensure_sport_offering_loaded())
        except Exception as e:
            self.logger.warning(f"Soft lines refresh failed: {e}")
            return False

    def _fetch_getlines_games(self) -> list:
        """Fetch games via GetLines with soft retries on empty/partial boards."""
        min_games = self.GETLINES_MIN_GAMES
        max_attempts = self.GETLINES_SOFT_RETRIES + 1
        for attempt in range(1, max_attempts + 1):
            try:
                games = self._fetch_lines_via_getlines_api()
            except SessionUnauthorizedError:
                raise
            if games and len(games) >= min_games:
                self._record_getlines_success(len(games))
                return games
            if games:
                self.logger.warning(
                    f"GetLines partial board ({len(games)} < {min_games} games); "
                    f"retry {attempt}/{max_attempts}"
                )
            elif attempt < max_attempts:
                self.logger.info(
                    f"GetLines empty; soft-refreshing lines context "
                    f"(retry {attempt}/{max_attempts})"
                )
            if attempt < max_attempts and self._soft_refresh_lines_context():
                time.sleep(0.5)
                continue
            if games:
                self.logger.info(
                    f"GetLines partial board accepted on final attempt: {len(games)} games"
                )
                self._record_getlines_success(len(games))
                return games
            break
        return []

    def _recover_odds_session(self, reason: str, recover_driver: bool = False) -> bool:
        self.logger.warning(f"Recovering BetWar session: {reason}")
        self._invalidate_wager_session()
        if recover_driver:
            try:
                self._recover_driver()
            except Exception as e:
                self.logger.error(f"Driver recovery failed: {e}")
                return False
        ok = self._relogin_after_recovery()
        if ok and hasattr(self, "_scan_health"):
            games, src = self._fetch_games_for_odds(allow_dom_fallback=True)
            if games:
                self._scan_health.mark_success(len(games))
        elif hasattr(self, "_scan_health"):
            self._scan_health.mark_failure(reason)
        return ok

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
                        const activeSelectors = [
                            '.sport-ssl-item.active',
                            '.sport-stl-item.active',
                            '.divSportFavoritesMenu .sport-ssl-item',
                            '.sport-ssl-item[data-sportname="MLB"]',
                            '.sport-ssl-item[data-sportname="NBA"]',
                        ];
                        let active = null;
                        for (const sel of activeSelectors) {
                            active = document.querySelector(sel);
                            if (active) break;
                        }
                        if (!active) {
                            const sslItems = document.querySelectorAll('.sport-ssl-item');
                            for (const item of sslItems) {
                                const name = (
                                    item.getAttribute('data-sportname')
                                    || item.textContent
                                    || ''
                                ).trim().toLowerCase();
                                if (name === 'mlb' || name === 'nba' || name.includes('major league')) {
                                    active = item;
                                    break;
                                }
                            }
                        }
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
            err = str(e)
            self.logger.warning(f"GetLines browser API call failed: {e}")
            if self._api_response_requires_relogin(err):
                raise SessionUnauthorizedError(err)
            return []

        if not result or result.get("error"):
            err = str((result or {}).get("error", ""))
            self.logger.warning(f"GetLines API unavailable: {result}")
            if self._api_response_requires_relogin(err):
                raise SessionUnauthorizedError(err or "GetLines API unavailable")
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
            team1 = self._standardize_team_name((s1.get("name") or "").strip())
            team2 = self._standardize_team_name((s2.get("name") or "").strip())
            ml1 = self._get_side_moneyline_odds(s1)
            ml2 = self._get_side_moneyline_odds(s2)
            sp1_line, sp1_odds = self._get_side_spread_from_getlines(s1)
            sp2_line, sp2_odds = self._get_side_spread_from_getlines(s2)

            if not rot1 or not rot2 or not team1 or not team2 or not ml1 or not ml2:
                continue

            normalized_dt = self._parse_betwar_game_datetime(ev.get("dateandtime"))

            spread = {
                "team_1_spread": None, "team_2_spread": None,
                "team_1_odds": None, "team_2_odds": None,
            }
            if sp1_line is not None and sp2_line is not None and sp1_odds and sp2_odds:
                spread = {
                    "team_1_spread": sp1_line,
                    "team_2_spread": sp2_line,
                    "team_1_odds": sp1_odds,
                    "team_2_odds": sp2_odds,
                }

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
                "spread": spread,
                "total": {
                    "over_total": None, "under_total": None,
                    "over_odds": None, "under_odds": None,
                },
            })

        self.logger.info(f"Parsed {len(games)} games from GetLines API")
        return games

    def _parse_player_portal_spread_by_rotation(self, html: str = None) -> dict:
        """Map rotation number -> (handicap, American odds) from modern spread line cards."""
        source = html if html is not None else (self.driver.page_source or "")
        soup = BeautifulSoup(source, "html.parser")
        by_rot = {}
        for rot_el in soup.select("span.lblRotation"):
            rot = rot_el.get_text(strip=True)
            if not rot:
                continue
            row = rot_el.find_parent("div", class_=lambda c: c and "row" in c.split())
            if not row:
                continue
            ps_el = row.select_one(
                "div.btnPSLine[data-linekey], div.gc-line.btnPSLine[data-linekey]"
            )
            if not ps_el:
                continue
            linekey = (ps_el.get("data-linekey") or "").strip()
            if linekey and not self._linekey_is_full_game_spread(linekey):
                continue
            spread, odds = self._parse_betwar_ps_line_text(ps_el.get_text(strip=True))
            if spread is None or not odds:
                continue
            normalized = self._normalize_ml_odds(odds)
            if normalized is not None:
                by_rot[rot] = (spread, normalized)
        return by_rot

    def _enrich_games_with_spreads(self, games: list) -> list:
        """Fill run-line odds from Player portal DOM (GetLines API often omits spread.line)."""
        if not games:
            return games

        try:
            by_rot = self._parse_player_portal_spread_by_rotation()
        except Exception as e:
            self.logger.debug(f"Spread DOM enrichment skipped: {e}")
            return games

        if not by_rot:
            return games

        enriched = 0
        for game in games:
            spread = game.get("spread") or {}
            if spread.get("team_1_odds") is not None:
                continue
            game_id = str(game.get("game_id") or "")
            parts = [p.strip() for p in game_id.split("-", 1)]
            if len(parts) != 2:
                continue
            rot1, rot2 = parts
            r1 = by_rot.get(rot1)
            r2 = by_rot.get(rot2)
            if not r1 or not r2:
                continue
            game["spread"] = {
                "team_1_spread": r1[0],
                "team_2_spread": r2[0],
                "team_1_odds": r1[1],
                "team_2_odds": r2[1],
            }
            enriched += 1

        if enriched:
            self.logger.info(
                f"Enriched {enriched}/{len(games)} games with spread/run-line from Player portal DOM"
            )
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

            r1["team"] = self._standardize_team_name(r1["team"])
            r2["team"] = self._standardize_team_name(r2["team"])

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
        lines_ready = False

        if self._click_favorites_mlb():
            lines_ready = self._wait_for_player_game_lines(timeout=20)
            if lines_ready:
                self.logger.info(f"{self.sport_name} lines loaded via Favorites shortcut")
                self._ensure_odds_mutation_observer()
                return True
            try:
                games = self._fetch_lines_via_getlines_api()
                if games:
                    self.logger.info(
                        f"{self.sport_name} GetLines ready via Favorites ({len(games)} games)"
                    )
                    self._ensure_odds_mutation_observer()
                    return True
            except Exception as e:
                self.logger.debug(f"GetLines after Favorites failed: {e}")
            self.logger.warning(
                f"Favorites MLB navigation did not populate lines; falling back to sidebar"
            )

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

        self._ensure_odds_mutation_observer()
        return lines_ready

    def _fetch_game_lines_via_api(self, raise_session_error: bool = True):
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
        except SessionUnauthorizedError:
            raise
        except Exception as e:
            err = str(e)
            if self._api_response_requires_relogin(err):
                if raise_session_error:
                    self.logger.error(f"Browser-context API call failed: {e}")
                    raise SessionUnauthorizedError(err)
                self.logger.warning(
                    f"GetSportOffering session error (non-fatal while GetLines healthy): {e}"
                )
                return []
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

            team1 = self._standardize_team_name(str(team1).strip())
            team2 = self._standardize_team_name(str(team2).strip())

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
                    "team_1": self._normalize_ml_odds(ml1),
                    "team_2": self._normalize_ml_odds(ml2),
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

    def _fetch_games_for_odds(
        self,
        allow_dom_fallback: bool = False,
        use_sport_offering_fallback: bool = False,
    ):
        """BetWar Player portal: GetLines API is the reliable path (not GetSportOffering)."""
        _ = use_sport_offering_fallback  # legacy arg; BetOnline API does not apply here

        games = self._fetch_getlines_games()
        if games:
            games = self._enrich_games_with_spreads(games)
            return games, "GetLines+Spread"

        if allow_dom_fallback:
            if not self._player_lines_populated():
                self._wait_for_player_game_lines(timeout=15)
            games = self._parse_player_portal_dom(self.driver.page_source)
            if games:
                games = self._filter_games_with_valid_moneylines(games)
                if games:
                    return games, "dom"

        return [], "none"

    def _poll_odds_watch_once(self, force_scan: bool = False, source: str = "watch", **kwargs) -> int:
        if not hasattr(self, "_last_saved_ml"):
            self._last_saved_ml = {}
        fetch_kwargs = {
            "allow_dom_fallback": True,
        }
        try:
            games, src = self._fetch_games_for_odds(**fetch_kwargs)
        except SessionUnauthorizedError as e:
            if self._try_soft_odds_recovery(e):
                try:
                    games, src = self._fetch_games_for_odds(**fetch_kwargs)
                except SessionUnauthorizedError as retry_err:
                    if not self._recover_odds_session(str(retry_err), recover_driver=False):
                        if hasattr(self, "_scan_health"):
                            self._scan_health.mark_failure(str(retry_err))
                        return 0
                    games, src = self._fetch_games_for_odds(allow_dom_fallback=True)
            elif not self._recover_odds_session(str(e), recover_driver=False):
                if hasattr(self, "_scan_health"):
                    self._scan_health.mark_failure(str(e))
                return 0
            else:
                games, src = self._fetch_games_for_odds(allow_dom_fallback=True)

        if games:
            self._consecutive_odds_failures = 0
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_success(len(games))
        else:
            self._consecutive_odds_failures = getattr(self, "_consecutive_odds_failures", 0) + 1
            if hasattr(self, "_scan_health"):
                self._scan_health.mark_failure(f"no lines ({src})")
            if self._consecutive_odds_failures >= 3:
                self.logger.warning(
                    f"BetWar odds empty {self._consecutive_odds_failures} polls; "
                    "attempting driver recovery + re-login"
                )
                self._recover_odds_session(
                    "repeated empty API polls", recover_driver=True
                )
                self._consecutive_odds_failures = 0
                try:
                    games, src = self._fetch_games_for_odds(allow_dom_fallback=True)
                    if games and hasattr(self, "_scan_health"):
                        self._scan_health.mark_success(len(games))
                except SessionUnauthorizedError as e:
                    if hasattr(self, "_scan_health"):
                        self._scan_health.mark_failure(str(e))

        label = f"{source}/{src}" if src != "none" else source
        if not games and force_scan:
            self.logger.warning(f"No {self.sport_name} lines from API/DOM on force scan")
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

    def _try_soft_odds_recovery(self, error: Exception) -> bool:
        """Light refresh before full re-login when GetLines was healthy recently."""
        if not self._getlines_recently_healthy():
            return False
        self.logger.warning(
            f"BetWar session error after recent healthy GetLines ({error}); "
            "trying soft refresh before full re-login"
        )
        return self._soft_refresh_lines_context()

    def _maybe_poll_odds_while_idle(self):
        if not hasattr(self, "_last_odds_force_scan"):
            self._last_odds_force_scan = 0.0
        try:
            self._last_odds_force_scan, processed = self._tick_odds_on_idle(
                self._last_odds_force_scan,
                idle_label="betting-idle",
            )
            if not processed:
                return
        except SessionUnauthorizedError as e:
            if self._try_soft_odds_recovery(e):
                try:
                    self._poll_odds_watch_once(source="betting-idle-soft-retry")
                except Exception as relogin_err:
                    self.logger.warning(f"Idle odds poll after soft refresh failed: {relogin_err}")
                return
            self._recover_odds_session(str(e), recover_driver=True)
            try:
                self._poll_odds_watch_once(source="betting-idle-relogin")
            except Exception as relogin_err:
                self.logger.warning(f"Idle odds poll after re-login failed: {relogin_err}")
        except Exception as e:
            if self._api_response_requires_relogin(str(e)):
                if self._try_soft_odds_recovery(e):
                    try:
                        self._poll_odds_watch_once(source="betting-idle-soft-retry")
                    except Exception as relogin_err:
                        self.logger.warning(
                            f"Idle odds poll after soft refresh failed: {relogin_err}"
                        )
                    return
                self._recover_odds_session(str(e), recover_driver=True)
                try:
                    self._poll_odds_watch_once(source="betting-idle-relogin")
                except Exception as relogin_err:
                    self.logger.warning(f"Idle odds poll after re-login failed: {relogin_err}")
            else:
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
        self._scan_health = OddsScanHealthWatchdog(self.logger)
        self._scan_health.start()
        self._consecutive_odds_failures = 0
        self._cleanup_stale_temp_dirs()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                self._ensure_odds_session()
                self.__ensure_sport_offering_loaded()
                setup_ok = True
                break
            except Exception as e:
                self.logger.error(f"Odds watch setup failed (attempt {attempt}/5): {e}")
                self._recover_driver()
                time.sleep(5)

        if not setup_ok:
            self.logger.error("Could not start BetWar odds watch")
            return

        last_force_scan = 0.0

        try:
            while True:
                watchdog.beat()
                try:
                    current_url = self.driver.current_url
                except Exception as e:
                    self.logger.error(f"Odds watch driver error: {e}")
                    self._recover_driver()
                    if self._relogin_after_recovery():
                        self._ensure_odds_session()
                        self.__ensure_sport_offering_loaded()
                    time.sleep(5)
                    continue

                if "/player/" not in (current_url or "").lower():
                    self.logger.warning(f"Odds watch off player page ({current_url}); recovering")
                    self._recover_driver()
                    if self._relogin_after_recovery():
                        self._ensure_odds_session()
                        self.__ensure_sport_offering_loaded()
                    time.sleep(3)
                    continue

                try:
                    last_force_scan, processed = self._tick_odds_on_idle(
                        last_force_scan, idle_label="watch"
                    )
                except SessionUnauthorizedError as e:
                    self.logger.warning(f"Odds watch poll unauthorized: {e}")
                    self._recover_odds_session(str(e), recover_driver=True)
                    processed = False

                if not processed:
                    time.sleep(poll_interval)

        except KeyboardInterrupt:
            self.logger.info("BetWar odds watch stopped by user")
        except Exception as e:
            self.logger.error(f"Fatal BetWar odds watch error: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            self.logger.info(f"========== Odds Watch ({self.sport_name}) (END) ==========")

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
            self._last_saved_ml = {}
            persist_moneyline_games(
                self.cache,
                self.storage,
                self.logger,
                games,
                self.sport_name,
                self.league,
                self._last_saved_ml,
                source=odds_source,
            )

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

        # Modern layout: team name in divGameTeam, moneyline click target in sibling divLineContainer.
        for team_label in self.driver.find_elements(By.CSS_SELECTOR, "span.lblTeamName"):
            label_text = (team_label.text or "").strip()
            if not self._team_name_matches(label_text, team_name):
                continue

            team_row = team_label.find_element(
                By.XPATH,
                "./ancestor::div[contains(@class,'row')][.//div[contains(@class,'divLineContainer')]][1]",
            )
            team_block = team_label.find_element(
                By.XPATH,
                "./ancestor::div[contains(@class,'divGameTeam')][1]",
            )
            rot_spans = team_block.find_elements(By.CSS_SELECTOR, "span.lblRotation")
            rot_text = (rot_spans[0].text if rot_spans else "").strip()
            if rotations and rot_text and rot_text not in rotations:
                continue

            for ml_card in team_row.find_elements(
                By.CSS_SELECTOR,
                "div.btnMLLine, div.gc-line.btnMLLine, div.divMLLine .gc-line",
            ):
                linekey = (ml_card.get_attribute("data-linekey") or "").strip()
                if linekey and not self._linekey_is_full_game_moneyline(linekey):
                    continue
                if not linekey and not self._element_is_full_game_moneyline(ml_card):
                    continue
                odds_elem = None
                for cand in ml_card.find_elements(By.CSS_SELECTOR, "span.odds, .odds"):
                    txt = (cand.text or "").strip()
                    if self._odds_text_matches(txt, moneyline_odd):
                        odds_elem = cand
                        break
                if not odds_elem:
                    txt = (ml_card.text or "").strip()
                if not self._odds_text_matches(txt, moneyline_odd):
                    continue
                click_target = self._pick_moneyline_click_target(ml_card)
                if click_target:
                    return click_target

        for row in self.driver.find_elements(By.CSS_SELECTOR, "div.divGameTeam"):
            rot_spans = row.find_elements(By.CSS_SELECTOR, "span.lblRotation")
            rot_text = (rot_spans[0].text if rot_spans else "").strip()
            if rotations and rot_text and rot_text not in rotations:
                continue
            name_spans = row.find_elements(By.CSS_SELECTOR, "span.lblTeamName")
            row_team = (name_spans[0].text if name_spans else row.text or "").strip()
            if not self._team_name_matches(row_team, team_name):
                continue

            for odds_elem in row.find_elements(
                By.CSS_SELECTOR, "span.odds, .odds, [class*='odds']"
            ):
                txt = (odds_elem.text or "").strip()
                if not self._odds_text_matches(txt, moneyline_odd):
                    continue
                try:
                    ml_card = odds_elem.find_element(
                        By.XPATH,
                        "./ancestor::div[contains(@class,'btnMLLine') or contains(@class,'gc-line')][1]",
                    )
                    click_target = self._pick_moneyline_click_target(ml_card)
                    if click_target:
                        return click_target
                except Exception:
                    if self._element_is_full_game_moneyline(odds_elem):
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
        period = 0  # Full-game ML only for arb placement.

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

        # Strategy 2: GameNum embedded in full-game M1_/M2_ button id + odds text match
        if not moneyline_elem and game_num is not None:
            for prefix in ("M1_", "M2_"):
                candidates = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    f"button[id^='{prefix}'][id*='_{game_num}_0'], "
                    f"span[id^='{prefix}'][id*='_{game_num}_0']",
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
                elem_id = (cand.get_attribute("id") or "").strip()
                match = re.match(r"M(\d)_(\d+)_(\d+)", elem_id, re.I)
                if match and int(match.group(3)) != 0:
                    continue
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
                    elem_id = (cand.get_attribute("id") or "").strip()
                    match = re.match(r"M(\d)_(\d+)_(\d+)", elem_id, re.I)
                    if match and int(match.group(3)) != 0:
                        continue
                    if not self._element_is_full_game_moneyline(cand):
                        continue
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

    def _has_existing_open_bet(self, team_name: str, team_1: str, team_2: str, stake=None) -> bool:
        if stake is None:
            return False
        return self._my_bets_has_wager(team_name, stake)

    def _message_requires_relogin(self, message: str) -> bool:
        msg_l = (message or "").lower()
        return any(marker in msg_l for marker in self.WAGER_SESSION_EXPIRED_MARKERS) or (
            self._api_response_requires_relogin(message)
        )

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

    def _is_wager_submission_response(self, url: str, body: str) -> bool:
        url_l = (url or "").lower()
        if any(
            probe in url_l
            for probe in ("getwagerpicks", "getsportoffering", "getlines", "getpending")
        ):
            return False
        body_l = (body or "").lstrip().lower()
        if body_l.startswith("<!doctype") or body_l.startswith("<html"):
            return False
        return any(
            marker in url_l
            for marker in ("processwager", "savewager", "placewager", "submitwager", "wagerajx")
        )

    def _open_my_bets_tab(self):
        tab = self.driver.find_element(By.CSS_SELECTOR, "#pillsPendingTab")
        selected = (tab.get_attribute("aria-selected") or "").lower() == "true"
        if not selected:
            self.driver.execute_script("arguments[0].click();", tab)

    def _refresh_my_bets_tab(self):
        """Toggle away from My Bets and back so pending wagers reload after Place Bets."""
        try:
            for selector in ("#pillsBetSlipTab", "#pills-betslip-tab", "#pills-betslip"):
                tabs = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if tabs:
                    self.driver.execute_script("arguments[0].click();", tabs[0])
                    time.sleep(0.4)
                    break
        except Exception:
            pass
        try:
            pending_tab = self.driver.find_element(By.CSS_SELECTOR, "#pillsPendingTab")
            self.driver.execute_script(
                "arguments[0].setAttribute('data-loaded', 'false');", pending_tab
            )
            self.driver.execute_script("arguments[0].click();", pending_tab)
            time.sleep(0.6)
        except Exception:
            self._open_my_bets_tab()

    def _my_bets_tab_text(self, timeout: int = 10) -> str:
        """Load and return visible text from the Player portal My Bets tab."""
        self._open_my_bets_tab()
        deadline = time.time() + timeout
        last_text = ""
        while time.time() < deadline:
            try:
                pending = self.driver.find_element(By.CSS_SELECTOR, "#pills-pending")
                text = (pending.text or "").strip()
                if text:
                    last_text = text
                text_l = text.lower()
                loading = not text or text_l in ("my bets", "loading", "loading...")
                if not loading and self._my_bets_tab_has_payload(text):
                    return text
                tab_loaded = (self.driver.find_element(
                    By.CSS_SELECTOR, "#pillsPendingTab"
                ).get_attribute("data-loaded") or "").lower() == "true"
                if tab_loaded and self._my_bets_tab_has_payload(text):
                    return text
            except Exception:
                pass
            time.sleep(0.5)
        return last_text

    def _verify_open_bet_on_my_bets(
        self,
        team_name: str,
        stake: float = None,
        team_1: str = None,
        team_2: str = None,
    ):
        """
        BetWar Player portal confirmation: verify the wager appears on My Bets.
        The legacy /sports/Api/Betting.asmx endpoints are not available here.
        """
        try:
            text = self._my_bets_tab_text(timeout=12)
            if not text:
                return False, "My Bets tab did not load"
            if not self._my_bets_tab_has_payload(text):
                return False, "My Bets tab did not load wager rows"
            text_l = text.lower()
            self.logger.info(f"My Bets tab preview: {text[:250]}")

            team_found = team_name.lower() in text_l
            if not team_found:
                team_found = teams_same(team_name, text)
            if not team_found:
                last_word = team_name.strip().split()[-1].lower() if team_name.strip() else ""
                team_found = bool(last_word and last_word in text_l)
            if not team_found:
                return False, "Team not found on My Bets tab"

            if stake is not None:
                for row in self._parse_my_bets_rows(text):
                    desc = row.get("description", "")
                    if not self._my_bets_row_matches_team(desc, team_name):
                        continue
                    if self._text_indicates_non_full_game_ml(desc):
                        return False, (
                            f"My Bets shows alternate-period market (not full-game ML): {desc}"
                        )
                    if self._my_bets_row_matches_stake(row, stake):
                        return True, "Open bet confirmed on My Bets tab"
                stake_label = (
                    format_base_amount_stake(stake)
                    if isinstance(stake, BaseAmountStake)
                    else f"${float(stake):.2f}"
                )
                return False, f"Team/stake {stake_label} not found on My Bets tab"

            return True, "Open bet confirmed on My Bets tab"
        except Exception as e:
            return False, str(e)


    def _scan_rejection_ui(self):
        if self._betslip_shows_insufficient_available():
            return True, f"Bet slip rejection: {self._betslip_text()[:300]}"
        return self._scan_hard_rejection_ui()

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
        """
        Confirm BetWar wagers by checking the My Bets tab after Place Bets is clicked.
        """
        deadline = time.time() + timeout
        success_markers = (
            "wager accepted", "bet accepted", "ticket accepted",
            "successfully placed", "your wager has been accepted",
        )

        self.logger.info("Confirming wager via My Bets tab")

        while time.time() < deadline:
            rejected, reject_msg = self._scan_hard_rejection_ui()
            if rejected:
                if self._message_requires_relogin(reject_msg):
                    self._invalidate_wager_session()
                self.logger.error(f"Bet rejected by bookmaker UI: {reject_msg}")
                return False, reject_msg

            confirmed, message = self._verify_open_bet_on_my_bets(
                team_name, stake=stake, team_1=team_1, team_2=team_2
            )
            if confirmed:
                return True, message

            page_l = (self.driver.page_source or "").lower()
            for marker in success_markers:
                if marker in page_l:
                    confirmed, message = self._verify_open_bet_on_my_bets(
                        team_name, stake=stake, team_1=team_1, team_2=team_2
                    )
                    if confirmed:
                        return True, message

            for entry in self._get_wager_network_log():
                url = entry.get("url") or ""
                body = entry.get("body") or ""
                if not self._is_wager_submission_response(url, body):
                    continue
                body_l = body.lower()
                if any(m in body_l for m in ("rejected", "declined", "another user")):
                    self.logger.error(f"Wager API rejection ({url}): {body[:500]}")
                    return False, f"Wager API rejected: {url}"

            time.sleep(1)

        self.logger.warning(
            f"Bet not confirmed within {timeout}s; final My Bets retries"
        )
        for attempt in range(1, 4):
            confirmed, message = self._verify_open_bet_on_my_bets(
                team_name, stake=stake, team_1=team_1, team_2=team_2
            )
            if confirmed:
                return True, f"{message} (retry)"
            if attempt < 3:
                time.sleep(5)
        return False, "Bet not confirmed on My Bets tab"

    # --------------------------------------------------------
    # Execute Bet (BetWar Player portal main.aspx + bet slip)
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
        blocked = block_real_money_bet(self.logger, stake, bet_type=bet_type)
        if blocked is not None:
            self._last_bet_error = REAL_MONEY_BETTING_PAUSED_MSG
            return blocked
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)

        try:
            for attempt in range(1, 4):
                try:
                    if attempt > 1:
                        self.logger.info(f"Retrying wager after session recovery (attempt {attempt}/3)")
                    return self._execute_bet_attempt(
                        game_id, team_name, moneyline_odd, stake,
                        team_1=team_1, team_2=team_2,
                        bet_type=bet_type,
                        spread_line=spread_line,
                    )
                except SessionUnauthorizedError as e:
                    if attempt >= 3:
                        raise
                    self.logger.warning(
                        f"Wager blocked by expired session ({e}); recovering and retrying"
                    )
                    if not self._recover_odds_session(str(e), recover_driver=(attempt >= 2)):
                        self._invalidate_wager_session()
                        self.__login()
                        self._ensure_bet_board_ready()
                    continue
                except Exception as e:
                    if attempt >= 3 or not self._message_requires_relogin(str(e)):
                        raise
                    self.logger.warning(
                        f"Wager blocked by session error ({e}); recovering and retrying"
                    )
                    if not self._recover_odds_session(str(e), recover_driver=(attempt >= 2)):
                        self._invalidate_wager_session()
                        self.__login()
                        self._ensure_bet_board_ready()
                    continue
            return False, stake

        except Exception as e:
            self._last_bet_error = format_bet_failure_reason(str(e), self.bookmaker)
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
            self._prepare_bet_slip_for_wager()

            team_no, display_name, live_odds = self._lookup_side_from_getlines(
                game_id, team_name
            )
            if team_no is None:
                team_no = self._infer_team_no(team_name, team_1, team_2)
            lookup_name = display_name or team_name
            if display_name and display_name != team_name:
                self.logger.info(
                    f"GetLines team label for {team_name}: {display_name} (team_no={team_no})"
                )

            if not self._ensure_bet_board_ready(game_id):
                raise Exception(f"{self.sport_name} lines not loaded before bet placement")

            try:
                game_line, api_team_no = self._lookup_game_line_from_api(game_id, team_name)
            except SessionUnauthorizedError:
                raise
            except Exception as e:
                self.logger.warning(f"GetSportOffering game-line lookup failed: {e}")
                game_line, api_team_no = None, None

            if not game_line:
                game_line = self._fallback_game_line_from_rotations(game_id)
                if game_line:
                    self.logger.warning(
                        f"Using GetLines rotation fallback for {game_id} "
                        f"(GameNum={game_line.get('GameNum')})"
                    )

            if team_no is None:
                team_no = api_team_no
            if team_no is None:
                team_no = self._infer_team_no(team_name, team_1, team_2)

            board_visible = self._rotation_on_board(game_id)
            if not board_visible:
                board_visible = self._refresh_bet_board_for_game(game_id)
            angular_only = (
                not board_visible
                and game_line
                and (
                    game_line.get("GameNum") is not None
                    or game_line.get("Team1RotNum")
                )
            )
            if not board_visible and not angular_only:
                self._save_bet_board_debug(game_id, "missing_rotations")
                raise Exception(
                    f"Game {game_id} not visible on BetWar bet board "
                    f"(GetLines has it; DOM does not)"
                )
            if angular_only:
                self.logger.warning(
                    f"Game {game_id} not on DOM board; using Angular GameLineAction "
                    f"(GameNum={game_line.get('GameNum')})"
                )

            if bet_type == "spread" and game_line:
                live_spread_odds = game_line.get(f"SpreadAdj{team_no}")
                if live_spread_odds is not None and not self._odds_text_matches(
                    str(live_spread_odds), moneyline_odd
                ):
                    raise Exception(
                        f"Line moved: live spread odds {live_spread_odds} differ from arb odds {moneyline_odd}"
                    )
                if spread_line is not None:
                    team_1_spread, team_2_spread = resolve_ticosports_spread_lines(
                        game_line.get("Spread"),
                        game_line.get("MoneyLine1"),
                        game_line.get("MoneyLine2"),
                    )
                    live_spread = team_1_spread if team_no == 1 else team_2_spread
                    if live_spread is not None and not spread_values_match(live_spread, spread_line):
                        raise Exception(
                            f"Spread line moved: live {live_spread} differs from arb {spread_line}"
                        )

            slip_team = lookup_name or team_name
            slip_populated = False

            if bet_type == "spread":
                spread_elem = self._find_spread_on_board(
                    game_id,
                    lookup_name,
                    moneyline_odd,
                    team_no=team_no,
                    spread_line=spread_line,
                    game_line=game_line,
                )
                if not spread_elem and lookup_name != team_name:
                    spread_elem = self._find_spread_on_board(
                        game_id,
                        team_name,
                        moneyline_odd,
                        team_no=team_no,
                        spread_line=spread_line,
                        game_line=game_line,
                    )
                if not spread_elem:
                    self.logger.warning(
                        f"Spread element not found on first pass for {lookup_name} @ "
                        f"{moneyline_odd}, refreshing board"
                    )
                    self._refresh_bet_board_for_game(game_id)
                    time.sleep(1.0)
                    spread_elem = self._find_spread_on_board(
                        game_id,
                        lookup_name,
                        moneyline_odd,
                        team_no=team_no,
                        spread_line=spread_line,
                        game_line=game_line,
                    )
                if not spread_elem:
                    self._save_bet_board_debug(game_id, "missing_spread")
                    if game_line and team_no in (1, 2):
                        self.logger.info(
                            f"Spread DOM click failed for {lookup_name}; trying Angular GameLineAction"
                        )
                        slip_populated = self._add_spread_to_slip(
                            game_line, team_no, slip_team, spread_elem=None
                        )
                    if not slip_populated:
                        visible = self._board_rotation_numbers()
                        raise Exception(
                            f"Spread not found for {lookup_name} @ {moneyline_odd} "
                            f"(game_id={game_id}; board rotations={visible[:12]})"
                        )
                elif not game_line or game_line.get("GameNum") is None:
                    if spread_elem and self._element_is_full_game_spread(spread_elem):
                        game_line, parsed_team_no = self._parse_game_line_from_spread_button(
                            spread_elem, game_id
                        )
                    else:
                        if not game_line:
                            game_line = self._fallback_game_line_from_rotations(game_id)
                        game_line, parsed_team_no = self._resolve_game_line_for_bet(
                            game_id, team_name, None, team_no=team_no
                        )
                    if not game_line:
                        game_line = self._fallback_game_line_from_rotations(game_id)
                    if team_no is None:
                        team_no = parsed_team_no

                if game_line and team_no in (1, 2):
                    slip_populated = self._add_spread_to_slip(
                        game_line, team_no, slip_team, spread_elem=spread_elem
                    )
            else:
                moneyline_elem = None
                if not angular_only:
                    moneyline_elem = self._find_moneyline_on_board(
                        game_id, lookup_name, moneyline_odd, team_no=team_no
                    )

                    if not moneyline_elem and lookup_name != team_name:
                        moneyline_elem = self._find_moneyline_on_board(
                            game_id, team_name, moneyline_odd, team_no=team_no
                        )

                    if not moneyline_elem:
                        self.logger.warning(
                            f"Moneyline element not found on first pass for {lookup_name} @ "
                            f"{moneyline_odd}, refreshing board"
                        )
                        self._refresh_bet_board_for_game(game_id)
                        time.sleep(1.0)
                        moneyline_elem = self._find_moneyline_on_board(
                            game_id, lookup_name, moneyline_odd, team_no=team_no
                        )
                        if not moneyline_elem and lookup_name != team_name:
                            moneyline_elem = self._find_moneyline_on_board(
                                game_id, team_name, moneyline_odd, team_no=team_no
                            )

                if moneyline_elem:
                    game_line, parsed_team_no = self._resolve_game_line_for_bet(
                        game_id, team_name, moneyline_elem, team_no=team_no
                    )
                elif game_line:
                    parsed_team_no = team_no
                else:
                    parsed_team_no = None

                if team_no is None:
                    team_no = parsed_team_no
                if team_no is None:
                    team_no = self._infer_team_no(team_name, team_1, team_2)

                if not moneyline_elem and not (game_line and team_no in (1, 2)):
                    self._save_bet_board_debug(game_id, "missing_moneyline")
                    visible = self._board_rotation_numbers()
                    raise Exception(
                        f"Moneyline not found for {lookup_name} @ {moneyline_odd} "
                        f"(game_id={game_id}; board rotations={visible[:12]})"
                    )

                if game_line and game_line.get("GameNum") is not None:
                    self.logger.info(
                        f"Resolved GameNum={game_line.get('GameNum')} "
                        f"(linekey={game_line.get('LineKey')}) for {game_id}"
                    )

                if game_line and team_no in (1, 2):
                    if moneyline_elem:
                        slip_populated = self._add_moneyline_to_slip(
                            game_line, team_no, slip_team,
                            moneyline_elem=moneyline_elem,
                        )
                    elif self._click_moneyline_via_angular(game_line, team_no):
                        slip_populated = self._wait_for_betslip_team(slip_team, timeout=6)

                if not slip_populated and not self._wait_for_betslip_team(slip_team, timeout=3):
                    if game_line and team_no in (1, 2):
                        self.logger.info("Retrying bet slip via Angular GameLineAction")
                        if self._click_moneyline_via_angular(game_line, team_no):
                            slip_populated = self._wait_for_betslip_team(slip_team, timeout=6)

            if not self._wait_for_betslip_team(slip_team, timeout=8, require_stake_inputs=True):
                slip_preview = self._betslip_text()[:200]
                raise Exception(
                    f"Bet slip still empty after click for {slip_team} (game_id={game_id}): {slip_preview}"
                )

            limits_text = self._betslip_text()
            self.logger.info(f"Bet slip populated: {limits_text[:200]}")
            if bet_type != "spread":
                self._assert_betslip_is_full_game_moneyline(slip_team)

            self._ensure_betslip_expanded()
            if not fill_betslip_stake_input(
                self.driver, stake_plan, self.logger, scope_css="#divBetSlip, #pills-betslip"
            ):
                raise Exception("Could not locate bet slip stake input for base amount")

            self._accept_line_changes()
            if not self._fill_wager_password_if_required():
                raise Exception("BetWar confirm password required but could not be entered")

            if self._page_has_login_required_marker():
                self._invalidate_wager_session()
                raise Exception("Rejection marker on page: please log in")

            self._install_wager_network_hook()
            self._accept_line_changes()
            if not self._submit_place_bets_with_retries(stake_plan=stake_plan):
                raise Exception("Place Bets did not confirm in bet slip")

            self.logger.info("Place Bets confirmed in bet slip; verifying on My Bets tab")
            self._refresh_my_bets_tab()
            time.sleep(1.5)

            matchup_1 = team_1 or ""
            matchup_2 = team_2 or ""
            if not matchup_1 or not matchup_2:
                for game in self._parse_player_portal_dom(self.driver.page_source):
                    if game.get("game_id") == game_id:
                        matchup_1 = game.get("team_1") or matchup_1
                        matchup_2 = game.get("team_2") or matchup_2
                        break
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

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()
        self._scan_health = OddsScanHealthWatchdog(self.logger)
        self._scan_health.start()
        self._consecutive_odds_failures = 0

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
                games, src = self._fetch_games_for_odds(allow_dom_fallback=True)
                if games:
                    self._scan_health.mark_success(len(games))

                setup_ok = True
                break
            except SessionUnauthorizedError as e:
                self.logger.error(f"Initial setup unauthorized (attempt {attempt}/5): {e}")
                if self._recover_odds_session(str(e), recover_driver=(attempt >= 3)):
                    setup_ok = True
                    break
                self._recover_driver()
                time.sleep(8)
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
        while True:
            watchdog.beat()
            self._exposure_cleanup_at = tick_exposure_cleanup(
                self.cache, self.logger, self._exposure_cleanup_at
            )
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
            arbs = get_arbitrage_for_placement(self.cache, self.bookmaker)
            if not arbs:
                self._maybe_poll_odds_while_idle()
                self.logger.info("Waiting for Arbitrage")
                continue

            self.logger.info(f"Arbitrage opportunities: {len(arbs)}")

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

                if self.cache.is_leg_placed(self.bookmaker, bet_type, game_id):
                    self.logger.info(
                        f"Skipping — leg already confirmed on {self.bookmaker} | "
                        f"{team_name} | {team_1} vs {team_2}"
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
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

                stake_plan = base_amount_stake_from_odds(wager_odds, stake)
                if self._my_bets_has_wager(team_name, stake_plan):
                    self.logger.info(
                        f"Recovering existing My Bets wager for {team_name} on {self.bookmaker} "
                        f"(matches intended base stake)"
                    )
                    screenshot_path = capture_bet_screenshot_for_alert(
                        self.logger,
                        self.bookmaker,
                        arb,
                        team_name,
                        game_id,
                        stake_plan,
                        wager_odds,
                        driver=self.driver,
                    )
                    finalize_confirmed_bet(
                        self.cache,
                        self.storage,
                        self.logger,
                        arb,
                        self.bookmaker,
                        team_name,
                        game_id,
                        stake_plan,
                        wager_odds,
                        bet_type,
                        TELEGRAM,
                        screenshot_path=screenshot_path,
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                if self._my_bets_has_team_wager(team_name):
                    self.logger.warning(
                        f"Open wager for {team_name} already on My Bets ({self.bookmaker}); "
                        f"skipping duplicate placement"
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
                if bet_placed:
                    self.logger.info("Bet Placement Completed")
                    screenshot_path = capture_bet_screenshot_for_alert(
                        self.logger,
                        self.bookmaker,
                        arb,
                        team_name,
                        game_id,
                        stake_used,
                        wager_odds,
                        driver=self.driver,
                    )
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
