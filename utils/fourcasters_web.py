"""Selenium helpers for 4casters.io Active Wagers screenshots."""

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
        if "active-wagers" in url or "my-bets" in url:
            body = (driver.find_element("tag name", "body").text or "").upper()
            if "ACTIVE WAGERS" in body or "TAKEN" in body or "PENDING" in body:
                return False
        if "log-in" in url or "login" in url or "sign-up" in url:
            return True
        if driver.find_elements("css selector", 'input[type="password"]'):
            body = (driver.find_element("tag name", "body").text or "").upper()
            if "ACTIVE WAGERS" not in body and "WALLET" not in body:
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

        # Vue form validation needs input events; Enter on password submits reliably.
        driver.execute_script(
            """
            arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
            arguments[1].dispatchEvent(new Event('input', {bubbles: true}));
            """,
            user_el,
            pass_el,
        )
        from selenium.webdriver.common.keys import Keys

        pass_el.send_keys(Keys.RETURN)

        time.sleep(4.0)
        driver.get(FOURCASTERS_ACTIVE_WAGERS_URL)
        time.sleep(2.5)
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


def _fourcasters_odds_needles(odds) -> list[str]:
    if odds is None or odds == "":
        return []
    needles: list[str] = []
    try:
        val = float(odds)
        n = int(round(val))
        needles.extend([f"{n:+d}", f"{n:d}"])
        if n > 0:
            needles.append(f"+{n}")
    except (TypeError, ValueError):
        needles.append(str(odds).strip())
    out: list[str] = []
    for needle in needles:
        needle = needle.strip()
        if needle and needle not in out:
            out.append(needle)
    return out


def _fourcasters_odds_matches(text: str, odds, *, tolerance: int = 20) -> bool:
    import re

    if odds is None or odds == "":
        return True
    try:
        target = int(round(float(odds)))
    except (TypeError, ValueError):
        return str(odds).lower() in (text or "").lower()
    for raw in re.findall(r"[+-]\d{2,4}", text or ""):
        try:
            if abs(int(raw) - target) <= tolerance:
                return True
        except ValueError:
            continue
    return False


def _fourcasters_nav_or_homepage_text(text: str) -> bool:
    tl = (text or "").lower()
    markers = (
        "create new market",
        "secure your withdrawals",
        "about our story",
        "wallet balance",
        "view >",
        "match betting",
        "pending bets",
        "graded positions",
        "open orders",
        "my positions",
    )
    hits = sum(1 for m in markers if m in tl)
    return hits >= 2 or "create new market" in tl or "wallet balance" in tl


def _fourcasters_wager_detail_valid(
    text: str,
    *,
    team_name: str,
    team_1: str = "",
    team_2: str = "",
    odds=None,
    stake=None,
) -> bool:
    import re

    if not text or len(text) < 20 or _fourcasters_nav_or_homepage_text(text):
        return False
    if not _wager_text_matches(text, team_name, team_1, team_2):
        return False
    if not re.search(r"[+-]\d{2,4}", text):
        return False
    tl = text.lower()
    if not (
        re.search(r"\$\s*\d", text)
        or "taken" in tl
        or "@" in text
    ):
        return False
    if not _fourcasters_odds_matches(text, odds):
        return False
    if stake is not None and not _stake_in_wager_text(text, stake):
        pass  # stake mismatch alone does not invalidate — odds/team are primary
    return True


def _fourcasters_on_active_wagers_page(driver) -> bool:
    try:
        url = (driver.current_url or "").lower()
        if "active-wagers" not in url and "my-bets" not in url:
            return False
        body = (driver.find_element("tag name", "body").text or "").upper()
        return (
            "ACTIVE WAGERS" in body
            or "TAKEN" in body
            or "MY POSITIONS" in body
            or "PENDING BETS" in body
        )
    except Exception:
        return False


def _find_fourcasters_wager_element(driver, team_name, team_1, team_2, odds):
    return driver.execute_script(
        """
        const teamName = (arguments[0] || '').toLowerCase();
        const team1 = (arguments[1] || '').toLowerCase();
        const team2 = (arguments[2] || '').toLowerCase();
        const needles = [teamName, team1, team2]
          .flatMap(v => v ? [v, v.split(' ').pop()] : [])
          .filter(Boolean);

        function matchesTeam(text) {
          const tl = (text || '').toLowerCase();
          return needles.some(n => n && tl.includes(n));
        }

        function isTabChrome(text) {
          const tl = (text || '').toLowerCase();
          const markers = ['pending bets', 'graded positions', 'open orders', 'my positions'];
          return markers.filter(m => tl.includes(m)).length >= 2;
        }

        function isNavOrHome(text) {
          const tl = (text || '').toLowerCase();
          return tl.includes('create new market')
            || tl.includes('secure your withdrawals')
            || tl.includes('wallet balance');
        }

        function isWagerRow(el) {
          const t = (el.innerText || '').trim();
          if (t.length < 25 || t.length > 350) return false;
          if (isNavOrHome(t) || isTabChrome(t)) return false;
          if (!matchesTeam(t)) return false;
          if (!/[+-]\\d{2,4}/.test(t)) return false;
          if (!/\\$\\s*\\d/.test(t) || !/taken/i.test(t)) return false;
          if (!t.includes('@')) return false;
          const rect = el.getBoundingClientRect();
          if (rect.width < 80 || rect.height < 24) return false;
          if (rect.width > window.innerWidth * 0.92) return false;
          if (rect.height > 160) return false;
          return true;
        }

        const candidates = [];
        for (const el of document.querySelectorAll('tr, li, div, article')) {
          if (!isWagerRow(el)) continue;
          let dominated = false;
          for (const other of candidates) {
            if (other !== el && other.contains(el)) { dominated = true; break; }
          }
          if (dominated) continue;
          candidates.push(el);
        }

        if (!candidates.length) return null;

        candidates.sort((a, b) => {
          const ra = a.getBoundingClientRect();
          const rb = b.getBoundingClientRect();
          const scoreA = ra.height * 10 + ra.width;
          const scoreB = rb.height * 10 + rb.width;
          return scoreA - scoreB;
        });
        return candidates[0];
        """,
        team_name,
        team_1,
        team_2,
    )


def capture_fourcasters_active_wager(
    driver,
    path: str,
    logger,
    *,
    team_name: str,
    team_1: str = "",
    team_2: str = "",
    stake=None,
    odds=None,
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

        if not _fourcasters_on_active_wagers_page(driver):
            logger.warning(
                f"4casters screenshot: not on Active Wagers page (url={driver.current_url})"
            )
            return None

        matched_row = None
        for _ in range(24):
            matched_row = _find_fourcasters_wager_element(
                driver, team_name, team_1, team_2, odds
            )
            if matched_row:
                row_text = (matched_row.text or "").strip()
                if _fourcasters_wager_detail_valid(
                    row_text,
                    team_name=team_name,
                    team_1=team_1,
                    team_2=team_2,
                    odds=odds,
                    stake=stake,
                ):
                    break
            matched_row = None
            time.sleep(0.5)

        if not matched_row:
            logger.warning(
                f"4casters Active Wagers: no wager row for {team_name!r} "
                f"({team_1} vs {team_2}, odds={odds})"
            )
            return None

        row_text = (matched_row.text or "").strip()
        if stake is not None and not _stake_in_wager_text(row_text, stake):
            logger.info(
                "4casters Active Wagers: row matched team/odds but stake differs — using row anyway"
            )

        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});",
            matched_row,
        )
        time.sleep(0.3)

        detail_el = _find_fourcasters_wager_element(
            driver, team_name, team_1, team_2, odds
        ) or matched_row

        detail_text = (detail_el.text or "").strip() if detail_el else row_text
        if not _fourcasters_wager_detail_valid(
            detail_text,
            team_name=team_name,
            team_1=team_1,
            team_2=team_2,
            odds=odds,
            stake=stake,
        ):
            logger.warning(
                f"4casters Active Wagers: capture target lacks wager detail for {team_name!r}"
            )
            return None

        png = None
        for candidate in (detail_el, matched_row):
            if candidate is None:
                continue
            try:
                png = candidate.screenshot_as_png
                if png:
                    break
            except Exception:
                continue
        if png:
            preview = " ".join(detail_text.split())[:72]
            logger.info(f"4casters Active Wagers screenshot | {team_name} | {preview}")
            return _write_png(path, png, logger)
        logger.warning("4casters Active Wagers: element screenshot empty")
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
