from controllers.Sports411Controller import Sports411Controller
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import SPORTS411

db = __get_db1_session__()

def main():
    account = Accounts(
        account = '8715',
        password = 'eqr0mjx-MXY*rcn1ana',
        label = 'Reader'
    )

    controller = Sports411Controller(account, SPORTS411)
    controller.fetch_odds()

if __name__ == "__main__":
    main()
