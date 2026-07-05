#!/usr/bin/env python3
"""One-off: place Braves $11 to-win via ProcessTicket API (single submit)."""
import sys
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from utils.betamapola_wager_api import (
    betamapola_process_ticket_via_api,
    parse_process_ticket_response,
)
from utils.config import BETAMAPOLA, BETAMAPOLA_ACCOUNT, BETAMAPOLA_PASSWORD, BETAMAPOLA_LABEL
from utils.stake_sizing import base_amount_stake_from_odds


def main():
    account = Accounts(
        account=BETAMAPOLA_ACCOUNT,
        password=BETAMAPOLA_PASSWORD,
        label=BETAMAPOLA_LABEL,
    )
    controller = BetamapolaController(account, BETAMAPOLA, sport="baseball")
    try:
        controller._BetamapolaController__login()
        controller._BetamapolaController__ensure_sport_offering_loaded(fast=True)

        game_line, team_no = controller._lookup_game_line_from_api("905-906", "Braves")
        if not game_line:
            print("Braves game not found")
            return 1

        live_odd = str(game_line.get(f"MoneyLine{team_no}"))
        team_label = game_line.get(f"Team{team_no}ID")
        print(f"Pick: {team_label} ML {live_odd} | $11 to-win")

        if controller._has_existing_open_bet(
            team_label or "Braves",
            game_line.get("Team1ID") or "",
            game_line.get("Team2ID") or "",
        ):
            print("Open bet already exists — refusing duplicate placement")
            return 1

        controller._return_to_sport_page()
        controller.wait.until(EC.presence_of_element_located((By.ID, "betSlipDiv")))
        button = controller._wait_for_moneyline_button(
            game_line.get("GameNum"), team_no, timeout=25
        )
        if not button:
            print("Moneyline button not found")
            return 1

        controller._prepare_bet_slip_for_wager()
        controller.driver.execute_script("arguments[0].click();", button)
        time.sleep(0.8)

        stake_plan = base_amount_stake_from_odds(live_odd, 11.0)
        if not controller._fill_betamapola_stake(stake_plan):
            print("Stake entry failed")
            return 1
        controller._accept_line_changes()

        controller._install_wager_network_hook()
        posted, data, msg, submit_mode = betamapola_process_ticket_via_api(
            controller.driver, password=BETAMAPOLA_PASSWORD
        )
        if not posted:
            print("ProcessTicket failed:", msg)
            return 1
        ok, ticket_number, detail = parse_process_ticket_response(data)
        if not ok or not ticket_number:
            print("ProcessTicket rejected:", detail or msg)
            return 1

        print(f"SUCCESS ticket={ticket_number} submit_mode={submit_mode}")
        return 0
    finally:
        controller._quit_driver()


if __name__ == "__main__":
    sys.exit(main())
