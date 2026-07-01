#!/usr/bin/env python3
"""Print current 3et MLB moneylines and spreads."""
import os
import sys

os.environ.setdefault("SKIP_DB_BOOTSTRAP", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

from controllers.ThreeEtController import ThreeEtController
from utils.config import THREEET, THREEET_ACCOUNT, THREEET_PASSWORD, THREEET_LABEL


def main():
    if not THREEET_ACCOUNT or not THREEET_PASSWORD:
        raise SystemExit("THREEET_ACCOUNT / THREEET_PASSWORD required in .env")

    acc = SimpleNamespace(
        account=THREEET_ACCOUNT,
        password=THREEET_PASSWORD,
        label=THREEET_LABEL,
    )
    ctrl = ThreeEtController(acc, THREEET, sport="baseball")
    ctrl._login()
    games = ctrl._refresh_schedule_cache()
    games.sort(key=lambda g: g.get("game_datetime") or "")

    print(f"\n3et MLB pregame — {len(games)} games with moneyline\n")
    hdr = f"{'Match':<50} {'ML (team1 / team2)':<22} {'Spread (line / odds)':<28} Start (UTC)"
    print(hdr)
    print("-" * len(hdr))

    for g in games:
        match = f"{g['team_1']} vs {g['team_2']}"[:48]
        ml = f"{g['moneyline']['team_1']} / {g['moneyline']['team_2']}"
        sp = g.get("spread") or {}
        sv = sp.get("team_1_spread")
        if sv is not None and sp.get("team_1_odds"):
            spread = (
                f"{sv:+.1f} ({sp['team_1_odds']}) / "
                f"{-float(sv):+.1f} ({sp.get('team_2_odds', '?')})"
            )
        else:
            spread = "—"
        dt = (g.get("game_datetime") or "")[:16]
        print(f"{match:<50} {ml:<22} {spread:<28} {dt}")


if __name__ == "__main__":
    main()
