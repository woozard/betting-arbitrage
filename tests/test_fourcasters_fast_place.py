"""Fast-place path: fire order straight from scan odds, no extra orderbook GETs."""

import logging

from controllers.FourCastersController import FourCastersController
from utils.stake_sizing import base_amount_stake_from_odds


class _FakeApi:
    def __init__(self):
        self.token = "tok"
        self.orderbook_calls = 0
        self.place_calls = []

    def ensure_login(self, *a, **k):
        return {"user": {"auth": self.token}}

    def get_orderbook(self, *a, **k):
        self.orderbook_calls += 1
        return []

    def place_orders(self, orders):
        self.place_calls.append(orders)
        order = orders[0]
        return [
            {
                "matched": [
                    {
                        "txID": "tx-1",
                        "risk": order["bet"],
                        "win": round(order["bet"] * 1.19, 2),
                    }
                ]
            }
        ]


def _controller():
    ctrl = FourCastersController.__new__(FourCastersController)
    ctrl.api = _FakeApi()
    ctrl.logger = logging.getLogger("test-4casters")
    ctrl.account_id = "acct"
    ctrl.password = "pw"
    ctrl.bookmaker = "4casters"
    ctrl.sport_name = "MLB"
    ctrl.league = "MLB"
    ctrl._force_relogin = False
    ctrl._schedule_cache = [
        {
            "game_id": "g1",
            "team_1": "Athletics",
            "team_2": "Detroit Tigers",
            "line_ids": {"team_1": "away-id", "team_2": "home-id"},
            "spread": {},
        }
    ]
    return ctrl


def test_fast_place_skips_orderbook_and_places(monkeypatch):
    monkeypatch.setattr(
        "controllers.FourCastersController.FOURCASTERS_FAST_PLACE", True
    )
    ctrl = _controller()

    ok, stake = ctrl._execute_bet_attempt(
        "g1", "Athletics", "+119", 20.0,
        team_1="Athletics", team_2="Detroit Tigers",
        bet_type="moneyline",
    )

    assert ok is True
    # No pre-place orderbook GETs: schedule cache had the game already.
    assert ctrl.api.orderbook_calls == 0
    # Exactly one place call, with the arb odds we scanned.
    assert len(ctrl.api.place_calls) == 1
    order = ctrl.api.place_calls[0][0]
    assert order["odds"] == 119
    assert order["side"] == "away-id"
    assert order["orderType"] == "fillAndKill"
    assert stake.risk == 20.0


def test_fast_place_disabled_uses_full_path(monkeypatch):
    monkeypatch.setattr(
        "controllers.FourCastersController.FOURCASTERS_FAST_PLACE", False
    )
    ctrl = _controller()

    ok, _ = ctrl._execute_bet_attempt(
        "g1", "Athletics", "+119", 20.0,
        team_1="Athletics", team_2="Detroit Tigers",
        bet_type="moneyline",
    )

    assert ok is True
    # Full path re-reads the orderbook (line-move check + max-risk cap).
    assert ctrl.api.orderbook_calls >= 1
