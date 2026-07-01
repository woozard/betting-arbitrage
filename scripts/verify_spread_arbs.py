#!/usr/bin/env python3
"""Verify spread arb alignment and math for flagged Telegram alerts."""

from decimal import Decimal

from controllers.ArbitrageController import ArbitrageController
from utils.helpers import align_cross_book_spreads


def row(book, t1, t2, sv, s1, s2, gid):
    return {
        "bookmaker": book,
        "team_1": t1,
        "team_2": t2,
        "spread_value": sv,
        "spread_team_1": s1,
        "spread_team_2": s2,
        "game_id": gid,
        "sport": "mlb",
        "league": "mlb",
        "game_datetime": "2026-07-01 23:05:00",
    }


def main():
    ctrl = ArbitrageController()
    cases = [
        (
            "Rangers alert (Paradise -143 / Betamapola +173)",
            row("paradisewager", "Texas Rangers", "Cleveland Guardians", -1.5, -143, 175, "965-966"),
            row("betamapola", "Texas Rangers", "Cleveland Guardians", -1.5, -203, 173, "965-966"),
        ),
        (
            "Giants alert (Betamapola -190 / Paradise +231)",
            row("betamapola", "San Francisco Giants", "Arizona Diamondbacks", -1.5, -190, 165, "961-962"),
            row("paradisewager", "San Francisco Giants", "Arizona Diamondbacks", -1.5, -174, 231, "961-962"),
        ),
        (
            "S411 vs Betamapola (spread_value sign mismatch)",
            row("sports411", "Texas Rangers", "Cleveland Guardians", 1.5, -208, 174, "52321146"),
            row("betamapola", "Texas Rangers", "Cleveland Guardians", -1.5, -203, 173, "965-966"),
        ),
        (
            "3et vs Betamapola (possible odds inversion on 3et)",
            row("3et", "Texas Rangers", "Cleveland Guardians", -1.5, 174, -213, "2550171"),
            row("betamapola", "Texas Rangers", "Cleveland Guardians", -1.5, -203, 173, "965-966"),
        ),
    ]

    for name, o1, o2 in cases:
        print("=" * 70)
        print(name)
        for o in (o1, o2):
            print(
                f"  {o['bookmaker']:14} sv={o['spread_value']:+.1f} "
                f"odds={o['spread_team_1']}/{o['spread_team_2']} "
                f"({o['team_1']} / {o['team_2']})"
            )
        aligned = align_cross_book_spreads(o1, o2)
        print(f"  aligned: {aligned is not None}")
        if not aligned:
            continue
        a_t1, a_t2, b_t1, b_t2, sv = aligned
        for label, x, y in (
            ("o1.t1 + o2.t2", a_t1, b_t2),
            ("o2.t1 + o1.t2", b_t1, a_t2),
        ):
            total = ctrl._ArbitrageController__calc_arb_total(x, y)
            if total:
                profit = float((Decimal(1) - total) * 100)
                print(f"  {label}: odds {x}/{y} -> total={float(total):.4f} profit={profit:.2f}%")


if __name__ == "__main__":
    main()
