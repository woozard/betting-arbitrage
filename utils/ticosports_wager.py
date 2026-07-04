"""TicoSports-family wager helpers (Betamapola, BetWar)."""

import json


def wager_network_entry_confirms(entry: dict) -> bool:
    """True when a hooked network response indicates a ticket was posted (not GetWagerPicks poll)."""
    url = (entry.get("url") or "").lower()
    body_l = (entry.get("body") or "").lower()
    if not url or not body_l:
        return False
    if any(
        skip in url
        for skip in (
            "getwagerpicks",
            "getsportoffering",
            "getlines",
            "getcustomer",
            "wagertypes.json",
        )
    ):
        return False
    if not any(
        token in url
        for token in ("processticket", "postticket", "saveticket", "placewager", "submit")
    ):
        return False
    if any(m in body_l for m in ("rejected", "declined", "error", "not accepted")):
        return False
    return any(
        m in body_l
        for m in (
            "ticketnumber",
            "confirmationnumber",
            "wagernumber",
            "reference",
            "accepted",
            "confirmed",
        )
    )


def pick_looks_like_open_wager(pick) -> bool:
    if not isinstance(pick, dict):
        return False
    for key in (
        "Team1ID", "Team2ID", "TeamName", "Description", "LineDescription",
        "team1id", "team2id", "description", "Selection", "selection",
    ):
        if pick.get(key):
            return True
    text = json.dumps(pick).lower()
    if "team" in text and any(m in text for m in ("amount", "risk", "towin", "odds")):
        return True
    return False


def betslip_text_confirms_wager(slip: str) -> bool:
    import re

    slip_l = (slip or "").lower()
    if any(
        marker in slip_l
        for marker in (
            "wager(s) confirmed",
            "wagers confirmed",
            "your selections are now active",
            "ticket accepted",
            "wager accepted",
        )
    ):
        return True
    return bool(re.search(r"reference\s*id\s*#?\s*\d+", slip_l, re.I))


def click_line_via_angular(driver, game_line: dict, team_no: int, line_type: str) -> bool:
    """Add a pick via Angular GameLineAction — line_type 'M' (ML) or 'S' (spread)."""
    try:
        result = driver.execute_script(
            """
                var gameNum = arguments[0];
                var rot1 = String(arguments[1]);
                var rot2 = String(arguments[2]);
                var teamNo = arguments[3];
                var lineType = arguments[4];

                function invoke(scope) {
                    if (!scope || !scope.GameLineAction) return false;
                    var lines = scope.sortedGameLines || scope.GameLines || [];
                    for (var i = 0; i < lines.length; i++) {
                        var gl = lines[i];
                        if (!gl || gl.IsTitle) continue;
                        if (gl.PeriodNumber !== 0 && gl.PeriodNumber !== '0') continue;
                        var match = String(gl.GameNum) === String(gameNum)
                            || (String(gl.Team1RotNum) === rot1 && String(gl.Team2RotNum) === rot2);
                        if (!match) continue;
                        scope.GameLineAction(gl, lineType, teamNo);
                        if (scope.$apply) scope.$apply();
                        return true;
                    }
                    return false;
                }

                var root = document.getElementById('GameLinesCtrl')
                    || document.querySelector('#gamesAccordion')
                    || document.querySelector('app-sports');
                if (!root || typeof angular === 'undefined') return false;

                var scope = angular.element(root).scope();
                if (invoke(scope)) return true;

                var child = scope && scope.$$childHead;
                while (child) {
                    if (invoke(child)) return true;
                    child = child.$$nextSibling;
                }
                return false;
            """,
            game_line.get("GameNum"),
            game_line.get("Team1RotNum"),
            game_line.get("Team2RotNum"),
            team_no,
            line_type,
        )
        return bool(result)
    except Exception:
        return False


def _betamapola_scope_fn_js() -> str:
    return """
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


def betamapola_wager_item_count(driver) -> int:
    try:
        count = driver.execute_script(
            _betamapola_scope_fn_js()
            + """
            var scope = betamapolaBetSlipScope();
            if (!scope || !scope.ticketServiceView || !scope.ticketServiceView.Ticket) return 0;
            var ticket = scope.ticketServiceView.Ticket;
            return (ticket.WagerItems && ticket.WagerItems.length) || 0;
            """
        )
        return int(count or 0)
    except Exception:
        return 0


def betamapola_betslip_is_empty(slip_text: str, wager_count: int | None = None) -> bool:
    slip_l = (slip_text or "").lower()
    pick_markers = ("spread", "money line", "for game", "total")
    has_pick_text = any(m in slip_l for m in pick_markers) and "empty" not in slip_l

    if wager_count is not None:
        if wager_count > 0:
            return False
        if wager_count == 0 and has_pick_text:
            return False
        return True
    if not slip_l:
        return True
    empty_markers = (
        "bet slip is empty",
        "your bet slip is empty",
        "please make one or more selections",
        "0 selections",
    )
    return any(marker in slip_l for marker in empty_markers)


def invoke_betamapola_process_ticket(driver) -> str | None:
    """Submit the bet slip via Angular ProcessTicket(). Returns method used or None."""
    try:
        return driver.execute_script(
            _betamapola_scope_fn_js()
            + """
            var scope = betamapolaBetSlipScope();
            if (!scope) return null;
            if (scope.IsSafeToPostTicket && !scope.IsSafeToPostTicket()) return 'unsafe';
            scope.ProcessTicket();
            if (scope.$apply) scope.$apply();
            return 'ProcessTicket';
            """
        )
    except Exception:
        return None


def sync_betamapola_stake_models(driver, risk: float, to_win: float, entry_field: str) -> bool:
    """Push stake amounts into Angular ticket models when DOM inputs miss ng-model updates."""
    try:
        return bool(
            driver.execute_script(
                _betamapola_scope_fn_js()
                + """
                var scope = betamapolaBetSlipScope();
                if (!scope) return false;
                var ticket = scope.ticketServiceView.Ticket;
                var items = ticket.WagerItems || [];
                if (!items.length) return false;
                var item = items[0];
                var entryField = arguments[2];
                var risk = Number(arguments[0]);
                var toWin = Number(arguments[1]);
                if (entryField === 'to_win') {
                    item.ToWinAmount = toWin;
                    item.WinAmount = toWin;
                    item.ToWin = toWin;
                    item.AmountEntered = toWin;
                    if (typeof calculateBStxtRisk === 'function') calculateBStxtRisk();
                } else {
                    item.RiskAmount = risk;
                    item.Risk = risk;
                    item.AmountEntered = risk;
                    if (typeof calculateBStxtWin === 'function') calculateBStxtWin();
                }
                if (scope.$apply) scope.$apply();
                return !!(scope.IsSafeToPostTicket && scope.IsSafeToPostTicket());
                """,
                float(risk),
                float(to_win),
                entry_field,
            )
        )
    except Exception:
        return False
