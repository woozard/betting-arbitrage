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


class ArbitrageOdds(Base):
    __tablename__: str = 'arbitrage_odds'

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

    # Game info
    sport = Column('sport', String(50), nullable=False)
    league = Column('league', String(50), nullable=True)
    game_id = Column('game_id', String(50), nullable=False)
    game_datetime = Column('game_datetime', DateTime, nullable=False)

    team_1 = Column('team_1', String(100), nullable=False)
    team_2 = Column('team_2', String(100), nullable=False)

    bookmaker = Column('bookmaker', String(100), nullable=False)
    bet_type = Column(
        'bet_type',
        Enum('moneyline', 'spread', 'total'),
        nullable=False
    )

    # Moneyline odds
    moneyline_team_1 = Column('moneyline_team_1', DECIMAL(10, 2), nullable=True)
    moneyline_team_2 = Column('moneyline_team_2', DECIMAL(10, 2), nullable=True)
    moneyline_draw = Column('moneyline_draw', DECIMAL(10, 2), nullable=True)

    # Spread odds
    spread_team_1 = Column('spread_team_1', DECIMAL(10, 2), nullable=True)
    spread_team_2 = Column('spread_team_2', DECIMAL(10, 2), nullable=True)
    spread_value = Column('spread_value', DECIMAL(5, 2), nullable=True)

    # Total odds
    total_points = Column('total_points', DECIMAL(5, 2), nullable=True)
    over_odds = Column('over_odds', DECIMAL(10, 2), nullable=True)
    under_odds = Column('under_odds', DECIMAL(10, 2), nullable=True)

    def __init__(
        self,
        sport: object,
        league: object,
        game_id: object,
        game_datetime: object,
        team_1: object,
        team_2: object,
        bookmaker: object,
        bet_type: object,
        moneyline_team_1: object = None,
        moneyline_team_2: object = None,
        moneyline_draw: object = None,
        spread_team_1: object = None,
        spread_team_2: object = None,
        spread_value: object = None,
        total_points: object = None,
        over_odds: object = None,
        under_odds: object = None
    ):
        self.sport = sport
        self.league = league
        self.game_id = game_id
        self.game_datetime = game_datetime
        self.team_1 = team_1
        self.team_2 = team_2
        self.bookmaker = bookmaker
        self.bet_type = bet_type

        self.moneyline_team_1 = moneyline_team_1
        self.moneyline_team_2 = moneyline_team_2
        self.moneyline_draw = moneyline_draw

        self.spread_team_1 = spread_team_1
        self.spread_team_2 = spread_team_2
        self.spread_value = spread_value

        self.total_points = total_points
        self.over_odds = over_odds
        self.under_odds = under_odds


def __get_instance__():
    return Base
