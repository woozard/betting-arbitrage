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

    # === FETCH BOTH NBA AND MLB MONEYLINE ===
    print("=== Fetching NBA Moneyline ===")
    controller_nba = Sports411Controller(account, SPORTS411, sport="basketball")
    controller_nba.fetch_odds()

    print("\n=== Fetching MLB Moneyline ===")
    controller_mlb = Sports411Controller(account, SPORTS411, sport="baseball")
    controller_mlb.fetch_odds()

    print("\n✅ Finished fetching NBA + MLB moneyline odds")

if __name__ == "__main__":
    main()