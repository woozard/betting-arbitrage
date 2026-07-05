from utils.betamapola_offering import parse_get_sport_offering_response


def test_parse_get_sport_offering_valid():
    result = {
        "d": {
            "Data": {
                "GameLines": [{"Team1ID": "A", "PeriodNumber": 0}],
                "SportLimits": [{}],
            }
        }
    }
    lines, ok = parse_get_sport_offering_response(result)
    assert ok is True
    assert len(lines) == 1


def test_parse_get_sport_offering_null_d():
    lines, ok = parse_get_sport_offering_response({"d": None})
    assert ok is False
    assert lines == []


def test_parse_get_sport_offering_missing_data():
    lines, ok = parse_get_sport_offering_response({"d": {}})
    assert ok is False
    assert lines == []


def test_parse_get_sport_offering_empty_response():
    lines, ok = parse_get_sport_offering_response(None)
    assert ok is False
    assert lines == []
