from controllers.ParadiseWagerController import ParadiseWagerController
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import (
    PARADISEWAGER,
    PARADIESWAGER_ACCOUNT,
    PARADIESWAGER_PASSWORD,
    PARADIESWAGER_LABEL,
)

db = __get_db1_session__()


def main():
    if not PARADIESWAGER_ACCOUNT or not PARADIESWAGER_PASSWORD:
        raise ValueError("PARADIESWAGER_ACCOUNT and PARADIESWAGER_PASSWORD must be set in .env")

    account = Accounts(
        account=PARADIESWAGER_ACCOUNT,
        password=PARADIESWAGER_PASSWORD,
        label=PARADIESWAGER_LABEL,
    )

    sports = ["baseball", "basketball"]
    controller = ParadiseWagerController(account, PARADISEWAGER, sport=sports[0])

    for i, sport in enumerate(sports):
        print(f"=== Fetching {sport.upper()} Moneyline ===")
        controller._set_sport(sport)
        controller.fetch_odds(quit_driver=(i == len(sports) - 1))

    print("\nFinished fetching NBA + MLB moneyline odds from ParadiseWager")


if __name__ == "__main__":
    main()