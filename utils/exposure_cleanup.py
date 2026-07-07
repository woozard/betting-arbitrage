import os
import time
from datetime import datetime

import pytz

from utils.config import ARB_TTL_SECONDS
from utils.helpers import is_game_pregame

DEFAULT_MAX_AGE_SECONDS = int(
    os.getenv("EXPOSURE_STALE_SECONDS", str(max(3600, ARB_TTL_SECONDS * 6)))
)
DEFAULT_TICK_INTERVAL_SECONDS = int(os.getenv("EXPOSURE_CLEANUP_INTERVAL_SEC", "120"))

_EASTERN = pytz.timezone("America/New_York")


def _eastern_today():
    return datetime.now(_EASTERN).date()


def _game_still_actionable(parsed: dict, meta: dict | None) -> bool:
    """True while the event is still pregame (hedge window may be open)."""
    meta = meta or {}
    gdt = meta.get("game_datetime")
    if gdt:
        return is_game_pregame(gdt)

    game_date = (parsed or {}).get("game_date")
    if not game_date:
        return True
    try:
        slate_day = datetime.strptime(str(game_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    # Slate dates are Eastern; do not clear at UTC midnight while the game is still tonight.
    return _eastern_today() <= slate_day


def cleanup_stale_partial_exposure(cache, logger, max_age_seconds=None):
    """Clear abandoned partial-exposure flags and unlock stale arb scans."""
    max_age = max_age_seconds or DEFAULT_MAX_AGE_SECONDS
    now = time.time()
    cleared = 0

    legacy_removed = cache.purge_legacy_leg_placed_keys()
    if legacy_removed:
        logger.info(
            f"Cleared {legacy_removed} legacy leg_placed flag(s) "
            f"(migrated to pair-scoped arb_leg_placed keys)"
        )

    for pair_key in cache.list_partial_exposure_pair_keys():
        parsed = cache.parse_matchup_pair_key(pair_key)
        if not parsed:
            logger.warning(f"Stale exposure cleanup: unparseable pair_key {pair_key!r}")
            continue

        meta = cache.get_partial_exposure_meta(pair_key) or {}
        marked_at = meta.get("marked_at")
        age = (now - float(marked_at)) if marked_at else (max_age + 1)

        game_still_actionable = _game_still_actionable(parsed, meta)
        if age <= max_age and game_still_actionable:
            continue

        if not game_still_actionable:
            reason = "game started or finished"
        else:
            reason = f"exposure age {age:.0f}s"
        cache.clear_partial_exposure(pair_key)
        cache.clear_arb_legs_for_pair_key(pair_key)
        cache.unlock_arb_scan(
            parsed["team_1"],
            parsed["team_2"],
            parsed["book_1"],
            parsed["book_2"],
            parsed["game_date"],
            bet_type=parsed.get("bet_type"),
            spread_value=parsed.get("spread_value"),
        )
        cleared += 1
        logger.info(
            f"Cleared stale partial exposure ({reason}) | "
            f"{parsed['team_1']} vs {parsed['team_2']} | "
            f"{parsed['book_1']} x {parsed['book_2']}"
        )

    return cleared


def tick_exposure_cleanup(
    cache,
    logger,
    last_run_at=0.0,
    interval_seconds=None,
    max_age_seconds=None,
):
    """Run stale exposure cleanup at most once per interval."""
    interval = interval_seconds or DEFAULT_TICK_INTERVAL_SECONDS
    now = time.time()
    if now - last_run_at < interval:
        return last_run_at
    cleanup_stale_partial_exposure(cache, logger, max_age_seconds=max_age_seconds)
    return now
