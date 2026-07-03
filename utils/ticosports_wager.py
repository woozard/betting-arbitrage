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
