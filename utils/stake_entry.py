"""Selenium helpers for entering base-amount stakes in book bet slips."""
from __future__ import annotations

from typing import Sequence

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from utils.stake_sizing import BaseAmountStake

DEFAULT_RISK_SELECTORS: tuple[str, ...] = (
    "#betSlipDiv input.txtRiskAmount",
    "#betSlipDiv input.txtWinAmount",
    "#betSlipDiv input[id^='risk_']",
    "#betSlipDiv input[id^='win_']",
    "input[id^='risk_']",
    "input[name*='risk']",
    "input[ng-model*='risk']",
    "input.txtRiskAmount",
    "#pills-betslip input.txtRiskAmount",
    "#divBetSlip input.txtRiskAmount",
    "input[name='risk']",
    "input.risk",
)

DEFAULT_WIN_SELECTORS: tuple[str, ...] = (
    "input[id^='win_']",
    "input[name*='win']",
    "input[ng-model*='win']",
    "input.txtWinAmount",
    "#pills-betslip input.txtWinAmount",
    "#divBetSlip input.txtWinAmount",
    "input[name='win']",
    "input[name='towin']",
    "input.towin",
    "#betSlipDiv input[type='text']",
    "#betSlipDiv input[type='number']",
    "input[placeholder*='Risk']",
    "input[placeholder*='risk']",
    "input[placeholder*='Win']",
    "input[placeholder*='win']",
)


def _find_stake_input(
    driver: WebDriver,
    selectors: Sequence[str],
    scope_css: str | None = None,
):
    roots = [driver]
    if scope_css:
        try:
            roots = driver.find_elements(By.CSS_SELECTOR, scope_css) or [driver]
        except Exception:
            roots = [driver]

    for root in roots:
        for selector in selectors:
            try:
                elems = root.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for elem in elems:
                try:
                    if elem.is_displayed() and elem.is_enabled():
                        return elem
                except Exception:
                    continue
    return None


def fill_betslip_stake_input(
    driver: WebDriver,
    stake: BaseAmountStake,
    logger,
    *,
    risk_selectors: Sequence[str] = DEFAULT_RISK_SELECTORS,
    win_selectors: Sequence[str] = DEFAULT_WIN_SELECTORS,
    scope_css: str | None = None,
    risk_recalc_js: str | None = "calculateBStxtWin",
    win_recalc_js: str | None = "calculateBStxtRisk",
) -> bool:
    """Enter base amount in the correct bet-slip field (risk or to-win)."""
    selectors = win_selectors if stake.entry_field == "to_win" else risk_selectors
    stake_input = _find_stake_input(driver, selectors, scope_css=scope_css)
    entry_stake = stake
    if not stake_input and stake.entry_field == "to_win":
        stake_input = _find_stake_input(driver, risk_selectors, scope_css=scope_css)
        if stake_input:
            logger.warning(
                "To-win input not found; falling back to risk input for base-amount stake"
            )
            entry_stake = BaseAmountStake(
                base_amount=stake.base_amount,
                american_odds=stake.american_odds,
                entry_field="risk",
                entry_amount=stake.risk,
                risk=stake.risk,
                to_win=stake.to_win,
            )
    if not stake_input:
        return False

    amount_str = f"{entry_stake.entry_amount:.2f}"
    recalc_fn = win_recalc_js if entry_stake.entry_field == "to_win" else risk_recalc_js
    try:
        stake_input.click()
        stake_input.clear()
        stake_input.send_keys(amount_str)
    except Exception:
        recalc_line = ""
        if recalc_fn:
            recalc_line = f"if (typeof {recalc_fn} === 'function') {recalc_fn}(el);"
        driver.execute_script(
            f"""
            var el = arguments[0];
            var val = arguments[1];
            el.focus();
            el.value = val;
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            {recalc_line}
            """,
            stake_input,
            amount_str,
        )
    field_label = "to-win" if entry_stake.entry_field == "to_win" else "risk"
    logger.info(
        f"Base-amount stake entered in {field_label}: {amount_str} "
        f"(risk=${stake.risk:.2f}, to-win=${stake.to_win:.2f})"
    )
    return True
