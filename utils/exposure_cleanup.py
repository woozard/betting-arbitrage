import os
import time
from datetime import datetime

from utils.config import ARB_TTL_SECONDS

DEFAULT_MAX_AGE_SECONDS = int(
    os.getenv("EXPOSURE_STALE_SECONDS", str(max(3600, ARB_TTL_SECONDS * 6)))
)
DEFAULT_TICK_INTERVAL_SECONDS = int(os.getenv("EXPOSURE_CLEANUP_INTERVAL_SEC", "120"))


def cleanup_stale_partial_exposure(cache, logger, max_age_seconds=None):
    """Clear abandoned partial-exposure flags and unlock stale arb scans."""
    max_age = max_age_seconds or DEFAULT_MAX_AGE_SECONDS
    now = time.time()
    today = datetime.utcnow().date()
    cleared = 0

    for pair_key in cache.list_partial_exposure_pair_keys():
        parsed = cache.parse_matchup_pair_key(pair_key)
        if not parsed:
            logger.warning(f"Stale exposure cleanup: unparseable pair_key {pair_key!r}")
            continue

        meta = cache.get_partial_exposure_meta(pair_key) or {}
        marked_at = meta.get("marked_at")
        age = (now - float(marked_at)) if marked_at else (max_age + 1)

        game_day_passed = False
        try:
            game_day = datetime.strptime(parsed["game_date"], "%Y-%m-%d").date()
            game_day_passed = game_day < today
        except ValueError:
            pass

        if age <= max_age and not game_day_passed:
            continue

        reason = "game day passed" if game_day_passed else f"exposure age {age:.0f}s"
        cache.clear_partial_exposure(pair_key)
        cache.unlock_arb_scan(
            parsed["team_1"],
            parsed["team_2"],
            parsed["book_1"],
            parsed["book_2"],
            parsed["game_date"],
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
