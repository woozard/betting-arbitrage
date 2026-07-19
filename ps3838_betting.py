import os
import time

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
from utils.logger import Logger
from utils.ps3838_client import Ps3838ApiError, Ps3838Client

db = __get_db1_session__()


def main():
    if not PS3838_ACCOUNT or not PS3838_PASSWORD:
        raise ValueError("PS3838_ACCOUNT and PS3838_PASSWORD must be set in .env / stack env")

    logger = Logger.get_logger("ps3838-betting")
    # Fail fast on Cloudflare / geo blocks so we don't thrash the scheduler every 30s.
    try:
        Ps3838Client(PS3838_ACCOUNT, PS3838_PASSWORD).get_balance()
    except Ps3838ApiError as exc:
        msg = str(exc)
        if "Cloudflare" in msg or "403" in msg:
            cooldown = int(os.getenv("PS3838_CF_COOLDOWN_SEC", "600"))
            logger.error(f"{msg} — sleeping {cooldown}s before exit")
            time.sleep(cooldown)
            return
        raise

    account = Accounts(
        account=PS3838_ACCOUNT,
        password=PS3838_PASSWORD,
        label=PS3838_LABEL,
    )

    controller = Ps3838Controller(account, PS3838, sport=ARB_SPORT)
    controller.betting(stake=BET_STAKE)


if __name__ == "__main__":
    main()
