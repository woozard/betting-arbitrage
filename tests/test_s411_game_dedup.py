"""S411 duplicate game_id handling — never merge different idgame rows."""

from controllers.Sports411Controller import Sports411Controller


def test_dedupe_keeps_both_game_ids_for_same_teams():
    games = [
        {
            "game_id": "52483932",
            "team_1": "Arizona Diamondbacks",
            "team_2": "San Diego Padres",
            "game_datetime": "2026-07-09 02:10:00",
            "moneyline": {"team_1": "+127", "team_2": "-144"},
        },
        {
            "game_id": "52511173",
            "team_1": "Arizona Diamondbacks",
            "team_2": "San Diego Padres",
            "game_datetime": "2026-07-10 02:10:00",
            "moneyline": {"team_1": "+111", "team_2": "-125"},
        },
    ]
    ctrl = Sports411Controller.__new__(Sports411Controller)
    ctrl.logger = type("L", (), {"warning": lambda *a, **k: None})()
    out = ctrl._dedupe_games_by_matchup(games)
    ids = {g["game_id"] for g in out}
    assert ids == {"52483932", "52511173"}


def test_dedupe_collapses_same_game_id_only():
    games = [
        {
            "game_id": "52483932",
            "team_1": "Arizona Diamondbacks",
            "team_2": "San Diego Padres",
            "moneyline": {"team_1": "0", "team_2": "0"},
        },
        {
            "game_id": "52483932",
            "team_1": "Arizona Diamondbacks",
            "team_2": "San Diego Padres",
            "moneyline": {"team_1": "+127", "team_2": "-144"},
        },
    ]
    ctrl = Sports411Controller.__new__(Sports411Controller)
    ctrl.logger = type("L", (), {"warning": lambda *a, **k: None})()
    out = ctrl._dedupe_games_by_matchup(games)
    assert len(out) == 1
    assert out[0]["moneyline"]["team_2"] == "-144"
