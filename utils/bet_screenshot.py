"""Capture bet confirmation UI (Selenium) or render receipts (API books) for Telegram."""

from __future__ import annotations

import os
import re
import time
from typing import Callable

from utils.stake_sizing import (
    BaseAmountStake,
    format_base_amount_stake,
    stake_matches_verification_amount,
)
from utils.helpers import teams_same

SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "screenshots")


def get_screenshots_dir() -> str:
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    return SCREENSHOTS_DIR


def bet_screenshot_path(bookmaker: str, game_id: str) -> str:
    safe_book = "".join(c if c.isalnum() else "_" for c in (bookmaker or "book"))
    safe_game = "".join(c if c.isalnum() else "_" for c in str(game_id or "unknown"))[:40]
    return os.path.join(get_screenshots_dir(), f"{safe_book}_{safe_game}_{int(time.time())}.png")


def _write_png(path: str, png_bytes: bytes, logger) -> str | None:
    try:
        png_bytes = _normalize_png_for_telegram(png_bytes, logger)
        with open(path, "wb") as fh:
            fh.write(png_bytes)
        logger.info(f"Bet screenshot saved: {path}")
        return path
    except OSError as exc:
        logger.warning(f"Could not write bet screenshot {path}: {exc}")
        return None


def _normalize_png_for_telegram(png_bytes: bytes, logger) -> bytes:
    """Pad tiny element screenshots so Telegram accepts them (min ~200px height)."""
    try:
        from io import BytesIO

        from PIL import Image

        im = Image.open(BytesIO(png_bytes))
        width, height = im.size
        min_h = 200
        if height >= min_h:
            return png_bytes
        canvas = Image.new("RGB", (max(width, 400), max(height, min_h)), (255, 255, 255))
        canvas.paste(im, (0, 0))
        out = BytesIO()
        canvas.save(out, format="PNG")
        logger.info(f"Padded bet screenshot from {width}x{height} to {canvas.size[0]}x{canvas.size[1]}")
        return out.getvalue()
    except Exception as exc:
        logger.warning(f"Could not normalize screenshot dimensions: {exc}")
        return png_bytes


def capture_element_screenshot(driver, selectors: list[str], path: str, logger) -> str | None:
    """Try each CSS selector; screenshot the first visible element."""
    from selenium.webdriver.common.by import By

    for selector in selectors:
        try:
            element = driver.find_element(By.CSS_SELECTOR, selector)
            if not element.is_displayed():
                continue
            png = element.screenshot_as_png
            if png:
                return _write_png(path, png, logger)
        except Exception:
            continue
    return None


def capture_betwar_my_bets(driver, path: str, logger) -> str | None:
    """Capture full pending-wagers list (legacy / preview scripts)."""
    from selenium.webdriver.common.by import By

    try:
        tab = driver.find_element(By.CSS_SELECTOR, "#pillsPendingTab")
        if (tab.get_attribute("aria-selected") or "").lower() != "true":
            driver.execute_script("arguments[0].click();", tab)
        time.sleep(0.8)
    except Exception as exc:
        logger.warning(f"BetWar My Bets tab not available for screenshot: {exc}")

    return capture_element_screenshot(
        driver,
        ["#pills-pending", "#pills-pending .list-group", "#pills-pending .card"],
        path,
        logger,
    )


def _betwar_row_matches_team(row_text: str, team_name: str) -> bool:
    text = (row_text or "").strip()
    if not text or not team_name:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    desc = lines[1] if len(lines) > 1 else text
    if team_name.lower() in desc.lower():
        return True
    if teams_same(desc, team_name):
        return True
    last_word = team_name.strip().split()[-1].lower()
    return bool(last_word and last_word in desc.lower())


def _betwar_row_matches_stake(row_text: str, stake) -> bool:
    import re

    first_line = (row_text or "").splitlines()[0] if row_text else ""
    m = re.match(
        r"^(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)",
        first_line.replace(",", "").strip(),
    )
    if not m:
        return True
    risk_raw, win_raw = m.group(1), m.group(2)
    if stake_matches_verification_amount(stake, risk_raw):
        return True
    return bool(win_raw and stake_matches_verification_amount(stake, win_raw))


def capture_betwar_open_wager(
    driver,
    path: str,
    logger,
    team_name: str,
    stake=None,
) -> str | None:
    """Open My Bets, click the matching wager row, screenshot that bet only."""
    import re
    from selenium.webdriver.common.by import By

    try:
        tab = driver.find_element(By.CSS_SELECTOR, "#pillsPendingTab")
        if (tab.get_attribute("aria-selected") or "").lower() != "true":
            driver.execute_script("arguments[0].click();", tab)
        time.sleep(0.8)
    except Exception as exc:
        logger.warning(f"BetWar My Bets tab not available for screenshot: {exc}")
        return None

    try:
        rows = driver.find_elements(
            By.CSS_SELECTOR, "#tbodyPendingBetItems tr.wager-detail-info"
        )
    except Exception as exc:
        logger.warning(f"BetWar pending wager rows not found: {exc}")
        return None

    candidates = []
    for row in rows:
        text = (row.text or "").strip()
        if not text or not _betwar_row_matches_team(text, team_name):
            continue
        candidates.append((row, text))

    if not candidates:
        logger.warning(f"BetWar My Bets: no row matched team {team_name!r}")
        return None

    matched = candidates[0]
    if stake is not None:
        stake_matches = [
            (row, text)
            for row, text in candidates
            if _betwar_row_matches_stake(text, stake)
        ]
        if stake_matches:
            matched = stake_matches[0]
        elif len(candidates) > 1:
            logger.warning(
                f"BetWar My Bets: team {team_name!r} matched {len(candidates)} rows "
                f"but none matched stake — using first match"
            )

    row, row_text = matched
    try:
        driver.execute_script("arguments[0].click();", row)
        time.sleep(1.0)
        side = row.find_elements(By.CSS_SELECTOR, ".wager-side-description")
        if side:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                cls = (side[0].get_attribute("class") or "").lower()
                if "invisible" not in cls and (side[0].text or "").strip():
                    break
                time.sleep(0.25)
        png = row.screenshot_as_png
        if png:
            preview = re.sub(r"\s+", " ", (row_text.splitlines()[0] if row_text else ""))[:60]
            logger.info(f"BetWar single-wager screenshot | {team_name} | {preview}")
            return _write_png(path, png, logger)
    except Exception as exc:
        logger.warning(f"BetWar single-wager screenshot failed: {exc}")

    return None


def _betamapola_odds_needles(odds) -> list[str]:
    if odds is None or odds == "":
        return []
    needles: list[str] = []
    try:
        val = float(odds)
        if val == int(val):
            n = int(val)
            needles.extend([f"{n:+d}", f"{n:d}", f"+{n}" if n > 0 else str(n)])
        else:
            needles.append(f"{val:g}")
    except (TypeError, ValueError):
        needles.append(str(odds).strip())
    out: list[str] = []
    for needle in needles:
        needle = needle.strip()
        if needle and needle not in out:
            out.append(needle)
    return out


def _betamapola_team_needles(team_name: str = "", team_1: str = "", team_2: str = "") -> list[str]:
    needles: list[str] = []
    for name in (team_name, team_1, team_2):
        if not name:
            continue
        low = name.strip().lower()
        if low and low not in needles:
            needles.append(low)
        last = name.strip().split()[-1].lower()
        if last and last not in needles:
            needles.append(last)
    return needles


def _betamapola_open_bets_is_header_block(text: str) -> bool:
    """True when element text is the full open-bets table chrome, not a single wager row."""
    tl = (text or "").lower()
    header_markers = ("tik#", "accepted date", "export to pdf", "export to excel")
    return sum(1 for m in header_markers if m in tl) >= 2


def _betamapola_open_bets_row_valid(
    text: str,
    *,
    team_name: str = "",
    team_1: str = "",
    team_2: str = "",
    odds=None,
    ticket_number: int | str | None = None,
) -> bool:
    if _betamapola_open_bets_is_header_block(text):
        return False
    if not text or len(text) < 40 or len(text) > 900:
        return False
    tl = text.lower()
    needles = _betamapola_team_needles(team_name, team_1, team_2)
    if needles and not any(n in tl for n in needles):
        return False
    if ticket_number is not None and str(ticket_number).strip() not in text:
        return False
    odds_needles = [n.lower() for n in _betamapola_odds_needles(odds)]
    if odds_needles and not any(n in tl.replace(" ", "") for n in odds_needles):
        return False
    has_amounts = "$" in text and ("risk" in tl or "win" in tl)
    has_wager = (
        "money line" in tl
        or "spread" in tl
        or "total" in tl
        or "for game" in tl
        or bool(re.search(r"[+-]\d{2,4}", text))
    )
    return has_amounts and has_wager


def _betamapola_open_bets_ticket_valid(
    text: str,
    *,
    team_name: str = "",
    team_1: str = "",
    team_2: str = "",
    odds=None,
    ticket_number: int | str | None = None,
) -> bool:
    if not text or len(text) < 40:
        return False
    tl = text.lower()
    needles = _betamapola_team_needles(team_name, team_1, team_2)
    if needles and not any(n in tl for n in needles):
        return False
    if ticket_number is not None and str(ticket_number).strip() not in text:
        return False
    odds_needles = [n.lower() for n in _betamapola_odds_needles(odds)]
    if odds_needles and not any(n in tl.replace(" ", "") for n in odds_needles):
        return False
    has_amounts = "$" in text and ("risk" in tl or "win" in tl)
    has_wager = (
        "money line" in tl
        or "spread" in tl
        or "total" in tl
        or "for game" in tl
        or bool(re.search(r"[+-]\d{2,4}", text))
    )
    return has_amounts and has_wager


def _betamapola_text_has_wager_detail(
    text: str,
    *,
    team_name: str = "",
    team_1: str = "",
    team_2: str = "",
    odds=None,
) -> bool:
    if not text or len(text) < 30:
        return False
    tl = text.lower()
    needles = _betamapola_team_needles(team_name, team_1, team_2)
    if needles and not any(n in tl for n in needles):
        return False
    odds_needles = [n.lower() for n in _betamapola_odds_needles(odds)]
    has_odds = not odds_needles or any(n in tl.replace(" ", "") for n in odds_needles)
    has_amounts = "$" in text or "risk" in tl or "to win" in tl or "win" in tl
    return has_odds and has_amounts


def _find_betamapola_open_bets_ticket(
    driver,
    team_name: str = "",
    team_1: str = "",
    team_2: str = "",
    odds=None,
    ticket_number: int | str | None = None,
):
    team_needles = _betamapola_team_needles(team_name, team_1, team_2)
    odds_needles = _betamapola_odds_needles(odds)
    ticket_s = str(ticket_number).strip() if ticket_number is not None else ""
    return driver.execute_script(
        """
        const teamNeedles = arguments[0] || [];
        const oddsNeedles = (arguments[1] || []).map(s => String(s).toLowerCase());
        const ticket = String(arguments[2] || '');

        function rowMatches(el) {
          const t = (el.innerText || '').trim();
          const tl = t.toLowerCase();
          if (t.length < 40 || t.length > 900) return false;
          const headerHits = ['tik#', 'accepted date', 'export to pdf'].filter(m => tl.includes(m)).length;
          if (headerHits >= 2) return false;
          if (ticket && !t.includes(ticket)) return false;
          if (teamNeedles.length && !teamNeedles.some(n => n && tl.includes(n))) return false;
          if (oddsNeedles.length) {
            const compact = tl.replace(/\\s/g, '');
            if (!oddsNeedles.some(n => compact.includes(String(n).replace(/\\s/g, '')))) return false;
          }
          const hasAmounts = /\\$\\s*\\d/.test(t) && (/risk/i.test(tl) || /win/i.test(tl));
          const hasWager = /money line|spread|total|for game/i.test(tl) || /[+-]\\d{2,4}/.test(t);
          return hasAmounts && hasWager;
        }

        function score(el) {
          let s = 0;
          const cls = (el.className || '').toLowerCase();
          const r = el.getBoundingClientRect();
          const t = (el.innerText || '').trim();
          const tl = t.toLowerCase();
          if (cls.includes('graded-data')) s += 200;
          if (cls.includes('ticket')) s -= 80;
          if (cls.includes('report-detail')) s -= 40;
          if (/baseball|for game|pitcher/i.test(t)) s += 30;
          if (/money line|spread|total/i.test(tl)) s += 20;
          if (r.height >= 40 && r.height <= 120) s += 80;
          else if (r.height > 120 && r.height <= 180) s += 20;
          else if (r.height > 180) s -= 60;
          if (r.width >= 800 && r.width <= 1700) s += 30;
          if (t.length >= 80 && t.length <= 450) s += 40;
          else if (t.length > 600) s -= 50;
          const hasChildRow = [...el.querySelectorAll('.row.graded-data, [class*="graded-data"]')]
            .some(child => child !== el && rowMatches(child));
          if (hasChildRow) s -= 100;
          return s;
        }

        const selectors = [
          '.row.graded-data',
          '[class*="graded-data"]',
          '.ticket.mb-5',
          '.ticket',
          '.report-detail-open-bets',
          'table tbody tr',
          '.wager-item', '.bet-item', '.open-bet-row',
          '[ng-repeat*="wager"]', '[ng-repeat*="pick"]',
          '.card', '.open-bets .row', '.openBets .row',
        ];
        let best = null;
        let bestScore = -1;
        for (const sel of selectors) {
          for (const el of document.querySelectorAll(sel)) {
            if (!rowMatches(el)) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 80 || r.height < 24) continue;
            const s = score(el);
            if (s > bestScore) {
              best = el;
              bestScore = s;
            }
          }
        }
        return best;
        """,
        team_needles,
        odds_needles,
        ticket_s,
    )


def _capture_betamapola_open_bets_row(
    driver,
    path: str,
    logger,
    *,
    team_name: str = "",
    team_1: str = "",
    team_2: str = "",
    odds=None,
    ticket_number: int | str | None = None,
) -> str | None:
    import re
    import time

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    base_url = (driver.current_url or "").split("#")[0].rstrip("/")
    if not base_url:
        base_url = "https://betamapola.com/sports"
    open_bets_url = f"{base_url}#/openBets"
    sport_url = driver.current_url

    try:
        driver.get(open_bets_url)
        time.sleep(2.5)
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".ticket, [class*='graded-data'], .report-detail-open-bets")
                )
            )
        except Exception:
            pass

        element = None
        for _ in range(12):
            element = _find_betamapola_open_bets_ticket(
                driver,
                team_name=team_name,
                team_1=team_1,
                team_2=team_2,
                odds=odds,
                ticket_number=ticket_number,
            )
            if element:
                ticket_text = (element.text or "").strip()
                if _betamapola_open_bets_row_valid(
                    ticket_text,
                    team_name=team_name,
                    team_1=team_1,
                    team_2=team_2,
                    odds=odds,
                    ticket_number=ticket_number,
                ):
                    break
            element = None
            time.sleep(0.5)

        if not element:
            logger.warning(
                f"Betamapola open-bets: no ticket card for {team_name!r} "
                f"({team_1} vs {team_2}, odds={odds}, ticket={ticket_number})"
            )
            return None

        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});",
            element,
        )
        time.sleep(0.4)

        png = None
        try:
            png = element.screenshot_as_png
        except Exception:
            pass
        if not png:
            logger.warning("Betamapola open-bets: element screenshot empty")
            return None

        preview = re.sub(r"\s+", " ", (element.text or ""))[:72]
        logger.info(f"Betamapola open-bets screenshot | {team_name} | {preview}")
        return _write_png(path, png, logger)
    except Exception as exc:
        logger.warning(f"Betamapola open-bets screenshot failed: {exc}")
    finally:
        if sport_url:
            try:
                driver.get(sport_url)
                time.sleep(1.0)
            except Exception:
                pass
    return None


def capture_betamapola_betslip(driver, path: str, logger) -> str | None:
    """Capture Betamapola post-acceptance UI (not the pre-submit Place Bet slip)."""
    return capture_betamapola_confirmation(driver, path, logger)


def capture_betamapola_confirmation(
    driver,
    path: str,
    logger,
    team_name: str = "",
    team_1: str = "",
    team_2: str = "",
    odds=None,
    ticket_number: int | str | None = None,
) -> str | None:
    import time

    from selenium.webdriver.common.by import By

    from utils.ticosports_wager import betslip_text_confirms_wager

    if team_name or team_1 or team_2:
        shot = _capture_betamapola_open_bets_row(
            driver,
            path,
            logger,
            team_name=team_name,
            team_1=team_1,
            team_2=team_2,
            odds=odds,
            ticket_number=ticket_number,
        )
        if shot:
            return shot

    def _slip_text() -> str:
        try:
            return (driver.find_element(By.ID, "betSlipDiv").text or "").strip()
        except Exception:
            return ""

    def _slip_confirmed(text: str) -> bool:
        return betslip_text_confirms_wager(text)

    def _place_bet_visible() -> bool:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, "#betSlipDiv button, #betSlipDiv a"):
                label = (btn.text or "").strip().lower()
                if "place bet" in label and btn.is_displayed():
                    return True
        except Exception:
            pass
        return False

    deadline = time.time() + 6.0
    while time.time() < deadline:
        slip = _slip_text()
        if (
            _slip_confirmed(slip)
            and not _place_bet_visible()
            and _betamapola_text_has_wager_detail(
                slip,
                team_name=team_name,
                team_1=team_1,
                team_2=team_2,
                odds=odds,
            )
        ):
            shot = capture_element_screenshot(
                driver,
                ["#betSlipDiv"],
                path,
                logger,
            )
            if shot:
                return shot
        time.sleep(0.4)

    if _place_bet_visible() and not _slip_confirmed(_slip_text()):
        logger.warning(
            "Betamapola bet slip still shows Place Bet — skipping pre-confirmation screenshot"
        )
        return None

    shot = _capture_betamapola_open_bets_row(
        driver,
        path,
        logger,
        team_name=team_name,
        team_1=team_1,
        team_2=team_2,
        odds=odds,
    )
    if shot:
        return shot

    logger.warning(
        f"Betamapola screenshot has no open-bets row with teams/odds for {team_name or team_1}"
    )
    return None


def _s411_odds_needles(odds) -> list[str]:
    needles = _betamapola_odds_needles(odds)
    out: list[str] = []
    for needle in needles:
        for variant in (needle, f"ML{needle}", f"ML {needle}"):
            v = variant.strip()
            if v and v not in out:
                out.append(v)
    compact = (str(odds) if odds is not None else "").strip().replace(" ", "")
    if compact and compact not in out:
        out.append(compact)
    return out


def _s411_open_bets_row_valid(
    text: str,
    *,
    team_name: str = "",
    team_1: str = "",
    team_2: str = "",
    odds=None,
    stake=None,
) -> bool:
    """True for a single S411 open-bet row, not the full My Open Bets page."""
    text = (text or "").strip()
    if len(text) < 20 or len(text) > 500:
        return False
    if re.search(r"my\s+open\s+bets", text, re.I) and len(text) > 250:
        return False
    action_hits = len(re.findall(r"\(\s*ACTION\s*\)", text, re.I))
    if action_hits != 1:
        return False
    if len(re.findall(r"Risk\s*:", text, re.I)) != 1:
        return False

    text_l = text.lower()
    team_needles = _betamapola_team_needles(team_name, team_1, team_2)
    if not any(needle in text_l for needle in team_needles):
        return False

    if odds is not None and str(odds).strip() != "":
        compact = re.sub(r"\s+", "", text)
        if not any(
            re.sub(r"\s+", "", needle) in compact
            for needle in _s411_odds_needles(odds)
        ):
            return False

    if stake is not None and not _betwar_row_matches_stake(text, stake):
        return False

    return True


def _find_s411_open_bets_row(
    driver,
    *,
    team_name: str,
    team_1: str = "",
    team_2: str = "",
    odds=None,
    stake=None,
):
    from selenium.webdriver.common.by import By

    rows = []
    for selector in (
        '[class*="wager"]',
        '[class*="Wager"]',
        '[class*="open-bet"]',
        '[class*="OpenBet"]',
        '[class*="bet-item"]',
        "main li",
        "main article",
        "main div",
    ):
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            text = (element.text or "").strip()
            if not _s411_open_bets_row_valid(
                text,
                team_name=team_name,
                team_1=team_1,
                team_2=team_2,
                odds=odds,
                stake=stake,
            ):
                continue
            rows.append((len(text), element, text))

    if not rows:
        return None, None

    rows.sort(key=lambda item: item[0])
    _, element, text = rows[0]
    return element, text


def capture_s411_open_bet(
    driver,
    open_bets_url: str,
    team_name: str,
    path: str,
    logger,
    return_to_sport: Callable[[], None] | None = None,
    *,
    team_1: str = "",
    team_2: str = "",
    odds=None,
    stake=None,
) -> str | None:
    """Screenshot one open-bet row on S411 (not the full pending list)."""
    try:
        driver.get(open_bets_url)
        time.sleep(2.5)

        element, row_text = _find_s411_open_bets_row(
            driver,
            team_name=team_name,
            team_1=team_1,
            team_2=team_2,
            odds=odds,
            stake=stake,
        )
        if element is None and stake is not None:
            element, row_text = _find_s411_open_bets_row(
                driver,
                team_name=team_name,
                team_1=team_1,
                team_2=team_2,
                odds=odds,
                stake=None,
            )

        if element is None:
            logger.warning(
                f"S411 open-bets: no single wager row for {team_name!r} "
                f"odds={odds!r} stake={stake!r}"
            )
            return None

        png = element.screenshot_as_png
        if png:
            preview = re.sub(r"\s+", " ", row_text.splitlines()[0] if row_text else "")[:72]
            logger.info(f"S411 open-bets row screenshot | {team_name} | {preview}")
            return _write_png(path, png, logger)
        return None
    except Exception as exc:
        logger.warning(f"S411 open-bets screenshot failed: {exc}")
        return None
    finally:
        if return_to_sport:
            try:
                return_to_sport()
            except Exception as exc:
                logger.warning(f"S411 return to sport page after screenshot failed: {exc}")


def _stake_display(stake) -> str:
    if isinstance(stake, BaseAmountStake):
        return format_base_amount_stake(stake)
    try:
        return f"${float(stake):.2f}"
    except (TypeError, ValueError):
        return str(stake)


def _bet_type_label(bet_type: str, spread_line) -> str:
    bt = (bet_type or "moneyline").lower()
    if bt == "spread" and spread_line is not None:
        try:
            line = float(spread_line)
            sign = "+" if line > 0 else ""
            return f"Spread {sign}{line:g}"
        except (TypeError, ValueError):
            return "Spread"
    return "Moneyline"


def render_open_bets_receipt(
    path: str,
    bookmaker: str,
    bets: list[dict],
    *,
    title: str = "OPEN BETS",
    logger=None,
) -> str | None:
    """Render a list of open wagers for API books (3et, 4casters, Paradise)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        if logger:
            logger.warning("Pillow not installed — skipping open bets receipt")
        return None

    if not bets:
        return None

    width = 760
    row_height = 88
    height = max(260, 130 + len(bets) * row_height)
    img = Image.new("RGB", (width, height), color=(18, 24, 38))
    draw = ImageDraw.Draw(img)

    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        body_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
    except OSError:
        title_font = ImageFont.load_default()
        body_font = title_font
        small_font = title_font

    y = 24
    draw.text(
        (24, y),
        f"{(bookmaker or 'book').upper()} — {title}",
        fill=(76, 175, 80),
        font=title_font,
    )
    y += 40
    draw.line([(24, y), (width - 24, y)], fill=(60, 70, 90), width=1)
    y += 18

    for bet in bets:
        desc = bet.get("description") or bet.get("team_name") or "Wager"
        match = bet.get("match") or ""
        odds = bet.get("odds", "")
        stake = bet.get("stake_display") or _stake_display(bet.get("stake", ""))
        status = bet.get("status", "")
        extra = bet.get("extra", "")

        draw.text((24, y), desc, fill=(230, 235, 245), font=body_font)
        y += 26
        detail_parts = [p for p in (match, f"Odds: {odds}" if odds else "", f"Stake: {stake}") if p]
        if detail_parts:
            draw.text((24, y), " · ".join(detail_parts), fill=(170, 180, 200), font=small_font)
            y += 22
        tail = " · ".join(p for p in (f"Status: {status}" if status else "", extra) if p)
        if tail:
            draw.text((24, y), tail, fill=(130, 140, 160), font=small_font)
            y += 22
        y += 12

    draw.text(
        (24, height - 32),
        time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        fill=(130, 140, 160),
        font=small_font,
    )

    try:
        img.save(path, format="PNG")
        if logger:
            logger.info(f"Open bets receipt saved: {path}")
        return path
    except OSError as exc:
        if logger:
            logger.warning(f"Could not save open bets receipt {path}: {exc}")
        return None


def render_bet_receipt(
    path: str,
    bookmaker: str,
    *,
    team_1: str,
    team_2: str,
    team_name: str,
    odds,
    stake,
    bet_type: str = "moneyline",
    spread_line=None,
    game_date: str | None = None,
    ticket_number=None,
    extra_lines: list[str] | None = None,
    logger=None,
) -> str | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        if logger:
            logger.warning("Pillow not installed — skipping API bet receipt screenshot")
        return None

    width, height = 720, 480
    img = Image.new("RGB", (width, height), color=(18, 24, 38))
    draw = ImageDraw.Draw(img)

    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        body_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except OSError:
        title_font = ImageFont.load_default()
        body_font = title_font
        small_font = title_font

    y = 24
    draw.text((24, y), f"{(bookmaker or 'book').upper()} — BET CONFIRMED", fill=(76, 175, 80), font=title_font)
    y += 44
    draw.line([(24, y), (width - 24, y)], fill=(60, 70, 90), width=1)
    y += 16

    lines = [
        f"Match: {team_1} vs {team_2}",
    ]
    if game_date:
        lines.append(f"Game date: {game_date}")
    lines.extend([
        f"Selection: {team_name}",
        f"Market: {_bet_type_label(bet_type, spread_line)}",
        f"Odds: {odds}",
        f"Stake: {_stake_display(stake)}",
    ])
    if ticket_number not in (None, "", 0, "0"):
        ticket_text = str(ticket_number).strip()
        if ticket_text:
            lines.append(f"Ticket: #{ticket_text}")
    if extra_lines:
        lines.extend(extra_lines)

    for line in lines:
        draw.text((24, y), line, fill=(230, 235, 245), font=body_font)
        y += 32

    draw.text(
        (24, height - 36),
        time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        fill=(130, 140, 160),
        font=small_font,
    )

    try:
        img.save(path, format="PNG")
        if logger:
            logger.info(f"Bet receipt screenshot saved: {path}")
        return path
    except OSError as exc:
        if logger:
            logger.warning(f"Could not save bet receipt {path}: {exc}")
        return None


def capture_confirmed_bet_screenshot(
    *,
    bookmaker: str,
    game_id: str,
    team_name: str,
    team_1: str,
    team_2: str,
    odds,
    stake,
    bet_type: str = "moneyline",
    spread_line=None,
    game_date: str | None = None,
    ticket_number=None,
    driver=None,
    open_bets_url: str | None = None,
    return_to_sport: Callable[[], None] | None = None,
    extra_lines: list[str] | None = None,
    logger=None,
) -> str | None:
    """Best-effort screenshot for a confirmed real-money bet."""
    if not logger:
        return None

    path = bet_screenshot_path(bookmaker, game_id)
    bm = (bookmaker or "").strip().lower()

    if bm == "betwar" and driver is not None:
        shot = capture_betwar_open_wager(
            driver, path, logger, team_name=team_name, stake=stake
        )
        if shot:
            return shot
        logger.warning(
            f"BetWar single-wager screenshot unavailable for {team_name}; skipping full-list fallback"
        )
        return None

    if bm == "betamapola" and driver is not None:
        shot = capture_betamapola_confirmation(
            driver,
            path,
            logger,
            team_name=team_name,
            team_1=team_1,
            team_2=team_2,
            odds=odds,
            ticket_number=ticket_number,
        )
        if shot:
            return shot

    if bm == "sports411" and driver is not None and open_bets_url:
        shot = capture_s411_open_bet(
            driver,
            open_bets_url,
            team_name,
            path,
            logger,
            return_to_sport=return_to_sport,
            team_1=team_1,
            team_2=team_2,
            odds=odds,
            stake=stake,
        )
        if shot:
            return shot

    if bm == "4casters":
        if driver is not None:
            from utils.fourcasters_web import capture_fourcasters_active_wager

            shot = capture_fourcasters_active_wager(
                driver,
                path,
                logger,
                team_name=team_name,
                team_1=team_1,
                team_2=team_2,
                stake=stake,
                odds=odds,
                open_bets_url=open_bets_url,
            )
            if shot:
                return shot
        logger.warning(
            f"4casters Active Wagers screenshot unavailable for {team_name}; "
            "skipping auto-generated receipt"
        )
        return None

    if bm in ("paradisewager", "paradise") and driver is not None:
        from utils.paradise_web import capture_paradise_pending_wager

        shot = capture_paradise_pending_wager(
            driver,
            path,
            logger,
            team_name=team_name,
            team_1=team_1,
            team_2=team_2,
            odds=odds,
            stake=stake,
            open_bets_url=open_bets_url,
            return_to_sport=return_to_sport,
        )
        if shot:
            return shot
        logger.warning(
            f"Paradise pending wager screenshot unavailable for {team_name}; "
            "falling back to rendered receipt"
        )

    return render_bet_receipt(
        path,
        bookmaker,
        team_1=team_1,
        team_2=team_2,
        team_name=team_name,
        odds=odds,
        stake=stake,
        bet_type=bet_type,
        spread_line=spread_line,
        game_date=game_date,
        ticket_number=ticket_number,
        extra_lines=extra_lines,
        logger=logger,
    )


def prune_screenshots(max_age_hours: int = 72) -> None:
    """Remove old bet screenshots from screenshots/."""
    directory = get_screenshots_dir()
    cutoff = time.time() - max_age_hours * 3600
    try:
        for fname in os.listdir(directory):
            fpath = os.path.join(directory, fname)
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
    except OSError:
        pass
