from database.config import __get_db2_session__
from database.models.Odds2 import Odds2
from utils.helpers import currency_to_float
from sqlalchemy.sql import text
from sqlalchemy.exc import IntegrityError
from datetime import datetime

class Storage2:
    def __init__(self, logger):
        self.db = __get_db2_session__()
        self.logger = logger

    def save_odds2(self, odd: dict) -> bool:
        """
        Save a single odd into the odds2 table.
        Skips duplicate rows based on unique constraint (matchup_id, market_type, side, odds).

        :param odd: Dictionary representing one flat odd row
        :return: True if inserted, False if skipped or error
        """
        self.logger.info("========== Save Odds2 (START) ==========")

        if not odd:
            self.logger.warning("DB - No odd to save")
            return False

        saved = False
        try:
            odds_row = Odds2(
                bookmaker=odd["bookmaker"],
                matchup_id=odd["matchup_id"],
                market_key=odd.get("market_key"),
                market_type=odd.get("market_type"),
                period=odd.get("period", 0),
                cutoff_at=odd.get("cutoff_at"),
                is_alternate=odd.get("is_alternate", False),
                status=odd.get("status", "open"),
                version=odd.get("version"),
                side=odd.get("side"),
                team_name=odd.get("team_name"),
                participant_id=odd.get("participant_id"),
                line=odd.get("line"),
                odds=odd.get("odds"),
                limit_type=odd.get("limit_type"),
                limit_amount=odd.get("limit_amount")
            )

            self.db.add(odds_row)
            self.db.commit()
            saved = True

            self.logger.info(
                f"DB - Odds2 Saved (bookmaker = {odd['bookmaker']} - matchup_id = {odd['matchup_id']} - market_type = {odd.get('market_type')} - "
                f"side = {odd.get('side')} - odds = {odd.get('odds')})"
            )

        except IntegrityError:
            self.db.rollback()
            self.logger.warning(
                f"DB - Odds2 Skipped (duplicate) (bookmaker = {odd['bookmaker']} - matchup_id = {odd['matchup_id']} - market_type = {odd.get('market_type')} - "
                f"side = {odd.get('side')} - odds = {odd.get('odds')})"
            )
        except Exception as e:
            self.db.rollback()
            self.logger.exception(f"DB - Odds2 Error: {e}")

        self.logger.info("========== Save Odds2 (END) ==========")
        return saved

