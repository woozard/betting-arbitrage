#!/usr/bin/env python3
"""Try LowVig login without proxy (direct EC2 IP)."""
import time
import tempfile

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from utils.config import LOWVIG_ACCOUNT, LOWVIG_PASSWORD

options = webdriver.ChromeOptions()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument(
    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
user_data = tempfile.mkdtemp(prefix="lowvig_direct_")
options.add_argument(f"--user-data-dir={user_data}")

driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 120)
try:
    driver.get("https://www.lowvig.ag/")
    time.sleep(10)
    print("home", driver.title, "cf", "just a moment" in driver.page_source.lower())
    if driver.find_elements(By.CSS_SELECTOR, "#lvbtn"):
        driver.find_element(By.CSS_SELECTOR, "#lvbtn").click()
        print("clicked login")
    else:
        driver.get("https://account.lowvig.ag/Login/AuthenticationUser")
        print("direct auth url")

    for i in range(36):
        time.sleep(5)
        src = driver.page_source.lower()
        print(f"  {(i+1)*5}s cf={'just a moment' in src} url={driver.current_url[:90]} title={(driver.title or '')[:50]}")
        for sel in ("#CustomerID", "#customerID", "input[name='CustomerID']", "#Password", "input[type='password']"):
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].is_displayed():
                print("    FOUND", sel)
                els[0].send_keys(LOWVIG_ACCOUNT if "pass" not in sel.lower() else LOWVIG_PASSWORD)
        pwd = driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
        user = driver.find_elements(By.CSS_SELECTOR, "#CustomerID, #customerID, input[name='CustomerID']")
        if user and pwd and user[0].is_displayed() and pwd[0].is_displayed() and i == 0:
            pass
        if user and pwd and user[0].is_displayed() and pwd[0].is_displayed():
            user[0].clear(); user[0].send_keys(LOWVIG_ACCOUNT)
            pwd[0].clear(); pwd[0].send_keys(LOWVIG_PASSWORD)
            for sel in ("#btnLogin", "button[type='submit']", "input[type='submit']"):
                btns = driver.find_elements(By.CSS_SELECTOR, sel)
                if btns:
                    btns[0].click()
                    print("    submitted", sel)
                    break
            time.sleep(15)
            print("    post", driver.current_url, driver.title)
            break
        if "just a moment" not in src and user and pwd:
            break
finally:
    driver.quit()
