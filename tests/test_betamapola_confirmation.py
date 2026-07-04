from utils.ticosports_wager import (
    betslip_text_confirms_wager,
    pick_looks_like_open_wager,
    wager_network_entry_confirms,
    betamapola_betslip_is_empty,
)
from utils.stake_entry import _find_stake_input


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


def test_betamapola_betslip_is_empty():
    assert betamapola_betslip_is_empty("Your bet slip is empty\nPlace Bet", 0) is True
    assert betamapola_betslip_is_empty("×\nPlace Bet", 0) is True
    assert betamapola_betslip_is_empty("", 1) is False
    assert betamapola_betslip_is_empty(
        "Spread -1½ -101\nBoston Red Sox", 0
    ) is False
    assert betamapola_betslip_is_empty("please make one or more selections", None) is True


class _FakeElement:
    def __init__(self, displayed=True, enabled=True):
        self._displayed = displayed
        self._enabled = enabled

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled


class _ScopedDriver:
    def __init__(self, scoped_inputs=None, page_inputs=None):
        self.scoped_inputs = scoped_inputs or []
        self.page_inputs = page_inputs or []

    def find_elements(self, by, selector):
        if selector == "#betSlipBody":
            return [_FakeRoot(self.scoped_inputs)] if self.scoped_inputs is not None else []
        if selector == "input.offering-input":
            return self.page_inputs
        return []


class _FakeRoot:
    def __init__(self, inputs):
        self.inputs = inputs

    def find_elements(self, by, selector):
        return self.inputs


def test_find_stake_input_does_not_fallback_when_scope_missing():
    driver = _ScopedDriver(scoped_inputs=[], page_inputs=[_FakeElement()])
    assert _find_stake_input(driver, ("input.offering-input",), scope_css="#betSlipBody") is None
