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
try:
    d.get("https://www.lowvig.ag/")
    time.sleep(10)
    html = d.page_source
    low = html.lower()
    for kw in ["login", "sign in", "account", "password", "sportsbook", "join now", "log in"]:
        print(kw, low.count(kw))
    links = re.findall(r'href="([^"]+)"', html)
    login_links = [
        l for l in links
        if any(k in l.lower() for k in ("login", "sign", "sports", "join"))
    ]
    print("login-ish links:", login_links[:30])
    for text in ["Login", "Log In", "SIGN IN", "Sign In", "Join Now"]:
        els = d.find_elements(
            By.XPATH,
            f"//*[contains(translate(normalize-space(text()),"
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]",
        )
        if els:
            el = els[0]
            print("text match", text, "tag", el.tag_name, "href", el.get_attribute("href"))
    path = "logs/debug/lowvig_home_probe.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print("saved", path, "len", len(html))
finally:
    c._quit_driver()
