from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from utils.config import DB1, DB2

Base = declarative_base()

def __get_base__():
    return Base

# These imports are REQUIRED for SQLAlchemy model registration (side-effect)
# Do NOT remove even if IDE shows them as unused/greyed-out
# (Importing after Base ensures all model classes register on the shared metadata
#  so create_all will create every table, including 'arbitrage'.)
from database.models.AccountBalance import AccountBalance
from database.models.Accounts import Accounts
from database.models.Arbitrage import Arbitrage
from database.models.ArbitrageBets import ArbitrageBets
from database.models.ArbitrageOdds import ArbitrageOdds
from database.models.DailyFigures import DailyFigures
from database.models.Odds2 import Odds2
from database.models.Trades import Trades

# ---------- DB1 ----------
DB1_URL = (
    f"mysql+mysqlconnector://{DB1['username']}:{DB1['password']}"
    f"@{DB1['host']}:{DB1['port']}/{DB1['database']}"
)

engine1 = create_engine(DB1_URL)
Session1 = sessionmaker(bind=engine1)
session1 = Session1()

Base.metadata.create_all(bind=engine1)

def __get_db1_session__():
    return session1

# ---------- DB2 ----------
# DB2_URL = (
#     f"mysql+mysqlconnector://{DB2['username']}:{DB2['password']}"
#     f"@{DB2['host']}:{DB2['port']}/{DB2['database']}"
# )

# engine2 = create_engine(DB2_URL)
# Session2 = sessionmaker(bind=engine2)
# session2 = Session2()

# Base.metadata.create_all(bind=engine2)

# def __get_db2_session__():
#     return session2
