from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import BETAMAPOLA

db = __get_db1_session__()

def main():

    account = Accounts(
        account = 'PC8396',
        password = 'SUN87',
        label = 'Reader-30K'
    )

    # === FETCH BOTH NBA AND MLB MONEYLINE ===
    print("=== Fetching NBA Moneyline ===")
    controller_nba = BetamapolaController(account, BETAMAPOLA, sport="basketball")
    controller_nba.fetch_odds()

    print("\n=== Fetching MLB Moneyline ===")
    controller_mlb = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    controller_mlb.fetch_odds()

    print("\n✅ Finished fetching NBA + MLB moneyline odds")

if __name__ == "__main__":
    main()
