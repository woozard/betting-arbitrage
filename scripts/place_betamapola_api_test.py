#!/usr/bin/env python3
"""Place a single Betamapola test bet using the API path."""
import argparse
import os
import sys
import time

if "--api-only" in sys.argv:
    os.environ["BETAMAPOLA_API_PLACEMENT"] = "true"

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from utils.betamapola_wager_api import (
    betamapola_process_ticket_via_api,
    parse_process_ticket_response,
)
from utils.config import BETAMAPOLA, BETAMAPOLA_ACCOUNT, BETAMAPOLA_PASSWORD, BETAMAPOLA_LABEL
from utils.stake_sizing import BaseAmountStake, base_amount_stake_from_odds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--team-name", default="Red Sox")
    parser.add_argument("--game-id", default="923-924")
    parser.add_argument("--stake", type=float, default=20.0)
    parser.add_argument(
        "--force-risk-entry",
        action="store_true",
        help="Enter stake as risk dollars (avoids Amount Exceeded on tight limits)",
    )
    args = parser.parse_args()

    account = Accounts(
        account=BETAMAPOLA_ACCOUNT,
        password=BETAMAPOLA_PASSWORD,
        label=BETAMAPOLA_LABEL,
    )
    c = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    c.API_PLACEMENT_ENABLED = True
    try:
        c._BetamapolaController__login()
        c._BetamapolaController__ensure_sport_offering_loaded()
        gl, team_no = c._lookup_game_line_from_api(
            args.game_id, args.team_name
        )
        if not gl:
            print("Game not found")
            return 1

        live_odd = str(gl.get(f"MoneyLine{team_no}"))
        c._return_to_sport_page()
        c.wait.until(EC.presence_of_element_located((By.ID, "betSlipDiv")))
        game_num = gl.get("GameNum")
        btn = c._wait_for_moneyline_button(game_num, team_no, timeout=25)
        if not btn:
            print("Moneyline button not in DOM")
            return 1

        c._prepare_bet_slip_for_wager()
        c.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        c.driver.execute_script("arguments[0].click();", btn)
        time.sleep(1.0)

        if args.force_risk_entry:
            stake_plan = BaseAmountStake(
                base_amount=args.stake,
                american_odds=int(float(live_odd)),
                entry_field="risk",
                entry_amount=args.stake,
                risk=args.stake,
                to_win=round(args.stake * 100 / abs(int(float(live_odd))), 2),
            )
        else:
            stake_plan = base_amount_stake_from_odds(live_odd, args.stake)

        print(f"Pick: {gl.get(f'Team{team_no}ID')} ML {live_odd} | {stake_plan}")

        if not c._fill_betamapola_stake(stake_plan):
            print("Stake fill failed")
            return 1
        c._accept_line_changes()

        diag = c.driver.execute_script("""
            var root = document.getElementById('betSlipDiv');
            var scope = angular.element(root).scope();
            while (scope && !(scope.ticketServiceView && scope.ticketServiceView.Ticket)) scope = scope.$parent;
            var item = (scope.ticketServiceView.Ticket.WagerItems || [])[0] || {};
            return {
                safe: scope.IsSafeToPostTicket ? scope.IsSafeToPostTicket() : null,
                err: item.ErrorMessage,
                isOk: item.IsOk,
                risk: item.RiskAmt,
                towin: item.ToWinAmt,
            };
        """)
        print("Pre-submit:", diag)
        if diag.get("err"):
            print(f"Cannot submit: {diag['err']}")
            if not args.force_risk_entry:
                print("Hint: retry with --force-risk-entry if limit is below computed risk")
            return 1

        posted, data, msg = betamapola_process_ticket_via_api(c.driver)
        print("ProcessTicket:", posted, msg)
        if data:
            ok, ticket, detail = parse_process_ticket_response(data)
            print("Parsed:", ok, ticket, detail)

        if not posted:
            return 1

        confirmed, message = c._confirm_bet_accepted_fast(
            gl.get(f"Team{team_no}ID"),
            gl.get("Team1ID"),
            gl.get("Team2ID"),
            process_data=data if isinstance(data, dict) else None,
        )
        print("Confirmed:", confirmed, message)
        return 0 if confirmed else 1
    finally:
        c._quit_driver()


if __name__ == "__main__":
    sys.exit(main())
