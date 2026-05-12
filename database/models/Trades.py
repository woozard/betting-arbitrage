from sqlalchemy import (
    Column,
    String,
    BigInteger,
    DateTime,
    DECIMAL,
    Integer,
    Enum,
    func,
    UniqueConstraint,
    Index
)
from database.config import __get_base__

Base = __get_base__()


class Trades(Base):
    __tablename__ = 'trades'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(DateTime, nullable=False, server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    # Trade details
    bookmaker = Column(String(100), nullable=False)
    label = Column(String(100), nullable=True)         # optional label, nullable
    trade_id = Column(String(100), nullable=False)       # transaction hash
    bet = Column(String(255), nullable=False)            # title of the bet
    side = Column(Enum('BUY', 'SELL', name='trade_side'), nullable=False)
    outcome = Column(String(255), nullable=False)
    price = Column(DECIMAL(5, 2), nullable=False)      # price in cents
    amount = Column(DECIMAL(18, 2), nullable=False)    # USDC amount
    

    __table_args__ = (
        UniqueConstraint('bookmaker', 'trade_id', name='unique_trade'),
        Index('idx_bookmaker', 'bookmaker'),
        Index('idx_trade_id', 'trade_id'),
        Index('idx_outcome', 'outcome'),
    )

    def __repr__(self):
        return (
            f"<Trades(id={self.id}, bookmaker={self.bookmaker}, trade_id={self.trade_id}, "
            f"bet={self.bet}, side={self.side}, outcome={self.outcome}, "
            f"price={self.price}, amount={self.amount}, label={self.label})>"
        )
