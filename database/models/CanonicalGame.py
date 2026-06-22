from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    String,
    TIMESTAMP,
    UniqueConstraint,
    func,
)

from database.config import __get_base__

Base = __get_base__()


class CanonicalGame(Base):
    """One logical sporting event shared across books."""

    __tablename__ = "canonical_games"
    __table_args__ = (
        UniqueConstraint("matchup_key", name="uq_canonical_games_matchup_key"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    sport = Column(String(50), nullable=False)
    league = Column(String(50), nullable=True)
    game_date = Column(Date, nullable=False)
    team_1_canonical = Column(String(80), nullable=False)
    team_2_canonical = Column(String(80), nullable=False)
    game_datetime = Column(DateTime, nullable=True)
    matchup_key = Column(String(220), nullable=False)

    def __init__(
        self,
        sport,
        league,
        game_date,
        team_1_canonical,
        team_2_canonical,
        game_datetime=None,
        matchup_key=None,
    ):
        self.sport = sport
        self.league = league
        self.game_date = game_date
        self.team_1_canonical = team_1_canonical
        self.team_2_canonical = team_2_canonical
        self.game_datetime = game_datetime
        self.matchup_key = matchup_key


def __get_instance__():
    return Base
