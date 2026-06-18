from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import BETAMAPOLA, BETAMAPOLA_ACCOUNT, BETAMAPOLA_PASSWORD

db = __get_db1_session__()


def main():
    if not BETAMAPOLA_ACCOUNT or not BETAMAPOLA_PASSWORD:
        raise ValueError("BETAMAPOLA_ACCOUNT and BETAMAPOLA_PASSWORD must be set in .env")

    account = Accounts(
        account=BETAMAPOLA_ACCOUNT,
        password=BETAMAPOLA_PASSWORD,
        label='Reader-30K',
    )

    print("=== Fetching MLB Moneyline ===")
    controller = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    controller.fetch_odds()
    print("\n✅ Finished fetching MLB moneyline odds")


if __name__ == "__main__":
    main()
