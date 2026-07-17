#!/usr/bin/env python3
"""List current MLB games offered on the configured PS3838 account."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from types import SimpleNamespace

from utils.config import PS3838, PS3838_ACCOUNT, PS3838_PASSWORD, PS3838_LABEL
from controllers.Ps3838Controller import Ps3838Controller


def main() -> int:
    if not PS3838_ACCOUNT or not PS3838_PASSWORD:
        print("PS3838_ACCOUNT / PS3838_PASSWORD not set", file=sys.stderr)
        return 2

    account = SimpleNamespace(
        account=PS3838_ACCOUNT,
        password=PS3838_PASSWORD,
        label=PS3838_LABEL,
    )
    ctrl = Ps3838Controller(account, PS3838, sport="baseball")
    try:
        games = ctrl.list_mlb_games()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"PS3838 account {PS3838_ACCOUNT}: {len(games)} MLB game(s)")
    for g in games:
        ml = g.get("moneyline") or {}
        meta = g.get("meta") or {}
        print(
            f"- {g.get('game_datetime')} | {g.get('team_1')} vs {g.get('team_2')} | "
            f"ML {ml.get('team_1')}/{ml.get('team_2')} | "
            f"league={meta.get('league_name')} live={meta.get('live_status')} status={meta.get('status')}"
        )
    if "--json" in sys.argv:
        print(json.dumps(games, indent=2, default=str)[:200000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
