#!/usr/bin/env python3
import re
import time

from selenium.webdriver.common.by import By

from controllers.LowVigController import LowVigController
from database.models.Accounts import Accounts
from utils.config import LOWVIG, LOWVIG_ACCOUNT, LOWVIG_PASSWORD, LOWVIG_LABEL

c = LowVigController(
    Accounts(account=LOWVIG_ACCOUNT, password=LOWVIG_PASSWORD, label=LOWVIG_LABEL),
    LOWVIG,
)
d = c.driver
selectors = [
    "#account", "#username", "#CustomerID", "#password",
    "input[name=username]", "input[type=password]", "#LogInAccount",
    "button[type=submit]", ".login-form",
]
urls = [
    "https://account.lowvig.ag/",
    "https://account.lowvig.ag/login",
    "https://www.lowvig.ag/",
    "https://sports.lowvig.ag/",
]
try:
    for url in urls:
        d.get(url)
        time.sleep(10)
        src = d.page_source.lower()
        print("URL", url, "->", d.current_url, "title:", (d.title or "")[:70])
        print("  cf:", "just a moment" in src)
        for sel in selectors:
            found = d.find_elements(By.CSS_SELECTOR, sel)
            if found:
                print("  FOUND", sel)
        ids = re.findall(r'id="([^"]+)"', d.page_source[:100000])
        interesting = [i for i in ids if any(k in i.lower() for k in ("account", "user", "login", "pass", "customer"))]
        print("  ids:", interesting[:20])
        print()
finally:
    c._quit_driver()
