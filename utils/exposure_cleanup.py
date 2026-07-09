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
        game_still_actionable = _game_still_actionable(parsed, meta)
        if game_still_actionable:
            # Keep unhedged exposure until the game starts — do not clear on age alone.
            continue

        reason = "game started or finished"
        cache.clear_partial_exposure(pair_key)
        cache.clear_arb_legs_for_pair_key(pair_key)
        arb_stub = {
            "team_1": parsed["team_1"],
            "team_2": parsed["team_2"],
            "team_1_bookmaker": parsed["book_1"],
            "team_2_bookmaker": parsed["book_2"],
            "game_date": parsed["game_date"],
            "bet_type": parsed.get("bet_type", "moneyline"),
        }
        if parsed.get("spread_value") is not None:
            arb_stub["spread_value"] = parsed["spread_value"]
        cache.clear_game_event_owner(arb_stub)
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
