"""Smoke tests for cross-book team canonicalization."""

from utils.team_registry import canonical_matchup_key, canonical_team, standard_team_name
from utils.game_registry import build_matchup_key
from utils.helpers import normalize_team, teams_same


def test_betwar_s411_same_matchup_key():
    s411 = ("New York Yankees", "Detroit Tigers", "2026-06-22 18:10:00")
    war = ("NY Yankees", "DET Tigers", "2026-06-22 18:10:00")
    assert canonical_matchup_key(*s411) == canonical_matchup_key(*war)
    assert normalize_team(s411[0]) == normalize_team(war[0])
    assert teams_same(s411[0], war[0])
    assert teams_same(s411[1], war[1])


def test_standard_team_name_expansion():
    assert standard_team_name("NY Yankees") == "New York Yankees"
    assert standard_team_name("DET Tigers") == "Detroit Tigers"
    assert standard_team_name("CHI White Sox") == "Chicago White Sox"


def test_chicago_teams_distinct():
    assert canonical_team("CHI Cubs") != canonical_team("CHI White Sox")
    assert canonical_team("Chicago Cubs") != canonical_team("Chicago White Sox")


def test_matchup_key_aligns_betwar_s411():
    s411_key = build_matchup_key(
        "baseball", "mlb", "New York Yankees", "Detroit Tigers", "2026-06-22 18:10:00"
    )
    war_key = build_matchup_key(
        "baseball", "mlb", "NY Yankees", "DET Tigers", "2026-06-22 18:10:00"
    )
    assert s411_key == war_key


if __name__ == "__main__":
    test_betwar_s411_same_matchup_key()
    test_standard_team_name_expansion()
    test_chicago_teams_distinct()
    test_matchup_key_aligns_betwar_s411()
    print("ok")
