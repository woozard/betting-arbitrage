import os
import threading
import time


class SessionUnauthorizedError(Exception):
    """Book API returned 401 or an HTML/login page instead of JSON."""


class BettingLoopWatchdog:
    """Exit the process if the betting loop stops heartbeating (silent Chrome hang)."""

    def __init__(self, logger, max_silent_seconds: int = 300, check_interval: int = 60):
        self.logger = logger
        self.max_silent_seconds = max_silent_seconds
        self.check_interval = check_interval
        self._last_beat = time.time()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="betting-watchdog", daemon=True)
        self._thread.start()

    def beat(self):
        self._last_beat = time.time()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self.check_interval):
            silent_for = time.time() - self._last_beat
            if silent_for < self.max_silent_seconds:
                continue
            self.logger.error(
                f"Watchdog: no betting loop heartbeat for {silent_for:.0f}s "
                f"(limit {self.max_silent_seconds}s); exiting so scheduler can restart"
            )
            os._exit(1)


class OddsScanHealthWatchdog:
    """Exit the process if odds ingestion stops producing games for too long.

    Scheduler will spawn a fresh process (new Chrome + login).
    """

    def __init__(
        self,
        logger,
        max_unhealthy_seconds: int = None,
        check_interval: int = None,
    ):
        self.logger = logger
        self.max_unhealthy_seconds = max_unhealthy_seconds or int(
            os.getenv("ODDS_SCAN_UNHEALTHY_SEC", "600")
        )
        self.check_interval = check_interval or int(
            os.getenv("ODDS_SCAN_CHECK_INTERVAL", "30")
        )
        self._last_success = time.time()
        self._last_failure_reason = ""
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="odds-scan-health", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def mark_success(self, games_count: int = None):
        self._last_success = time.time()
        self._last_failure_reason = ""

    def mark_failure(self, reason: str):
        self._last_failure_reason = reason or "unknown"

    def _run(self):
        while not self._stop.wait(self.check_interval):
            unhealthy_for = time.time() - self._last_success
            if unhealthy_for < self.max_unhealthy_seconds:
                continue
            detail = self._last_failure_reason or "no successful odds scan"
            self.logger.error(
                f"Odds scan health watchdog: no healthy scan for {unhealthy_for:.0f}s "
                f"(limit {self.max_unhealthy_seconds}s; last issue: {detail}); "
                "exiting so scheduler can restart"
            )
            os._exit(1)