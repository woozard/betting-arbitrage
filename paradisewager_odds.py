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

    print("=== Watching MLB moneyline (persistent session + player-api) ===")
    controller = ParadiseWagerController(account, PARADISEWAGER, sport="baseball")
    controller.watch_odds()
    print("\n✅ ParadiseWager odds watch ended")


if __name__ == "__main__":
    main()
