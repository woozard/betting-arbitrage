"""S411 fast place: first attempt skips the pre-place open-bets navigation."""

import logging

from controllers.Sports411Controller import Sports411Controller


def _controller():
    ctrl = Sports411Controller.__new__(Sports411Controller)
    ctrl.logger = logging.getLogger("test-s411")
    ctrl.bookmaker = "sports411"
    ctrl._last_bet_error = None
    ctrl.calls = []
    return ctrl


def _install_stubs(ctrl, monkeypatch):
    """Track whether the pre-place open-bets verify was invoked."""
    ctrl.verify_calls = 0
    ctrl.opened = False
    ctrl.submitted = False

    def _verify(*a, **k):
        ctrl.verify_calls += 1
        return False, "not found"

    def _refresh(*a, **k):
        pass

    def _open(*a, **k):
        ctrl.opened = True

    def _submit(stake_plan, *a, **k):
        ctrl.submitted = True
        return True, stake_plan

    monkeypatch.setattr(ctrl, "_verify_open_bet_on_pending", _verify)
    monkeypatch.setattr(ctrl, "_refresh_session_before_wager", _refresh)
    monkeypatch.setattr(ctrl, "_open_betslip_for_wager", _open)
    monkeypatch.setattr(ctrl, "_submit_betslip_wager", _submit)


def test_fast_place_skips_preplace_open_bets_check(monkeypatch):
    monkeypatch.setattr(
        "controllers.Sports411Controller.S411_FAST_PLACE", True
    )
    ctrl = _controller()
    _install_stubs(ctrl, monkeypatch)

    ok, _ = ctrl._execute_bet_attempt(
        "g1", "Detroit Tigers", "-123", 20.0,
        team_1="Athletics", team_2="Detroit Tigers",
    )

    assert ok is True
    assert ctrl.opened and ctrl.submitted
    # No pre-place open-bets navigation on the first attempt.
    assert ctrl.verify_calls == 0


def test_full_path_still_checks_open_bets_first(monkeypatch):
    monkeypatch.setattr(
        "controllers.Sports411Controller.S411_FAST_PLACE", False
    )
    ctrl = _controller()
    _install_stubs(ctrl, monkeypatch)

    ok, _ = ctrl._execute_bet_attempt(
        "g1", "Detroit Tigers", "-123", 20.0,
        team_1="Athletics", team_2="Detroit Tigers",
    )

    assert ok is True
    # Full path does the pre-place open-bets navigation.
    assert ctrl.verify_calls == 1
