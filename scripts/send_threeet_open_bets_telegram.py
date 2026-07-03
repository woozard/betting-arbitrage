#!/usr/bin/env python3
"""Fetch 3et open bets and send summary + receipt screenshot to real-bets Telegram."""
import argparse
import asyncio
import json
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from database.config import __get_db1_session__
from database.models.ArbitrageBets import ArbitrageBets
from utils.bet_screenshot import bet_screenshot_path, render_open_bets_receipt
from utils.config import TELEGRAM
from utils.helpers import format_utc_timestamp, send_telegram_alert, send_telegram_photo
from utils.logger import Logger
from utils.threeet_client import ThreeEtApiError, ThreeEtClient

OPEN_STATUSES = frozenset({"PENDING", "OPEN", "ACCEPTED", "PLACED"})


def _normalize_bets_payload(data) -> list[dict]:
    if isinstance(data, list):
        return [b for b in data if isinstance(b, dict)]
    if isinstance(data, dict):
        for key in ("bets", "content", "data", "openBets", "items"):
            val = data.get(key)
            if isinstance(val, list):
                return [b for b in val if isinstance(b, dict)]
    return []


def _bet_from_api_row(row: dict) -> dict:
    event = row.get("eventName") or ""
    runner = row.get("runnerName") or row.get("team_name") or ""
    market = row.get("marketName") or row.get("bet_type") or "Wager"
    odds = row.get("odds") or row.get("userSubmittedOdds") or row.get("displayOdds")
    if odds is not None:
        try:
            o = int(round(float(odds)))
            odds_str = f"+{o}" if o > 0 else str(o)
        except (TypeError, ValueError):
            odds_str = str(odds)
    else:
        odds_str = ""

    stake = row.get("stake") or row.get("requestedStake")
    try:
        stake_val = float(stake) if stake is not None else None
        stake_display = f"${stake_val:.2f}" if stake_val is not None else ""
    except (TypeError, ValueError):
        stake_display = str(stake or "")

    profit = row.get("potentialProfit")
    risk_win = stake_display
    if profit is not None:
        try:
            risk_win = f"{stake_display} / ${float(profit):.2f} win"
        except (TypeError, ValueError):
            pass

    bet_id = row.get("id") or row.get("betId")
    status = row.get("status") or ""
    return {
        "description": f"{runner} {market} {odds_str}".strip(),
        "match": event.replace(" at ", " vs ").replace(" @ ", " vs "),
        "team_name": runner,
        "odds": odds_str,
        "stake": stake_val if stake is not None else stake,
        "stake_display": risk_win,
        "status": status,
        "extra": f"Bet ID: {bet_id}" if bet_id else "",
        "bet_type": (market or "moneyline").lower(),
    }


def _bet_from_db_row(row: ArbitrageBets) -> dict:
    odds = row.odds
    try:
        o = int(round(float(odds)))
        odds_str = f"+{o}" if o > 0 else str(o)
    except (TypeError, ValueError):
        odds_str = str(odds)

    stake = row.stake
    try:
        stake_display = f"${float(stake):.2f}"
    except (TypeError, ValueError):
        stake_display = str(stake or "")

    market = (row.bet_type or "moneyline").replace("_", " ").title()
    return {
        "description": f"{row.team_name} {market} {odds_str}".strip(),
        "match": f"{row.team_1} vs {row.team_2}",
        "team_name": row.team_name,
        "odds": odds_str,
        "stake": stake,
        "stake_display": stake_display,
        "status": "PENDING",
        "extra": f"Game ID: {row.game_id}",
        "bet_type": row.bet_type or "moneyline",
    }


def fetch_open_bets_via_api(logger) -> list[dict] | None:
    from utils.config import THREEET_ACCOUNT, THREEET_PASSWORD

    if not THREEET_ACCOUNT or not THREEET_PASSWORD:
        return None

    api = ThreeEtClient(max_retries=4, retry_sleep=4)
    try:
        api.login(THREEET_ACCOUNT, THREEET_PASSWORD)
    except ThreeEtApiError as exc:
        logger.warning(f"3et API login failed: {exc}")
        return None

    paths = (
        "/betting/v3/bets?betStatus=PENDING",
        "/betting/v3/bets?betStatus=OPEN",
        "/betting/v3/bets?betStatus=UNSETTLED",
        "/betting/v3/bets",
    )
    for path in paths:
        try:
            data = api.get(path, js_render=False)
            rows = _normalize_bets_payload(data)
            if not rows and isinstance(data, dict):
                logger.info(f"3et {path} empty keys={list(data.keys())[:8]}")
                continue
            open_rows = [
                r for r in rows
                if (r.get("status") or "").upper() in OPEN_STATUSES or path.endswith("PENDING")
            ]
            if not open_rows and rows:
                open_rows = rows
            if open_rows:
                logger.info(f"3et open bets via {path}: {len(open_rows)}")
                return [_bet_from_api_row(r) for r in open_rows]
        except ThreeEtApiError as exc:
            logger.warning(f"3et {path} failed: {exc}")
        except Exception as exc:
            logger.warning(f"3et {path} error: {exc}")
    return None


def fetch_open_bets_from_db(logger, today_only: bool = True) -> list[dict]:
    db = __get_db1_session__()
    q = db.query(ArbitrageBets).filter(ArbitrageBets.bookmaker == "3et")
    if today_only:
        today = datetime.now().date()
        q = q.filter(ArbitrageBets.game_datetime >= datetime.combine(today, datetime.min.time()))
    rows = q.order_by(ArbitrageBets.id.desc()).limit(10).all()
    logger.info(f"3et open bets fallback from DB: {len(rows)} row(s)")
    return [_bet_from_db_row(r) for r in rows]


def format_open_bets_message(bets: list[dict], source: str) -> str:
    lines = [
        "===== 3et Open Bets =====",
        f"As of: {format_utc_timestamp()}",
        f"Source: {source}",
        "",
        f"Open wagers: {len(bets)}",
        "",
    ]
    if not bets:
        lines.append("No open wagers found.")
        return "\n".join(lines)

    for bet in bets:
        lines.append(f"• {bet.get('description', 'Wager')}")
        match = bet.get("match")
        if match:
            lines.append(f"  Match: {match}")
        stake_display = bet.get("stake_display")
        if stake_display:
            lines.append(f"  Stake: {stake_display}")
        status = bet.get("status")
        extra = bet.get("extra")
        meta = " · ".join(p for p in (f"Status: {status}" if status else "", extra) if p)
        if meta:
            lines.append(f"  {meta}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all-recent-db", action="store_true", help="Include all recent DB rows, not just today")
    args = parser.parse_args()

    chat_id = TELEGRAM.get("real_bets")
    if not chat_id and not args.dry_run:
        print("TELEGRAM_CHAT_REAL_BETS not set")
        sys.exit(1)

    logger = Logger.get_logger("3et-open-bets-telegram")
    bets = fetch_open_bets_via_api(logger)
    source = "3et API"
    if not bets:
        bets = fetch_open_bets_from_db(logger, today_only=not args.all_recent_db)
        source = "arb DB (today's 3et legs)" if not args.all_recent_db else "arb DB (recent 3et legs)"

    message = format_open_bets_message(bets, source)
    out = args.output or bet_screenshot_path("3et", "open_bets")
    path = render_open_bets_receipt(out, "3et", bets, title="OPEN BETS", logger=logger)
    if not path:
        print("Screenshot render failed")
        sys.exit(1)

    print(message)
    print(f"Screenshot: {path}")

    if args.dry_run:
        return

    asyncio.run(send_telegram_alert(message, chat_id))
    caption = "\n".join(
        ln for ln in message.splitlines()
        if ln.strip() and not ln.startswith("=====")
    )[:1024]
    asyncio.run(send_telegram_photo(path, caption=caption, chat_id=chat_id))
    print("Sent to TELEGRAM_CHAT_REAL_BETS")


if __name__ == "__main__":
    main()
