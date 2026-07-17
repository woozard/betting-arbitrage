from controllers.Ps3838Controller import Ps3838Controller
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import (
    PS3838,
    PS3838_ACCOUNT,
    PS3838_PASSWORD,
    PS3838_LABEL,
    BET_STAKE,
    ARB_SPORT,
)

db = __get_db1_session__()


def main():
    if not PS3838_ACCOUNT or not PS3838_PASSWORD:
        raise ValueError("PS3838_ACCOUNT and PS3838_PASSWORD must be set in .env / stack env")

    account = Accounts(
        account=PS3838_ACCOUNT,
        password=PS3838_PASSWORD,
        label=PS3838_LABEL,
    )

    controller = Ps3838Controller(account, PS3838, sport=ARB_SPORT)
    controller.betting(stake=BET_STAKE)


if __name__ == "__main__":
    main()
