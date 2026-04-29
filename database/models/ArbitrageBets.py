from sqlalchemy import (
    Column,
    String,
    Integer,
    BigInteger,
    DateTime,
    Enum,
    DECIMAL,
    TIMESTAMP,
    func
)
from database.config import __get_base__

Base = __get_base__()


class ArbitrageBets(Base):
    __tablename__: str = 'arbitrage_bets'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Timestamps
    created_at = Column(
        'created_at',
        TIMESTAMP,
        nullable=False,
        server_default=func.now()
    )
    updated_at = Column(
        'updated_at',
        TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )

    # Game Info
    sport = Column('sport', String(50), nullable=False)
    league = Column('league', String(50), nullable=True)
    game_id = Column('game_id', String(50), nullable=False)
    game_datetime = Column('game_datetime', DateTime, nullable=False)

    # Teams
    team_1 = Column('team_1', String(100), nullable=False)
    team_2 = Column('team_2', String(100), nullable=False)

    # Bookmaker & Bet Type
    bookmaker = Column('bookmaker', String(100), nullable=False)
    bet_type = Column(
        'bet_type',
        Enum('moneyline', 'spread', 'total'),
        nullable=False
    )

    # Bet Info
    team_no = Column('team_no', Integer, nullable=False)  # 1, 2 (3 later if needed)
    team_name = Column('team_name', String(100), nullable=True)

    odds = Column('odds', DECIMAL(10, 2), nullable=False)
    stake = Column('stake', DECIMAL(10, 2), nullable=True)

    def __init__(
        self,
        sport: str,
        league: str,
        game_id: str,
        game_datetime,
        team_1: str,
        team_2: str,
        bookmaker: str,
        bet_type: str,
        team_no: int,
        team_name: str = None,
        odds=None,
        stake=None
    ):
        self.sport = sport
        self.league = league
        self.game_id = game_id
        self.game_datetime = game_datetime

        self.team_1 = team_1
        self.team_2 = team_2

        self.bookmaker = bookmaker
        self.bet_type = bet_type

        self.team_no = team_no
        self.team_name = team_name

        self.odds = odds
        self.stake = stake


def __get_instance__():
    return Base