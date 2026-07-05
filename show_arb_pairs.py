#!/usr/bin/env python3
"""Show lowest cross-book cumulative implied probability for each book pair."""

import re
from collections import defaultdict
from decimal import Decimal
from itertools import combinations

from controllers.ArbitrageController import ArbitrageController
from utils.helpers import normalize_team, teams_same
from utils.game_registry import matchup_group_key

BOOKS = ("betamapola", "sports411", "paradisewager", "betwar", "lowvig", "3et", "4casters")
BOOK_PAIRS = list(combinations(BOOKS, 2))


def valid_ml(value) -> bool:
    if value is None:
        return False
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return False


def matchup_key(row: dict) -> tuple:
    return matchup_group_key(row)


def display_matchup(rows: list, key: tuple) -> str:
    """Prefer the longest team-name variant seen across books."""
    norm_a, norm_b = key[0]
    label_a, label_b = norm_a.title(), norm_b.title()
    for row in rows:
        for name in (row["team_1"], row["team_2"]):
            n = normalize_team(name)
            if teams_same(n, norm_a) and len(name) > len(label_a):
                label_a = name
            elif teams_same(n, norm_b) and len(name) > len(label_b):
                label_b = name
    return f"{label_a} vs {label_b}"


def align_moneylines(o1: dict, o2: dict):
    """Return (book_a_t1_ml, book_a_t2_ml, book_b_t1_ml, book_b_t2_ml) on same physical teams."""
    if teams_same(o1["team_1"], o2["team_1"]) and teams_same(o1["team_2"], o2["team_2"]):
        return (
            o1["moneyline_team_1"],
            o1["moneyline_team_2"],
            o2["moneyline_team_1"],
            o2["moneyline_team_2"],
        )
    if teams_same(o1["team_1"], o2["team_2"]) and teams_same(o1["team_2"], o2["team_1"]):
        return (
            o1["moneyline_team_1"],
            o1["moneyline_team_2"],
            o2["moneyline_team_2"],
            o2["moneyline_team_1"],
        )
    return None


def calc_arb_total(ctrl: ArbitrageController, odds_1, odds_2):
    if not valid_ml(odds_1) or not valid_ml(odds_2):
        return None
    return ctrl.implied_prob(odds_1) + ctrl.implied_prob(odds_2)


def best_pair_arb(ctrl: ArbitrageController, o1: dict, o2: dict):
    aligned = align_moneylines(o1, o2)
    if not aligned:
        return None

    a_t1, a_t2, b_t1, b_t2 = aligned
    # Direction 1: team_1 on book A, team_2 on book B
    p1 = calc_arb_total(ctrl, a_t1, b_t2)
    # Direction 2: team_1 on book B, team_2 on book A
    p2 = calc_arb_total(ctrl, b_t1, a_t2)

    candidates = []
    if p1 is not None:
        candidates.append((p1, f"{o1['bookmaker']} T1 {a_t1:g} + {o2['bookmaker']} T2 {b_t2:g}"))
    if p2 is not None:
        candidates.append((p2, f"{o2['bookmaker']} T1 {b_t1:g} + {o1['bookmaker']} T2 {a_t2:g}"))

    if not candidates:
        return None
    return min(candidates, key=lambda x: x[0])


def main(minutes: int = 30):
    ctrl = ArbitrageController()
    odds = ctrl.get_recent_moneyline_odds_from_db(minutes=minutes)

    by_matchup = defaultdict(dict)
    for row in odds:
        if row["bookmaker"] not in BOOKS:
            continue
        if not valid_ml(row.get("moneyline_team_1")) or not valid_ml(row.get("moneyline_team_2")):
            continue
        key = matchup_key(row)
        by_matchup[key][row["bookmaker"]] = row

    if not by_matchup:
        print(f"No valid moneyline odds in the last {minutes} minutes.")
        return

    print(f"Cross-book arb scan (last {minutes} min) — {len(BOOK_PAIRS)} book pairs\n")

    global_best = []

    for key in sorted(by_matchup.keys()):
        books = by_matchup[key]
        if len(books) < 2:
            continue

        rows = list(books.values())
        label = display_matchup(rows, key)
        day = key[1]
        print(f"=== {label} ({day}) ===")

        for b1, b2 in BOOK_PAIRS:
            if b1 not in books or b2 not in books:
                print(f"  {b1:14} x {b2:14}  —  (no overlap)")
                continue

            result = best_pair_arb(ctrl, books[b1], books[b2])
            if result is None:
                print(f"  {b1:14} x {b2:14}  —  (invalid / unaligned odds)")
                continue

            total, detail = result
            flag = "ARB" if total < Decimal("1") else "   "
            print(f"  {b1:14} x {b2:14}  {float(total):.4f}  {flag}  {detail}")
            global_best.append((total, label, b1, b2, detail))

        print()

    if global_best:
        total, label, b1, b2, detail = min(global_best, key=lambda x: x[0])
        print("--- Best overall right now ---")
        print(f"{label}: {b1} x {b2} = {float(total):.4f}")
        print(detail)


if __name__ == "__main__":
    main()