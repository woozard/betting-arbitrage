#!/usr/bin/env python3
"""Login via www.lowvig.ag Log In button, then wait for auth form."""
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from controllers.LowVigController import LowVigController
from database.models.Accounts import Accounts
from utils.config import LOWVIG, LOWVIG_ACCOUNT, LOWVIG_PASSWORD, LOWVIG_LABEL
from utils.helpers import debug_filepath

c = LowVigController(
    Accounts(account=LOWVIG_ACCOUNT, password=LOWVIG_PASSWORD, label=LOWVIG_LABEL),
    LOWVIG,
)
d = c.driver

try:
    d.get("https://www.lowvig.ag/")
    time.sleep(10)
    print("home", d.title, d.current_url)

    btn = d.find_element(By.CSS_SELECTOR, "#lvbtn")
    d.execute_script("arguments[0].click();", btn)
    print("clicked login, waiting...")
    for i in range(24):
        time.sleep(5)
        src = d.page_source.lower()
        url = d.current_url
        title = d.title or ""
        print(f"  {i*5+5}s cf={'just a moment' in src} url={url[:90]} title={title[:50]}")
        for sel in (
            "#CustomerID", "#customerID", "input[name='CustomerID']",
            "#Password", "#password", "input[type='password']",
            "#account", "#username",
        ):
            els = d.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].is_displayed():
                print("    FOUND", sel)
        if "just a moment" not in src:
            inputs = d.find_elements(By.CSS_SELECTOR, "input")
            visible = [x for x in inputs if x.is_displayed()]
            if visible:
                print("    visible inputs:", len(visible))
                for inp in visible[:8]:
                    print("     ", inp.get_attribute("id"), inp.get_attribute("name"), inp.get_attribute("type"))
                break

    with open(debug_filepath("debug_lowvig_after_login_click"), "w", encoding="utf-8") as f:
        f.write(d.page_source)
finally:
    c._quit_driver()
