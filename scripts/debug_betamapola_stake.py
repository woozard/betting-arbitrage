#!/usr/bin/env python3
import time
from cache.arbitrage_cache import ArbitrageCache  # noqa: F401 — warms import path
from controllers.BetamapolaController import BetamapolaController
from database.models.Accounts import Accounts
from utils.config import BETAMAPOLA, BETAMAPOLA_ACCOUNT, BETAMAPOLA_PASSWORD, BETAMAPOLA_LABEL
from utils.betamapola_wager_api import click_line_via_dom_button
from utils.stake_sizing import base_amount_stake_from_odds


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
        gl, team_no = c._lookup_game_line_from_api("923-924", "Boston Red Sox")
        game_num = gl.get("GameNum")
        print("game", game_num, "team_no", team_no)
        c._return_to_sport_page()
        btn = c._wait_for_moneyline_button(game_num, team_no, timeout=25)
        print("button found", bool(btn), btn.get_attribute("id") if btn else None)
        c.wait.until(lambda d: d.find_element("id", "betSlipDiv"))
        c._prepare_bet_slip_for_wager()
        if btn:
            c.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.4)
            c.driver.execute_script("arguments[0].click();", btn)
        time.sleep(1.5)
        import json
        print("before", json.dumps(c.driver.execute_script(_DIAG_JS), default=str)[:2000])
        stake = base_amount_stake_from_odds("-157", 20)
        ok = c._fill_betamapola_stake(stake)
        print("fill_ok", ok)
        time.sleep(0.5)
        c._accept_line_changes()
        time.sleep(0.3)
        print("after", json.dumps(c.driver.execute_script(_DIAG_JS), default=str)[:2000])
    finally:
        c._quit_driver()


_DIAG_JS = """
return (function(){
    var inputs = Array.from(document.querySelectorAll("#betSlipBody input, #betSlipDiv input"));
    var root = document.getElementById("betSlipDiv");
    var scope = root && typeof angular !== 'undefined' ? angular.element(root).scope() : null;
    while (scope && !(scope.ticketServiceView && scope.ticketServiceView.Ticket)) scope = scope.$parent;
    var items = scope && scope.ticketServiceView ? (scope.ticketServiceView.Ticket.WagerItems || []) : [];
    return {
        inputs: inputs.map(function(el){
            return {id: el.id, ng: el.getAttribute("ng-model"), val: el.value, shown: !!(el.offsetParent)};
        }),
        count: items.length,
        safe: scope && scope.IsSafeToPostTicket ? scope.IsSafeToPostTicket() : null,
        item0: items[0] || null,
        slip: (document.getElementById("betSlipBody")||{}).innerText || ""
    };
})();
"""


if __name__ == "__main__":
    main()
