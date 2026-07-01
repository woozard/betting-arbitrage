"""Shared idle odds-watch tick helpers for book controllers."""

from __future__ import annotations

from utils.odds_observer import tick_odds_watch


def tick_controller_odds_watch(
    controller,
    last_force_scan: float,
    *,
    idle_label: str = "betting-idle",
    force_scan_interval: float | None = None,
):
    """Poll ML + spread when DOM changes or force-scan interval elapses."""
    force_scan_interval = (
        force_scan_interval or controller.ODDS_WATCH_FORCE_SCAN_SECONDS
    )
    selectors = getattr(controller, "ODDS_OBSERVER_SELECTORS", None)

    def poll(source, force_scan=False):
        controller._poll_odds_watch_once(force_scan=force_scan, source=source)

    return tick_odds_watch(
        getattr(controller, "driver", None),
        last_force_scan,
        force_scan_interval,
        poll,
        selectors=selectors,
        logger=getattr(controller, "logger", None),
        force_label=f"{idle_label}/force-scan",
        change_label=f"{idle_label}/dom-change",
    )
