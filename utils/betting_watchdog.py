import os
import threading
import time


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