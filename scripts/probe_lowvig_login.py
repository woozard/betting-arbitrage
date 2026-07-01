#!/usr/bin/env python3
import re
import time

from selenium.webdriver.common.by import By

from controllers.LowVigController import LowVigController
from database.models.Accounts import Accounts
from utils.config import LOWVIG, LOWVIG_ACCOUNT, LOWVIG_PASSWORD, LOWVIG_LABEL

account = Accounts(account=LOWVIG_ACCOUNT, password=LOWVIG_PASSWORD, label=LOWVIG_LABEL)
c = LowVigController(account, LOWVIG)
driver = c.driver

urls = [
    "https://www.lowvig.ag/",
    "https://www.lowvig.ag/login",
    "https://sportsbook.lowvig.ag/",
    "https://sports.lowvig.ag/sportsbook",
    "https://www.lowvig.ag/sportsbook",
]
selectors = [
    "#account",
    "#username",
    "#CustomerID",
    "input[name=username]",
    "input[type=password]",
    "#LogInAccount",
    "button[type=submit]",
]

try:
    for url in urls:
        driver.get(url)
        time.sleep(8)
        src = driver.page_source.lower()
        print("URL", url)
        print("  ->", driver.current_url)
        print("  title:", (driver.title or "")[:80])
        print("  cf:", "just a moment" in src or "attention required" in src)
        for sel in selectors:
            found = driver.find_elements(By.CSS_SELECTOR, sel)
            if found:
                print("  FOUND", sel, "count", len(found))
        ids = re.findall(r'id="([^"]+)"', driver.page_source[:80000])
        interesting = [
            i for i in ids
            if any(k in i.lower() for k in ("account", "user", "login", "pass", "customer"))
        ]
        print("  interesting ids:", interesting[:20])
        print()
finally:
    c._quit_driver()
