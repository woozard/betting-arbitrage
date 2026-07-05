"""Selenium helpers for 4casters.io Active Wagers screenshots (API books use web UI for receipts)."""

from __future__ import annotations

import os
import tempfile
import time

from utils.helpers import teams_same
from utils.stake_sizing import BaseAmountStake, stake_matches_verification_amount

FOURCASTERS_ACTIVE_WAGERS_URL = "https://4casters.io/my-bets/active-wagers"
FOURCASTERS_LOGIN_URL = "https://4casters.io/log-in"


def _page_requires_login(driver) -> bool:
    try:
        url = (driver.current_url or "").lower()
        if "log-in" in url or "login" in url or "sign-up" in url:
            return True
        body = (driver.find_element("tag name", "body").text or "").upper()
        if "LOG IN" in body and "ACTIVE WAGERS" not in body:
            # Login landing page — not the wagers list.
            if driver.find_elements("css selector", 'input[type="password"]'):
                return True
    except Exception:
        return True
    return False


def create_fourcasters_driver():
    from selenium import webdriver

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--disable-notifications")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    user_data_dir = tempfile.mkdtemp(prefix="fourcasters_chrome_")
    options.add_argument(f"--user-data-dir={user_data_dir}")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    driver._fourcasters_user_data_dir = user_data_dir  # type: ignore[attr-defined]
    return driver


def quit_fourcasters_driver(driver) -> None:
    if not driver:
        return
    user_data_dir = getattr(driver, "_fourcasters_user_data_dir", None)
    try:
        driver.quit()
    except Exception:
        pass
    if user_data_dir and os.path.isdir(user_data_dir):
        try:
            import shutil

            shutil.rmtree(user_data_dir, ignore_errors=True)
        except Exception:
            pass


def _inject_api_token(driver, api_token: str) -> None:
    driver.get("https://4casters.io/")
    time.sleep(1.0)
    driver.execute_script(
        """
        const token = arguments[0];
        const userPayload = JSON.stringify({ auth: token });
        for (const key of ['auth', 'token', 'authToken', 'authorization', 'user']) {
          try { localStorage.setItem(key, key === 'user' ? userPayload : token); } catch (e) {}
        }
        try { sessionStorage.setItem('auth', token); } catch (e) {}
        """,
        api_token,
    )


def login_fourcasters_web(
    driver,
    username: str,
    password: str,
    logger,
    *,
    api_token: str | None = None,
) -> bool:
    """Authenticate on 4casters.io (token injection first, then form login)."""
    if api_token:
        try:
            _inject_api_token(driver, api_token)
            driver.get(FOURCASTERS_ACTIVE_WAGERS_URL)
            time.sleep(2.0)
            if not _page_requires_login(driver):
                logger.info("4casters web session ready (API token)")
                return True
        except Exception as exc:
            logger.warning(f"4casters token injection failed, trying form login: {exc}")

    driver.get(FOURCASTERS_LOGIN_URL)
    time.sleep(2.0)

    try:
        user_el = driver.execute_script(
            """
            const inputs = [...document.querySelectorAll('input')];
            return inputs.find(el => {
              const t = (el.type || '').toLowerCase();
              const n = (el.name || el.id || el.placeholder || '').toLowerCase();
              return t === 'text' || t === 'email' || n.includes('user') || n.includes('email');
            });
            """
        )
        pass_el = driver.execute_script(
            """
            return [...document.querySelectorAll('input[type="password"]')][0] || null;
            """
        )
        if not user_el or not pass_el:
            logger.warning("4casters login form fields not found")
            return False

        user_el.clear()
        user_el.send_keys(username)
        pass_el.clear()
        pass_el.send_keys(password)

        clicked = driver.execute_script(
            """
            const labels = ['log in', 'login', 'sign in'];
            for (const el of document.querySelectorAll('button, a, input[type="submit"]')) {
              const t = (el.innerText || el.value || '').trim().toLowerCase();
              if (labels.some(l => t === l || t.includes(l))) {
                el.click();
                return true;
              }
            }
            return false;
            """
        )
        if not clicked:
            pass_el.submit()

        time.sleep(3.0)
        driver.get(FOURCASTERS_ACTIVE_WAGERS_URL)
        time.sleep(2.0)
        if _page_requires_login(driver):
            logger.warning("4casters web login did not reach Active Wagers")
            return False
        logger.info("4casters web session ready (form login)")
        return True
    except Exception as exc:
        logger.warning(f"4casters web login failed: {exc}")
        return False


def ensure_fourcasters_web_session(
    username: str,
    password: str,
    logger,
    *,
    api_token: str | None = None,
    existing_driver=None,
):
    driver = existing_driver
    created = False
    if driver is None:
        driver = create_fourcasters_driver()
        created = True

    try:
        driver.get(FOURCASTERS_ACTIVE_WAGERS_URL)
        time.sleep(1.5)
        if _page_requires_login(driver):
            if not login_fourcasters_web(
                driver, username, password, logger, api_token=api_token
            ):
                if created:
                    quit_fourcasters_driver(driver)
                return None
        return driver
    except Exception as exc:
        logger.warning(f"4casters web session setup failed: {exc}")
        if created:
            quit_fourcasters_driver(driver)
        return None


def _wager_text_matches(
    text: str,
    team_name: str,
    team_1: str = "",
    team_2: str = "",
) -> bool:
    blob = (text or "").strip()
    if not blob:
        return False
    if team_name and teams_same(blob, team_name):
        return True
    if team_name and team_name.lower() in blob.lower():
        return True
    last = (team_name or "").strip().split()[-1].lower()
    if last and last in blob.lower():
        return True
    for side in (team_1, team_2):
        if side and teams_same(blob, side):
            return True
        side_last = side.strip().split()[-1].lower() if side else ""
        if side_last and side_last in blob.lower():
            return True
    return False


def _stake_in_wager_text(text: str, stake) -> bool:
    if stake is None:
        return True
    import re

    first_line = (text or "").splitlines()[0] if text else ""
    amounts = re.findall(r"\$?\s*(\d+(?:\.\d+)?)", first_line.replace(",", ""))
    for raw in amounts:
        if stake_matches_verification_amount(stake, raw):
            return True
    if isinstance(stake, BaseAmountStake):
        for val in (stake.risk, stake.to_win, stake.entry_amount, stake.base_amount):
            if val is not None and str(int(round(float(val)))) in (text or ""):
                return True
    return False


def capture_fourcasters_active_wager(
    driver,
    path: str,
    logger,
    *,
    team_name: str,
    team_1: str = "",
    team_2: str = "",
    stake=None,
    open_bets_url: str | None = None,
    return_to_url: str | None = None,
) -> str | None:
    """
    Open Active Wagers, expand the matching game row, screenshot the newest wager
    in that game's summary (first row in the expanded list).
    """
    from utils.bet_screenshot import _write_png

    url = open_bets_url or FOURCASTERS_ACTIVE_WAGERS_URL
    try:
        driver.get(url)
        time.sleep(2.5)

        matched_row = driver.execute_script(
            """
            const teamName = (arguments[0] || '').toLowerCase();
            const team1 = (arguments[1] || '').toLowerCase();
            const team2 = (arguments[2] || '').toLowerCase();
            const needles = [teamName, team1, team2]
              .flatMap(v => v ? [v, v.split(' ').pop()] : [])
              .filter(Boolean);

            function matches(text) {
              const tl = (text || '').toLowerCase();
              return needles.some(n => n && tl.includes(n));
            }

            function isRowLike(el) {
              const t = (el.innerText || '').trim();
              if (t.length < 12 || t.length > 600) return false;
              if (!matches(t)) return false;
              if (!/[+-]\\d{2,4}/.test(t)) return false;
              return true;
            }

            const candidates = [];
            for (const el of document.querySelectorAll('tr, li, div, a, button')) {
              if (!isRowLike(el)) continue;
              let dominated = false;
              for (const other of candidates) {
                if (other !== el && other.contains(el)) { dominated = true; break; }
              }
              if (dominated) continue;
              candidates.push(el);
            }

            if (!candidates.length) return null;
            return candidates[candidates.length - 1];
            """,
            team_name,
            team_1,
            team_2,
        )

        if not matched_row:
            logger.warning(
                f"4casters Active Wagers: no row matched {team_name!r} "
                f"({team_1} vs {team_2})"
            )
            return None

        row_text = (matched_row.text or "").strip()
        if stake is not None and not _stake_in_wager_text(row_text, stake):
            logger.info(
                f"4casters Active Wagers: row matched team but stake differs — using row anyway"
            )

        driver.execute_script(
            """
            const row = arguments[0];
            const clickTarget = row.querySelector(
              'svg, button, [class*="expand"], [class*="plus"], [aria-expanded], .icon'
            ) || row;
            clickTarget.scrollIntoView({block: 'center'});
            clickTarget.click();
            """,
            matched_row,
        )
        time.sleep(1.5)

        detail_el = driver.execute_script(
            """
            const row = arguments[0];
            const root =
              row.closest('[class*="wager"], [class*="bet"], [class*="game"], section, tbody, table')
              || document.body;

            const items = [];
            for (const el of root.querySelectorAll('tr, li, div, article')) {
              const t = (el.innerText || '').trim();
              if (t.length < 10 || t.length > 500) continue;
              if (!/[+-]\\d{2,4}/.test(t)) continue;
              if (!(t.includes('TAKEN') || t.includes('ACTIVE') || /\\$\\d/.test(t))) continue;
              let dominated = false;
              for (const other of items) {
                if (other !== el && other.contains(el)) { dominated = true; break; }
              }
              if (!dominated) items.push(el);
            }

            if (items.length) return items[0];

            const panel = document.querySelector(
              '[class*="expanded"], [class*="detail"], [class*="summary"], [class*="modal"], [role="dialog"]'
            );
            return panel || row;
            """,
            matched_row,
        )

        target = detail_el or matched_row
        png = target.screenshot_as_png
        if png:
            preview = " ".join((row_text or "").split())[:72]
            logger.info(f"4casters Active Wagers screenshot | {team_name} | {preview}")
            return _write_png(path, png, logger)
    except Exception as exc:
        logger.warning(f"4casters Active Wagers screenshot failed: {exc}")
    finally:
        if return_to_url:
            try:
                driver.get(return_to_url)
                time.sleep(0.5)
            except Exception:
                pass

    return None
