from sqlalchemy import (
    Column,
    String,
    BigInteger,
    DateTime,
    DECIMAL,
    Boolean,
    SmallInteger,
    Integer,
    func,
    UniqueConstraint,
    Index
)
from database.config import __get_base__

Base = __get_base__()


class Odds2(Base):
    __tablename__ = 'odds2'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(DateTime, nullable=False, server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    # Match & market
    bookmaker = Column(String(100), nullable=False)
    matchup_id = Column(BigInteger, nullable=False)
    market_key = Column(String(50), nullable=False)          # e.g. s;0;m, s;0;s;-1.5
    market_type = Column(String(20), nullable=False)        # moneyline, spread, total
    period = Column(Integer, nullable=False, default=0)

    cutoff_at = Column(DateTime, nullable=False)
    is_alternate = Column(Boolean, default=False)
    status = Column(String(20), default='open')
    version = Column(BigInteger, nullable=True)

    # Selection
    side = Column(String(20), nullable=True)               # home, away, over, under
    team_name = Column(String(100), nullable=True)         # Gen.G, KT Rolster, etc.
    participant_id = Column(BigInteger, nullable=True)     # for multi-runner markets

    # Line & odds
    line = Column(DECIMAL(6, 2), nullable=True)         # spread / total points
    odds = Column(Integer, nullable=False)               # American odds

    # Limits
    limit_type = Column(String(50), nullable=True)        # maxRiskStake
    limit_amount = Column(DECIMAL(10, 2), nullable=True)

    __table_args__ = (
        UniqueConstraint('matchup_id', 'market_type', 'side', 'odds', name='uniq_matchup_market_side_price'),
        Index('idx_matchup', 'matchup_id'),
        Index('idx_market', 'matchup_id', 'market_type', 'period'),
        Index('idx_cutoff', 'cutoff_at'),
        Index('idx_team', 'team_name'),
        Index('idx_participant', 'participant_id'),
    )

    def __repr__(self):
        return (
            f"<Odds2(id={self.id}, matchup_id={self.matchup_id}, market_type={self.market_type}, "
            f"side={self.side}, team_name={self.team_name}, odds={self.odds}, points={self.points})>"
        )
