from sqlalchemy import Column, String, Integer, BigInteger, TIMESTAMP
from database.config import __get_base__

Base = __get_base__()

class Accounts(Base):
    __tablename__ = 'accounts'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url = Column(String(255), nullable=False)
    account = Column(String(255), nullable=False)
    password = Column(String(255), nullable=True)
    label = Column(String(255), nullable=True)
    skin = Column(String(255), nullable=True)
    telegram_chat_id = Column(String(255), nullable=True)
    active_for_telegram = Column(Integer, nullable=False, default=0)
    active_for_balance = Column(Integer, nullable=False, default=0)
    active_for_scrapper = Column(Integer, nullable=False, default=0)
    figures = Column(Integer, nullable=True)
    is_telegram_disabled = Column(Integer, nullable=False, default=1)
    is_scrapper_disabled = Column(Integer, nullable=False, default=1)
    created_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)
    
def __get_instance__():
    return Base
