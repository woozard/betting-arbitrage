"""Capture bet confirmation UI (Selenium) or render receipts (API books) for Telegram."""

from __future__ import annotations

import os
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
        with open(path, "wb") as fh:
            fh.write(png_bytes)
        logger.info(f"Bet screenshot saved: {path}")
        return path
    except OSError as exc:
        logger.warning(f"Could not write bet screenshot {path}: {exc}")
        return None


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
) -> str | None:
    import time

    from selenium.webdriver.common.by import By

    from utils.ticosports_wager import betslip_text_confirms_wager

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

    deadline = time.time() + 8.0
    while time.time() < deadline:
        try:
            alert = driver.find_element(By.CSS_SELECTOR, "#alertSuccess")
            if alert.is_displayed():
                png = alert.screenshot_as_png
                if png:
                    return _write_png(path, png, logger)
        except Exception:
            pass

        slip = _slip_text()
        if _slip_confirmed(slip) and not _place_bet_visible():
            shot = capture_element_screenshot(
                driver,
                ["#alertSuccess", "#betSlipDiv .alert-success", "#betSlipDiv"],
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

    base_url = (driver.current_url or "").split("#")[0].rstrip("/")
    if not base_url:
        base_url = "https://betamapola.com/sports"
    open_bets_url = f"{base_url}#/openBets"
    sport_url = driver.current_url

    try:
        driver.get(open_bets_url)
        time.sleep(2.5)
        element = driver.execute_script(
            """
            const needles = [arguments[0], arguments[1], arguments[2]]
              .filter(Boolean)
              .map(s => String(s).toLowerCase());
            const selectors = [
              '.open-bets', '.openBets', '.wager-item', '.bet-item',
              'table tbody tr', '.card', '[ng-repeat*="wager"]', '[ng-repeat*="pick"]',
              'main', '#content', '.content-wrapper'
            ];
            for (const sel of selectors) {
              for (const el of document.querySelectorAll(sel)) {
                const t = (el.innerText || '').trim();
                if (t.length < 20) continue;
                const tl = t.toLowerCase();
                if (needles.some(n => n && tl.includes(n))) return el;
              }
            }
            return document.querySelector('table')
                || document.querySelector('.open-bets, .openBets, main, #content')
                || document.body;
            """,
            team_name,
            team_1,
            team_2,
        )
        if element:
            png = element.screenshot_as_png
            if png:
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


def capture_s411_open_bet(
    driver,
    open_bets_url: str,
    team_name: str,
    path: str,
    logger,
    return_to_sport: Callable[[], None] | None = None,
) -> str | None:
    try:
        driver.get(open_bets_url)
        time.sleep(2.5)
        element = driver.execute_script(
            """
            const needle = (arguments[0] || '').toLowerCase();
            const selectors = [
              '.bet-item', '.wager-item', '.open-bet', '.pending-bet',
              'tr', '.card', '[class*="Wager"]', '[class*="wager"]',
              'main section', '.content', '.open-bets-list'
            ];
            for (const sel of selectors) {
              for (const el of document.querySelectorAll(sel)) {
                const t = (el.innerText || '').trim();
                if (t.length > 20 && t.toLowerCase().includes(needle)) return el;
              }
            }
            return document.querySelector('main, .open-bets, #content, .content-wrapper')
                || document.body;
            """,
            team_name,
        )
        if element:
            png = element.screenshot_as_png
            if png:
                return _write_png(path, png, logger)

        return capture_element_screenshot(
            driver,
            ["main", ".open-bets", "#content", "body"],
            path,
            logger,
        )
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
    extra_lines: list[str] | None = None,
    logger=None,
) -> str | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        if logger:
            logger.warning("Pillow not installed — skipping API bet receipt screenshot")
        return None

    width, height = 720, 420
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
        f"Selection: {team_name}",
        f"Market: {_bet_type_label(bet_type, spread_line)}",
        f"Odds: {odds}",
        f"Stake: {_stake_display(stake)}",
    ]
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
        )
        if shot:
            return shot

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
