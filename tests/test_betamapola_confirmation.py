from utils.ticosports_wager import (
    betslip_text_confirms_wager,
    pick_looks_like_open_wager,
    wager_network_entry_confirms,
)


def test_wager_network_entry_ignores_getwagerpicks_success():
    entry = {
        "url": "https://betamapola.com/sports/Api/Betting.asmx/GetWagerPicks",
        "body": '{"d":{"Data":{"MinPicks":1,"MaxPicks":999},"IsSuccess":true}}',
    }
    assert wager_network_entry_confirms(entry) is False


def test_wager_network_entry_accepts_process_ticket():
    entry = {
        "url": "https://betamapola.com/sports/Api/Betting.asmx/ProcessTicket",
        "body": '{"d":{"TicketNumber":12345,"IsSuccess":true,"Message":"accepted"}}',
    }
    assert wager_network_entry_confirms(entry) is True


def test_pick_looks_like_open_wager_requires_team_data():
    assert pick_looks_like_open_wager({"MinPicks": 1}) is False
    assert pick_looks_like_open_wager(
        {"Team1ID": "Padres", "Risk": 20, "ToWin": 21.6}
    ) is True


def test_betslip_text_confirms_wager_reference_id():
    assert betslip_text_confirms_wager("REFERENCE ID # 998877") is True
    assert betslip_text_confirms_wager("Place Bet\nRisk $20") is False
