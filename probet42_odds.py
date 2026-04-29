from controllers.Web5Controller import Web5Controller
from database.models.Accounts import Accounts   
from database.config import __get_db1_session__
from utils.config import PROBET42

# Create a database session
db = __get_db1_session__()

def main():

    account = Accounts(
        account = 'user1',
        password = '***********',
        label = 'Reader'
    )
    web5 = Web5Controller(account, PROBET42)
    web5.fetch_odds()

if __name__ == "__main__":
    main()

