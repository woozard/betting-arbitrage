from controllers.PolymarketController import PolymarketController
from utils.config import POLYMARKET


def main():
    print("=== Fetching MLB Moneyline from Polymarket (Gamma API) ===")
    controller = PolymarketController(POLYMARKET, sport="baseball")
    controller.fetch_odds()
    print("\nFinished fetching MLB moneyline odds from Polymarket")


if __name__ == "__main__":
    main()
