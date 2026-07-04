"""Low-latency helpers for bookmaker betting loops."""

from __future__ import annotations

import time


def wait_for_arb_or_idle(
    cache,
    bookmaker: str,
    *,
    idle_poll_fn=None,
    idle_poll_interval: float | None = None,
    last_idle_poll_at: float = 0.0,
) -> tuple[bool, float]:
    """
    Wait briefly for a Redis bet-wake signal; optionally run idle odds poll on interval.

    Returns (woke, last_idle_poll_at).
    """
    from utils.config import BETTING_IDLE_POLL_SECONDS

    interval = (
        BETTING_IDLE_POLL_SECONDS
        if idle_poll_interval is None
        else idle_poll_interval
    )

    payload = cache.wait_bet_wake(bookmaker)
    if payload:
        return True, last_idle_poll_at

    now = time.time()
    if idle_poll_fn and (now - last_idle_poll_at) >= interval:
        idle_poll_fn()
        return False, now

    return False, last_idle_poll_at
