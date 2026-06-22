#!/usr/bin/env python3
"""Backfill canonical_games + game_book_links from recent arbitrage_odds rows."""

from datetime import datetime, timedelta

from database.config import db1_session_scope
from database.models.ArbitrageOdds import ArbitrageOdds
from utils.game_registry import register_game_from_odds


def main(hours: int = 72) -> None:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    seen = set()
    created = 0

    with db1_session_scope() as db:
        rows = (
            db.query(ArbitrageOdds)
            .filter(ArbitrageOdds.bet_type == "moneyline")
            .filter(ArbitrageOdds.created_at >= cutoff)
            .order_by(ArbitrageOdds.created_at.asc())
            .all()
        )
        for row in rows:
            key = (row.bookmaker, row.game_id)
            if key in seen:
                continue
            seen.add(key)
            odd = {
                "sport": row.sport,
                "league": row.league,
                "game_id": row.game_id,
                "game_datetime": row.game_datetime,
                "team_1": row.team_1,
                "team_2": row.team_2,
                "bookmaker": row.bookmaker,
                "bet_type": row.bet_type,
            }
            if register_game_from_odds(db, odd):
                created += 1
        db.commit()

    print(f"Processed {len(seen)} unique book games; registered {created} canonical links")


if __name__ == "__main__":
    main()
