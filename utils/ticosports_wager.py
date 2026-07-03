"""TicoSports-family wager helpers (Betamapola, BetWar)."""


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
