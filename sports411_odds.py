from controllers.Sports411Controller import Sports411Controller
from database.models.Accounts import Accounts
from database.config import __get_db1_session__
from utils.config import SPORTS411

db = __get_db1_session__()


def main():
    account = Accounts(
        account='8715',
        password='eqr0mjx-MXY*rcn1ana',
        label='Reader',
    )

    # One browser session for all sports — login once per odds cycle, not per sport.
    sports = ["baseball", "basketball"]
    controller = Sports411Controller(account, SPORTS411, sport=sports[0])

    for i, sport in enumerate(sports):
        print(f"=== Fetching {sport.upper()} Moneyline ===")
        controller._set_sport(sport)
        controller.fetch_odds(quit_driver=(i == len(sports) - 1))

    print("\n✅ Finished fetching NBA + MLB moneyline odds")


if __name__ == "__main__":
    main()