"""Betamapola GetSportOffering API response helpers."""


def parse_get_sport_offering_response(result) -> tuple[list, bool]:
    """Parse GetSportOffering JSON. Returns (game_lines, payload_ok)."""
    if not result or not isinstance(result, dict):
        return [], False

    inner = result.get("d")
    if inner is None:
        return [], False
    if not isinstance(inner, dict):
        return [], False

    data = inner.get("Data")
    if not isinstance(data, dict):
        return [], False

    lines = data.get("GameLines") or []
    if not isinstance(lines, list):
        return [], False
    return lines, True
