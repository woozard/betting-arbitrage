from controllers.FourCastersController import FourCastersController
from database.models.Accounts import Accounts
from utils.config import (
    FOURCASTERS,
    FOURCASTERS_ACCOUNT,
    FOURCASTERS_PASSWORD,
    FOURCASTERS_LABEL,
)


def main():
    if not FOURCASTERS_ACCOUNT or not FOURCASTERS_PASSWORD:
        raise ValueError("FOURCASTERS_ACCOUNT and FOURCASTERS_PASSWORD must be set in .env")

    account = Accounts(
        account=FOURCASTERS_ACCOUNT,
        password=FOURCASTERS_PASSWORD,
        label=FOURCASTERS_LABEL,
    )
    controller = FourCastersController(account, FOURCASTERS, sport="baseball")
    controller.watch_odds()


if __name__ == "__main__":
    main()
