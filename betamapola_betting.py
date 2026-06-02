from sqlalchemy import or_, and_
from datetime import datetime, timedelta, timezone
from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from database.models.Arbitrage import Arbitrage
from database.config import __get_db1_session__
from utils.config import BETAMAPOLA

db = __get_db1_session__()

def main():

    bookmaker = BETAMAPOLA['bookmaker']
    account = Accounts(
        account = 'PC8396',
        password = 'SUN87',
        label = 'Bettor'
    )

    controller = BetamapolaController(account, BETAMAPOLA)
    controller.betting(stake=100)
    

if __name__ == "__main__":
    main()
