from utils.bet_placement import _build_arb_complete_alert, _build_leg_confirmed_alert
from utils.helpers import (
    align_cross_book_spreads,
    build_spread_odd_row,
    fix_spread_odds_orientation,
    format_arb_complete_alert,
    format_arb_opportunity_alert,
    resolve_ticosports_spread_lines,
    sanitize_spread_odds,
)


def test_mlb_run_line_keeps_odds_on_correct_team():
    """Favorite -1.5 often has plus juice; dog +1.5 often has minus juice."""
    spread = {
        "team_1_spread": -1.5,
        "team_2_spread": 1.5,
        "team_1_odds": 158,
        "team_2_odds": -189,
    }
    cleaned = sanitize_spread_odds(spread)
    assert cleaned is not None
    assert cleaned["team_1_odds"] == 158
    assert cleaned["team_2_odds"] == -189
    assert cleaned["team_1_spread"] == -1.5
    assert cleaned["team_2_spread"] == 1.5


def test_fix_spread_odds_orientation_does_not_swap():
    t1, t2 = fix_spread_odds_orientation(-1.5, 158, -189)
    assert t1 == 158
    assert t2 == -189


def test_tigers_rangers_amapola_lines():
    """TEX favorite: DET +1.5, TEX -1.5 regardless of unsigned Spread magnitude."""
    team_1_spread, team_2_spread = resolve_ticosports_spread_lines(1.5, 154, -185)
    assert team_1_spread == 1.5
    assert team_2_spread == -1.5

    team_1_spread, team_2_spread = resolve_ticosports_spread_lines(-1.5, 154, -185)
    assert team_1_spread == 1.5
    assert team_2_spread == -1.5


def test_cross_book_rejects_opposite_team_1_lines():
    """3et DET -1.5 vs amapola DET +1.5 must not align."""
    base = {
        "team_1": "Detroit Tigers",
        "team_2": "Texas Rangers",
        "spread_team_1": 158,
        "spread_team_2": -189,
    }
    o1 = {**base, "spread_value": -1.5}
    o2 = {
        **base,
        "spread_value": 1.5,
        "spread_team_1": -243,
        "spread_team_2": 203,
    }
    assert align_cross_book_spreads(o1, o2) is None


def test_spread_alert_shows_per_team_lines():
    arb = {
        "sport": "MLB",
        "bet_type": "spread",
        "profit_pct": 1.6,
        "team_1": "Detroit Tigers",
        "team_2": "Texas Rangers",
        "team_1_bookmaker": "3et",
        "team_2_bookmaker": "betamapola",
        "team_1_odds": -189,
        "team_2_odds": 203,
        "spread_value": -1.5,
        "spread_line_team_1": -1.5,
        "spread_line_team_2": 1.5,
    }
    alert = format_arb_opportunity_alert(arb)
    assert "Tigers -1.5 -189 3et" in alert
    assert "Rangers +1.5 +203 amapola" in alert


def test_format_arb_complete_alert_per_leg_stakes():
    arb = {
        "team_1": "Pittsburgh Pirates",
        "team_2": "Washington Nationals",
        "team_1_bookmaker": "betwar",
        "team_2_bookmaker": "3et",
        "team_1_odds": 140,
        "team_2_odds": -133,
        "bet_type": "moneyline",
        "profit_pct": 1.25,
    }
    alert = format_arb_complete_alert(arb, base_amount=20)
    assert alert.startswith("ML · +1.25% ✓")
    assert "Pirates +140 betwar · $20.00→$28.00" in alert
    assert "Nationals -133 3et · $26.60→$20.00" in alert
    assert "Identified At:" not in alert


def test_build_leg_confirmed_alert_leg_one_of_two():
    arb = {
        "identified_at": 1710000000,
        "sport": "MLB",
        "league": "MLB",
        "game_date": "2026-07-03",
        "team_1": "Pittsburgh Pirates",
        "team_2": "Washington Nationals",
        "team_1_bookmaker": "betwar",
        "team_2_bookmaker": "3et",
        "bet_type": "moneyline",
    }
    alert = _build_leg_confirmed_alert(
        arb,
        "betwar",
        1,
        "Pirates",
        20.0,
        "+140",
        "3et",
        other_leg_placed=False,
    )
    assert "Leg 1 of 2" in alert
    assert "Book: betwar" in alert
    assert "This bet: Pirates +140" in alert
    assert "Waiting for leg 2 on 3et" in alert
    assert "Screenshot: attached below" in alert
    assert "3et" not in alert.split("This bet:")[1].split("Real money:")[0]


def test_build_leg_confirmed_alert_leg_two_of_two():
    arb = {
        "identified_at": 1710000000,
        "sport": "MLB",
        "league": "MLB",
        "game_date": "2026-07-03",
        "team_1": "Pittsburgh Pirates",
        "team_2": "Washington Nationals",
        "team_1_bookmaker": "betwar",
        "team_2_bookmaker": "3et",
        "bet_type": "moneyline",
    }
    alert = _build_leg_confirmed_alert(
        arb,
        "3et",
        2,
        "Nationals",
        26.60,
        "-133",
        "betwar",
        other_leg_placed=True,
    )
    assert "Leg 2 of 2" in alert
    assert "Book: 3et" in alert
    assert "This bet: Nationals -133" in alert
    assert "full arb summary follows" in alert
    assert "Screenshot: attached below" in alert


def test_build_arb_complete_alert_includes_header():
    arb = {
        "team_1": "Pittsburgh Pirates",
        "team_2": "Washington Nationals",
        "team_1_bookmaker": "betwar",
        "team_2_bookmaker": "3et",
        "team_1_odds": 140,
        "team_2_odds": -133,
        "bet_type": "moneyline",
        "profit_pct": 1.25,
    }
    alert = _build_arb_complete_alert(arb, 20.0)
    assert alert.startswith("===== Arb Complete (Real Money) =====")
    assert "ML · +1.25% ✓" in alert
    assert "Pirates +140 betwar · $20.00→$28.00" in alert


def test_build_spread_odd_row_from_threeet_style():
    row = build_spread_odd_row(
        {"sport": "MLB", "league": "MLB", "game_id": "1", "game_datetime": None,
         "team_1": "Detroit Tigers", "team_2": "Texas Rangers", "bookmaker": "3et"},
        {
            "team_1_spread": -1.5,
            "team_2_spread": 1.5,
            "team_1_odds": 158,
            "team_2_odds": -189,
        },
    )
    assert row is not None
    assert float(row["spread_team_1"]) == 158
    assert float(row["spread_team_2"]) == -189
    assert float(row["spread_value"]) == -1.5
