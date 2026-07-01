"""Collect live health + arb status for the monitoring dashboard."""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from controllers.ArbitrageController import ArbitrageController
from cache.arbitrage_cache import ArbitrageCache
from utils.arb_scan_report import (
    BOOK_LABELS,
    SCAN_ODDS_WINDOW_MINUTES,
    _book_freshness_lines,
    _store_matchup_row,
    scan_pair_rows,
)
from utils.config import (
    ACTIVE_ARB_BOOK_PAIRS,
    ACTIVE_ARB_BOOKMAKERS,
    ARB_MAX_TOTAL_PROB,
    BET_STAKE,
    MIN_ARB_PROFIT_PCT,
    SEQUENTIAL_ARB_BETTING,
)

BASE_PATH = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_PATH / "logs"

JOB_SPECS = [
    {"name": "scheduler", "pattern": "scheduler.py", "log": None, "label": "Scheduler"},
    {"name": "arbitrage", "pattern": "arbitrage.py", "log": "arbitrage.log", "label": "Arb scanner"},
    {"name": "sports411_betting", "pattern": "sports411_betting.py", "log": "sports411_betting.log", "label": "S411"},
    {"name": "betamapola_betting", "pattern": "betamapola_betting.py", "log": "betamapola_betting.log", "label": "Betamapola"},
    {"name": "paradisewager_betting", "pattern": "paradisewager_betting.py", "log": "paradisewager_betting.log", "label": "Paradise"},
    {"name": "betwar_betting", "pattern": "betwar_betting.py", "log": "betwar_betting.log", "label": "BetWar"},
    {"name": "lowvig_betting", "pattern": "lowvig_betting.py", "log": "lowvig_betting.log", "label": "LowVig"},
    {"name": "threeet_betting", "pattern": "threeet_betting.py", "log": "threeet_betting.log", "label": "3et"},
    {"name": "fourcasters_betting", "pattern": "fourcasters_betting.py", "log": "fourcasters_betting.log", "label": "4casters"},
    {"name": "ops_health_agent", "pattern": "ops_health_agent.py", "log": "ops_health_agent.log", "label": "Ops agent"},
    {"name": "polymarket_odds", "pattern": "polymarket_odds.py", "log": "polymarket_odds.log", "label": "Polymarket"},
    {"name": "telegram_ops_bot", "pattern": "telegram_ops_bot.py", "log": "telegram_ops_bot.log", "label": "Telegram bot"},
]

BOOK_LOG_MAP = {
    "sports411": "sports411_betting.log",
    "betamapola": "betamapola_betting.log",
    "paradisewager": "paradisewager_betting.log",
    "betwar": "betwar_betting.log",
    "lowvig": "lowvig_betting.log",
    "3et": "threeet_betting.log",
    "4casters": "fourcasters_betting.log",
}

POLL_PATTERNS = re.compile(
    r"Parsed \d+|Published \d+|Extracted \d+|API success|"
    r"Resolved MLB|Waiting for Arbitrage|Odds watch",
    re.I,
)
WARN_PATTERNS = re.compile(
    r"401|unauthorized|Recovering|ERROR|failed|STALE|HTML|SyntaxError",
    re.I,
)
SCAN_PATTERN = re.compile(
    r"Odds: (\d+) - Matches: (\d+) - Arbs: (\d+)(?: \(closest total prob: ([0-9.]+)\))?"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    dt = dt or _utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _age_seconds(ts: Optional[float]) -> Optional[int]:
    if ts is None:
        return None
    return max(0, int(time.time() - ts))


def _process_running(pattern: str) -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def _systemd_active(unit: str = "betting-arb") -> Optional[str]:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (r.stdout or "").strip() or None
    except Exception:
        return None


def _tail_lines(log_name: Optional[str], n: int = 80) -> list[str]:
    if not log_name:
        return []
    path = LOG_DIR / log_name
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def _log_mtime(log_name: Optional[str]) -> Optional[float]:
    if not log_name:
        return None
    path = LOG_DIR / log_name
    if not path.exists():
        return None
    try:
        return path.stat().st_mtime
    except Exception:
        return None


def _last_matching_line(lines: list[str], pattern: re.Pattern) -> Optional[str]:
    for line in reversed(lines):
        if pattern.search(line):
            return line.strip()
    return None


def _job_health(running: bool, log_age: Optional[int], last_line: Optional[str]) -> str:
    if not running:
        return "down"
    if log_age is None:
        return "unknown"
    if log_age > 600:
        return "down"
    if log_age > 120:
        return "degraded"
    if last_line and WARN_PATTERNS.search(last_line):
        return "degraded"
    return "healthy"


def _collect_jobs() -> list[dict]:
    rows = []
    for spec in JOB_SPECS:
        running = _process_running(spec["pattern"])
        mtime = _log_mtime(spec["log"])
        log_age = _age_seconds(mtime)
        lines = _tail_lines(spec["log"])
        last_poll = _last_matching_line(lines, POLL_PATTERNS)
        last_warn = _last_matching_line(lines, WARN_PATTERNS)
        rows.append(
            {
                "name": spec["name"],
                "label": spec["label"],
                "running": running,
                "log_age_sec": log_age,
                "last_activity": last_poll or (lines[-1].strip() if lines else None),
                "last_warning": last_warn,
                "health": _job_health(running, log_age, last_warn or last_poll),
            }
        )
    return rows


def _parse_last_scan(lines: list[str]) -> Optional[dict]:
    for line in reversed(lines):
        m = SCAN_PATTERN.search(line)
        if m:
            return {
                "odds": int(m.group(1)),
                "matches": int(m.group(2)),
                "arbs": int(m.group(3)),
                "closest_prob": float(m.group(4)) if m.group(4) else None,
                "line": line.strip(),
            }
    return None


def _book_rows_from_odds(odds: list) -> dict[str, dict]:
    by_book: dict[str, list] = defaultdict(list)
    latest_ts: dict[str, datetime] = {}
    for row in odds:
        book = row.get("bookmaker")
        if not book:
            continue
        by_book[book].append(row)
        ts = row.get("created_at")
        if ts and (book not in latest_ts or ts > latest_ts[book]):
            latest_ts[book] = ts

    out = {}
    for book in ACTIVE_ARB_BOOKMAKERS:
        log_name = BOOK_LOG_MAP.get(book)
        lines = _tail_lines(log_name)
        mtime = _log_mtime(log_name)
        last_poll = _last_matching_line(lines, POLL_PATTERNS)
        last_warn = _last_matching_line(lines, WARN_PATTERNS)
        db_ts = latest_ts.get(book)
        db_age = None
        if db_ts:
            db_age = max(0, int((_utc_now().replace(tzinfo=None) - db_ts).total_seconds()))
        log_age = _age_seconds(mtime)
        running = _process_running(BOOK_LOG_MAP.get(book, "").replace(".log", ".py"))
        if book == "sports411":
            running = _process_running("sports411_betting.py")

        health = "healthy"
        if not running:
            health = "down"
        elif last_warn and WARN_PATTERNS.search(last_warn):
            health = "degraded"
        elif log_age is not None and log_age > 600:
            health = "down"
        elif log_age is not None and log_age > 120:
            health = "degraded"

        out[book] = {
            "label": BOOK_LABELS.get(book, book),
            "games_in_window": len(by_book.get(book, [])),
            "last_db_write_sec": db_age,
            "last_log_activity_sec": log_age,
            "last_poll_line": last_poll,
            "last_warning": last_warn,
            "process_running": running,
            "health": health,
        }
    return out


def _pair_summary(ctrl: ArbitrageController, by_matchup: dict) -> list[dict]:
    rows = []
    for pair in sorted(ACTIVE_ARB_BOOK_PAIRS, key=lambda p: sorted(p)):
        books = sorted(pair)
        book_a, book_b = books[0], books[1]
        pair_rows = scan_pair_rows(ctrl, by_matchup, book_a, book_b)
        label_a = BOOK_LABELS.get(book_a, book_a)
        label_b = BOOK_LABELS.get(book_b, book_b)
        if not pair_rows:
            rows.append(
                {
                    "pair": f"{label_a} × {label_b}",
                    "matchups": 0,
                    "executable": 0,
                    "closest_match": None,
                    "closest_profit_pct": None,
                    "closest_total_prob": None,
                    "best_legs": None,
                    "health": "no_overlap",
                }
            )
            continue
        best = pair_rows[0]
        executable = sum(1 for r in pair_rows if r["arb"])
        rows.append(
            {
                "pair": f"{label_a} × {label_b}",
                "matchups": len(pair_rows),
                "executable": executable,
                "closest_match": best["match"],
                "closest_profit_pct": best["profit"],
                "closest_total_prob": best["total"],
                "best_legs": best["legs"],
                "health": "executable" if executable else "watching",
            }
        )
    rows.sort(key=lambda r: r["closest_total_prob"] if r["closest_total_prob"] is not None else 999)
    return rows


def _active_arbs_redis() -> list[dict]:
    cache = ArbitrageCache()
    arbs = cache.get_arbitrage(bet_type="moneyline")
    seen = set()
    out = []
    for arb in arbs:
        key = (
            arb.get("team_1"),
            arb.get("team_2"),
            arb.get("team_1_bookmaker"),
            arb.get("team_2_bookmaker"),
        )
        if key in seen:
            continue
        seen.add(key)
        age = cache.arb_age_seconds(arb) if hasattr(cache, "arb_age_seconds") else None
        out.append(
            {
                "match": f"{arb.get('team_1')} vs {arb.get('team_2')}",
                "books": f"{arb.get('team_1_bookmaker')} × {arb.get('team_2_bookmaker')}",
                "profit_pct": arb.get("profit_pct"),
                "age_sec": age,
                "team_1_odds": arb.get("team_1_odds"),
                "team_2_odds": arb.get("team_2_odds"),
            }
        )
    return out


def build_status_payload(minutes: int = None) -> dict[str, Any]:
    if minutes is None:
        minutes = int(os.getenv("MONITOR_ODDS_WINDOW_MINUTES", "60"))
    from database.config import db1_session_scope

    with db1_session_scope() as db:
        ctrl = ArbitrageController(db=db)
        odds = ctrl.get_recent_moneyline_odds_from_db(
            minutes=minutes,
            keep_created_at=True,
            require_plausible_moneyline=True,
        )

    by_matchup = defaultdict(dict)
    for row in odds:
        if row["bookmaker"] not in ACTIVE_ARB_BOOKMAKERS:
            continue
        _store_matchup_row(by_matchup, row)

    arb_lines = _tail_lines("arbitrage.log", 120)
    last_scan = _parse_last_scan(arb_lines)

    pair_rows = _pair_summary(ctrl, by_matchup)
    global_best = next((p for p in pair_rows if p["closest_total_prob"] is not None), None)

    freshness = _book_freshness_lines(odds)

    return {
        "timestamp": _iso(),
        "config": {
            "bet_stake": BET_STAKE,
            "min_arb_profit_pct": MIN_ARB_PROFIT_PCT,
            "arb_max_total_prob": ARB_MAX_TOTAL_PROB,
            "sequential_betting": SEQUENTIAL_ARB_BETTING,
            "odds_window_minutes": minutes,
        },
        "system": {
            "systemd_betting_arb": _systemd_active("betting-arb"),
            "systemd_monitor": _systemd_active("betting-arb-monitor"),
        },
        "jobs": _collect_jobs(),
        "books": _book_rows_from_odds(odds),
        "scanner": {
            "last_scan": last_scan,
            "freshness_lines": freshness,
        },
        "pairs": pair_rows,
        "active_arbs": _active_arbs_redis(),
        "summary": {
            "executable_pairs": sum(1 for p in pair_rows if p.get("executable", 0) > 0),
            "closest_pair": global_best["pair"] if global_best else None,
            "closest_match": global_best["closest_match"] if global_best else None,
            "closest_profit_pct": global_best["closest_profit_pct"] if global_best else None,
            "would_fire_now": bool(
                global_best
                and global_best["closest_total_prob"] is not None
                and global_best["closest_total_prob"] < ARB_MAX_TOTAL_PROB
            ),
        },
    }
