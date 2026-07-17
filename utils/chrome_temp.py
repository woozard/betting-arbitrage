import glob
import os
import shutil
import tempfile
import time

# Project-local tmp avoids FileNotFoundError under systemd / small /tmp.
PROJECT_TMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tmp"))
os.makedirs(PROJECT_TMP_DIR, exist_ok=True)
tempfile.tempdir = PROJECT_TMP_DIR

INIT_FAILURE_STALE_AGE_SECONDS = 60


def chrome_temp_prefix(kind: str = "chrome_user_data") -> str:
    """Stack-scoped tempfile prefix so WNBA/MLB Chrome dirs don't collide in cleanup."""
    stack = (os.getenv("STACK_NAME") or os.getenv("ARB_STACK") or "").strip().lower()
    if stack:
        return f"{kind}_{stack}_"
    return f"{kind}_"


def discard_temp_dirs(*dirs, logger=None):
    """Remove specific Chrome/proxy temp dirs (e.g. from a failed __init__)."""
    removed = []
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        try:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
        except Exception:
            pass
    if logger and removed:
        logger.info(f"Discarded failed-init temp dirs: {', '.join(removed)}")


def cleanup_stale_temp_dirs(active_dirs=None, max_age_seconds=3600, logger=None):
    """Remove old brightdata_proxy_* / chrome_user_data_* dirs not in active_dirs.

    When STACK_NAME is set, only prune dirs for that stack.
    """
    active = {d for d in (active_dirs or []) if d}
    now = time.time()
    removed = 0
    stack = (os.getenv("STACK_NAME") or os.getenv("ARB_STACK") or "").strip().lower()
    if stack:
        patterns = (
            f"brightdata_proxy_{stack}_*",
            f"chrome_user_data_{stack}_*",
        )
    else:
        patterns = ("brightdata_proxy_*", "chrome_user_data_*")
    try:
        for pat in patterns:
            for d in glob.glob(os.path.join(PROJECT_TMP_DIR, pat)):
                if d in active:
                    continue
                try:
                    if now - os.path.getmtime(d) < max_age_seconds:
                        continue
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    if logger and removed:
        logger.info(
            f"Pruned {removed} stale chrome temp dir(s) (max_age={max_age_seconds}s)"
        )


def handle_init_driver_failure(logger, user_data_dir=None, proxy_extension_dir=None):
    """Called when webdriver.Chrome() fails in controller __init__."""
    discard_temp_dirs(user_data_dir, proxy_extension_dir, logger=logger)
    cleanup_stale_temp_dirs(
        active_dirs=(),
        max_age_seconds=INIT_FAILURE_STALE_AGE_SECONDS,
        logger=logger,
    )
