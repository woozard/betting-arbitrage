from sqlalchemy import Column, String, Integer, BigInteger, Float, Date, TIMESTAMP
from database.config import __get_base__

Base = __get_base__()

class AccountBalance(Base):
    __tablename__ = 'account_balance'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)
    date = Column(Date, nullable=False)  # Laravel uses `date()`, so we use Date
    website = Column(String(255), nullable=False)
    account_id = Column(String(255), nullable=False)
    amount = Column(Float(precision=15), nullable=False, default=0)

def __get_instance__():
    return Base
