"""Fast Betamapola wager placement via browser-context HTTP (TicoSports Betting.asmx)."""

import json
import time

PROCESS_TICKET_PATH = "/sports/Api/Betting.asmx/ProcessTicket"


def parse_process_ticket_response(result: dict | None) -> tuple[bool, int | None, str]:
    """Parse ProcessTicket ASMX JSON response."""
    if not result:
        return False, None, "empty ProcessTicket response"

    wrapper = result.get("d") if isinstance(result, dict) else None
    if not isinstance(wrapper, dict):
        return False, None, "invalid ProcessTicket response shape"

    if not wrapper.get("IsSuccess"):
        msg = (wrapper.get("Message") or "").strip()
        code = wrapper.get("Code")
        detail = msg or f"Code={code}"
        return False, None, detail

    data = wrapper.get("Data") or {}
    ticket_number = data.get("TicketNumber")
    try:
        ticket_int = int(ticket_number) if ticket_number is not None else None
    except (TypeError, ValueError):
        ticket_int = None

    if ticket_int and ticket_int > 0:
        return True, ticket_int, f"TicketNumber={ticket_int}"

    return True, ticket_int, "ProcessTicket IsSuccess"


def wager_network_body_confirms(body: str) -> bool:
    from utils.ticosports_wager import wager_network_entry_confirms

    return wager_network_entry_confirms({"url": "ProcessTicket", "body": body or ""})


_BETSLIP_SCOPE_JS = """
function betamapolaBetSlipScope() {
    var root = document.getElementById('betSlipDiv');
    if (!root || typeof angular === 'undefined') return null;
    var scope = angular.element(root).scope();
    while (scope) {
        if (scope.ticketServiceView && scope.ticketServiceView.Ticket) return scope;
        if (typeof scope.ProcessTicket === 'function') return scope;
        scope = scope.$parent;
    }
    return null;
}
"""

_BUILD_PROCESS_TICKET_BODY_JS = (
    _BETSLIP_SCOPE_JS
    + """
    function betamapolaBuildProcessTicketBody() {
        var scope = betamapolaBetSlipScope();
        if (!scope || !scope.ticketServiceView || !scope.ticketServiceView.Ticket) return null;
        var ticket = scope.ticketServiceView.Ticket;
        var items = ticket.WagerItems || [];
        if (!items.length) return null;
        if (typeof scope.AcceptAllLineChanges === 'function') {
            scope.AcceptAllLineChanges();
        } else if (typeof scope.AcceptLineChanges === 'function') {
            scope.AcceptLineChanges();
        }
        for (var j = 0; j < items.length; j++) {
            items[j].AcceptChangesFlag = true;
            items[j].LineChanged = false;
            items[j].Changed = false;
        }
        if (scope.$apply) scope.$apply();
        var wagersData = [];
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            try {
                wagersData.push(JSON.parse(JSON.stringify(item)));
            } catch (e) {
                wagersData.push({
                    AmountEntered: item.AmountEntered != null ? item.AmountEntered : item.ToWinAmount,
                    ArAmount: item.ArAmount,
                    ControlCode: item.ControlCode,
                    FinalLine: item.FinalLine,
                    FinalPrice: item.FinalPrice,
                    GameNum: item.GameNum,
                    PeriodNumber: item.PeriodNumber,
                    PlayCount: item.PlayCount || 0,
                    RifTicketNumber: item.RifTicketNumber || 0,
                    RifWagerNumber: item.RifWagerNumber || 0,
                    RifWinOnlyFlag: item.RifWinOnlyFlag || false,
                    RiskAmount: item.RiskAmount,
                    RoundRobinValue: item.RoundRobinValue || 0,
                    RrAmount: item.RrAmount || 0,
                    ToWinAmount: item.ToWinAmount,
                    WagerAmt: item.RiskAmount,
                    pitcher1ReqFlag: item.pitcher1ReqFlag || false,
                    pitcher2ReqFlag: item.pitcher2ReqFlag || false,
                    AcceptChangesFlag: true,
                    LineChanged: false,
                    Changed: false,
                });
            }
        }
        return {
            wGBS: ticket.wGBS,
            password: ticket.Password || '',
            useFreePlay: !!ticket.UseFreePlay,
            wagersData: wagersData,
            openPlayTotalPicks: ticket.OpenPlayTotalPicks || 0,
            teaserName: ticket.TeaserName || '',
            arbc: !!ticket.ARBC,
            wagerTypeId: 0,
        };
    }
    """
)


def wait_for_angular_game_lines(driver, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if angular_has_game_line_action(driver):
            return True
        time.sleep(0.25)
    return False


def betamapola_is_safe_to_post(driver) -> bool:
    try:
        return bool(
            driver.execute_script(
                _BETSLIP_SCOPE_JS
                + """
                var scope = betamapolaBetSlipScope();
                if (!scope || !scope.ticketServiceView || !scope.ticketServiceView.Ticket) return false;
                var items = scope.ticketServiceView.Ticket.WagerItems || [];
                if (!items.length) return false;
                var item = items[0];
                var hasRisk = Number(item.RiskAmount || item.Risk || item.AmountEntered || 0) > 0;
                var hasWin = Number(item.ToWinAmount || item.WinAmount || item.ToWin || 0) > 0;
                if (!hasRisk && !hasWin) return false;
                return !(scope.IsSafeToPostTicket) || scope.IsSafeToPostTicket();
                """
            )
        )
    except Exception:
        return False


def ensure_betamapola_stake_ready(
    driver,
    risk: float,
    to_win: float,
    entry_field: str,
    timeout: float = 6.0,
) -> bool:
    from utils.ticosports_wager import sync_betamapola_stake_models

    deadline = time.time() + timeout
    while time.time() < deadline:
        sync_betamapola_stake_models(driver, risk, to_win, entry_field)
        if betamapola_is_safe_to_post(driver):
            return True
        time.sleep(0.2)
    return False


def click_line_via_dom_button(driver, game_num, team_no: int, line_type: str) -> bool:
    """Fallback: click M/S button by TicoSports id when Angular scope is unavailable."""
    prefix = "M" if line_type == "M" else "S"
    selector = f"button#{prefix}{team_no}_{game_num}_0, #{prefix}{team_no}_{game_num}_0"
    try:
        return bool(
            driver.execute_script(
                """
                var sel = arguments[0];
                var el = document.querySelector(sel);
                if (!el) return false;
                el.scrollIntoView({block: 'center'});
                el.click();
                return true;
                """,
                selector,
            )
        )
    except Exception:
        return False


def angular_has_game_line_action(driver) -> bool:
    try:
        return bool(
            driver.execute_script(
                """
                if (typeof angular === 'undefined') return false;
                function hasAction(scope) {
                    return scope && typeof scope.GameLineAction === 'function'
                        && (scope.sortedGameLines || scope.GameLines);
                }
                var root = document.getElementById('GameLinesCtrl')
                    || document.querySelector('#gamesAccordion')
                    || document.querySelector('app-sports');
                if (!root) return false;
                var scope = angular.element(root).scope();
                if (hasAction(scope)) return true;
                var child = scope && scope.$$childHead;
                while (child) {
                    if (hasAction(child)) return true;
                    child = child.$$nextSibling;
                }
                return false;
                """
            )
        )
    except Exception:
        return False


def wait_for_betamapola_wager_items(driver, min_count: int = 1, timeout: float = 2.5) -> int:
    from utils.ticosports_wager import betamapola_wager_item_count

    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        count = betamapola_wager_item_count(driver)
        if count >= min_count:
            return count
        time.sleep(0.08)
    return count


def accept_betamapola_line_changes(driver) -> bool:
    """Accept odds/line change prompts via Angular scope or bet-slip checkboxes."""
    try:
        return bool(
            driver.execute_script(
                _BETSLIP_SCOPE_JS
                + """
                var scope = betamapolaBetSlipScope();
                if (scope) {
                    if (typeof scope.AcceptAllLineChanges === 'function') {
                        scope.AcceptAllLineChanges();
                        if (scope.$apply) scope.$apply();
                        return true;
                    }
                    if (typeof scope.AcceptLineChanges === 'function') {
                        scope.AcceptLineChanges();
                        if (scope.$apply) scope.$apply();
                        return true;
                    }
                    var ticket = scope.ticketServiceView && scope.ticketServiceView.Ticket;
                    var items = ticket && ticket.WagerItems;
                    if (items && items.length) {
                        for (var i = 0; i < items.length; i++) {
                            items[i].AcceptChangesFlag = true;
                            items[i].LineChanged = false;
                            items[i].Changed = false;
                        }
                        if (scope.$apply) scope.$apply();
                        return true;
                    }
                }
                var boxes = document.querySelectorAll(
                    "#betSlipDiv input[type='checkbox'], #betSlipBody input[type='checkbox']"
                );
                var clicked = false;
                for (var j = 0; j < boxes.length; j++) {
                    var cb = boxes[j];
                    var nearby = (cb.parentElement && cb.parentElement.textContent || '').toLowerCase();
                    if (nearby.indexOf('accept') >= 0 && !cb.checked) {
                        cb.click();
                        clicked = true;
                    }
                }
                return clicked;
                """
            )
        )
    except Exception:
        return False


_PROCESS_TICKET_CAPTURE_JS = (
    _BETSLIP_SCOPE_JS
    + """
    function betamapolaCaptureProcessTicket(cb) {
        var captured = null;
        var origFetch = window.fetch;
        window.fetch = function(url, opts) {
            return origFetch.apply(this, arguments).then(function(resp) {
                var u = String(url || '').toLowerCase();
                if (u.indexOf('processticket') >= 0) {
                    return resp.clone().json().then(function(data) {
                        captured = data;
                        return resp;
                    }).catch(function() { return resp; });
                }
                return resp;
            });
        };
        var origOpen = XMLHttpRequest.prototype.open;
        var origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url) {
            this.__ptUrl = url;
            return origOpen.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function() {
            var xhr = this;
            xhr.addEventListener('load', function() {
                var u = String(xhr.__ptUrl || '').toLowerCase();
                if (u.indexOf('processticket') >= 0 && xhr.responseText) {
                    try { captured = JSON.parse(xhr.responseText); } catch (e) {}
                }
            });
            return origSend.apply(this, arguments);
        };

        var scope = betamapolaBetSlipScope();
        if (!scope) {
            cb({ ok: false, message: 'bet slip scope unavailable' });
            return;
        }
        if (scope.IsSafeToPostTicket && !scope.IsSafeToPostTicket()) {
            cb({ ok: false, message: 'IsSafeToPostTicket() is false' });
            return;
        }
        if (typeof scope.AcceptAllLineChanges === 'function') scope.AcceptAllLineChanges();
        else if (typeof scope.AcceptLineChanges === 'function') scope.AcceptLineChanges();
        if (typeof scope.ProcessTicket === 'function') {
            scope.ProcessTicket();
            if (scope.$apply) scope.$apply();
        } else {
            cb({ ok: false, message: 'ProcessTicket() unavailable' });
            return;
        }
        setTimeout(function() {
            cb({ ok: !!captured, data: captured, mode: 'process_ticket_api' });
        }, 3500);
    }
    """
)


def betamapola_process_ticket_via_api(driver, password: str = "") -> tuple[bool, dict | None, str, str]:
    """
    Submit exactly one ProcessTicket call (browser session HTTP to Betting.asmx).
    Uses Angular ProcessTicket() so the site builds the correct payload; no Place Bet
    DOM click and no second submit fallback.
    Returns (posted, response_data, message, submit_mode).
    """
    del password  # session cookie auth; password is embedded in ticket when required
    accept_betamapola_line_changes(driver)
    script = _PROCESS_TICKET_CAPTURE_JS + """
        var cb = arguments[arguments.length - 1];
        betamapolaCaptureProcessTicket(cb);
    """
    try:
        outcome = driver.execute_async_script(script)
    except Exception as exc:
        return False, None, str(exc), "error"

    if not outcome or not outcome.get("ok"):
        return False, None, (outcome or {}).get("message") or "ProcessTicket failed", "error"

    data = outcome.get("data")
    ok, ticket_number, msg = parse_process_ticket_response(data)
    if ok and ticket_number:
        return True, data, msg, "process_ticket_api"

    wrapper = (data or {}).get("d") if isinstance(data, dict) else None
    detail = msg or "ProcessTicket rejected"
    if isinstance(wrapper, dict):
        detail = (wrapper.get("Message") or detail).strip()

    return False, data, detail, "process_ticket_api"
