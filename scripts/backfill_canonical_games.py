#!/usr/bin/env python3
"""Backfill canonical_games + game_book_links from recent arbitrage_odds rows."""

from datetime import datetime, timedelta

from sqlalchemy import func

from database.config import db1_session_scope
from database.models.ArbitrageOdds import ArbitrageOdds
from utils.game_registry import register_game_from_odds


def main(hours: int = 72) -> None:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    registered = 0

    with db1_session_scope() as db:
        keys = (
            db.query(
                ArbitrageOdds.bookmaker,
                ArbitrageOdds.game_id,
                func.max(ArbitrageOdds.id).label("latest_id"),
            )
            .filter(ArbitrageOdds.bet_type == "moneyline")
            .filter(ArbitrageOdds.created_at >= cutoff)
            .group_by(ArbitrageOdds.bookmaker, ArbitrageOdds.game_id)
            .all()
        )
        latest_ids = [row.latest_id for row in keys]
        rows = (
            db.query(ArbitrageOdds)
            .filter(ArbitrageOdds.id.in_(latest_ids))
            .all()
        )
        for row in rows:
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
                registered += 1
        db.commit()

    print(
        f"Processed {len(rows)} unique book games from last {hours}h; "
        f"registered {registered} canonical links"
    )


if __name__ == "__main__":
    main()
