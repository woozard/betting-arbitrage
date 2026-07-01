#!/usr/bin/env python3
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from controllers.LowVigController import LowVigController
from database.models.Accounts import Accounts
from utils.config import LOWVIG, LOWVIG_ACCOUNT, LOWVIG_PASSWORD, LOWVIG_LABEL
from utils.helpers import debug_filepath

LOGIN_URL = "https://account.lowvig.ag/Login/AuthenticationUser"

c = LowVigController(
    Accounts(account=LOWVIG_ACCOUNT, password=LOWVIG_PASSWORD, label=LOWVIG_LABEL),
    LOWVIG,
)
d = c.driver
wait = WebDriverWait(d, 90)

try:
    d.get(LOGIN_URL)
    for sec in range(0, 91, 15):
        time.sleep(15 if sec else 5)
        title = d.title or ""
        url = d.current_url
        src = d.page_source.lower()
        print(f"t+{sec+5}s title={title[:60]} url={url[:100]} cf={'just a moment' in src}")
        for sel in [
            "#CustomerID", "#customerID", "#account", "#username",
            "#Password", "#password", "input[type=password]",
            "button[type=submit]", "input[type=submit]", "#btnLogin",
        ]:
            found = d.find_elements(By.CSS_SELECTOR, sel)
            if found:
                print("  FOUND", sel, "displayed", found[0].is_displayed())
        if "just a moment" not in src and any(
            d.find_elements(By.CSS_SELECTOR, s)
            for s in ("#CustomerID", "#customerID", "#account", "#username")
        ):
            print("FORM READY")
            break

    path = debug_filepath("debug_lowvig_login_probe")
    with open(path, "w", encoding="utf-8") as f:
        f.write(d.page_source)
    print("saved", path)

    # attempt login if fields exist
    user_el = None
    for sel in ("#CustomerID", "#customerID", "#account", "#username", "input[name=CustomerID]"):
        els = d.find_elements(By.CSS_SELECTOR, sel)
        if els and els[0].is_displayed():
            user_el = els[0]
            print("user field", sel)
            break
    pass_el = None
    for sel in ("#Password", "#password", "input[type=password]"):
        els = d.find_elements(By.CSS_SELECTOR, sel)
        if els and els[0].is_displayed():
            pass_el = els[0]
            print("pass field", sel)
            break

    if user_el and pass_el:
        user_el.clear()
        user_el.send_keys(LOWVIG_ACCOUNT)
        pass_el.clear()
        pass_el.send_keys(LOWVIG_PASSWORD)
        for sel in ("#btnLogin", "button[type=submit]", "input[type=submit]", "#LogInAccount"):
            btns = d.find_elements(By.CSS_SELECTOR, sel)
            if btns:
                btns[0].click()
                print("clicked", sel)
                break
        time.sleep(15)
        print("after login url:", d.current_url, "title:", d.title)
        d.get("https://sports.lowvig.ag/sportsbook")
        time.sleep(15)
        print("sports url:", d.current_url, "title:", d.title)
        lines_script = """
            return fetch('/sports/Api/Offering.asmx/GetSportOffering', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'include',
                body: JSON.stringify({
                    sportType: 'Baseball', sportSubType: 'MLB',
                    wagerType: 'Straight Bet', hoursAdjustment: 0,
                    periodNumber: null, gameNum: null, parentGameNum: null,
                    teaserName: '', requestMode: null
                })
            }).then(r => r.json()).catch(e => ({error: String(e)}));
        """
        result = d.execute_script(lines_script)
        data = (result or {}).get("d", {}).get("Data", {})
        lines = data.get("GameLines") or []
        print("GameLines:", len(lines))
    else:
        print("NO LOGIN FORM FOUND")
finally:
    c._quit_driver()
