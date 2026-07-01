#!/usr/bin/env python3
"""Try LowVig login with undetected-chromedriver + BrightData proxy."""
import os
import tempfile
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from utils.config import LOWVIG_ACCOUNT, LOWVIG_PASSWORD

proxy_host = "brd.superproxy.io"
proxy_port = 33335
proxy_user = "brd-customer-hl_70fad530-zone-arbitrage_bot"
proxy_pass = "truzviha7wip"

ext_dir = tempfile.mkdtemp(prefix="lowvig_proxy_ext_")
manifest = {
    "version": "1.0.0",
    "manifest_version": 2,
    "name": "Proxy",
    "permissions": ["proxy", "tabs", "unlimitedStorage", "storage", "webRequest", "webRequestBlocking"],
    "background": {"scripts": ["background.js"]},
}
bg = f"""
chrome.proxy.settings.set({{
  value: {{ mode: "fixed_servers", rules: {{ singleProxy: {{ scheme: "http", host: "{proxy_host}", port: {proxy_port} }} }} }},
  scope: "regular"
}}, function() {{}});
chrome.webRequest.onAuthRequired.addListener(
  function(details) {{ return {{ authCredentials: {{ username: "{proxy_user}", password: "{proxy_pass}" }} }}; }},
  {{urls: ["<all_urls>"]}}, ["blocking"]
);
"""
import json
with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
    json.dump(manifest, f)
with open(os.path.join(ext_dir, "background.js"), "w") as f:
    f.write(bg)

options = uc.ChromeOptions()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080")
options.add_argument(f"--load-extension={ext_dir}")
options.add_argument("--disable-extensions-except=" + ext_dir)
options.add_argument("--disable-blink-features=AutomationControlled")

driver = uc.Chrome(options=options, use_subprocess=True, headless=True, version_main=148)
wait = WebDriverWait(driver, 120)
try:
    driver.get("https://www.lowvig.ag/")
    time.sleep(8)
    print("home", driver.title)
    driver.find_element(By.CSS_SELECTOR, "#lvbtn").click()
    print("clicked login")
    for i in range(30):
        time.sleep(4)
        src = driver.page_source.lower()
        print(f"  {i*4}s cf={'just a moment' in src} url={driver.current_url[:80]}")
        for sel in ("#CustomerID", "#customerID", "input[name='CustomerID']", "#Password", "input[type='password']"):
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].is_displayed():
                print("FOUND", sel)
        if "just a moment" not in src:
            vis = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if e.is_displayed()]
            if vis:
                print("visible inputs", [(v.get_attribute("id"), v.get_attribute("name"), v.get_attribute("type")) for v in vis])
                user = None
                for sel in ("#CustomerID", "#customerID", "input[name='CustomerID']", "#username", "#account"):
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                    if els and els[0].is_displayed():
                        user = els[0]
                        break
                pwd = None
                for sel in ("#Password", "#password", "input[type='password']"):
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                    if els and els[0].is_displayed():
                        pwd = els[0]
                        break
                if user and pwd:
                    user.send_keys(LOWVIG_ACCOUNT)
                    pwd.send_keys(LOWVIG_PASSWORD)
                    for sel in ("#btnLogin", "button[type='submit']", "input[type='submit']"):
                        btns = driver.find_elements(By.CSS_SELECTOR, sel)
                        if btns:
                            btns[0].click()
                            print("submitted", sel)
                            break
                    time.sleep(15)
                    print("post-login", driver.current_url, driver.title)
                    driver.get("https://sports.lowvig.ag/sportsbook")
                    time.sleep(15)
                    print("sports", driver.current_url, driver.title)
                    result = driver.execute_script("""
                        return fetch('/sports/Api/Offering.asmx/GetSportOffering', {
                            method: 'POST', credentials: 'include',
                            headers: {'Content-Type':'application/json'},
                            body: JSON.stringify({sportType:'Baseball',sportSubType:'MLB',wagerType:'Straight Bet',hoursAdjustment:0,periodNumber:null,gameNum:null,parentGameNum:null,teaserName:'',requestMode:null})
                        }).then(r=>r.json()).catch(e=>({error:String(e)}));
                    """)
                    lines = ((result or {}).get("d") or {}).get("Data", {}).get("GameLines") or []
                    print("GameLines", len(lines))
                break
finally:
    driver.quit()
