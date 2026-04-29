from sqlalchemy import or_, and_
from datetime import datetime, timedelta, timezone
from controllers.Web5Controller import Web5Controller
from database.models.Accounts import Accounts
from database.models.Arbitrage import Arbitrage
from database.config import __get_db1_session__
from utils.config import PROBET42

db = __get_db1_session__()

def main():

    bookmaker = PROBET42['bookmaker']
    account = Accounts(
        account = 'user2',
        password = '***********',
        label = 'Bettor'
    )

    web5 = Web5Controller(account, PROBET42)
    web5.betting(stake=100)

if __name__ == "__main__":
    main()
