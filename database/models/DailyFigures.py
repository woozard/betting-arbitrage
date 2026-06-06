from sqlalchemy import Column, String, Integer, Text
from database.config import __get_base__

Base = __get_base__()

class DailyFigures(Base):
    __tablename__: str = 'daily_figures'

    id = Column(Integer, primary_key=True, autoincrement=True)
    website = Column('website', String(255))
    account_id = Column('account_id', String(255))
    bet_status = Column('type', String(255))
    book_ticket_id = Column('book_ticket_id', String(255))
    game_no = Column('game_no', String(255))
    team_1 = Column('team_1', String(255))
    team_2 = Column('team_2', String(255))
    bet_type = Column('bet_type', String(255))
    odds = Column('odds', String(255))
    spread = Column('spread', String(255))
    total = Column('total', String(255))
    team_bet_on = Column('team_bet_on', String(255))
    risk = Column('risk', String(255))
    win = Column('win', String(255))
    status = Column('status', String(255))
    final_score = Column('final_score', String(255))
    accepted = Column('accepted', String(255))
    placed_on = Column('placed_on', String(255))
    sport = Column('sport', String(255))
    period = Column('period', String(255))
    date = Column('date', String(255), nullable=True)
    time = Column('time', String(255), nullable=True)
    timezone = Column('timezone', String(255), nullable=True)

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
