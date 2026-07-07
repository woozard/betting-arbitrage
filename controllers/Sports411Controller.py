import time
import json
import asyncio
import re
import tempfile
import os
import subprocess
import random
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
import undetected_chromedriver as uc

from utils.config import PROXY1, PROXY2, TELEGRAM, ZENROWS_API_KEY, is_active_arb_pair
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import (
    currency_to_float,
    debug_filepath,
    extract_spread_line_odds_from_label,
    is_game_pregame,
    parse_to_mysql_datetime,
    prune_debug_files,
    send_monitoring_alert,
    spread_values_match,
)
from utils.moneyline_odds import arb_moneyline_odds_acceptable
from utils.arb_placement import get_arbitrage_for_placement, arb_leg_for_book
from utils.betting_loop import wait_for_arb_or_idle
from utils.odds_watch import persist_moneyline_games
from utils.bet_placement import (
    REAL_MONEY_BETTING_PAUSED_MSG,
    block_real_money_bet,
    finalize_confirmed_bet,
    finalize_confirmed_bet_with_screenshot,
    capture_bet_screenshot_for_alert,
    maybe_notify_partial_arb_exposure,
    should_defer_for_sequential_first_leg,
    resolve_arb_leg_stake,
    should_notify_failed_bet,
    should_pause_first_leg_for_exposure,
    odds_tolerance_for_placement,
    should_skip_spread_arb_for_placement,
    should_skip_arb_leg_in_betting_loop,
)
from utils.exposure_cleanup import tick_exposure_cleanup
from utils.betting_watchdog import BettingLoopWatchdog
from utils.stake_sizing import (
    BaseAmountStake,
    base_amount_stake_from_odds,
    format_base_amount_stake,
    page_contains_stake_amount,
)
from utils.stake_entry import fill_betslip_stake_input
from utils.timing import time_it
from utils.chrome_temp import cleanup_stale_temp_dirs, handle_init_driver_failure
from cache.arbitrage_cache import ArbitrageCache

class Sports411Controller:
    WAGER_SESSION_EXPIRED_MARKERS = (
        "please log in",
        "session expired",
        "logged out",
        "not authenticated",
        "unauthorized",
        '"error_code":"401"',
    )
    MAX_WAGER_ATTEMPTS_PER_ARB = 1
    PENDING_CHECK_CACHE_TTL = 45
    CONFIRM_TIMEOUT_SECONDS = 15
    OPEN_BETS_POLL_TIMEOUT_SECONDS = 45
    PLACE_BET_READY_TIMEOUT = 20
    MAX_SOFT_NAV_FAILURES = 3
    ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("SPORTS411_ODDS_FORCE_SCAN_SEC", "5"))
    ODDS_WATCH_POLL_SECONDS = float(os.getenv("SPORTS411_ODDS_POLL_SEC", "5"))

    # ===================================================================
    # Multi-sport support (NBA + MLB) + remove duplicate sport override
    # ===================================================================
    def __init__(self, account, site, sport="basketball", use_proxy=True, headless=True, use_stealth=False, attach_browser=False):

        # Credentials
        self.account_id = account.account
        self.password = account.password
        self.label = account.label if account.label else "N/A"
        self.use_proxy = use_proxy
        self.headless = headless
        self.use_stealth = use_stealth
        self.attach_browser = attach_browser
        xdotool_env = os.environ.get("SPORTS411_USE_XDOTOOL")
        if xdotool_env is None:
            self.use_xdotool = attach_browser
        else:
            self.use_xdotool = xdotool_env.strip().lower() in ("1", "true", "yes")
        # Login/navigation can stay on Selenium; xdotool is most critical for wager clicks.
        bet_only_env = os.environ.get("SPORTS411_XDOTOOL_BET_ONLY", "1")
        self.xdotool_bet_only = bet_only_env.strip().lower() in ("1", "true", "yes")
        self._debug_chrome_proc = None
        self._debug_port = None
        self._force_wager_relogin = False
        self._last_bet_error = None
        self._pending_check_cache = {}
        self._arb_fail_counts = {}

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
            handle_init_driver_failure(
                self.logger, self.user_data_dir, self.proxy_extension_dir
            )
            self.driver = None
            self.wait = None
            self.user_data_dir = None
            self.proxy_extension_dir = None

    @staticmethod
    def _chrome_major_version() -> int:
        for binary in ("google-chrome-stable", "google-chrome", "chromium-browser"):
            try:
                out = subprocess.check_output([binary, "--version"], text=True).strip()
                # e.g. "Google Chrome 148.0.7778.96"
                return int(out.split()[2].split(".")[0])
            except Exception:
                continue
        return 148

    def _build_chrome_options(self):
        """Build a fresh ChromeOptions instance (uc cannot reuse the same object)."""
        if self.use_stealth:
            options = uc.ChromeOptions()
            options.headless = self.headless
        else:
            options = webdriver.ChromeOptions()
            if self.headless:
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
        options.add_argument(f"--user-data-dir={self.user_data_dir}")

        if self.use_proxy and self.proxy_extension_dir:
            options.add_argument(f"--load-extension={self.proxy_extension_dir}")
            options.add_argument("--disable-extensions-except=" + self.proxy_extension_dir)

        if not self.use_stealth:
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            )
        return options

    def _apply_cdp_stealth(self):
        """Reduce automation fingerprints before Sports411 loads (attach mode)."""
        patches = (
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});",
            "window.chrome = { runtime: {} };",
            "Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});",
        )
        for source in patches:
            try:
                self.driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument", {"source": source}
                )
            except Exception as e:
                self.logger.warning(f"CDP stealth patch failed: {e}")

    def _human_type(self, element, text: str, force_xdotool: bool = False):
        """Type like a user — xdotool keyboard in attach mode."""
        use_xdotool = self.use_xdotool and (force_xdotool or not self.xdotool_bet_only)
        if use_xdotool:
            self._xdotool_type(element, text)
            return
        element.click()
        element.clear()
        for ch in str(text):
            element.send_keys(ch)
            time.sleep(0.05 + (0.03 * (ord(ch) % 3)))

    def _wait_for_debug_port(self, port: int, timeout: float = 20) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
                if resp.ok:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _launch_debug_chrome(self) -> int:
        """Launch plain Chrome with remote debugging (matches successful manual VNC bets)."""
        port = int(os.environ.get("SPORTS411_CHROME_DEBUG_PORT", "9222"))
        profile = os.environ.get("SPORTS411_CHROME_USER_DATA_DIR")
        if profile:
            self.user_data_dir = profile
            os.makedirs(profile, exist_ok=True)
        else:
            self.user_data_dir = tempfile.mkdtemp(prefix="chrome_user_data_")

        if self._wait_for_debug_port(port, timeout=1):
            self.logger.info(f"Reusing existing Chrome on debug port {port}")
            self._debug_port = port
            self._debug_chrome_proc = None
            return port
        chrome_bin = None
        mac_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(mac_chrome):
            chrome_bin = mac_chrome
        for candidate in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium"):
            if chrome_bin:
                break
            try:
                subprocess.check_output([candidate, "--version"], stderr=subprocess.DEVNULL)
                chrome_bin = candidate
                break
            except Exception:
                continue
        if not chrome_bin:
            raise RuntimeError("Chrome/Chromium binary not found for attach mode")

        cmd = [
            chrome_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1920,1080",
            "--window-position=0,0",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "about:blank",
        ]
        self._debug_port = port
        self._debug_chrome_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not self._wait_for_debug_port(port):
            raise RuntimeError(f"Chrome debug port {port} did not become ready")
        return port

    def _create_attached_driver(self):
        """Attach Selenium to a real Chrome instance (not automation-launched)."""
        port = self._launch_debug_chrome()
        options = webdriver.ChromeOptions()
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
        self.logger.info(f"Attaching to plain Chrome on debug port {port}")
        try:
            self.driver = webdriver.Chrome(options=options)
        except Exception as e:
            self.logger.warning(f"Plain Selenium attach failed ({e}); retrying with uc")
            options = uc.ChromeOptions()
            options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
            self.driver = uc.Chrome(
                options=options,
                use_subprocess=True,
                version_main=self._chrome_major_version(),
            )
        self._apply_cdp_stealth()
        self.wait = WebDriverWait(self.driver, 30)
        if self.use_xdotool and not self._xdotool_available():
            self.logger.warning(
                "xdotool or DISPLAY unavailable; falling back to Selenium clicks"
            )
            self.use_xdotool = False
        elif self.use_xdotool:
            self.logger.info(
                f"xdotool trusted input enabled (DISPLAY={os.environ.get('DISPLAY')})"
            )
        time.sleep(1)

    def _create_driver(self):
        """Launch Chrome (attach / undetected-chromedriver / plain Selenium)."""
        if self.attach_browser:
            self._create_attached_driver()
            return

        if self.use_proxy:
            proxy_host = "brd.superproxy.io"
            proxy_port = 33335
            proxy_user = "brd-customer-hl_70fad530-zone-arbitrage_bot"
            proxy_pass = "truzviha7wip"
            self.proxy_extension_dir = self._create_proxy_extension(
                proxy_host, proxy_port, proxy_user, proxy_pass
            )
        else:
            self.proxy_extension_dir = None

        self.user_data_dir = tempfile.mkdtemp(prefix="chrome_user_data_")

        driver_label = "undetected-chromedriver" if self.use_stealth else "selenium"
        proxy_label = "proxy" if self.use_proxy else "direct"
        self.logger.info(
            f"Starting Chrome ({driver_label}, {proxy_label}, headless={self.headless})"
        )

        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            options = self._build_chrome_options()
            try:
                if self.use_stealth:
                    self.driver = uc.Chrome(
                        options=options,
                        use_subprocess=True,
                        version_main=self._chrome_major_version(),
                    )
                else:
                    self.driver = webdriver.Chrome(options=options)

                try:
                    _ = self.driver.current_url
                except Exception as ve:
                    self.logger.warning(
                        f"Chrome created on attempt {attempt + 1} but session is dead: {ve}"
                    )
                    last_error = ve
                    self._quit_driver()
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(5)
                    continue
                break
            except Exception as e:
                last_error = e
                self.logger.warning(
                    f"Chrome driver start attempt {attempt + 1}/{max_retries} failed: {e}"
                )
                self._quit_driver()
                if attempt == max_retries - 1:
                    raise
                time.sleep(5)
        else:
            raise last_error or RuntimeError("Chrome driver creation failed")

        self.wait = WebDriverWait(self.driver, 30)
        time.sleep(2)

    def _xdotool_available(self) -> bool:
        if not os.environ.get("DISPLAY"):
            return False
        try:
            subprocess.run(
                ["xdotool", "version"],
                capture_output=True,
                check=True,
                env=os.environ,
            )
            return True
        except Exception:
            return False

    def _xdotool_run(self, *args, check: bool = True):
        return subprocess.run(
            ["xdotool", *args],
            capture_output=True,
            text=True,
            check=check,
            env=os.environ,
        )

    def _chrome_pids(self) -> list:
        pids = []
        proc = getattr(self, "_debug_chrome_proc", None)
        if proc and proc.poll() is None:
            pids.append(str(proc.pid))
        if self.user_data_dir:
            result = subprocess.run(
                ["pgrep", "-f", f"--user-data-dir={self.user_data_dir}"],
                capture_output=True,
                text=True,
            )
            pids.extend(wid for wid in result.stdout.strip().split("\n") if wid)
        return list(dict.fromkeys(pids))

    def _xdotool_window_ids_for_chrome(self) -> list:
        wids = []
        for pid in self._chrome_pids():
            result = self._xdotool_run("search", "--pid", pid, check=False)
            wids.extend(wid for wid in (result.stdout or "").strip().split("\n") if wid)

        if wids:
            return self._sort_chrome_windows_by_area(list(dict.fromkeys(wids)))

        try:
            title = (self.driver.title or "").strip()
            if title:
                result = self._xdotool_run("search", "--onlyvisible", "--name", title[:40], check=False)
                wids = [wid for wid in (result.stdout or "").strip().split("\n") if wid]
                if wids:
                    return self._sort_chrome_windows_by_area(wids)
        except Exception:
            pass

        for pattern in ("sports411", "MLB"):
            result = self._xdotool_run("search", "--onlyvisible", "--name", pattern, check=False)
            wids = [wid for wid in (result.stdout or "").strip().split("\n") if wid]
            if wids:
                return self._sort_chrome_windows_by_area(wids)

        result = self._xdotool_run(
            "search", "--onlyvisible", "--class", "google-chrome", check=False
        )
        wids = [wid for wid in (result.stdout or "").strip().split("\n") if wid]
        return self._sort_chrome_windows_by_area(wids)

    def _sort_chrome_windows_by_area(self, wids: list) -> list:
        scored = []
        for wid in wids:
            result = self._xdotool_run("getwindowgeometry", wid, check=False)
            width = height = 0
            for line in (result.stdout or "").splitlines():
                if line.startswith("Width:"):
                    width = int(line.split(":", 1)[1].strip())
                elif line.startswith("Height:"):
                    height = int(line.split(":", 1)[1].strip())
            scored.append((width * height, wid))
        scored.sort(reverse=True)
        return [wid for _, wid in scored]

    def _element_window_center(self, element) -> dict:
        """Center of element in xdotool --window coordinates (includes browser chrome)."""
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});", element
        )
        time.sleep(0.35)
        return self.driver.execute_script(
            """
            const rect = arguments[0].getBoundingClientRect();
            const borderLeft = (window.outerWidth - window.innerWidth) / 2;
            const borderTop = window.outerHeight - window.innerHeight;
            const jitterX = (Math.random() - 0.5) * Math.min(8, rect.width * 0.2);
            const jitterY = (Math.random() - 0.5) * Math.min(8, rect.height * 0.2);
            return {
                x: Math.round(borderLeft + rect.left + rect.width / 2 + jitterX),
                y: Math.round(borderTop + rect.top + rect.height / 2 + jitterY),
            };
            """,
            element,
        )

    def _xdotool_focus_chrome(self):
        """Activate the Sports411 Chrome window on the X display (best effort)."""
        wids = self._xdotool_window_ids_for_chrome()
        if not wids:
            self.logger.warning("xdotool could not find a Chrome window to focus")
            return None

        for wid in reversed(wids):
            self._xdotool_run("windowmap", wid, check=False)
            self._xdotool_run("windowraise", wid, check=False)
            focused = self._xdotool_run("windowfocus", "--sync", wid, check=False)
            if focused.returncode == 0:
                return wid
            activated = self._xdotool_run("windowactivate", "--sync", wid, check=False)
            if activated.returncode == 0:
                self._xdotool_run("windowfocus", "--sync", wid, check=False)
                return wid

        self.logger.warning(
            f"xdotool found Chrome windows {wids} but could not focus; using largest"
        )
        return wids[-1]

    def _element_viewport_center(self, element) -> dict:
        """Center of element in viewport coordinates (for xdotool --window)."""
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});", element
        )
        time.sleep(0.35)
        return self.driver.execute_script(
            """
            const rect = arguments[0].getBoundingClientRect();
            const jitterX = (Math.random() - 0.5) * Math.min(8, rect.width * 0.2);
            const jitterY = (Math.random() - 0.5) * Math.min(8, rect.height * 0.2);
            return {
                x: Math.round(rect.left + rect.width / 2 + jitterX),
                y: Math.round(rect.top + rect.height / 2 + jitterY),
            };
            """,
            element,
        )

    def _element_screen_center(self, element) -> dict:
        """Map a DOM element to absolute screen coordinates for xdotool."""
        coords = self._element_viewport_center(element)
        offset = self.driver.execute_script(
            """
            const borderLeft = (window.outerWidth - window.innerWidth) / 2;
            const borderTop = window.outerHeight - window.innerHeight;
            return {
                x: Math.round(window.screenX + borderLeft),
                y: Math.round(window.screenY + borderTop),
            };
            """
        )
        return {
            "x": offset["x"] + coords["x"],
            "y": offset["y"] + coords["y"],
        }

    def _xdotool_window_origin(self, wid: str) -> dict:
        result = self._xdotool_run("getwindowgeometry", "--shell", wid, check=False)
        origin = {"x": 0, "y": 0}
        for line in (result.stdout or "").splitlines():
            if line.startswith("X="):
                origin["x"] = int(line.split("=", 1)[1])
            elif line.startswith("Y="):
                origin["y"] = int(line.split("=", 1)[1])
        return origin

    def _element_screen_coords_for_xdotool(self, element) -> dict:
        """Absolute screen coords for xdotool; verifies elementFromPoint when possible."""
        coords = self._element_screen_center_selenium(element)
        if not self.driver:
            return {"x": coords["x"], "y": coords["y"], "wid": None}
        try:
            win = self.driver.get_window_rect()
            offsets = self.driver.execute_script(
                """
                return {
                    left: (window.outerWidth - window.innerWidth) / 2,
                    top: window.outerHeight - window.innerHeight,
                };
                """
            )
            vp_x = coords["x"] - win["x"] - offsets["left"]
            vp_y = coords["y"] - win["y"] - offsets["top"]
            hit = self.driver.execute_script(
                """
                const el = arguments[0];
                const x = arguments[1], y = arguments[2];
                const hit = document.elementFromPoint(x, y);
                return !!(hit && (hit === el || el.contains(hit)));
                """,
                element,
                vp_x,
                vp_y,
            )
            if not hit:
                self.logger.warning(
                    f"elementFromPoint miss at viewport ({vp_x}, {vp_y}); "
                    f"screen ({coords['x']}, {coords['y']})"
                )
        except Exception as e:
            self.logger.warning(f"Could not verify xdotool coords: {e}")
        return {"x": coords["x"], "y": coords["y"], "wid": None}

    def _xdotool_click_screen(self, x: int, y: int):
        self._xdotool_run("mousemove", "--sync", str(x), str(y))
        time.sleep(0.12 + random.random() * 0.08)
        self._xdotool_run("click", "1")

    def _xdotool_click_coords(self, x: int, y: int, window_id: str = None, use_screen: bool = False):
        """Click at coordinates. use_screen=True for absolute X11 coords."""
        if use_screen:
            self._xdotool_click_screen(x, y)
            return
        wid = window_id or self._xdotool_focus_chrome()
        if wid:
            self._xdotool_run("windowmap", wid, check=False)
            self._xdotool_run("windowraise", wid, check=False)
            self._xdotool_run("windowactivate", "--sync", wid, check=False)
            self._xdotool_run("windowfocus", "--sync", wid, check=False)
            self._xdotool_run("mousemove", "--window", wid, "--sync", str(x), str(y))
        else:
            self._xdotool_run("mousemove", "--sync", str(x), str(y))
        time.sleep(0.12 + random.random() * 0.08)
        self._xdotool_run("click", "1")

    def _xdotool_click_element(self, element, isolated: bool = False):
        """Click element via xdotool using screen coordinates (reliable on xvfb)."""
        coords = self._element_screen_coords_for_xdotool(element)
        self.logger.info(
            f"xdotool click at screen ({coords['x']}, {coords['y']})"
            f"{', isolated' if isolated else ''}"
        )
        if isolated and self.attach_browser:
            self._detach_driver_keep_chrome()
            try:
                self._xdotool_click_screen(coords["x"], coords["y"])
                time.sleep(6)
            finally:
                self._reattach_driver()
        else:
            self._xdotool_click_screen(coords["x"], coords["y"])
        return coords

    def _element_screen_center_selenium(self, element) -> dict:
        """Screen coords using Selenium window rect + element location."""
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});", element
        )
        time.sleep(0.35)
        win = self.driver.get_window_rect()
        loc = element.location
        size = element.size
        offsets = self.driver.execute_script(
            """
            return {
                left: (window.outerWidth - window.innerWidth) / 2,
                top: window.outerHeight - window.innerHeight,
            };
            """
        )
        jitter_x = random.uniform(-3, 3)
        jitter_y = random.uniform(-3, 3)
        return {
            "x": int(win["x"] + offsets["left"] + loc["x"] + size["width"] / 2 + jitter_x),
            "y": int(win["y"] + offsets["top"] + loc["y"] + size["height"] / 2 + jitter_y),
        }

    def _wager_sendbets_seen(self) -> bool:
        for entry in self._get_wager_network_log():
            if "sendbets" in (entry.get("url") or "").lower():
                return True
        return False

    def _detach_driver_keep_chrome(self):
        """Stop chromedriver control but leave the Chrome process running (attach mode)."""
        driver = getattr(self, "driver", None)
        if not driver:
            return
        try:
            service = getattr(driver, "service", None)
            if service:
                service.stop()
        except Exception:
            pass
        self.driver = None
        self.wait = None
        subprocess.run(["pkill", "-f", "chromedriver"], capture_output=True)
        time.sleep(0.3)

    def _reattach_driver(self):
        if not self.attach_browser:
            return
        port = self._debug_port or int(os.environ.get("SPORTS411_CHROME_DEBUG_PORT", "9222"))
        if not self._wait_for_debug_port(port, timeout=10):
            raise RuntimeError(f"Chrome debug port {port} unavailable for reattach")
        options = uc.ChromeOptions()
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
        self.driver = uc.Chrome(
            options=options,
            use_subprocess=True,
            version_main=self._chrome_major_version(),
        )
        self.wait = WebDriverWait(self.driver, 30)

    def _xdotool_click_isolated(self, element) -> dict:
        """xdotool click while chromedriver stays detached through SendBets."""
        return self._xdotool_click_element(element, isolated=True)

    def _xdotool_type(self, element, text: str):
        coords = self._element_screen_coords_for_xdotool(element)
        self._xdotool_click_screen(coords["x"], coords["y"])
        time.sleep(0.2)
        self._xdotool_run("key", "--clearmodifiers", "ctrl+a")
        time.sleep(0.05)
        self._xdotool_run("key", "--clearmodifiers", "BackSpace")
        time.sleep(0.1)
        self._xdotool_run("type", "--delay", "75", "--", str(text))
        time.sleep(0.15)

    def _human_click(self, element, force_xdotool: bool = False):
        """Real user input via xdotool (attach mode) or Selenium click."""
        use_xdotool = self.use_xdotool and (force_xdotool or not self.xdotool_bet_only)
        if use_xdotool and self.attach_browser and force_xdotool:
            self._xdotool_click_element(element, isolated=True)
            return
        if use_xdotool:
            self._xdotool_click_element(element, isolated=False)
            return
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", element
        )
        time.sleep(0.35)
        for _ in range(12):
            try:
                if element.is_displayed() and element.is_enabled():
                    element.click()
                    return
            except Exception:
                pass
            time.sleep(0.25)
        self.driver.execute_script("arguments[0].click();", element)

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
                self._force_wager_relogin = False
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
            password_input.clear()
            if self.attach_browser:
                self._human_type(account_input, self.account_id)
                self._human_type(password_input, self.password)
            else:
                account_input.send_keys(self.account_id)
                password_input.send_keys(self.password)

            login_btn = self.driver.find_element(
                By.CSS_SELECTOR, "input[type='submit'].login"
            )
            self._human_click(login_btn)

            self.wait.until(EC.url_contains("/en/sports/"))
            self._force_wager_relogin = False
            self.logger.info("Login Successful")
            return True

        except Exception as e:
            self.logger.error(f"Login Failed: {e}")
            with open(debug_filepath("debug_login_sports411_FAIL"), "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self._safe_send_monitoring_alert(e)  # <-- safe version
            raise


    def __inject_mutation_observer(self):
        from utils.odds_observer import install_mutation_observer
        self.logger.info("Injecting MutationObserver on schedule (JS)")
        install_mutation_observer(
            self.driver,
            [
                "app-american-schedule",
                ".sports-league-games",
                "body",
            ],
            self.logger,
        )

    def _ensure_odds_mutation_observer(self) -> bool:
        from utils.odds_observer import ensure_mutation_observer, mutation_observer_installed
        try:
            installed = mutation_observer_installed(self.driver)
        except Exception:
            installed = False
        if installed:
            return True
        return ensure_mutation_observer(
            self.driver,
            ["app-american-schedule", ".sports-league-games", "body"],
            self.logger,
        )

    def _drain_odds_mutation_buffer(self) -> list:
        try:
            updates = self.driver.execute_script("""
                const data = window.oddsBuffer || [];
                window.oddsBuffer = [];
                return data;
            """)
            return updates or []
        except Exception as e:
            self.logger.warning(f"Could not drain odds buffer: {e}")
            return []

    def _wait_for_odds_schedule_ready(self):
        try:
            self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "app-american-schedule"))
            )
        except Exception:
            self.logger.warning("Schedule component not found quickly.")

        try:
            spinner_locator = (By.CSS_SELECTOR, "div.component-loader, .fa-spinner-third")
            WebDriverWait(self.driver, 15).until(
                EC.invisibility_of_element_located(spinner_locator)
            )
        except Exception:
            self.logger.warning("Spinner did not disappear within 15s timeout.")

        for _ in range(12):
            if self._sport_games_present():
                return
            time.sleep(0.5)

        if not self._sport_games_present():
            self.logger.warning("Game rows not visible after schedule wait.")

    @staticmethod
    def _extract_team_odds_from_label(label):
        title = (label.get("title") or label.text or "").strip()
        match = re.match(r"^(.+?)\s+([+-]?\d+)", title)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        text = label.text.strip()
        match = re.match(r"^(.+?)\s+([+-]?\d+)", text)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        return None, None

    @staticmethod
    def _extract_spread_from_game(game) -> dict:
        spread = {
            "team_1_spread": None,
            "team_2_spread": None,
            "team_1_odds": None,
            "team_2_odds": None,
        }

        # Current S411 MLB DOM: run line lives under .hdp (handicap) cells.
        hdp_labels = game.select(".hdp label.bet-indicator")
        if len(hdp_labels) >= 2:
            spread_1, odds_1 = extract_spread_line_odds_from_label(hdp_labels[0])
            spread_2, odds_2 = extract_spread_line_odds_from_label(hdp_labels[1])
            if spread_1 is not None and spread_2 is not None and odds_1 and odds_2:
                spread["team_1_spread"] = spread_1
                spread["team_2_spread"] = spread_2
                spread["team_1_odds"] = odds_1
                spread["team_2_odds"] = odds_2
                return spread

        selectors = (
            (".hdp-1 label.bet-indicator", ".hdp-2 label.bet-indicator"),
            (".psline-1 label.bet-indicator", ".psline-2 label.bet-indicator"),
            (".sline-1 label.bet-indicator", ".sline-2 label.bet-indicator"),
            (".spread-1 label.bet-indicator", ".spread-2 label.bet-indicator"),
            (".runline-1 label.bet-indicator", ".runline-2 label.bet-indicator"),
        )
        for sel1, sel2 in selectors:
            line1 = game.select_one(sel1)
            line2 = game.select_one(sel2)
            if not line1 or not line2:
                continue
            spread_1, odds_1 = extract_spread_line_odds_from_label(line1)
            spread_2, odds_2 = extract_spread_line_odds_from_label(line2)
            if spread_1 is None or spread_2 is None or not odds_1 or not odds_2:
                continue
            spread["team_1_spread"] = spread_1
            spread["team_2_spread"] = spread_2
            spread["team_1_odds"] = odds_1
            spread["team_2_odds"] = odds_2
            return spread
        return spread

    def _parse_games_from_html(self, html: str) -> list:
        soup = BeautifulSoup(html or "", "html.parser")
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

                team_1, team_1_ml = self._extract_team_odds_from_label(mline1)
                team_2, team_2_ml = self._extract_team_odds_from_label(mline2)
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
                    "spread": self._extract_spread_from_game(game),
                    "total": {
                        "over_total": None, "under_total": None,
                        "over_odds": None, "under_odds": None,
                    },
                })
            except Exception as e:
                self.logger.error(f"Error parsing game: {e}", exc_info=True)
        raw_count = len(games)
        games = self._dedupe_games_by_matchup(games)
        if raw_count != len(games):
            self.logger.info(
                f"Deduped S411 matchups: {raw_count} DOM nodes -> {len(games)} unique games"
            )
        return games

    def _persist_games_odds(self, games: list, source: str = "scan") -> int:
        if not hasattr(self, "_last_saved_ml"):
            self._last_saved_ml = {}
        return persist_moneyline_games(
            self.cache,
            self.storage,
            self.logger,
            games,
            self.sport_name,
            self.league,
            self._last_saved_ml,
            source=source,
        )

    def _prepare_odds_watch_page(self) -> bool:
        self._ensure_odds_session()
        if not self._is_on_sport_page_with_games():
            self.logger.info(f"Navigating to {self.sport_url}")
            self.driver.get(self.sport_url)
        self._wait_for_odds_schedule_ready()
        if not self._sport_games_present():
            self.logger.warning(f"No {self.sport_name} games visible on odds watch page")
            return False
        if not self._ensure_odds_mutation_observer():
            self.logger.warning("MutationObserver not installed; force scans only")
        return True

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

    @staticmethod
    def _matchup_key(team_1: str, team_2: str, game_datetime: str) -> tuple:
        teams = tuple(sorted([(team_1 or "").strip().lower(), (team_2 or "").strip().lower()]))
        date_part = (game_datetime or "")[:10]
        return teams + (date_part,)

    @staticmethod
    def _moneyline_is_populated(game: dict) -> bool:
        ml = game.get("moneyline") or {}
        invalid = {"", "0", "none", "null"}
        t1 = str(ml.get("team_1") or "").strip().lower()
        t2 = str(ml.get("team_2") or "").strip().lower()
        return t1 not in invalid and t2 not in invalid

    @staticmethod
    def _pick_canonical_game(existing: dict, candidate: dict) -> dict:
        """When the DOM lists the same matchup twice, prefer rows with real ML odds.

        If both have odds, keep the higher idgame (matches live bet clicks).
        """
        existing_ok = Sports411Controller._moneyline_is_populated(existing)
        candidate_ok = Sports411Controller._moneyline_is_populated(candidate)
        if existing_ok and not candidate_ok:
            return existing
        if candidate_ok and not existing_ok:
            return candidate
        try:
            existing_id = int(existing.get("game_id") or 0)
            candidate_id = int(candidate.get("game_id") or 0)
        except (TypeError, ValueError):
            return existing if existing_ok else candidate
        return candidate if candidate_id > existing_id else existing

    def _dedupe_games_by_matchup(self, games: list) -> list:
        seen = {}
        for game in games:
            key = self._matchup_key(
                game.get("team_1"), game.get("team_2"), game.get("game_datetime")
            )
            if key not in seen:
                seen[key] = game
                continue

            prev = seen[key]
            chosen = self._pick_canonical_game(prev, game)
            dropped = prev if chosen is game else game
            kept = chosen
            self.logger.warning(
                f"Dropping duplicate S411 game_id={dropped.get('game_id')} "
                f"({dropped.get('team_1')} vs {dropped.get('team_2')}, "
                f"ML {dropped.get('moneyline')}); "
                f"keeping game_id={kept.get('game_id')} "
                f"(ML {kept.get('moneyline')})"
            )
            seen[key] = chosen

        return list(seen.values())

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
    def fetch_odds(self, refresh_interval=10, quit_driver=True):
        """One-shot odds scrape (manual/tests). Production uses watch_odds()."""
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self.logger.info(f"========== Fetching Odds ({self.sport_name}) via Selenium (START) ==========")
        prune_debug_files()

        try:
            if not self._prepare_odds_watch_page():
                self.logger.warning("Could not prepare odds page for one-shot fetch")
                return

            games = self._parse_games_from_html(self.driver.page_source)
            self.logger.info(f"Extracted {len(games)} {self.sport_name} matches via Selenium")

            if len(games) == 0:
                debug_file = debug_filepath(f"debug_sports411_{self.sport_name.lower()}")
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(self.driver.page_source)
                self.logger.warning(
                    f"No games found for {self.sport_name}. Inspect: {debug_file}"
                )

            self._last_saved_ml = {}
            self._persist_games_odds(games, source="fetch")

        except Exception as e:
            self.logger.error(f"Selenium fetch_odds failed: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            if quit_driver:
                self._quit_driver()
            self.logger.info(f"========= Fetching Odds ({self.sport_name}) via Selenium (END) ==========")

    def watch_odds(
        self,
        force_scan_interval: int = None,
        poll_interval: float = None,
    ):
        """Long-lived odds watcher: one login, stay on MLB/NBA page, push updates on DOM changes."""
        force_scan_interval = force_scan_interval or self.ODDS_WATCH_FORCE_SCAN_SECONDS
        poll_interval = poll_interval or self.ODDS_WATCH_POLL_SECONDS

        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self._last_saved_ml = {}

        self.logger.info(
            f"========== Odds Watch ({self.sport_name}) (START) — "
            f"force scan every {force_scan_interval}s, poll {poll_interval}s =========="
        )

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()
        self._cleanup_stale_temp_dirs()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                setup_ok = self._prepare_odds_watch_page()
                if setup_ok:
                    break
            except Exception as e:
                self.logger.error(f"Odds watch setup failed (attempt {attempt}/5): {e}")
            self._recover_driver()
            time.sleep(5)

        if not setup_ok:
            self.logger.error("Could not start odds watch after recoveries")
            self.logger.info(f"========== Odds Watch ({self.sport_name}) (END) ==========")
            return

        last_force_scan = 0.0
        consecutive_recoveries = 0
        consecutive_soft_nav_failures = 0

        try:
            while True:
                watchdog.beat()

                try:
                    current_url = self.driver.current_url
                except Exception as e:
                    self.logger.error(f"Odds watch driver error: {e}")
                    self._recover_driver()
                    consecutive_recoveries += 1
                    if self._relogin_after_recovery():
                        self._prepare_odds_watch_page()
                    time.sleep(5)
                    continue

                if self._is_off_sport_page(current_url):
                    if self._should_soft_navigate_back(current_url):
                        self.logger.warning(
                            f"Odds watch off {self.sport_name} page ({current_url}); soft nav back"
                        )
                        self._return_to_sport_page()
                        if self._is_on_sport_page_with_games():
                            self._ensure_odds_mutation_observer()
                            consecutive_soft_nav_failures = 0
                            continue
                        consecutive_soft_nav_failures += 1
                        if consecutive_soft_nav_failures >= self.MAX_SOFT_NAV_FAILURES:
                            self.logger.warning(
                                "Odds watch soft nav failed repeatedly; recovering driver"
                            )
                            consecutive_soft_nav_failures = 0
                            self._recover_driver()
                            if self._relogin_after_recovery():
                                self._prepare_odds_watch_page()
                        time.sleep(2)
                        continue

                    self.logger.warning(f"Odds watch unexpected URL ({current_url}); recovering")
                    self._recover_driver()
                    if self._relogin_after_recovery():
                        self._prepare_odds_watch_page()
                    time.sleep(5)
                    continue

                consecutive_recoveries = 0
                consecutive_soft_nav_failures = 0

                last_force_scan, processed = self._tick_odds_watch_once(
                    last_force_scan, force_scan_interval, idle_label="watch"
                )
                if not processed:
                    time.sleep(poll_interval)
                    continue

                time.sleep(poll_interval)

        except KeyboardInterrupt:
            self.logger.info("Odds watch stopped by user")
        except Exception as e:
            self.logger.error(f"Fatal odds watch error: {e}", exc_info=True)
            self._safe_send_monitoring_alert(e)
        finally:
            self.logger.info(f"========== Odds Watch ({self.sport_name}) (END) ==========")

    def _tick_odds_watch_once(
        self,
        last_force_scan: float,
        force_scan_interval: int = None,
        idle_label: str = "watch",
    ):
        """Single odds-watch iteration. Returns (last_force_scan, processed)."""
        force_scan_interval = force_scan_interval or self.ODDS_WATCH_FORCE_SCAN_SECONDS
        now = time.monotonic()
        is_force_scan = (
            last_force_scan == 0.0
            or (now - last_force_scan) >= force_scan_interval
        )
        if is_force_scan:
            self.logger.info(f"Odds watch — force scan ({idle_label})")
            updates = [self.driver.page_source]
            last_force_scan = now
        else:
            changed = self._drain_odds_mutation_buffer()
            if not changed:
                return last_force_scan, False
            updates = [self.driver.page_source]

        for html_chunk in updates:
            games = self._parse_games_from_html(html_chunk)
            source = f"force-scan" if is_force_scan else idle_label
            saved = self._persist_games_odds(games, source=source)
            if saved or is_force_scan:
                self.logger.info(
                    f"Extracted {len(games)} {self.sport_name} matches ({source})"
                )
        return last_force_scan, True

    def _resume_odds_watch_on_sport_page(self):
        """Return to game-lines and reinstall DOM observer after wager flow."""
        self._return_to_sport_page()
        if self._is_on_sport_page_with_games():
            self._ensure_odds_mutation_observer()

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

    def _message_requires_relogin(self, message: str) -> bool:
        msg_l = (message or "").lower()
        return any(marker in msg_l for marker in self.WAGER_SESSION_EXPIRED_MARKERS)

    def _invalidate_wager_session(self):
        self._force_wager_relogin = True

    def _page_has_login_required_marker(self) -> bool:
        try:
            for elem in self.driver.find_elements(
                By.CSS_SELECTOR, ".alert-overlay, .alert-message, #betslip"
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

    def _return_to_sport_page(self) -> bool:
        try:
            if self._is_on_sport_page_with_games():
                return True
            self.driver.get(self.sport_url)
            self._wait_for_sport_games_loaded()
            return self._is_on_sport_page_with_games()
        except Exception as e:
            self.logger.warning(f"Could not return to {self.sport_name} page: {e}")
            return False

    def _is_off_sport_page(self, url: str) -> bool:
        return self.game_lines_path not in (url or "")

    def _should_soft_navigate_back(self, url: str) -> bool:
        url_l = (url or "").lower()
        if self.game_lines_path in url_l:
            return False
        soft_markers = (
            "/en/open-bets",
            "/open-bets",
            "/account/",
            "/logout",
            "/en/sports/",
            f"www.{self.website}",
            f"be.{self.website}",
            "index.php",
        )
        return any(marker in url_l for marker in soft_markers)

    def _open_bets_url(self) -> str:
        return f"https://be.{self.website}/en/open-bets/"

    def _fetch_pending_page_text(self) -> str:
        """Fetch open-bets HTML in-browser without navigating away from the sport page."""
        try:
            return self.driver.execute_async_script(
                """
                const done = arguments[arguments.length - 1];
                const path = arguments[0];
                fetch(path, {
                    credentials: 'include',
                    headers: {'Accept': 'text/html,application/xhtml+xml'}
                })
                .then(r => r.text())
                .then(t => done(t || ''))
                .catch(() => done(''));
                """,
                "/en/open-bets/",
            ) or ""
        except Exception as e:
            self.logger.warning(f"Open-bets fetch failed: {e}")
            return ""

    @staticmethod
    def _page_text_has_open_wager(page: str, team_name: str, team_1: str, team_2: str) -> bool:
        page_l = (page or "").lower()
        team_l = team_name.lower()
        teams_present = (
            team_l in page_l
            and team_1.lower() in page_l
            and team_2.lower() in page_l
        )
        if not teams_present:
            return False
        return any(
            marker in page_l
            for marker in (
                "risk:", "to win:", "pending wager", "open bet",
                "wager #", "ticket #", "straight bet",
            )
        )

    def _has_existing_open_bet(self, team_name: str, team_1: str, team_2: str) -> bool:
        cache_key = f"{team_name}:{team_1}:{team_2}".lower()
        now = time.time()
        cached = self._pending_check_cache.get(cache_key)
        if cached and (now - cached["ts"]) < self.PENDING_CHECK_CACHE_TTL:
            return cached["found"]

        found = False
        try:
            self.driver.get(self._open_bets_url())
            time.sleep(2.5)
            page = self.driver.page_source
            found = self._page_text_has_open_wager(page, team_name, team_1, team_2)
            self._return_to_sport_page()
        except Exception as e:
            self.logger.warning(f"Could not check existing open bets: {e}")
            return False

        self._pending_check_cache[cache_key] = {"found": found, "ts": now}
        return found

    def _refresh_session_before_wager(self):
        if self._force_wager_relogin:
            self.logger.info("Wager session flagged invalid; performing full login before placement")
            self.__login()
            self._return_to_sport_page()
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
        self._return_to_sport_page()

    def _ensure_betting_session(self):
        """Login for the betting loop only when the browser session is missing or invalid."""
        if self._force_wager_relogin:
            self.logger.info("Session flagged invalid; performing full login for betting loop")
            self.__login()
            self._return_to_sport_page()
            return

        if self._is_session_valid() and self._is_on_sport_page_with_games():
            self.logger.info(
                "Session valid on sport page with games loaded; skipping login for betting loop"
            )
            return

        if self._is_session_valid():
            self.logger.info("Session valid for betting loop; navigating to sport page only")
            self._return_to_sport_page()
            return

        self.logger.info("Session invalid for betting loop; performing full login")
        self.__login()
        self._return_to_sport_page()

    def _game_visible_on_page(self, game_id: str) -> bool:
        try:
            return bool(
                self.driver.find_elements(
                    By.CSS_SELECTOR, f"div.sports-league-game[idgame='{game_id}']"
                )
            )
        except Exception:
            return False

    @staticmethod
    def _parse_wager_api_body(body: str):
        """Return ('rejected'|'accepted'|None, message)."""
        if not body:
            return None, ""
        body_l = body.lower()
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                return None, ""
            if data.get("WagerResult") is False:
                wagers = data.get("Wagers") or []
                if wagers:
                    details = ", ".join(
                        f"Result={w.get('Result')} Ttw={w.get('Ttw')}"
                        for w in wagers
                    )
                    return "rejected", f"SendBets WagerResult:false ({details})"
                return "rejected", "SendBets WagerResult:false"
            if data.get("WagerResult") is True:
                return "accepted", "SendBets confirmed"
            if data.get("ErrorCode") not in (None, 0, "0"):
                return "rejected", f"SendBets ErrorCode:{data.get('ErrorCode')}"
        except (json.JSONDecodeError, TypeError):
            pass
        if '"wagerresult":false' in body_l or '"wagerresult": false' in body_l:
            return "rejected", "SendBets WagerResult:false"
        return None, ""

    @staticmethod
    def _is_fast_book_rejection(error_message: str) -> bool:
        msg_l = (error_message or "").lower()
        return any(
            marker in msg_l
            for marker in (
                "wagerresult:false",
                "sendbets",
                "wager api rejected",
                "line changed",
                "odds changed",
                "line has changed",
                "line moved",
                "not accepted",
                "wager declined",
                "limit exceeded",
            )
        )

    def _set_sport(self, sport: str):
        """Switch sport context (URL/league) on an existing browser session."""
        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_url = f"https://be.{self.website}/en/sports/basketball/nba/game-lines/"
            self.sport_name = "NBA"
            self.league = "NBA"
            self.game_lines_path = "/basketball/nba/game-lines"
        elif self.sport in ["baseball", "mlb"]:
            self.sport_url = f"https://be.{self.website}/en/sports/baseball/mlb/game-lines/"
            self.sport_name = "MLB"
            self.league = "MLB"
            self.game_lines_path = "/baseball/mlb/game-lines"
        else:
            raise ValueError(f"Unsupported sport: {sport}")

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
            self.logger.info("Session valid for odds fetch; navigating to sport page only")
            self._return_to_sport_page()
            return

        self.logger.info("Session invalid for odds fetch; performing full login")
        self.__login()

    def _cached_odd_int(self, moneyline_odd) -> int:
        return int(float(moneyline_odd))

    def _arb_odds_match(self, cached_odd, live_odd) -> bool:
        tolerance = getattr(self, "_odds_tolerance", 0) or 0
        return arb_moneyline_odds_acceptable(cached_odd, live_odd, tolerance)

    def _reject_if_line_moved(self, cached_odd, live_odd, where: str):
        tolerance = getattr(self, "_odds_tolerance", 0) or 0
        if self._arb_odds_match(cached_odd, live_odd):
            if tolerance > 0:
                try:
                    from utils.helpers import american_odds_to_int
                    exp = american_odds_to_int(cached_odd)
                    liv = american_odds_to_int(live_odd)
                    if exp != liv:
                        self.logger.info(
                            f"Accepting line within ±{tolerance} at {where}: "
                            f"arb {cached_odd} vs live {live_odd}"
                        )
                except (TypeError, ValueError):
                    pass
            return
        raise Exception(
            f"Line moved ({where}): live odds {live_odd} differ from arb odds {cached_odd}"
            + (f" (tolerance ±{tolerance})" if tolerance > 0 else "")
        )

    def _find_moneyline_label(self, game_container, team_name: str, moneyline_odd):
        labels = game_container.find_elements(
            By.CSS_SELECTOR,
            ".mline-1 label.bet-indicator, .mline-2 label.bet-indicator",
        )
        team_match = None
        team_match_odds = None

        for idx, label in enumerate(labels):
            title = (label.get_attribute("title") or "").strip()
            team = ""
            odds_text = ""

            if title:
                match = re.match(r"(.+?)\s([+-]\d+)", title)
                if match:
                    team = match.group(1).strip()
                    odds_text = match.group(2).strip()

            if not odds_text:
                try:
                    odds_text = label.find_element(By.CSS_SELECTOR, ".odds span").text.strip()
                except Exception:
                    odds_text = ""

            self.logger.info(
                f"Moneyline [{idx}] | Team: '{team}' | Odds: '{odds_text}' | Title: '{title}'"
            )

            if team.lower() != team_name.lower():
                continue

            if odds_text and self._arb_odds_match(moneyline_odd, odds_text):
                self.logger.info(
                    f"Matched Moneyline | Index: {idx} | Team: {team} | Odds: {odds_text}"
                )
                return label, odds_text

            if odds_text and team_match is None:
                team_match = label
                team_match_odds = odds_text

        if team_match is not None:
            self._reject_if_line_moved(
                moneyline_odd, team_match_odds, f"board for {team_name}"
            )

        return None, None

    def _find_spread_label(
        self,
        game_container,
        team_name: str,
        spread_line: float | None,
        odds: str,
    ):
        labels = game_container.find_elements(
            By.CSS_SELECTOR,
            ".hdp label.bet-indicator",
        )
        team_match = None
        team_match_odds = None

        for idx, label in enumerate(labels):
            spread_val, odds_text = extract_spread_line_odds_from_label(label)
            if spread_val is None or not odds_text:
                continue

            title = (label.get_attribute("title") or "").strip()
            team = ""
            if title:
                match = re.match(r"(.+?)\s([+-]?\d+(?:\.\d+)?)", title)
                if match:
                    team = match.group(1).strip()

            self.logger.info(
                f"Spread [{idx}] | Team: '{team}' | Line: {spread_val} | Odds: '{odds_text}' | Title: '{title}'"
            )

            if team.lower() != team_name.lower():
                continue

            if spread_line is not None and not spread_values_match(spread_val, spread_line):
                continue

            if self._arb_odds_match(odds, odds_text):
                self.logger.info(
                    f"Matched Spread | Index: {idx} | Team: {team} | "
                    f"Line: {spread_val} | Odds: {odds_text}"
                )
                return label, odds_text

            if team_match is None:
                team_match = label
                team_match_odds = odds_text

        if team_match is not None:
            self._reject_if_line_moved(odds, team_match_odds, f"spread board for {team_name}")

        return None, None

    def _get_betslip_team_name(self) -> str:
        try:
            return (
                self.driver.find_element(By.CSS_SELECTOR, "#betslip .team-name").text or ""
            ).strip()
        except Exception:
            return ""

    def _betslip_is_empty(self) -> bool:
        try:
            slip_text = (self.driver.find_element(By.ID, "betslip").text or "").lower()
            if any(marker in slip_text for marker in ("empty", "no selections", "bet slip is empty")):
                return True
            return not self.driver.find_elements(By.CSS_SELECTOR, "#betslip .team-name")
        except Exception:
            return True

    def _clear_betslip(self):
        """Remove stale selections so rapid arb cycling does not read the previous leg."""
        for _ in range(6):
            if self._betslip_is_empty():
                return

            removed = False
            for selector in (
                "#betslip button.remove-bet",
                "#betslip .remove-bet",
                "#betslip .glyphicon-remove",
                "#betslip a.remove",
                "#betslip button.close",
                "#betslip [class*='remove']",
            ):
                for btn in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if btn.is_displayed():
                            self.driver.execute_script("arguments[0].click();", btn)
                            removed = True
                            time.sleep(0.3)
                            break
                    except Exception:
                        continue
                if removed:
                    break

            if not removed:
                break

        time.sleep(0.2)

    def _get_betslip_odds_text(self) -> str:
        for selector in (
            "#betslip .odds span",
            "#betslip .odds",
            "#betslip .line-odds",
            "#betslip .price",
        ):
            try:
                text = (
                    self.driver.find_element(By.CSS_SELECTOR, selector).text or ""
                ).strip()
                if text:
                    return text
            except Exception:
                continue
        return ""

    def _wait_for_betslip_team(self, team_name: str, timeout: int = 8) -> str:
        deadline = time.time() + timeout
        last_seen = ""
        while time.time() < deadline:
            last_seen = self._get_betslip_team_name()
            if last_seen.lower() == team_name.lower():
                return last_seen
            time.sleep(0.35)
        return last_seen

    def _install_wager_network_hook(self):
        self.driver.execute_script("""
            window.__wagerResponses = [];
            if (window.__wagerHookInstalled) return;
            window.__wagerHookInstalled = true;
            const capture = (url, body, request) => {
                if (!url) return;
                const u = String(url).toLowerCase();
                if (u.includes('wager') || u.includes('bet') || u.includes('ticket')
                    || u.includes('place') || u.includes('pending') || u.includes('sendbets')) {
                    window.__wagerResponses.push({
                        url: String(url),
                        request: String(request || '').slice(0, 8000),
                        body: String(body || '').slice(0, 4000)
                    });
                }
            };
            const origFetch = window.fetch;
            window.fetch = function(...args) {
                const reqUrl = args[0];
                const reqBody = args[1] && args[1].body ? args[1].body : '';
                return origFetch.apply(this, args).then(resp => {
                    resp.clone().text().then(t => capture(reqUrl, t, reqBody)).catch(() => {});
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
                const reqBody = args[0];
                this.addEventListener('load', function() {
                    capture(this.__arbUrl, this.responseText, reqBody);
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

    def _prefer_accept_all_line_changes(self):
        """Select 'Accept all line changes' on the betslip (automation is slower than manual)."""
        try:
            for elem in self.driver.find_elements(
                By.CSS_SELECTOR, "#betslip label, #betslip span, #betslip div"
            ):
                text = (elem.text or "").strip().lower()
                if "accept all line change" in text:
                    self._human_click(elem)
                    self.logger.info("Selected 'Accept all line changes' on betslip")
                    return True
        except Exception:
            pass
        for radio in self.driver.find_elements(
            By.CSS_SELECTOR, "#betslip input[type='radio']"
        ):
            try:
                label_id = radio.get_attribute("id")
                label_text = ""
                if label_id:
                    labels = self.driver.find_elements(By.CSS_SELECTOR, f"label[for='{label_id}']")
                    if labels:
                        label_text = (labels[0].text or "").lower()
                if "accept all" in label_text and not radio.is_selected():
                    self._human_click(radio)
                    self.logger.info("Selected 'Accept all line changes' radio on betslip")
                    return True
            except Exception:
                continue
        return False

    def _accept_line_changes(self):
        accepted = False
        try:
            accept_all = self.driver.find_element(By.ID, "accept_all")
            if not accept_all.is_selected():
                self._human_click(accept_all)
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
                        self._human_click(elem)
                        accepted = True
            except Exception:
                continue

        for btn in self.driver.find_elements(By.CSS_SELECTOR, "button, a"):
            try:
                text = (btn.text or "").strip().lower()
                if text in ("accept", "accept changes", "ok", "continue"):
                    self._human_click(btn)
                    accepted = True
                    time.sleep(0.5)
            except Exception:
                continue

        if accepted:
            self.logger.info("Accepted line changes / odds update prompts")
        return accepted

    def _betslip_needs_line_acceptance(self) -> bool:
        try:
            slip_l = (self.driver.find_element(By.ID, "betslip").text or "").lower()
        except Exception:
            return False
        return any(
            marker in slip_l
            for marker in (
                "line changed",
                "odds changed",
                "accept changes",
                "accept change",
                "price changed",
            )
        )

    def _dispatch_stake_input_events(self, stake_input):
        for event_name in ("input", "change", "blur"):
            self.driver.execute_script(
                f"arguments[0].dispatchEvent(new Event('{event_name}', {{bubbles:true}}));",
                stake_input,
            )

    def _find_enabled_place_bet_button(self):
        for selector in (
            ".place-bet-container button.btn-primary",
            "#betslip button.btn-primary",
            "#betslip .place-bet-container button",
        ):
            for btn in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if btn.is_displayed() and btn.is_enabled():
                        return btn
                except Exception:
                    continue
        return None

    def _require_place_bet_ready(self, cached_odd):
        if self._betslip_needs_line_acceptance():
            raise Exception(
                "Line moved: betslip shows line/odds change prompt; rejecting per arb odds"
            )

        slip_odds = self._get_betslip_odds_text()
        if slip_odds:
            self._reject_if_line_moved(cached_odd, slip_odds, "betslip")

        btn = self._find_enabled_place_bet_button()
        if btn is None:
            raise Exception(
                "Line moved: Place Bet button disabled (odds likely changed from arb)"
            )
        return btn

    def _stake_on_open_bets_page(self, page: str, stake) -> bool:
        if isinstance(stake, BaseAmountStake):
            return page_contains_stake_amount(page, stake)
        page_l = page or ""
        patterns = (
            f"risk:${stake:.2f}",
            f"risk: ${stake:.2f}",
            f"risk:${stake:.0f}",
            f"risk: ${stake:.0f}",
            f"${stake:.2f}",
            f"${stake:.0f}",
            f"{stake:.2f}",
        )
        page_compact = page_l.lower().replace(" ", "")
        return any(p.replace(" ", "").lower() in page_compact for p in patterns)

    def _verify_open_bet_on_pending(self, team_name: str, stake: float):
        try:
            page = self._fetch_pending_page_text()
            if not page or team_name.lower() not in (page or "").lower():
                self.driver.get(self._open_bets_url())
                time.sleep(2.5)
                page = self.driver.page_source
                self._return_to_sport_page()

            page_l = (page or "").lower()
            team_l = team_name.lower()
            if team_l not in page_l:
                return False, "Bet not found on open bets page"
            if self._stake_on_open_bets_page(page, stake):
                return True, "Open bet found on open bets page"
            return False, "Team found on open bets page but stake not verified"
        except Exception as e:
            return False, f"Could not verify open bet: {e}"

    def _poll_open_bet_on_pending(
        self, team_name: str, stake: float, timeout: int = None
    ):
        """SendBets uses asyncPost — wager may appear on open-bets after WagerResult:false."""
        timeout = timeout or self.OPEN_BETS_POLL_TIMEOUT_SECONDS
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            confirmed, message = self._verify_open_bet_on_pending(team_name, stake)
            if confirmed:
                self.logger.info(
                    f"Open bet confirmed (poll {attempt}): {message}"
                )
                return True, message
            self.logger.info(
                f"Open-bets poll {attempt}: not found yet ({message})"
            )
            time.sleep(2)
        return False, "Bet not found on open bets page after polling"

    def _recover_bet_from_open_bets(self, team_name: str, stake: float) -> tuple:
        """True when the book accepted the wager even if SendBets/confirm failed."""
        return self._poll_open_bet_on_pending(team_name, stake)

    def _confirm_bet_accepted(
        self,
        team_name: str,
        stake: float,
        timeout: int = None,
    ):
        timeout = timeout or self.CONFIRM_TIMEOUT_SECONDS
        deadline = time.time() + timeout
        sendbets_seen = False
        while time.time() < deadline:
            rejected, reject_msg = self._scan_rejection_ui()
            if rejected:
                if self._message_requires_relogin(reject_msg):
                    self._invalidate_wager_session()
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
                url = entry.get("url") or ""
                body = entry.get("body", "") or ""
                if "sendbets" in url.lower():
                    sendbets_seen = True
                verdict, detail = self._parse_wager_api_body(body)
                if verdict == "rejected":
                    if self._message_requires_relogin(body):
                        self._invalidate_wager_session()
                    req = entry.get("request") or ""
                    if req and "sendbets" in url.lower():
                        self.logger.info(f"SendBets request: {req[:1200]}")
                    self.logger.warning(
                        f"SendBets API rejected ({detail}); checking open bets (asyncPost)"
                    )
                    confirmed, open_msg = self._recover_bet_from_open_bets(
                        team_name, stake
                    )
                    if confirmed:
                        return True, f"Open bet confirmed ({open_msg})"
                    self.logger.error(f"Wager API rejection ({url}): {body[:800]}")
                    return False, f"Wager API rejected: {detail}"
                if verdict == "accepted":
                    self.logger.info(f"Wager API success ({url}): {body[:300]}")
                    return True, detail

                body_l = body.lower()
                if any(m in body_l for m in ("rejected", "declined", "failed")):
                    if self._message_requires_relogin(body):
                        self._invalidate_wager_session()
                    confirmed, open_msg = self._recover_bet_from_open_bets(
                        team_name, stake
                    )
                    if confirmed:
                        return True, f"Open bet confirmed ({open_msg})"
                    self.logger.error(f"Wager API rejection ({url}): {body[:800]}")
                    snippet = body[:200].replace("\n", " ").strip()
                    msg = f"{url} | {snippet}" if snippet else url
                    return False, f"Wager API rejected: {msg}"
                if any(m in body_l for m in ("accepted", "confirmed", "success", "ticket")):
                    self.logger.info(f"Wager API success ({url}): {body[:300]}")
                    return True, "Wager API confirmed"

            time.sleep(0.4)

        if sendbets_seen:
            self.logger.warning(
                "SendBets seen without acceptance; polling open bets page"
            )
            confirmed, message = self._recover_bet_from_open_bets(team_name, stake)
            if confirmed:
                return True, message
            return False, "SendBets returned no acceptance and bet not on open bets"

        self.logger.warning(
            f"No API confirmation within {timeout}s; checking open bets page"
        )
        confirmed, message = self._verify_open_bet_on_pending(team_name, stake)
        if confirmed:
            return True, message
        return False, message

    # Execute Bet
    # --------------------------------------------------------
    def __execute_bet(
        self,
        game_id: str,
        team_name: str,
        moneyline_odd: str,
        stake: float = 1.0,
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
            for attempt in range(1, 3):
                try:
                    if attempt > 1:
                        self.logger.info(f"Retrying wager after re-login (attempt {attempt}/2)")
                    return self._execute_bet_attempt(
                        game_id,
                        team_name,
                        moneyline_odd,
                        stake,
                        bet_type=bet_type,
                        spread_line=spread_line,
                    )
                except Exception as e:
                    confirmed, open_msg = self._verify_open_bet_on_pending(
                        team_name, stake_plan
                    )
                    if confirmed:
                        self.logger.info(
                            f"Bet on open bets despite error '{e}': {open_msg}"
                        )
                        return True, stake_plan
                    if attempt == 1 and self._message_requires_relogin(str(e)):
                        self.logger.warning(
                            f"Wager blocked by expired session ({e}); forcing re-login and retry"
                        )
                        self._invalidate_wager_session()
                        self.__login()
                        self._return_to_sport_page()
                        continue
                    raise
            return False, stake

        except Exception as e:
            self._last_bet_error = str(e)
            confirmed, pending_msg = self._recover_bet_from_open_bets(team_name, stake_plan)
            if confirmed:
                self.logger.info(
                    f"Place Bet raised '{e}' but bet is on open bets: {pending_msg}"
                )
                return True, stake_plan
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
        bet_type: str = "moneyline",
        spread_line: float | None = None,
    ):
        stake_plan = base_amount_stake_from_odds(moneyline_odd, stake)
        market_label = (
            f"spread {spread_line:+.1f}" if bet_type == "spread" and spread_line is not None else bet_type
        )
        self.logger.info(
            f"Placing Bet | Game ID: {game_id} | Team: {team_name} | "
            f"Market: {market_label} | Odds: {moneyline_odd} | {format_base_amount_stake(stake_plan)}"
        )

        already_open, open_msg = self._verify_open_bet_on_pending(team_name, stake_plan)
        if already_open:
            self.logger.info(
                f"Skipping placement — bet already on open bets: {open_msg}"
            )
            return True, stake_plan

        self._refresh_session_before_wager()

        if not self._game_visible_on_page(game_id):
            if self._is_off_sport_page(self.driver.current_url):
                self.logger.info(f"Navigating to {self.sport_name} page before bet placement")
                self._return_to_sport_page()

            game_container = None
            try:
                game_container = WebDriverWait(self.driver, 12).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, f"div.sports-league-game[idgame='{game_id}']")
                    )
                )
            except TimeoutException:
                self.logger.warning(
                    f"Game container not found for {game_id}, reloading {self.sport_name} page once"
                )
                self._return_to_sport_page()
                game_container = WebDriverWait(self.driver, 12).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, f"div.sports-league-game[idgame='{game_id}']")
                    )
                )
        else:
            game_container = self.driver.find_element(
                By.CSS_SELECTOR, f"div.sports-league-game[idgame='{game_id}']"
            )

        if bet_type == "spread":
            line_label, live_odds = self._find_spread_label(
                game_container, team_name, spread_line, moneyline_odd
            )
            if not line_label:
                raise Exception("Spread label not found for given team, line & odds")
            line_kind = "Spread"
        else:
            line_label, live_odds = self._find_moneyline_label(
                game_container, team_name, moneyline_odd
            )
            if not line_label:
                raise Exception("Moneyline label not found for given team & odds")
            line_kind = "Moneyline"

        self._reject_if_line_moved(moneyline_odd, live_odds, f"board click for {team_name}")

        self.logger.info(f"{line_kind} Label: {line_label}")

        self.wait.until(EC.presence_of_element_located((By.ID, "betslip")))
        self._clear_betslip()

        self._human_click(line_label, force_xdotool=self.use_xdotool and not self.xdotool_bet_only)
        self.logger.info(f"{line_kind} label clicked")

        betslip_team = self._wait_for_betslip_team(team_name, timeout=8)
        if betslip_team.lower() != team_name.lower():
            self.logger.warning(
                f"Betslip still shows '{betslip_team}' after first click; retrying {line_kind.lower()} click"
            )
            self._human_click(line_label, force_xdotool=self.use_xdotool and not self.xdotool_bet_only)
            betslip_team = self._wait_for_betslip_team(team_name, timeout=6)

        if betslip_team.lower() != team_name.lower():
            raise Exception(
                f"Betslip team mismatch | Expected: {team_name} | Found: {betslip_team}"
            )

        self.logger.info(f"Betslip verified | Team: {betslip_team}")
        self._reject_if_line_moved(moneyline_odd, self._get_betslip_odds_text(), "betslip after add")

        try:
            bet_limits = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#betslip .bet-limits"))
            )
            amounts = bet_limits.find_elements(By.CSS_SELECTOR, "span.amount")
            min_bet = currency_to_float(amounts[0].text.strip()) if len(amounts) > 0 else "N/A"
            max_bet = currency_to_float(amounts[1].text.strip()) if len(amounts) > 1 else "N/A"
            self.logger.info(
                f"Bet Limits | Min Bet: {min_bet} | Max Bet: {max_bet} | "
                f"Risk: {stake_plan.risk:.2f}"
            )
            if stake_plan.risk < min_bet:
                raise Exception(f"Risk {stake_plan.risk} is below minimum bet {min_bet}")
            if max_bet > 0 and stake_plan.risk > max_bet:
                raise Exception(f"Risk {stake_plan.risk} exceeds maximum bet {max_bet}")
        except Exception as e:
            self.logger.warning(f"Bet limits could not be determined: {e}")

        if self.use_xdotool and not self.xdotool_bet_only:
            win_sel = "input[id^='win_']"
            risk_sel = "input[id^='risk_']"
            css = win_sel if stake_plan.entry_field == "to_win" else risk_sel
            stake_input = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, css))
            )
            self._human_type(
                stake_input, f"{stake_plan.entry_amount:.2f}", force_xdotool=True
            )
        elif not fill_betslip_stake_input(
            self.driver, stake_plan, self.logger, scope_css="#betslip"
        ):
            raise Exception("Could not locate bet slip stake input for base amount")

        place_bet_btn = self._require_place_bet_ready(moneyline_odd)

        if self._page_has_login_required_marker():
            self._invalidate_wager_session()
            raise Exception("Rejection marker on page: please log in")

        self._prefer_accept_all_line_changes()
        self._accept_line_changes()
        self._install_wager_network_hook()

        # Production path: one Selenium Place Bet click (no retries — avoids duplicates).
        if self.attach_browser and self.use_xdotool:
            coords = self._element_screen_coords_for_xdotool(place_bet_btn)
            self.logger.info(
                f"xdotool Place Bet at screen ({coords['x']}, {coords['y']})"
            )
            self._xdotool_click_screen(coords["x"], coords["y"])
        else:
            self._human_click(place_bet_btn, force_xdotool=False)
        self.logger.info("Place Bet button clicked (single attempt)")

        network_log = self._get_wager_network_log()
        if network_log:
            self.logger.info(f"Wager network activity after click: {network_log[-3:]}")
        else:
            self.logger.warning(
                "No wager network activity detected immediately after Place Bet click"
            )

        confirmed, message = self._confirm_bet_accepted(team_name, stake_plan)
        if not confirmed:
            raise Exception(message or "Bet not accepted by bookmaker")

        self.logger.info(f"Bet accepted by bookmaker: {message}")
        return True, stake_plan

    def _quit_driver(self):
        """Safely terminate only this controller's WebDriver session."""
        driver = getattr(self, "driver", None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        self.driver = None
        self.wait = None

        proc = getattr(self, "_debug_chrome_proc", None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._debug_chrome_proc = None

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
        self._last_saved_ml = {}
        force_scan_interval = self.ODDS_WATCH_FORCE_SCAN_SECONDS
        poll_interval = self.ODDS_WATCH_POLL_SECONDS
        last_force_scan = 0.0

        self.logger.info(
            f"==================== Betting ({self.sport_name}) (START) — "
            f"unified session: odds watch when idle, bet when arb ({force_scan_interval}s force scan) "
            f"===================="
        )

        watchdog = BettingLoopWatchdog(self.logger, max_silent_seconds=300)
        watchdog.start()

        # Clean only stale temp dirs; never pkill all Chrome (other jobs may be running).
        self._cleanup_stale_temp_dirs()

        setup_ok = False
        for attempt in range(1, 6):
            watchdog.beat()
            try:
                setup_ok = self._prepare_odds_watch_page()
                if setup_ok:
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
        consecutive_soft_nav_failures = 0
        self._exposure_cleanup_at = 0.0
        last_idle_poll_at = 0.0
        while True:
            watchdog.beat()
            self._exposure_cleanup_at = tick_exposure_cleanup(
                self.cache, self.logger, self._exposure_cleanup_at
            )

            try:
                current_url = self.driver.current_url
            except Exception as e:
                self.logger.error(f"Driver error getting current URL: {e}. Attempting recovery...")
                self._recover_driver()
                consecutive_recoveries += 1
                consecutive_soft_nav_failures = 0
                if consecutive_recoveries >= 3:
                    backoff = min(60, 10 * consecutive_recoveries)
                    self.logger.warning(f"Multiple recoveries ({consecutive_recoveries}). Backing off {backoff}s.")
                    time.sleep(backoff)
                    consecutive_recoveries = 0
                if not self._relogin_after_recovery():
                    time.sleep(8)
                    continue
                if self._prepare_odds_watch_page():
                    last_force_scan = 0.0
                continue

            if self._is_off_sport_page(current_url):
                if self._should_soft_navigate_back(current_url):
                    self.logger.warning(
                        f"Off {self.sport_name} page ({current_url}); navigating back without driver reset"
                    )
                    self._return_to_sport_page()
                    if self._is_on_sport_page_with_games():
                        self._ensure_odds_mutation_observer()
                        consecutive_soft_nav_failures = 0
                        continue
                    consecutive_soft_nav_failures += 1
                    self.logger.warning(
                        f"Still off {self.sport_name} page after soft navigation "
                        f"({consecutive_soft_nav_failures}/{self.MAX_SOFT_NAV_FAILURES})"
                    )
                    if consecutive_soft_nav_failures >= self.MAX_SOFT_NAV_FAILURES:
                        self.logger.warning(
                            f"Soft navigation failed {consecutive_soft_nav_failures} times; "
                            "escalating to driver recovery"
                        )
                        consecutive_soft_nav_failures = 0
                        self._recover_driver()
                        consecutive_recoveries += 1
                        if consecutive_recoveries >= 3:
                            backoff = min(60, 10 * consecutive_recoveries)
                            self.logger.warning(
                                f"Multiple recoveries ({consecutive_recoveries}). Backing off {backoff}s."
                            )
                            time.sleep(backoff)
                            consecutive_recoveries = 0
                        if not self._relogin_after_recovery():
                            time.sleep(8)
                            continue
                        if self._prepare_odds_watch_page():
                            last_force_scan = 0.0
                    continue
                self.logger.warning(f"Unexpected URL detected ({current_url}). Re-establishing session...")
                self._recover_driver()
                consecutive_recoveries += 1
                consecutive_soft_nav_failures = 0
                if consecutive_recoveries >= 3:
                    backoff = min(60, 10 * consecutive_recoveries)
                    self.logger.warning(f"Multiple recoveries ({consecutive_recoveries}). Backing off {backoff}s.")
                    time.sleep(backoff)
                    consecutive_recoveries = 0
                if not self._relogin_after_recovery():
                    time.sleep(8)
                    continue
                if self._prepare_odds_watch_page():
                    last_force_scan = 0.0
                continue

            consecutive_recoveries = 0
            consecutive_soft_nav_failures = 0
            arbs = get_arbitrage_for_placement(self.cache, self.bookmaker)
            matching_arbs = [
                arb for arb in arbs
                if arb.get("sport") == self.sport_name and arb.get("league") == self.league
            ]
            if not matching_arbs:
                def _idle_odds_tick():
                    nonlocal last_force_scan
                    last_force_scan, _processed = self._tick_odds_watch_once(
                        last_force_scan,
                        force_scan_interval,
                        idle_label="betting-idle",
                    )

                _, last_idle_poll_at = wait_for_arb_or_idle(
                    self.cache,
                    self.bookmaker,
                    idle_poll_fn=_idle_odds_tick,
                    idle_poll_interval=poll_interval,
                    last_idle_poll_at=last_idle_poll_at,
                )
                continue

            self.logger.info(f"Arbitrage: {len(matching_arbs)} — pausing odds watch for placement")

            for arb in matching_arbs:

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

                if not is_game_pregame(game_datetime):
                    self.logger.info(
                        f"Skipping arb (game started) | Match: {team_1} vs {team_2}"
                    )
                    continue

                self.logger.info(
                    f"Arbitrage | Match: {team_1} vs {team_2}"
                )

                book_1 = arb.get("team_1_bookmaker")
                book_2 = arb.get("team_2_bookmaker")

                if not is_active_arb_pair(book_1, book_2):
                    self.logger.info(
                        f"Skipping arb — inactive book pair {book_1} x {book_2} | "
                        f"{team_1} vs {team_2}"
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
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

                if should_skip_arb_leg_in_betting_loop(
                    self.cache,
                    self.logger,
                    arb,
                    self.bookmaker,
                    team_name,
                    team_1,
                    team_2,
                ):
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

                if self._has_existing_open_bet(team_name, team_1, team_2):
                    self.logger.warning(
                        f"Open wager already on open-bets for {team_name} on {self.bookmaker}; "
                        f"skipping duplicate placement"
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                self._odds_tolerance = odds_tolerance_for_placement(
                    self.cache, arb, book_1, book_2, self.bookmaker, bet_type
                )
                if self._odds_tolerance:
                    self.logger.info(
                        f"Second-leg odds tolerance ±{self._odds_tolerance} | {team_1} vs {team_2}"
                    )

                stake = resolve_arb_leg_stake(
                    self.cache,
                    arb,
                    book_1,
                    book_2,
                    self.bookmaker,
                    wager_odds,
                    stake,
                    logger=self.logger,
                )

                bet_placed, stake_used = self.__execute_bet(
                    game_id,
                    team_name,
                    wager_odds,
                    stake,
                    bet_type=bet_type,
                    spread_line=spread_line,
                )

                if not bet_placed:
                    recovered, open_msg = self._recover_bet_from_open_bets(
                        team_name, stake
                    )
                    if recovered:
                        self.logger.info(
                            f"Recovered placement from open bets after failed confirm: "
                            f"{open_msg}"
                        )
                        bet_placed = True
                        stake_used = stake
                    elif should_notify_failed_bet(self._last_bet_error):
                        maybe_notify_partial_arb_exposure(
                            self.cache,
                            self.logger,
                            arb,
                            self.bookmaker,
                            stake,
                            self._last_bet_error or "Bet not accepted by bookmaker",
                            TELEGRAM,
                        )
                if (
                    not bet_placed
                    and self._last_bet_error
                    and (
                        "invalidate cached arb odds" in self._last_bet_error
                        or "line moved" in self._last_bet_error.lower()
                    )
                ):
                    self.logger.warning(
                        f"Removing stale arb from cache for {team_1} vs {team_2} on {self.bookmaker}"
                    )
                    self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                    continue

                if not bet_placed and self._is_fast_book_rejection(self._last_bet_error):
                    fail_key = f"{game_id}:{team_name}".lower()
                    self._arb_fail_counts[fail_key] = self._arb_fail_counts.get(fail_key, 0) + 1
                    fail_count = self._arb_fail_counts[fail_key]
                    if fail_count >= self.MAX_WAGER_ATTEMPTS_PER_ARB:
                        self.logger.warning(
                            f"Dropping arb after {fail_count} book rejections on {self.bookmaker} | "
                            f"{team_name} | {team_1} vs {team_2}"
                        )
                        self.cache.remove_arbitrage_for_bookmaker(arb, self.bookmaker)
                        self._arb_fail_counts.pop(fail_key, None)
                    continue

                if bet_placed:
                    self._arb_fail_counts.pop(f"{game_id}:{team_name}".lower(), None)
                    self.logger.info("Bet Placement Completed")
                    finalize_confirmed_bet_with_screenshot(
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
                        driver=self.driver,
                        open_bets_url=self._open_bets_url(),
                        return_to_sport=self._return_to_sport_page,
                        ticket_number=getattr(self, "_last_ticket_number", None),
                    )
                    self.logger.info("Returning to sport page and resuming odds watch")
                    self._resume_odds_watch_on_sport_page()
                    last_force_scan = 0.0

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




