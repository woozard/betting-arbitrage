from controllers.LowVigController import LowVigController
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import (
    LOWVIG,
    LOWVIG_ACCOUNT,
    LOWVIG_PASSWORD,
    LOWVIG_LABEL,
    BET_STAKE,
)

db = __get_db1_session__()


def main():
    if not LOWVIG_ACCOUNT or not LOWVIG_PASSWORD:
        raise ValueError("LOWVIG_ACCOUNT and LOWVIG_PASSWORD must be set in .env")

    account = Accounts(
        account=LOWVIG_ACCOUNT,
        password=LOWVIG_PASSWORD,
        label=LOWVIG_LABEL,
    )

    controller = LowVigController(account, LOWVIG)
    controller.betting(stake=BET_STAKE)


if __name__ == "__main__":
    main()
