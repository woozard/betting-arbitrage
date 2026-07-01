#!/usr/bin/env python3
"""LowVig login with headed Chrome on Xvfb (Cloudflare bypass attempt)."""
import os
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

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

options = webdriver.ChromeOptions()
# NO headless — run under xvfb-run
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080")
options.add_argument(f"--load-extension={ext_dir}")
options.add_argument("--disable-extensions-except=" + ext_dir)
options.add_argument("--disable-blink-features=AutomationControlled")
user_data = tempfile.mkdtemp(prefix="lowvig_xvfb_")
options.add_argument(f"--user-data-dir={user_data}")

driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 120)
try:
    driver.get("https://www.lowvig.ag/")
    time.sleep(8)
    print("home", driver.title)
    driver.find_element(By.CSS_SELECTOR, "#lvbtn").click()
    print("clicked login")
    for i in range(40):
        time.sleep(5)
        src = driver.page_source.lower()
        print(f"  {(i+1)*5}s cf={'just a moment' in src} url={driver.current_url[:90]}")
        user = None
        for sel in ("#CustomerID", "#customerID", "input[name='CustomerID']", "#username"):
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].is_displayed():
                user = els[0]
                break
        pwd_els = [e for e in driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if e.is_displayed()]
        if user and pwd_els and "just a moment" not in src:
            user.clear(); user.send_keys(LOWVIG_ACCOUNT)
            pwd_els[0].clear(); pwd_els[0].send_keys(LOWVIG_PASSWORD)
            for sel in ("#btnLogin", "button[type='submit']", "input[type='submit']", "button.btn-primary"):
                btns = driver.find_elements(By.CSS_SELECTOR, sel)
                if btns and btns[0].is_displayed():
                    btns[0].click()
                    print("submitted", sel)
                    break
            time.sleep(20)
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
            if lines:
                print("SUCCESS sample", lines[0].get("Team1ID"), lines[0].get("MoneyLine1"))
            break
finally:
    driver.quit()
