from controllers.BetWarController import BetWarController
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import BETWAR, BETWAR_ACCOUNT, BETWAR_PASSWORD, BETWAR_LABEL

db = __get_db1_session__()


def main():
    if not BETWAR_ACCOUNT or not BETWAR_PASSWORD:
        raise ValueError("BETWAR_ACCOUNT and BETWAR_PASSWORD must be set in .env")

    account = Accounts(
        account=BETWAR_ACCOUNT,
        password=BETWAR_PASSWORD,
        label=BETWAR_LABEL,
    )

    sports = ["baseball"]
    controller = BetWarController(account, BETWAR, sport=sports[0])

    for i, sport in enumerate(sports):
        print(f"=== Fetching {sport.upper()} Moneyline ===")
        controller._set_sport(sport)
        controller.fetch_odds(quit_driver=(i == len(sports) - 1))

    print("\nFinished fetching MLB moneyline odds from BetWar")


if __name__ == "__main__":
    main()