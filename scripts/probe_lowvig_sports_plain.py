#!/usr/bin/env python3
import re
import time

from selenium.webdriver.common.by import By

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
    for url in [
        "https://sports.lowvig.ag/sportsbook",
        "https://sports.lowvig.ag/",
        "https://www.lowvig.ag/sportsbook",
    ]:
        d.get(url)
        time.sleep(12)
        src = d.page_source
        print("URL", url)
        print(" ->", d.current_url)
        print(" title:", (d.title or "")[:70])
        print(" cf:", "just a moment" in src.lower())
        inputs = [(i.get_attribute("id"), i.get_attribute("name"), i.get_attribute("type"))
                  for i in d.find_elements(By.CSS_SELECTOR, "input") if i.is_displayed()]
        print(" inputs:", inputs[:10])
        if "Offering.asmx" in src or "GetSportOffering" in src:
            print(" has Offering API refs")
        if "GameLines" in src or "M1_" in src:
            print(" has game line markers")
        print()

    # try API without login on sports domain
    d.get("https://sports.lowvig.ag/sportsbook")
    time.sleep(10)
    result = d.execute_script("""
        return fetch('/sports/Api/Offering.asmx/GetSportOffering', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type':'application/json','Accept':'application/json'},
            body: JSON.stringify({sportType:'Baseball',sportSubType:'MLB',wagerType:'Straight Bet',hoursAdjustment:0,periodNumber:null,gameNum:null,parentGameNum:null,teaserName:'',requestMode:null})
        }).then(r=>({status:r.status, ok:r.ok, text:r.statusText})).catch(e=>({error:String(e)}));
    """)
    print("unauth API probe:", result)
    path = debug_filepath("debug_lowvig_sports_plain")
    open(path, "w", encoding="utf-8").write(d.page_source[:200000])
    print("saved", path)
finally:
    c._quit_driver()
