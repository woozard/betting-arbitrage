from sqlalchemy import (
    Column,
    String,
    BigInteger,
    DateTime,
    Date,
    Enum,
    DECIMAL,
    Boolean,
    SmallInteger,
    func,
    UniqueConstraint,
    Index
)
from database.config import __get_base__

Base = __get_base__()


class Arbitrage(Base):
    __tablename__ = 'arbitrage'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Record timestamp
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )

    # Game info
    sport = Column(String(100), nullable=True)
    league = Column(String(100), nullable=True)
    game_date = Column(Date, nullable=False)

    team_1 = Column(String(100), nullable=True)
    team_2 = Column(String(100), nullable=True)

    # Bet info
    bet_type = Column(
        Enum('moneyline', 'spread', 'total', name='bet_type_enum'),
        nullable=False
    )

    # Team 1 side
    team_1_bookmaker = Column(String(100), nullable=True)
    team_1_game_id = Column(String(50), nullable=True)
    team_1_odds = Column(DECIMAL(10, 2), nullable=True)
    team_1_bet_amount = Column(DECIMAL(10, 2), nullable=True)
    team_1_bet_placed_at = Column(DateTime, nullable=True)
    team_1_bet_placed = Column(Boolean, server_default='0', nullable=False)
    team_1_bet_placed_attempts = Column(SmallInteger, server_default='0', nullable=False)

    # Team 2 side
    team_2_bookmaker = Column(String(100), nullable=True)
    team_2_game_id = Column(String(50), nullable=True)
    team_2_odds = Column(DECIMAL(10, 2), nullable=True)
    team_2_bet_amount = Column(DECIMAL(10, 2), nullable=True)
    team_2_bet_placed_at = Column(DateTime, nullable=True)
    team_2_bet_placed = Column(Boolean, server_default='0', nullable=False)
    team_2_bet_placed_attempts = Column(SmallInteger, server_default='0', nullable=False)

    # Arbitrage math
    arb_total_prob = Column(DECIMAL(10, 4), nullable=True)
    profit_pct = Column(DECIMAL(10, 2), nullable=True)

    # UI / processing
    read = Column(Boolean, server_default='0', nullable=False)

    __table_args__ = (
        UniqueConstraint(
            'bet_type',
            'game_date',
            'team_1_bookmaker',
            'team_1_game_id',
            'team_1_odds',
            'team_2_bookmaker',
            'team_2_game_id',
            'team_2_odds',
            name='uniq_arb'
        ),
        Index('idx_game_date', 'game_date'),
        Index('idx_bet_type', 'bet_type'),
        Index('idx_read', 'read'),
    )

    def __repr__(self):
        return (
            f"<Arbitrage(id={self.id}, sport={self.sport}, "
            f"game_date={self.game_date}, bet_type={self.bet_type}, "
            f"arb_total_prob={self.arb_total_prob})>"
        )
