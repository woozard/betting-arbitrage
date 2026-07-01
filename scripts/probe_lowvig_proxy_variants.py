#!/usr/bin/env python3
"""Try LowVig login with BrightData proxy username flags (country/session)."""
import json
import os
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.common.by import By

from utils.config import LOWVIG_ACCOUNT, LOWVIG_PASSWORD

CUSTOMER = "hl_70fad530"
ZONE = "arbitrage_bot"
PASS = "truzviha7wip"

USER_VARIANTS = [
    f"brd-customer-{CUSTOMER}-zone-{ZONE}",
    f"brd-customer-{CUSTOMER}-zone-{ZONE}-country-us",
    f"brd-customer-{CUSTOMER}-zone-{ZONE}-country-us-session-lowvig1",
    f"brd-customer-{CUSTOMER}-zone-residential",
    f"brd-customer-{CUSTOMER}-zone-residential-country-us",
    f"brd-customer-{CUSTOMER}-zone-isp",
    f"brd-customer-{CUSTOMER}-zone-mobile",
]


def make_driver(proxy_user: str):
    ext_dir = tempfile.mkdtemp(prefix="lv_proxy_")
    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy",
        "permissions": ["proxy", "tabs", "unlimitedStorage", "storage", "webRequest", "webRequestBlocking"],
        "background": {"scripts": ["background.js"]},
    }
    bg = f"""
chrome.proxy.settings.set({{value:{{mode:"fixed_servers",rules:{{singleProxy:{{scheme:"http",host:"brd.superproxy.io",port:33335}}}}}},scope:"regular"}},function(){{}});
chrome.webRequest.onAuthRequired.addListener(function(d){{return{{authCredentials:{{username:"{proxy_user}",password:"{PASS}"}}}};}},{{urls:["<all_urls>"]}},["blocking"]);
"""
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(bg)
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--load-extension={ext_dir}")
    options.add_argument("--disable-extensions-except=" + ext_dir)
    return webdriver.Chrome(options=options), ext_dir


for user in USER_VARIANTS:
    print("\n=== trying", user, "===")
    driver = None
    try:
        driver, _ = make_driver(user)
        driver.get("https://www.lowvig.ag/")
        time.sleep(8)
        print(" home", driver.title[:50])
        if "just a moment" in (driver.page_source or "").lower():
            print(" home CF blocked")
            continue
        driver.find_element(By.CSS_SELECTOR, "#lvbtn").click()
        time.sleep(20)
        cf = "just a moment" in driver.page_source.lower()
        print(" auth cf=", cf, "url=", driver.current_url[:80])
        for sel in ("#CustomerID", "#customerID", "input[type='password']"):
            if driver.find_elements(By.CSS_SELECTOR, sel):
                print(" FOUND", sel)
                break
        else:
            if not cf:
                print(" no CF but no form either")
    except Exception as e:
        print(" error", e)
    finally:
        if driver:
            driver.quit()
