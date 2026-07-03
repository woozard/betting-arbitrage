import logging
import os
import tempfile

from utils.bet_screenshot import render_bet_receipt


def test_render_bet_receipt_writes_png():
    logger = logging.getLogger("test_bet_screenshot")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "receipt.png")
        result = render_bet_receipt(
            path,
            "3et",
            team_1="Pirates",
            team_2="Nationals",
            team_name="Nationals",
            odds="-133",
            stake=26.60,
            bet_type="moneyline",
            extra_lines=["Bet ID: 12345"],
            logger=logger,
        )
        assert result == path
        assert os.path.isfile(path)
        assert os.path.getsize(path) > 500
