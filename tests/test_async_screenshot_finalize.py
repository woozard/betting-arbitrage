"""finalize_confirmed_bet_with_screenshot: leg acknowledged sync, screenshot async."""

import threading
import time

import utils.bet_placement as bp


class _FakeCache:
    def __init__(self):
        self.acknowledged = []
        self._legs = {}

    def arb_pair_key_from_arb(self, arb):
        return "pair:test"

    def event_date_for_arb(self, arb):
        return "2026-07-09"

    def mark_arb_leg_placed(self, arb, bookmaker, game_id):
        self._legs[(bookmaker or "").lower()] = True
        self.acknowledged.append(bookmaker)

    def is_arb_leg_placed(self, arb, bookmaker):
        return self._legs.get((bookmaker or "").lower(), False)

    def lock_arb_scan(self, *a, **k):
        pass

    def mark_game_pair_daily_bet(self, *a, **k):
        pass


def _arb():
    return {
        "team_1": "Athletics",
        "team_2": "Detroit Tigers",
        "team_1_bookmaker": "4casters",
        "team_2_bookmaker": "sports411",
        "team_1_game_id": "g1",
        "team_2_game_id": "s1",
        "bet_type": "moneyline",
        "game_date": "2026-07-09",
    }


def test_async_screenshot_runs_off_thread(monkeypatch):
    cache = _FakeCache()
    started = threading.Event()
    release = threading.Event()
    finalize_calls = []

    monkeypatch.setattr(bp, "record_confirmed_leg", lambda *a, **k: None)

    def _slow_screenshot(*a, **k):
        started.set()
        release.wait(timeout=5)
        return "/tmp/shot.png"

    def _finalize(*a, **k):
        finalize_calls.append(k.get("screenshot_path"))

    monkeypatch.setattr(bp, "capture_bet_screenshot_for_alert", _slow_screenshot)
    monkeypatch.setattr(bp, "finalize_confirmed_bet", _finalize)

    bp.finalize_confirmed_bet_with_screenshot(
        cache, object(), _Logger(), _arb(), "4casters", 1, "Athletics", "g1",
        20.0, "+119", {},
        async_screenshot=True,
        driver_factory=lambda: object(),
    )

    # Leg acknowledged synchronously before the (still-blocked) screenshot finishes.
    assert cache.is_arb_leg_placed(_arb(), "4casters")
    assert started.wait(timeout=2)
    assert finalize_calls == []  # finalize hasn't run yet — screenshot still blocked

    release.set()
    for _ in range(50):
        if finalize_calls:
            break
        time.sleep(0.02)
    assert finalize_calls == ["/tmp/shot.png"]


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass
