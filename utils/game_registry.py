"""Register and resolve canonical games across books."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from database.models.CanonicalGame import CanonicalGame
from database.models.GameBookLink import GameBookLink
from utils.team_registry import canonical_matchup_key, standard_team_name


def _parse_game_date(game_datetime) -> date:
    if isinstance(game_datetime, datetime):
        return game_datetime.date()
    if isinstance(game_datetime, date):
        return game_datetime
    text = str(game_datetime or "").strip()
    if len(text) >= 10:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    return datetime.utcnow().date()


def _parse_game_datetime(game_datetime) -> Optional[datetime]:
    if isinstance(game_datetime, datetime):
        return game_datetime
    text = str(game_datetime or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def build_matchup_key(
    sport: str,
    league: str,
    team_1: str,
    team_2: str,
    game_datetime,
) -> str:
    pair, date_key = canonical_matchup_key(
        team_1,
        team_2,
        game_datetime,
        sport=sport,
        league=league,
    )
    sport_l = (sport or "").strip().lower()
    league_l = (league or "").strip().lower()
    return f"{sport_l}|{league_l}|{pair[0]}|{pair[1]}|{date_key}"


def register_game_from_odds(db: Session, odd: dict, logger=None) -> Optional[int]:
    """Upsert canonical game + book link from a moneyline odds row."""
    bookmaker = (odd.get("bookmaker") or "").strip().lower()
    book_game_id = str(odd.get("game_id") or "").strip()
    sport = odd.get("sport") or "baseball"
    league = odd.get("league") or "mlb"
    team_1 = standard_team_name(odd.get("team_1") or "", sport=sport, league=league)
    team_2 = standard_team_name(odd.get("team_2") or "", sport=sport, league=league)
    game_datetime = _parse_game_datetime(odd.get("game_datetime"))

    if not bookmaker or not book_game_id or not team_1 or not team_2:
        return None

    pair, _date_key = canonical_matchup_key(
        team_1, team_2, game_datetime or odd.get("game_datetime"),
        sport=sport, league=league,
    )
    matchup_key = build_matchup_key(sport, league, team_1, team_2, game_datetime or odd.get("game_datetime"))
    game_date = _parse_game_date(game_datetime or odd.get("game_datetime"))

    canonical = db.query(CanonicalGame).filter_by(matchup_key=matchup_key).first()
    if canonical is None:
        canonical = CanonicalGame(
            sport=sport,
            league=league,
            game_date=game_date,
            team_1_canonical=pair[0],
            team_2_canonical=pair[1],
            game_datetime=game_datetime,
            matchup_key=matchup_key,
        )
        db.add(canonical)
        db.flush()
    elif game_datetime and (
        canonical.game_datetime is None or game_datetime < canonical.game_datetime
    ):
        canonical.game_datetime = game_datetime

    link = (
        db.query(GameBookLink)
        .filter_by(bookmaker=bookmaker, book_game_id=book_game_id)
        .first()
    )
    if link is None:
        link = GameBookLink(
            canonical_game_id=canonical.id,
            bookmaker=bookmaker,
            book_game_id=book_game_id,
            team_1=team_1,
            team_2=team_2,
        )
        db.add(link)
    else:
        link.canonical_game_id = canonical.id
        link.team_1 = team_1
        link.team_2 = team_2

    try:
        db.flush()
    except Exception as exc:
        if logger:
            logger.warning(f"Canonical game registry flush failed: {exc}")
        db.rollback()
        return None

    return canonical.id


def get_book_game_id(
    db: Session,
    *,
    bookmaker: str,
    canonical_game_id: int,
) -> Optional[str]:
    link = (
        db.query(GameBookLink)
        .filter_by(
            canonical_game_id=canonical_game_id,
            bookmaker=(bookmaker or "").strip().lower(),
        )
        .first()
    )
    return link.book_game_id if link else None


def get_canonical_game_id(
    db: Session,
    *,
    bookmaker: str,
    book_game_id: str,
) -> Optional[int]:
    link = (
        db.query(GameBookLink)
        .filter_by(
            bookmaker=(bookmaker or "").strip().lower(),
            book_game_id=str(book_game_id),
        )
        .first()
    )
    return link.canonical_game_id if link else None
