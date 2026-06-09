import multiprocessing

from controllers.Sports411Controller import Sports411Controller
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import SPORTS411

db = __get_db1_session__()


def run_betting(sport: str):
    account = Accounts(
        account='8715',
        password='eqr0mjx-MXY*rcn1ana',
        label='Bettor'
    )
    controller = Sports411Controller(account, SPORTS411, sport=sport)
    controller.betting(stake=100)


def main():
    sports = ["baseball", "basketball"]
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