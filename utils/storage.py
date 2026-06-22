from database.config import __get_db1_session__
from database.models.DailyFigures import DailyFigures
from database.models.AccountBalance import AccountBalance
from database.models.ArbitrageOdds import ArbitrageOdds
from database.models.Arbitrage import Arbitrage
from database.models.ArbitrageBets import ArbitrageBets
from database.models.Trades import Trades
from utils.helpers import currency_to_float
from utils.game_registry import register_game_from_odds
from sqlalchemy.sql import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from datetime import datetime

class Storage:
    def __init__(self, logger):
        self.db = __get_db1_session__()
        self.logger = logger

    def save_daily_figures(self, data, account_id, url):
        self.logger.info("========== Save Daily Figures (START) ==========")
        filtered_data = [x for x in data if x is not None]
        for item in filtered_data:
            daily_figures = DailyFigures(*item)
            book_ticket_id = item[3]

            # Check for existing entry in the database
            existing_entry = (
                self.db.query(DailyFigures).filter_by(book_ticket_id=book_ticket_id).first()
            )

            if existing_entry is None:
                self.logger.info(f"DB - Daily Figure Added (book_ticket_id = {book_ticket_id})")
                self.logger.debug(f"daily_figures: {item}")
                self.db.add(daily_figures)
            else:
                self.logger.warning(f"DB - Daily Figures Not Saved (book_ticket_id = {book_ticket_id})")

        try:
            self.logger.info("DB - Commit")
            self.db.commit()
        except Exception as e:
            self.logger.exception(f"DB - Exception: {e}")
        self.logger.info("========== Save Daily Figures (END) ==========")

    def save_telegram_alert(self, book_ticket_id, account_id, *args) -> bool:
        self.logger.info("========== Save Telegram Alert (START) ==========")

        # Check for existing entry in the database
        existing_entry = self.db.execute(
            text("SELECT * FROM telegram_alerts WHERE book_ticket_id = :book_ticket_id AND account_id = :account_id"),
            {"book_ticket_id": book_ticket_id, "account_id": account_id}
        ).fetchone()

        saved = False
        if existing_entry is None:
            insert_values = {
                "website": args[0],
                "account_id": args[1],
                "type": args[2],
                "book_ticket_id": args[3],
                "game_no": args[4],
                "team_1": args[5],
                "team_2": args[6],
                "bet_type": args[7],
                "odds": args[8],
                "spread": args[9],
                "total": args[10],
                "team_bet_on": args[11],
                "risk": args[12],
                "win": args[13],
                "status": args[14],
                "sport": args[15],
                "date_time": args[16],
                "created_at": args[17],
                "updated_at": args[18]
            }
            self.db.execute(
                text("INSERT INTO telegram_alerts (website, account_id, type, book_ticket_id, game_no, team_1, team_2, bet_type, odds, spread, total, team_bet_on, risk, win, status, sport, date_time, created_at, updated_at) VALUES (:website, :account_id, :type, :book_ticket_id, :game_no, :team_1, :team_2, :bet_type, :odds, :spread, :total, :team_bet_on, :risk, :win, :status, :sport, :date_time, :created_at, :updated_at)"),
                insert_values
            )
            saved = True
            self.logger.info(f"DB - Telegram Alert Saved (account_id = {account_id} - book_ticket_id = {book_ticket_id})")
        else:
            self.logger.warning(f"DB - Telegram Alert Not Saved (account_id = {account_id} - book_ticket_id = {book_ticket_id})")

        try:
            self.logger.info("DB - Commit")
            self.db.commit()
        except Exception as e:
            self.logger.exception(f"DB - Exception: {e}")
            self.db.rollback()
        self.logger.info("========== Save Telegram Alert (END) ==========")
        return saved

    def save_balance(self, website, account_id, date, amount):
        self.logger.info("========== Save Balance (START) ==========")
        try:
            # Convert amount to a float
            amount = currency_to_float(amount)

             # Try to find an existing record
            existing_record = self.db.query(AccountBalance).filter_by(
                website=website, account_id=account_id, date=date
            ).first()
            now = datetime.now()

            if existing_record:
                 # Update existing record
                existing_record.updated_at = now
                existing_record.amount = amount
                self.logger.info(f"DB - Balance Updated (website = {website} - account_id = {account_id} - date = {date} - amount = {amount})")
            else:
                # Insert new record
                new_record = AccountBalance(
                    created_at=now, 
                    updated_at=now, 
                    date=date,
                    website=website, 
                    account_id=account_id, 
                    amount=amount
                )
                self.db.add(new_record)
                self.logger.info(f"DB - Balance Saved (website = {website} - account_id = {account_id} - date = {date} - amount = {amount})")

            self.db.commit()
        except Exception as e:
            self.logger.exception(f"DB - Exception: {e}")
            self.db.rollback()
        self.logger.info("========== Save Balance (END) ==========")
    
    def save_odds(self, odd: dict) -> bool:
        self.logger.info("========== Save Odds (START) ==========")

        if not odd:
            self.logger.warning("DB - No odds to save")
            return False
        
        # Only save if bet_type is 'moneyline'
        if odd.get("bet_type") != "moneyline":
            self.logger.info(f"DB - Skipping odds with bet_type = {odd.get('bet_type')}")
            self.logger.info("========== Save Odds (END) ==========")
            return False

        saved = False

        try:
            odds_row = ArbitrageOdds(
                sport=odd["sport"],
                league=odd.get("league"),
                game_id=odd["game_id"],
                game_datetime=odd["game_datetime"],
                team_1=odd["team_1"],
                team_2=odd["team_2"],
                bookmaker=odd["bookmaker"],
                bet_type=odd["bet_type"],

                moneyline_team_1=odd.get("moneyline_team_1"),
                moneyline_team_2=odd.get("moneyline_team_2"),
                moneyline_draw=odd.get("moneyline_draw"),

                spread_team_1=odd.get("spread_team_1"),
                spread_team_2=odd.get("spread_team_2"),
                spread_value=odd.get("spread_value"),

                total_points=odd.get("total_points"),
                over_odds=odd.get("over_odds"),
                under_odds=odd.get("under_odds"),
            )

            self.db.add(odds_row)
            self.db.commit()
            saved = True

            try:
                register_game_from_odds(self.db, odd, logger=self.logger)
                self.db.commit()
            except Exception as reg_err:
                self.db.rollback()
                self.logger.warning(f"Canonical game registry skipped: {reg_err}")

            self.logger.info(f"DB - Odds Saved (bookmaker = {odd['bookmaker']} - bet_type = {odd['bet_type']} - game_id = {odd['game_id']})")
                
        except IntegrityError:
            self.db.rollback()
            self.logger.warning("DB - Duplicate Skipped")
        except Exception as e:
            self.db.rollback()
            self.logger.exception(f"DB - Odds Error: {e}")

        self.logger.info("========== Save Odds (END) ==========")
        return saved
    
    def save_bet(self, bet: dict) -> bool:
        self.logger.info("========== Save Bet (START) ==========")

        if not bet:
            self.logger.warning("DB - No bet to save")
            return False

        saved = False

        try:
            bet_row = ArbitrageBets(
                sport=bet["sport"],
                league=bet.get("league"),
                game_id=bet["game_id"],
                game_datetime=bet["game_datetime"],

                team_1=bet["team_1"],
                team_2=bet["team_2"],

                bookmaker=bet["bookmaker"],
                bet_type=bet["bet_type"],

                team_no=bet["team_no"],
                team_name=bet.get("team_name"),

                odds=bet["odds"],
                stake=bet.get("stake")
            )

            self.db.add(bet_row)
            self.db.commit()
            saved = True

            self.logger.info(
                f"DB - Bet Saved (bookmaker={bet['bookmaker']} - "
                f"bet_type={bet['bet_type']} - game_id={bet['game_id']} - "
                f"team_no={bet['team_no']})"
            )

        except IntegrityError:
            self.db.rollback()
            self.logger.warning("DB - Duplicate Bet Skipped")

        except Exception as e:
            self.db.rollback()
            self.logger.exception(f"DB - Bet Error: {e}")

        self.logger.info("========== Save Bet (END) ==========")
        return saved

    def mark_arbitrage_bet_placed(self, arb_id: int, team: int, stake: float, success: bool = True) -> bool:
        """
        Mark an arbitrage bet as placed for the given team, and increment attempts.

        :param arb_id: Arbitrage record ID
        :param team: 1 for team_1, 2 for team_2
        :param success: True if bet succeeded, False if failed
        :return: True if updated successfully, False otherwise
        """
        try:
            arb = self.db.query(Arbitrage).filter_by(id=arb_id).first()
            if not arb:
                self.logger.warning(f"DB - Arbitrage Not Found (id={arb_id})")
                return False

            now = datetime.now()

            if team == 1:
                arb.team_1_bet_placed_attempts = (arb.team_1_bet_placed_attempts or 0) + 1
                if success:
                    arb.team_1_bet_amount = stake
                    arb.team_1_bet_placed = 1
                    arb.team_1_bet_placed_at = now
            elif team == 2:
                arb.team_2_bet_placed_attempts = (arb.team_2_bet_placed_attempts or 0) + 1
                if success:
                    arb.team_2_bet_amount = stake
                    arb.team_2_bet_placed = 1
                    arb.team_2_bet_placed_at = now
            else:
                self.logger.error(f"Invalid team number: {team}")
                return False

            self.db.commit()
            self.logger.info(
                f"Arbitrage bet updated (id = {arb_id} - team = {team} - success = {success} - attempts = {arb.team_1_bet_placed_attempts if team==1 else arb.team_2_bet_placed_attempts})"
            )
            return True

        except Exception as e:
            self.db.rollback()
            self.logger.exception(f"Error updating arbitrage bet (id={arb_id}, team={team}): {e}")
            return False
    
    def save_trade(self, bookmaker: str, label: str, trade: dict) -> bool:
        """
        Save a trade to the database if it doesn't already exist.
        
        Args:
            bookmaker (str): The bookmaker name
            label (str): The label name
            trade_id (str): The unique trade/transaction hash
            trade (dict): Trade details with keys: bet, side, outcome, price, amount, optional label
        
        Returns:
            bool: True if trade was saved, False if it already exists
        """
        self.logger.info("========== Save Trade (START) ==========")
        saved = False

        try:
            # Trade ID
            trade_id=trade.get("trade_id", None)

            # Check if trade already exists
            existing_trade = self.db.query(Trades).filter_by(
                bookmaker=bookmaker,
                trade_id=trade_id
            ).first()

            if existing_trade is None:
                # Create new Trades instance
                new_trade = Trades(
                    bookmaker=bookmaker,
                    label=label,
                    trade_id=trade_id,
                    bet=trade.get("bet", ""),
                    side=trade.get("side", ""),
                    outcome=trade.get("outcome", ""),
                    price=trade.get("price", 0),
                    amount=trade.get("amount", 0),   
                )
                self.db.add(new_trade)
                self.db.commit()
                saved = True
                self.logger.info(f"DB - Trade Saved (bookmaker={bookmaker}, trade_id={trade_id})")
            else:
                self.logger.warning(f"DB - Trade Not Saved (bookmaker={bookmaker}, trade_id={trade_id})")

        except SQLAlchemyError as e:
            self.logger.exception(f"DB - Exception occurred while saving trade: {e}")
            self.db.rollback()

        self.logger.info("========== Save Trade (END) ==========")
        return saved

