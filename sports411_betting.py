import multiprocessing
import os

from controllers.Sports411Controller import Sports411Controller
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import (
    SPORTS411,
    SPORTS411_ACCOUNT,
    SPORTS411_PASSWORD,
    SPORTS411_LABEL,
    BET_STAKE,
    ARB_SPORT,
)

db = __get_db1_session__()


def run_betting(sport: str):
    account_id = SPORTS411_ACCOUNT or os.getenv("SPORTS411_ACCOUNT")
    password = SPORTS411_PASSWORD or os.getenv("SPORTS411_PASSWORD")
    if not account_id or not password:
        raise ValueError("SPORTS411_ACCOUNT and SPORTS411_PASSWORD must be set")

    account = Accounts(
        account=account_id,
        password=password,
        label=SPORTS411_LABEL or "Bettor",
    )
    controller = Sports411Controller(
        account,
        SPORTS411,
        sport=sport,
    )
    controller.betting(stake=BET_STAKE)


def main():
    sports = [ARB_SPORT]
    processes = []

    for sport in sports:
        proc = multiprocessing.Process(
            target=run_betting,
            args=(sport,),
            name=f"sports411-betting-{sport}",
        )
        proc.start()
        processes.append(proc)

    for proc in processes:
        proc.join()


if __name__ == "__main__":
    main()
