from utils.betamapola_wager_api import (
    parse_process_ticket_response,
    wager_network_body_confirms,
)


def test_parse_process_ticket_success():
    ok, ticket, msg = parse_process_ticket_response(
        {"d": {"IsSuccess": True, "Data": {"TicketNumber": 135413488}}}
    )
    assert ok is True
    assert ticket == 135413488
    assert "135413488" in msg


def test_parse_process_ticket_session_conflict():
    ok, ticket, msg = parse_process_ticket_response(
        {
            "d": {
                "IsSuccess": False,
                "Code": 5,
                "Message": "Another user has taken over your session.",
                "Data": None,
            }
        }
    )
    assert ok is False
    assert ticket is None
    assert "session" in msg.lower()


def test_parse_process_ticket_success_without_ticket_number():
    ok, ticket, msg = parse_process_ticket_response(
        {"d": {"IsSuccess": True, "Data": {"TicketNumber": 0}}}
    )
    assert ok is True
    assert ticket == 0
    assert msg == "ProcessTicket IsSuccess"


def test_wager_network_body_confirms_process_ticket():
    body = '{"d":{"Data":{"TicketNumber":12345},"IsSuccess":true}}'
    assert wager_network_body_confirms(body) is True
