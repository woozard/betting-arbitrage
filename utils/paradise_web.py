"""Selenium helpers for ParadiseWager pending-bets screenshots."""

from __future__ import annotations

import re
import time
from typing import Callable

from utils.helpers import teams_same
from utils.stake_sizing import BaseAmountStake, stake_matches_verification_amount

PARADISE_PENDING_WAGERS_URL = "https://paradisewager.com/v2/#/pendings"


def _format_odds_needles(odds) -> list[str]:
    if odds in (None, ""):
        return []
    text = str(odds).strip().replace("−", "-")
    try:
        val = int(round(float(text)))
    except (TypeError, ValueError):
        return [text.lower()]
    if val > 0:
        return [f"+{val}", f"(+{val})", f"({val})"]
    return [str(val), f"({val})"]


def _wager_text_matches(
    text: str,
    team_name: str,
    team_1: str = "",
    team_2: str = "",
    odds=None,
) -> bool:
    blob = (text or "").strip()
    if not blob:
        return False

    team_hit = False
    if team_name and teams_same(blob, team_name):
        team_hit = True
    elif team_name and team_name.lower() in blob.lower():
        team_hit = True
    else:
        last = (team_name or "").strip().split()[-1].lower()
        if last and last in blob.lower():
            team_hit = True
        else:
            for side in (team_1, team_2):
                if side and (teams_same(blob, side) or side.lower() in blob.lower()):
                    team_hit = True
                    break
                side_last = side.strip().split()[-1].lower() if side else ""
                if side_last and side_last in blob.lower():
                    team_hit = True
                    break

    if not team_hit:
        return False

    needles = _format_odds_needles(odds)
    if not needles:
        return True
    return any(n.lower() in blob.lower() for n in needles)


def _stake_in_wager_text(text: str, stake) -> bool:
    if stake is None:
        return True
    amounts = re.findall(r"\$?\s*(\d+(?:\.\d+)?)", (text or "").replace(",", ""))
    for raw in amounts:
        if stake_matches_verification_amount(stake, raw):
            return True
    if isinstance(stake, BaseAmountStake):
        for val in (stake.risk, stake.to_win, stake.entry_amount, stake.base_amount):
            if val is not None:
                formatted = f"{float(val):.2f}".rstrip("0").rstrip(".")
                if formatted in (text or ""):
                    return True
    return False


def capture_paradise_pending_wager(
    driver,
    path: str,
    logger,
    *,
    team_name: str,
    team_1: str = "",
    team_2: str = "",
    odds=None,
    stake=None,
    open_bets_url: str | None = None,
    return_to_sport: Callable[[], None] | str | None = None,
) -> str | None:
    """
    Open Pending Wagers, expand the matching wager row, screenshot the detail panel.
    """
    from utils.bet_screenshot import _write_png

    url = open_bets_url or PARADISE_PENDING_WAGERS_URL
    odds_needles = _format_odds_needles(odds)

    try:
        driver.get(url)
        time.sleep(2.5)

        matched_row = driver.execute_script(
            """
            const teamName = (arguments[0] || '').toLowerCase();
            const team1 = (arguments[1] || '').toLowerCase();
            const team2 = (arguments[2] || '').toLowerCase();
            const oddsNeedles = (arguments[3] || []).map(v => String(v).toLowerCase());
            const needles = [teamName, team1, team2]
              .flatMap(v => v ? [v, v.split(' ').pop()] : [])
              .filter(Boolean);

            function teamMatches(text) {
              const tl = (text || '').toLowerCase();
              return needles.some(n => n && tl.includes(n));
            }

            function oddsMatches(text) {
              if (!oddsNeedles.length) return true;
              const tl = (text || '').toLowerCase();
              return oddsNeedles.some(n => n && tl.includes(n));
            }

            function isCollapsedRow(el) {
              const t = (el.innerText || '').trim();
              if (t.length < 8 || t.length > 350) return false;
              if (!teamMatches(t) || !oddsMatches(t)) return false;
              if (t.includes('Ticket #') && t.includes('Risk/Win')) return false;
              return true;
            }

            const candidates = [];
            for (const el of document.querySelectorAll(
              'tr, li, div, a, button, [ng-repeat], [class*="wager"], [class*="pending"], [class*="bet"]'
            )) {
              if (!isCollapsedRow(el)) continue;
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
            odds_needles,
        )

        if not matched_row:
            logger.warning(
                f"Paradise pending wagers: no row matched {team_name!r} @ {odds} "
                f"({team_1} vs {team_2})"
            )
            return None

        row_text = (matched_row.text or "").strip()
        if stake is not None and not _stake_in_wager_text(row_text, stake):
            logger.info(
                "Paradise pending wagers: row matched team/odds but stake differs — using row anyway"
            )

        driver.execute_script(
            """
            const row = arguments[0];
            row.scrollIntoView({block: 'center'});
            row.click();
            """,
            matched_row,
        )
        time.sleep(1.5)

        detail_el = driver.execute_script(
            """
            const row = arguments[0];
            const teamNeedles = [arguments[1], arguments[2], arguments[3]]
              .flatMap(v => v ? [String(v).toLowerCase(), String(v).split(' ').pop().toLowerCase()] : [])
              .filter(Boolean);

            function detailMatches(text) {
              const tl = (text || '').toLowerCase();
              if (!tl.includes('ticket #')) return false;
              if (!tl.includes('risk/win')) return false;
              return teamNeedles.some(n => n && tl.includes(n));
            }

            const near = row.closest('tr, li, div, section, table, tbody, [class*="wager"], [class*="pending"]')
              || row.parentElement;
            const searchRoots = [near, near ? near.parentElement : null, document.body].filter(Boolean);

            for (const root of searchRoots) {
              for (const el of root.querySelectorAll('tr, li, div, section, table, tbody')) {
                const t = (el.innerText || '').trim();
                if (t.length < 40 || t.length > 2500) continue;
                if (!detailMatches(t)) continue;
                if (row.contains(el)) continue;
                let dominated = false;
                for (const other of root.querySelectorAll('tr, li, div, section, table, tbody')) {
                  if (other !== el && other.contains(el) && detailMatches(other.innerText || '')) {
                    dominated = true;
                    break;
                  }
                }
                if (!dominated) return el;
              }
            }

            // Expanded content may live inside the same row container.
            let parent = row;
            for (let i = 0; i < 4 && parent; i++) {
              const t = (parent.innerText || '').trim();
              if (detailMatches(t)) return parent;
              parent = parent.parentElement;
            }
            return row;
            """,
            matched_row,
            team_name,
            team_1,
            team_2,
        )

        target = detail_el or matched_row
        png = target.screenshot_as_png
        if png:
            preview = " ".join((row_text or "").split())[:72]
            logger.info(f"Paradise pending wager screenshot | {team_name} @ {odds} | {preview}")
            return _write_png(path, png, logger)
    except Exception as exc:
        logger.warning(f"Paradise pending wager screenshot failed: {exc}")
    finally:
        if return_to_sport:
            try:
                if callable(return_to_sport):
                    return_to_sport()
                else:
                    driver.get(return_to_sport)
                    time.sleep(1.0)
            except Exception:
                pass

    return None
