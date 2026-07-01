"""Shared DOM MutationObserver helpers for on-page ML + spread odds watching."""

from __future__ import annotations

import time

MUTATION_OBSERVER_JS = """
window.oddsBuffer = window.oddsBuffer || [];
if (window.oddsObserverHandle) {
    try { window.oddsObserverHandle.disconnect(); } catch (e) {}
}
window.oddsObserverInstalled = false;
const __oddsTargets = %TARGETS_JSON%;
let __oddsTarget = null;
for (const sel of __oddsTargets) {
    try {
        const el = document.querySelector(sel);
        if (el) { __oddsTarget = el; break; }
    } catch (e) {}
}
if (!__oddsTarget) {
    __oddsTarget = document.body;
}
window.oddsObserverHandle = new MutationObserver(() => {
    if (window.oddsFlushTimer) {
        return;
    }
    window.oddsFlushTimer = setTimeout(() => {
        window.oddsBuffer.push(Date.now());
        window.oddsFlushTimer = null;
    }, 250);
});
window.oddsObserverHandle.observe(__oddsTarget, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
});
window.oddsObserverInstalled = true;
return true;
"""


def install_mutation_observer(driver, selectors: list[str], logger=None) -> bool:
    """Install debounced MutationObserver on the first matching selector."""
    import json

    if not selectors:
        selectors = ["body"]
    try:
        return bool(
            driver.execute_script(
                MUTATION_OBSERVER_JS.replace("%TARGETS_JSON%", json.dumps(selectors))
            )
        )
    except Exception as exc:
        if logger:
            logger.warning(f"Could not install odds MutationObserver: {exc}")
        return False


def mutation_observer_installed(driver) -> bool:
    try:
        return bool(driver.execute_script("return !!window.oddsObserverInstalled"))
    except Exception:
        return False


def ensure_mutation_observer(driver, selectors: list[str], logger=None) -> bool:
    if mutation_observer_installed(driver):
        return True
    return install_mutation_observer(driver, selectors, logger=logger)


def drain_mutation_buffer(driver) -> bool:
    """Return True if the observer recorded DOM changes since last drain."""
    try:
        count = driver.execute_script(
            """
            const n = (window.oddsBuffer || []).length;
            window.oddsBuffer = [];
            return n;
            """
        )
        return bool(count)
    except Exception:
        return False


def should_force_scan(last_force_scan: float, force_scan_interval: float) -> bool:
    if not last_force_scan:
        return True
    return (time.monotonic() - last_force_scan) >= force_scan_interval


def tick_odds_watch(
    driver,
    last_force_scan: float,
    force_scan_interval: float,
    poll_callback,
    *,
    selectors: list[str] | None = None,
    logger=None,
    force_label: str = "force-scan",
    change_label: str = "dom-change",
):
    """
    Run poll_callback(source) when DOM changed or force-scan interval elapsed.

    Returns (new_last_force_scan, processed_bool).
    """
    if selectors:
        ensure_mutation_observer(driver, selectors, logger=logger)

    force = should_force_scan(last_force_scan, force_scan_interval)
    changed = drain_mutation_buffer(driver) if driver else False

    if not force and not changed:
        return last_force_scan, False

    if force:
        last_force_scan = time.monotonic()
        source = force_label
    else:
        source = change_label

    poll_callback(source=source, force_scan=force)
    return last_force_scan, True
