from sqlalchemy import or_, and_
from datetime import datetime, timedelta, timezone
from controllers.Sports411Controller import Sports411Controller
from database.models.Accounts import Accounts
from database.models.Arbitrage import Arbitrage
from database.config import __get_db1_session__
from utils.config import SPORTS411

db = __get_db1_session__()

def main():

    bookmaker = SPORTS411['bookmaker']
    account = Accounts(
        account = '8715',
        password = 'eqr0mjx-MXY*rcn1ana',
        label = 'Bettor'
    )

    controller = Sports411Controller(account, SPORTS411)
    controller.betting(stake=100)
    

if __name__ == "__main__":
    main()
