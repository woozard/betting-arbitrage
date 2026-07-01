"""Register and resolve canonical games across books."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

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
    from database.models.CanonicalGame import CanonicalGame
    from database.models.GameBookLink import GameBookLink

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
    from database.models.GameBookLink import GameBookLink

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
    from database.models.GameBookLink import GameBookLink

    link = (
        db.query(GameBookLink)
        .filter_by(
            bookmaker=(bookmaker or "").strip().lower(),
            book_game_id=str(book_game_id),
        )
        .first()
    )
    return link.canonical_game_id if link else None


def attach_canonical_game_ids(db: Session, rows: list[dict]) -> int:
    """Set canonical_game_id on odds rows using game_book_links (batch lookup)."""
    from sqlalchemy import and_, or_

    from database.models.GameBookLink import GameBookLink

    pairs = set()
    for row in rows:
        bookmaker = (row.get("bookmaker") or "").strip().lower()
        game_id = str(row.get("game_id") or "").strip()
        if bookmaker and game_id:
            pairs.add((bookmaker, game_id))
    if not pairs:
        return 0

    links = (
        db.query(GameBookLink)
        .filter(
            or_(
                *[
                    and_(
                        GameBookLink.bookmaker == bookmaker,
                        GameBookLink.book_game_id == game_id,
                    )
                    for bookmaker, game_id in pairs
                ]
            )
        )
        .all()
    )
    lookup = {
        (link.bookmaker, link.book_game_id): link.canonical_game_id for link in links
    }
    attached = 0
    for row in rows:
        bookmaker = (row.get("bookmaker") or "").strip().lower()
        game_id = str(row.get("game_id") or "").strip()
        canonical_game_id = lookup.get((bookmaker, game_id))
        if canonical_game_id:
            row["canonical_game_id"] = canonical_game_id
            attached += 1
    return attached


def odds_dedup_key(row: dict) -> tuple:
    """Latest-odds dedup key: one row per book per canonical game when linked."""
    bookmaker = row.get("bookmaker")
    bet_type = row.get("bet_type") or "moneyline"
    spread_value = row.get("spread_value")
    canonical_game_id = row.get("canonical_game_id")
    if canonical_game_id:
        if bet_type == "spread":
            return bookmaker, "cg", canonical_game_id, bet_type, spread_value
        return bookmaker, "cg", canonical_game_id, bet_type

    team_a, team_b = sorted(
        [normalize_team_slug(row.get("team_1") or ""), normalize_team_slug(row.get("team_2") or "")]
    )
    dt = row.get("game_datetime") or ""
    date_key = (dt[:10] if isinstance(dt, str) else str(dt)[:10]) if dt else ""
    if bet_type == "spread":
        return bookmaker, "mk", team_a, team_b, date_key, bet_type, spread_value
    return bookmaker, "mk", team_a, team_b, date_key, bet_type


def matchup_group_key(row: dict) -> tuple:
    """Group odds from different books that refer to the same game (and spread line)."""
    bet_type = row.get("bet_type") or "moneyline"
    spread_value = row.get("spread_value")
    canonical_game_id = row.get("canonical_game_id")
    if canonical_game_id:
        if bet_type == "spread":
            return ("cg", canonical_game_id, bet_type, spread_value)
        return ("cg", canonical_game_id, bet_type)

    sport = row.get("sport") or "baseball"
    league = row.get("league") or "mlb"
    matchup_key = build_matchup_key(
        sport,
        league,
        row.get("team_1") or "",
        row.get("team_2") or "",
        row.get("game_datetime"),
    )
    if bet_type == "spread":
        return ("mk", matchup_key, bet_type, spread_value)
    return ("mk", matchup_key, bet_type)


def normalize_team_slug(name: str) -> str:
    from utils.team_registry import canonical_team

    return canonical_team(name)
