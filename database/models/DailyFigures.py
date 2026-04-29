from sqlalchemy import Column, String, Integer
from database.config import __get_base__

Base = __get_base__()

class DailyFigures(Base):
    __tablename__: str = 'daily_figures'

    id = Column(Integer, primary_key=True, autoincrement=True)
    website = Column('website', String)
    account_id = Column('account_id', String)
    bet_status = Column('type', String)
    book_ticket_id = Column('book_ticket_id', String)
    game_no = Column('game_no', String)
    team_1 = Column('team_1', String)
    team_2 = Column('team_2', String)
    bet_type = Column('bet_type', String)
    odds = Column('odds', String)
    spread = Column('spread', String)
    total = Column('total', String)
    team_bet_on = Column('team_bet_on', String)
    risk = Column('risk', String)
    win = Column('win', String)
    status = Column('status', String)
    final_score = Column('final_score', String)
    accepted = Column('accepted', String)
    placed_on = Column('placed_on', String)
    sport = Column('sport', String)
    period = Column('period', String)
    date = Column('date', String, nullable=True)
    time = Column('time', String, nullable=True)
    timezone = Column('timezone', String, nullable=True)

    def __init__(self, website: object, account_id: object, bet_status: object, book_ticket_id: object, game_no: object,
                 team_1: object,
                 team_2: object,
                 bet_type: object,
                 odds: object, spread: object, total: object, team_bet_on: object, risk: object, win: object,
                 status: object,
                 final_score: object,
                 accepted: object, placed_on: object, sport: object, period: object, date: object, time: object,
                 timezone: object):
        self.website = website
        self.account_id = account_id
        self.bet_status = bet_status
        self.book_ticket_id = book_ticket_id
        self.game_no = game_no
        self.team_1 = team_1
        self.team_2 = team_2
        self.bet_type = bet_type
        self.odds = odds
        self.spread = spread
        self.total = total
        self.team_bet_on = team_bet_on
        self.risk = risk
        self.win = win
        self.status = status
        self.final_score = final_score
        self.accepted = accepted
        self.placed_on = placed_on
        self.sport = sport
        self.period = period
        self.date = date
        self.time = time
        self.timezone = timezone

def __get_instance__():
    return Base
