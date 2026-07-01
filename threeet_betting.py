from controllers.ThreeEtController import ThreeEtController
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import (
    THREEET,
    THREEET_ACCOUNT,
    THREEET_PASSWORD,
    THREEET_LABEL,
    BET_STAKE,
)

db = __get_db1_session__()


def main():
    if not THREEET_ACCOUNT or not THREEET_PASSWORD:
        raise ValueError("THREEET_ACCOUNT and THREEET_PASSWORD must be set in .env")

    account = Accounts(
        account=THREEET_ACCOUNT,
        password=THREEET_PASSWORD,
        label=THREEET_LABEL,
    )

    controller = ThreeEtController(account, THREEET, sport="baseball")
    controller.betting(stake=BET_STAKE)


if __name__ == "__main__":
    main()
