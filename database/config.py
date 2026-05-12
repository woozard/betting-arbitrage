from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from utils.config import DB1, DB2

Base = declarative_base()

def __get_base__():
    return Base

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
