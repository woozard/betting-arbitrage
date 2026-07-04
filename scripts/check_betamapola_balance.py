#!/usr/bin/env python3
from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from utils.config import BETAMAPOLA, BETAMAPOLA_ACCOUNT, BETAMAPOLA_PASSWORD, BETAMAPOLA_LABEL


def main():
    account = Accounts(
        account=BETAMAPOLA_ACCOUNT,
        password=BETAMAPOLA_PASSWORD,
        label=BETAMAPOLA_LABEL,
    )
    c = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    try:
        c._BetamapolaController__login()
        c._BetamapolaController__ensure_sport_offering_loaded()
        bal = c.driver.execute_script(
            """
            if (typeof angular === 'undefined') return null;
            var root = document.querySelector('app-sports') || document.body;
            var scope = angular.element(root).scope();
            while (scope) {
                if (scope.customerServiceView) {
                    var csv = scope.customerServiceView;
                    return {
                        AvailableBalance: csv.AvailableBalance,
                        CurrentBalance: csv.CurrentBalance,
                        CreditLimit: csv.CreditLimit,
                        FreePlayBalance: csv.FreePlayBalance,
                        PendingWagerBalance: csv.PendingWagerBalance,
                        CustomerID: csv.CustomerID,
                    };
                }
                scope = scope.$parent;
            }
            return null;
            """
        )
        print("balance", bal)
    finally:
        c._quit_driver()


if __name__ == "__main__":
    main()
