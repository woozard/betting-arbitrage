from utils.helpers import is_plausible_moneyline_pair
from utils.moneyline_arb import (
    is_valid_moneyline_arb_legs,
    validate_cross_leg_moneyline_signs,
    validate_moneyline_arb_payload,
)
from utils.moneyline_odds import (
    arb_moneyline_odds_acceptable,
    moneyline_int_odds_acceptable,
    moneyline_odds_acceptable,
)


def test_valid_moneyline_arb_legs():
    assert is_valid_moneyline_arb_legs(-103, 110)
    assert is_valid_moneyline_arb_legs(110, -103)
    assert not is_valid_moneyline_arb_legs(-103, -104)
    assert not is_valid_moneyline_arb_legs(110, 115)


def test_validate_moneyline_arb_payload():
    good = {
        "bet_type": "moneyline",
        "team_1_odds": -103,
        "team_2_odds": 110,
    }
    assert validate_moneyline_arb_payload(good) is None

    bad = {
        "bet_type": "moneyline",
        "team_1_odds": -103,
        "team_2_odds": -104,
    }
    reason = validate_moneyline_arb_payload(bad)
    assert reason is not None
    assert "same side" in reason


def test_arb_moneyline_odds_rejects_opposite_side():
    assert not arb_moneyline_odds_acceptable(110, -104, tolerance=2)
    assert not arb_moneyline_odds_acceptable(-103, 110, tolerance=2)
    assert arb_moneyline_odds_acceptable(110, 108, tolerance=2)
    assert arb_moneyline_odds_acceptable(-103, -104, tolerance=2)


def test_moneyline_int_odds_acceptable():
    assert moneyline_int_odds_acceptable(108, 110, tolerance=2)
    assert not moneyline_int_odds_acceptable(-104, 110, tolerance=2)


class _MemRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl=None):
        self.data[key] = value


class _FakeCache:
    def __init__(self):
        self.redis = _MemRedis()
        self._placed = set()

    def arb_pair_key_from_arb(self, arb):
        return (
            f"{arb['team_1']}:{arb['team_2']}:"
            f"{arb['team_1_bookmaker']}:{arb['team_2_bookmaker']}:moneyline"
        )

    def is_arb_leg_placed(self, arb, bookmaker):
        return (self.arb_pair_key_from_arb(arb), bookmaker) in self._placed

    def mark_placed(self, arb, bookmaker, odds):
        self._placed.add((self.arb_pair_key_from_arb(arb), bookmaker))
        key = f"arb_real_bets_summary:{self.arb_pair_key_from_arb(arb)}"
        side = "leg1" if bookmaker == arb["team_1_bookmaker"] else "leg2"
        self.redis.set(key, {side: {"placed": True, "odds": odds}})


def test_validate_cross_leg_blocks_same_side_exposure():
    arb = {
        "bet_type": "moneyline",
        "team_1": "Mets",
        "team_2": "Braves",
        "team_1_bookmaker": "4casters",
        "team_1_game_id": "1",
        "team_1_odds": -103,
        "team_2_bookmaker": "betwar",
        "team_2_game_id": "2",
        "team_2_odds": 110,
    }
    cache = _FakeCache()
    cache.mark_placed(arb, "4casters", -103)

    reason = validate_cross_leg_moneyline_signs(cache, arb, "betwar")
    assert reason is None

    bad_arb = dict(arb, team_2_odds=-104)
    reason = validate_cross_leg_moneyline_signs(cache, bad_arb, "betwar")
    assert reason is not None
    assert "same-side exposure" in reason


def test_is_plausible_moneyline_pair_matches_cross_book_rule():
    assert is_plausible_moneyline_pair(-103, 110)
    assert not is_plausible_moneyline_pair(-103, -104)
