import asyncio
import random
from bs4 import BeautifulSoup
from datetime import datetime

from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import parse_odds, send_monitoring_alert
from utils.timing import time_it
from cache.arbitrage_cache import ArbitrageCache

from playwright.async_api import async_playwright


class Probet42Controller:
    def __init__(self, account, site, sport="baseball"):  # MLB priority
        self.account_id = account.account
        self.password = account.password
        self.label = getattr(account, 'label', 'N/A')

        self.bookmaker = site['bookmaker']
        self.website = site['website']

        self.sport = sport.lower()
        if self.sport in ["basketball", "nba"]:
            self.sport_url = f"https://www.{self.website}/en/sports/basketball/nba/game-lines/"
            self.sport_name = "NBA"
        else:
            self.sport_url = f"https://www.{self.website}/en/sports/baseball/mlb/game-lines/"
            self.sport_name = "MLB"
        self.league = self.sport_name

        self.logger = Logger.get_logger(f"{self.bookmaker}-scraping-browser")
        self.storage = Storage(self.logger)
        self.cache = ArbitrageCache()

    @time_it
    async def fetch_odds(self):
        self.logger.info(f"========== Fetching {self.sport_name} Odds via BrightData Scraping Browser (START) ==========")
        try:
            async with async_playwright() as p:
                # Corrected Scraping Browser connection with full credentials
                browser = await p.chromium.connect_over_cdp(
                    "wss://brd.superproxy.io:9222?customer=hl_70fad530&zone=arbitrage_bot&password=truzviha7wip"
                )

                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                    locale="en-US",
                    timezone_id="America/New_York"
                )

                page = await context.new_page()

                await page.goto(self.sport_url, wait_until="domcontentloaded", timeout=120000)
                await page.wait_for_timeout(random.randint(10000, 15000))

                # Human-like behavior
                for _ in range(4):
                    await page.mouse.move(random.randint(100, 1700), random.randint(100, 900), steps=15)
                    await page.wait_for_timeout(random.randint(600, 1800))
                    await page.evaluate(f"window.scrollBy(0, {random.randint(300, 900)})")
                    await page.wait_for_timeout(random.randint(1000, 2500))

                html = await page.content()

                debug_file = f"debug_probet42_{self.sport_name}_{int(datetime.now().timestamp())}.html"
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(html)
                self.logger.info(f"💾 Saved debug HTML: {debug_file}")

                screenshot_path = f"debug_probet42_{self.sport_name}_{int(datetime.now().timestamp())}.png"
                await page.screenshot(path=screenshot_path)
                self.logger.info(f"📸 Screenshot saved: {screenshot_path}")

                self.logger.info(f"Page title: {await page.title()}")

                await browser.close()

        except Exception as e:
            self.logger.error(f"Fetch failed: {e}", exc_info=True)
        finally:
            self.logger.info(f"========== Fetching {self.sport_name} Odds via BrightData Scraping Browser (END) ==========")


def main():
    from database.models.Accounts import Accounts
    from utils.config import PROBET42
    account = Accounts(account='PWF2820009', password='***********', label='Reader')
    controller = Probet42Controller(account, PROBET42, sport="baseball")
    asyncio.run(controller.fetch_odds())

if __name__ == "__main__":
    main()