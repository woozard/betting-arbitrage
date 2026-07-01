"""Build MLB cross-book scan reports for CLI and Telegram /scan."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple

from controllers.ArbitrageController import ArbitrageController
from utils.config import ACTIVE_ARB_BOOKMAKERS, ACTIVE_ARB_BOOK_PAIRS, ARB_MAX_TOTAL_PROB, MIN_ARB_PROFIT_PCT
from utils.helpers import format_utc_timestamp, is_plausible_moneyline_pair, normalize_team, teams_same
from utils.game_registry import matchup_group_key

SCAN_ODDS_WINDOW_MINUTES = 10

BOOK_LABELS = {
    "sports411": "S411",
    "betamapola": "Amapola",
    "paradisewager": "Paradise",
    "betwar": "BetWar",
    "lowvig": "LowVig",
    "3et": "3et",
}


def valid_ml(value) -> bool:
    if value is None:
        return False
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return False


def _row_is_usable(row: dict) -> bool:
    ml_1 = row.get("moneyline_team_1")
    ml_2 = row.get("moneyline_team_2")
    return (
        valid_ml(ml_1)
        and valid_ml(ml_2)
        and is_plausible_moneyline_pair(ml_1, ml_2)
    )


def _store_matchup_row(by_matchup: dict, row: dict) -> None:
    """Keep the newest scrape per bookmaker for each normalized matchup."""
    if not _row_is_usable(row):
        return
    key = matchup_key(row)
    book = row["bookmaker"]
    current = by_matchup[key].get(book)
    if current is None:
        by_matchup[key][book] = row
        return
    cur_ts = current.get("created_at")
    new_ts = row.get("created_at")
    if cur_ts is None or (new_ts is not None and new_ts >= cur_ts):
        by_matchup[key][book] = row


def _book_freshness_lines(odds: list) -> List[str]:
    latest_by_book = {}
    for row in odds:
        book = row.get("bookmaker")
        ts = row.get("created_at")
        if not book or ts is None:
            continue
        if book not in latest_by_book or ts > latest_by_book[book]:
            latest_by_book[book] = ts

    if not latest_by_book:
        return ["Book freshness: no recent scrapes in window"]

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lines = ["Book freshness (latest DB scrape):"]
    for book in sorted(latest_by_book):
        ts = latest_by_book[book]
        age_s = max(0, int((now - ts).total_seconds()))
        label = BOOK_LABELS.get(book, book)
        lines.append(
            f"  {label:<9} {format_utc_timestamp(dt=ts, time_only=True)} ({age_s}s ago)"
        )
    return lines


def matchup_key(row: dict) -> tuple:
    return matchup_group_key(row)


def align_moneylines(o1: dict, o2: dict):
    if teams_same(o1["team_1"], o2["team_1"]) and teams_same(o1["team_2"], o2["team_2"]):
        return (
            o1["team_1"],
            o1["team_2"],
            o1["moneyline_team_1"],
            o1["moneyline_team_2"],
            o2["moneyline_team_1"],
            o2["moneyline_team_2"],
        )
    if teams_same(o1["team_1"], o2["team_2"]) and teams_same(o1["team_2"], o2["team_1"]):
        return (
            o1["team_1"],
            o1["team_2"],
            o1["moneyline_team_1"],
            o1["moneyline_team_2"],
            o2["moneyline_team_2"],
            o2["moneyline_team_1"],
        )
    return None


def _short_match(t1: str, t2: str, max_len: int = 28) -> str:
    label = f"{t1} vs {t2}"
    if len(label) <= max_len:
        return label
    return label[: max_len - 1] + "…"


def _bar_for_prob(total_prob: float, width: int = 18) -> str:
    """Bar showing distance above arb threshold 1.0000."""
    excess = max(0.0, float(total_prob) - 1.0)
    scale = 0.035  # ~3.5% above threshold fills the bar
    filled = min(width, max(0, int(round(excess / scale * width))))
    return ("█" * filled) + ("░" * (width - filled))


def scan_pair_rows(
    ctrl: ArbitrageController,
    by_matchup: dict,
    book_a: str,
    book_b: str,
) -> List[dict]:
    rows = []
    label_a = BOOK_LABELS.get(book_a, book_a)
    label_b = BOOK_LABELS.get(book_b, book_b)

    for _key, books in sorted(by_matchup.items()):
        if book_a not in books or book_b not in books:
            continue
        o1, o2 = books[book_a], books[book_b]
        aligned = align_moneylines(o1, o2)
        if not aligned:
            continue

        t1, t2, a1, a2, b1, b2 = aligned
        p_dir1 = ctrl.implied_prob(a1) + ctrl.implied_prob(b2)
        p_dir2 = ctrl.implied_prob(b1) + ctrl.implied_prob(a2)

        if p_dir1 <= p_dir2:
            total = p_dir1
            legs = f"{label_a} {t1} ({a1:g}) + {label_b} {t2} ({b2:g})"
        else:
            total = p_dir2
            legs = f"{label_b} {t1} ({b1:g}) + {label_a} {t2} ({a2:g})"

        profit = float((Decimal(1) - total) * 100)
        rows.append(
            {
                "match": _short_match(t1, t2, 30),
                "book_a_ml": f"{a1:g}/{a2:g}",
                "book_b_ml": f"{b1:g}/{b2:g}",
                "total": float(total),
                "profit": profit,
                "legs": legs,
                "arb": total < Decimal(str(ARB_MAX_TOTAL_PROB)),
            }
        )

    rows.sort(key=lambda r: r["total"])
    return rows


def format_pair_section(book_a: str, book_b: str, rows: List[dict]) -> str:
    label_a = BOOK_LABELS.get(book_a, book_a)
    label_b = BOOK_LABELS.get(book_b, book_b)
    lines = [f"{label_a} x {label_b}"]

    if not rows:
        lines.append("(no overlapping MLB matchups in window)")
        return "\n".join(lines)

    arb_count = sum(1 for r in rows if r["arb"])
    best = rows[0]
    lines.append(
        f"Arbs: {arb_count} | Closest: {best['match']} "
        f"{best['total']:.4f} ({best['profit']:+.2f}%)"
    )
    lines.append("")
    lines.append(f"{'Match':<31} {label_a:<11} {label_b:<11} {'Prob':<7} {'Profit'}")
    lines.append("-" * 72)

    for row in rows:
        flag = "✓" if row["arb"] else " "
        lines.append(
            f"{row['match']:<31} {row['book_a_ml']:<11} {row['book_b_ml']:<11} "
            f"{row['total']:.4f} {row['profit']:+.2f}% {flag}"
        )

    lines.append("")
    lines.append("Visual (distance from arb @ 1.0000):")
    for row in rows[:8]:
        short = row["match"][:22].ljust(22)
        bar = _bar_for_prob(row["total"])
        lines.append(f"{short} {bar} {row['total']:.4f} ({row['profit']:+.2f}%)")

    if best and not best["arb"]:
        lines.append("")
        lines.append(f"Best legs: {best['legs']}")

    return "\n".join(lines)


def build_scan_report(minutes: int = SCAN_ODDS_WINDOW_MINUTES) -> str:
    from database.config import db1_session_scope

    with db1_session_scope() as db:
        return _build_scan_report_with_db(db, minutes=minutes)


def _build_scan_report_with_db(db, minutes: int = SCAN_ODDS_WINDOW_MINUTES) -> str:
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

    now = format_utc_timestamp()
    if MIN_ARB_PROFIT_PCT != 0:
        threshold_line = (
            f"Execute when profit >= {MIN_ARB_PROFIT_PCT:.2f}% "
            f"(total prob < {ARB_MAX_TOTAL_PROB:.4f})"
        )
    else:
        threshold_line = f"Execute when total prob < {ARB_MAX_TOTAL_PROB:.4f} (positive profit %)"
    sections = [
        "===== MLB Scan =====",
        f"Time: {now}",
        f"Odds window: last {minutes} min (latest scrape per book/matchup)",
        threshold_line,
        "",
    ]
    sections.extend(_book_freshness_lines(odds))
    sections.append("")

    global_best: Optional[Tuple[float, str, dict]] = None
    pair_sections = []

    for pair in sorted(ACTIVE_ARB_BOOK_PAIRS, key=lambda p: sorted(p)):
        books = sorted(pair)
        book_a, book_b = books[0], books[1]
        rows = scan_pair_rows(ctrl, by_matchup, book_a, book_b)
        pair_sections.append(format_pair_section(book_a, book_b, rows))
        if rows:
            best = rows[0]
            if global_best is None or best["total"] < global_best[0]:
                global_best = (best["total"], f"{BOOK_LABELS.get(book_a, book_a)} x {BOOK_LABELS.get(book_b, book_b)}", best)

    sections.extend(pair_sections)

    sections.append("")
    if global_best:
        total, pair_label, best = global_best
        if best["arb"]:
            sections.append(f"Summary: {pair_label} has EXECUTABLE ARB ({best['profit']:+.2f}%)")
        else:
            sections.append(
                f"Summary: No executable arbs. Closest overall: {best['match']} "
                f"({pair_label}) at {total:.4f} ({best['profit']:+.2f}%)"
            )
    else:
        sections.append("Summary: No overlapping odds found for active pairs.")

    return "\n".join(sections)


def split_telegram_messages(text: str, limit: int = 4000) -> List[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    chunk = []
    size = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if size + line_len > limit and chunk:
            parts.append("\n".join(chunk))
            chunk = [line]
            size = line_len
        else:
            chunk.append(line)
            size += line_len
    if chunk:
        parts.append("\n".join(chunk))
    return parts
