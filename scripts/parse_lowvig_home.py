import re
html = open("/home/ubuntu/betting-arbitrage/logs/debug/lowvig_home_probe.html").read()
for pat in ["client_id", "keycloak", "openid", "api.lowvig", "token", "AuthenticationUser", "CustomerID"]:
    print(pat, len(re.findall(pat, html, re.I)))
for m in re.finditer(r"https://api\.lowvig[^\"'\s<>]+", html):
    print(m.group(0)[:150])
