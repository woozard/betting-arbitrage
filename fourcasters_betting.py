from controllers.FourCastersController import FourCastersController
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import (
    FOURCASTERS,
    FOURCASTERS_ACCOUNT,
    FOURCASTERS_PASSWORD,
    FOURCASTERS_LABEL,
    BET_STAKE,
    ARB_SPORT,
)

db = __get_db1_session__()


def main():
    if not FOURCASTERS_ACCOUNT or not FOURCASTERS_PASSWORD:
        raise ValueError("FOURCASTERS_ACCOUNT and FOURCASTERS_PASSWORD must be set in .env")

    account = Accounts(
        account=FOURCASTERS_ACCOUNT,
        password=FOURCASTERS_PASSWORD,
        label=FOURCASTERS_LABEL,
    )

    controller = FourCastersController(account, FOURCASTERS, sport=ARB_SPORT)
    controller.betting(stake=BET_STAKE)


if __name__ == "__main__":
    main()
