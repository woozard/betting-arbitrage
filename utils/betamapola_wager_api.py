"""Fast Betamapola wager placement via browser-context HTTP (TicoSports Betting.asmx)."""

import json
import time


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


def betamapola_api_fetch(driver, path: str, body=None) -> dict | None:
    """POST JSON to a TicoSports ASMX endpoint inside the authenticated browser."""
    script = """
        const path = arguments[0];
        const body = arguments[1];
        const opts = {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
            },
            credentials: 'include',
        };
        if (body !== null && body !== undefined) {
            opts.body = JSON.stringify(body);
        }
        return fetch(path, opts)
            .then(async (resp) => {
                let data = null;
                try { data = await resp.json(); } catch (e) {}
                return { status: resp.status, ok: resp.ok, data };
            })
            .catch(err => ({ status: 0, ok: false, error: String(err) }));
    """
    try:
        return driver.execute_script(script, path, body)
    except Exception:
        return None


def betamapola_process_ticket_via_api(driver) -> tuple[bool, dict | None, str]:
    """
    Submit the current Angular ticket via ProcessTicket HTTP API.
    Falls back to invoking Angular ProcessTicket() when direct POST fails.
    """
    script = (
        _BETSLIP_SCOPE_JS
        + """
        var scope = betamapolaBetSlipScope();
        if (!scope || !scope.ticketServiceView || !scope.ticketServiceView.Ticket) {
            return { mode: 'error', message: 'bet slip ticket unavailable' };
        }
        if (scope.IsSafeToPostTicket && !scope.IsSafeToPostTicket()) {
            return { mode: 'error', message: 'IsSafeToPostTicket() is false' };
        }

        var ticket = scope.ticketServiceView.Ticket;
        var payloads = [
            { ticket: ticket },
            { Ticket: ticket },
        ];

        function postTicket(body) {
            return fetch('/sports/Api/Betting.asmx/ProcessTicket', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*',
                },
                credentials: 'include',
                body: JSON.stringify(body),
            }).then(function(resp) {
                return resp.json().then(function(data) {
                    return { ok: resp.ok, status: resp.status, data: data };
                });
            });
        }

        function tryPayloads(index) {
            if (index >= payloads.length) {
                scope.ProcessTicket();
                if (scope.$apply) scope.$apply();
                return { mode: 'angular', message: 'ProcessTicket invoked' };
            }
            return postTicket(payloads[index]).then(function(result) {
                var wrapper = result && result.data && result.data.d;
                if (wrapper && wrapper.IsSuccess) {
                    return { mode: 'fetch', payloadIndex: index, result: result };
                }
                if (wrapper && wrapper.Message) {
                    return { mode: 'error', message: wrapper.Message, code: wrapper.Code };
                }
                return tryPayloads(index + 1);
            }).catch(function(err) {
                if (index + 1 >= payloads.length) {
                    scope.ProcessTicket();
                    if (scope.$apply) scope.$apply();
                    return { mode: 'angular', message: String(err) };
                }
                return tryPayloads(index + 1);
            });
        }

        return tryPayloads(0);
        """
    )
    try:
        outcome = driver.execute_script(script)
    except Exception as exc:
        return False, None, str(exc)

    if not outcome:
        return False, None, "ProcessTicket script returned nothing"

    mode = outcome.get("mode")
    if mode == "error":
        return False, None, outcome.get("message") or "ProcessTicket rejected"

    if mode == "fetch":
        result = outcome.get("result") or {}
        data = result.get("data")
        ok, ticket_number, msg = parse_process_ticket_response(data)
        if ok:
            return True, data, msg
        return False, data, msg

    if mode == "angular":
        return True, None, outcome.get("message") or "ProcessTicket invoked via Angular"

    return False, None, json.dumps(outcome)[:200]
