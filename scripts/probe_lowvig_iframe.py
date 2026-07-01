#!/usr/bin/env python3
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from controllers.LowVigController import LowVigController
from database.models.Accounts import Accounts
from utils.config import LOWVIG, LOWVIG_ACCOUNT, LOWVIG_PASSWORD, LOWVIG_LABEL

c = LowVigController(
    Accounts(account=LOWVIG_ACCOUNT, password=LOWVIG_PASSWORD, label=LOWVIG_LABEL),
    LOWVIG,
)
d = c.driver
wait = WebDriverWait(d, 30)
try:
    d.get("https://www.lowvig.ag/")
    time.sleep(8)
    iframes = d.find_elements(By.TAG_NAME, "iframe")
    print("iframes on home:", len(iframes))
    for i, fr in enumerate(iframes):
        print(i, fr.get_attribute("src"), fr.get_attribute("id"))

    # try click Login link/button
    for xp in [
        "//a[contains(., 'Login')]",
        "//button[contains(., 'Login')]",
        "//*[contains(@class,'login')]",
    ]:
        els = d.find_elements(By.XPATH, xp)
        if els:
            print("click candidate", xp, len(els))
            try:
                d.execute_script("arguments[0].click();", els[0])
                time.sleep(8)
                print("after click url:", d.current_url, "title:", d.title)
                break
            except Exception as e:
                print("click fail", e)

    iframes2 = d.find_elements(By.TAG_NAME, "iframe")
    print("iframes after click:", len(iframes2))
    for i, fr in enumerate(iframes2):
        src = fr.get_attribute("src")
        print(i, src)
        if src and "login" in src.lower():
            d.switch_to.frame(fr)
            time.sleep(3)
            for sel in ["#CustomerID", "#customerID", "#account", "#username", "#password"]:
                found = d.find_elements(By.CSS_SELECTOR, sel)
                if found:
                    print("  in iframe FOUND", sel)
            d.switch_to.default_content()

    # direct sports with session?
    d.get("https://sports.lowvig.ag/sportsbook")
    time.sleep(10)
    print("sports url:", d.current_url, "title:", d.title)
    for sel in ["#account", "#CustomerID", "#username", "input[type=password]", "#LogInAccount"]:
        found = d.find_elements(By.CSS_SELECTOR, sel)
        if found:
            print("sports FOUND", sel)
    with open("logs/debug/lowvig_sports_probe.html", "w", encoding="utf-8") as f:
        f.write(d.page_source[:150000])
    print("saved sports probe")
finally:
    c._quit_driver()
