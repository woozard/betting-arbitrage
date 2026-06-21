from controllers.PolymarketController import PolymarketController
from utils.config import POLYMARKET


def main():
    print("=== Watching MLB moneyline (Gamma API poll) ===")
    controller = PolymarketController(POLYMARKET, sport="baseball")
    controller.watch_odds()
    print("\n✅ Polymarket odds watch ended")


if __name__ == "__main__":
    main()
