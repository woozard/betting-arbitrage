from utils.bet_placement import (
    parallel_arb_betting_enabled,
    resolve_arb_leg_stake,
    should_defer_for_sequential_first_leg,
    should_s411_exchange_hedge_preposition,
    should_wait_for_s411_hedge_preposition,
)


def _arb():
    return {
        "team_1": "Colorado Rockies",
        "team_2": "San Francisco Giants",
        "team_1_bookmaker": "4casters",
        "team_2_bookmaker": "sports411",
        "team_1_game_id": "fc1",
        "team_2_game_id": "s4111",
        "team_1_odds": -125,
        "team_2_odds": 120,
        "bet_type": "moneyline",
    }


class _FakeCache:
    def arb_pair_key_from_arb(self, arb):
        return "pair:test"


def test_parallel_disables_preposition_and_defer(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.SEQUENTIAL_ARB_BETTING", False)
    monkeypatch.setattr("utils.bet_placement.PARALLEL_EXCHANGE_ARB_BETTING", True)
    monkeypatch.setattr(
        "utils.config.S411_EXCHANGE_HEDGE_PREPOSITION", True, raising=False
    )
    arb = _arb()
    cache = _FakeCache()

    assert parallel_arb_betting_enabled("4casters", "sports411")
    assert not should_s411_exchange_hedge_preposition(
        "4casters", "sports411", "sports411", "moneyline"
    )
    assert not should_wait_for_s411_hedge_preposition(arb, "4casters")
    assert not should_defer_for_sequential_first_leg(
        cache, arb, "4casters", "sports411", "sports411", "moneyline"
    )


def test_parallel_resolve_stake_from_planned_first_leg(monkeypatch):
    monkeypatch.setattr("utils.bet_placement.PARALLEL_EXCHANGE_ARB_BETTING", True)
    arb = _arb()
    cache = _FakeCache()

    assert (
        resolve_arb_leg_stake(
            cache, arb, "4casters", "sports411", "4casters", -125, 20.0
        )
        == 20.0
    )
    stake = resolve_arb_leg_stake(
        cache, arb, "4casters", "sports411", "sports411", 120, 20.0
    )
    assert 19.0 < stake < 21.0
