from dotenv import load_dotenv
import os

load_dotenv()  # Load environment variables from .env file

# General
APP = {
    'name': os.getenv('APP_NAME'),
    'env': os.getenv('APP_ENV')
}

# Database
DB1 = {
    'username': os.getenv('DB_USERNAME'),
    'password': os.getenv('DB_PASSWORD'),
    'host': os.getenv('DB_HOST'),
    'port': os.getenv('DB_PORT'),
    'database': os.getenv('DB_NAME'),
}

DB2 = {
    'username': os.getenv('DB2_USERNAME'),
    'password': os.getenv('DB2_PASSWORD'),
    'host': os.getenv('DB2_HOST'),
    'port': os.getenv('DB2_PORT'),
    'database': os.getenv('DB2_NAME'),
}

# Redis
REDIS = {
    'host': os.getenv('REDIS_HOST'),
    'port': os.getenv('REDIS_PORT'),
    'db': 0,
    'decode_responses': True,
}

# Telegram — channel routing:
#   TELEGRAM_CHAT_HEALTH       → KC Arb Health Status (ops agent, scanner errors, system alerts)
#   TELEGRAM_CHAT_REAL_BETS    → KC Arb Real Bets (one compact summary per arb: complete or failed)
#   TELEGRAM_CHAT_SCREENSHOTS  → KC Arb Screenshots (per-leg bet photos only — no captions)
#   TELEGRAM_CHAT_ARBITRAGE    → arb opportunity alerts (/scan bot)
_TELEGRAM_HEALTH = os.getenv('TELEGRAM_CHAT_HEALTH')

TELEGRAM = {
    'bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
    'chat_id': os.getenv('TELEGRAM_CHAT_ID'),
    'health': _TELEGRAM_HEALTH,
    'monitoring': os.getenv('TELEGRAM_CHAT_MONITORING') or _TELEGRAM_HEALTH,
    'testing': os.getenv('TELEGRAM_CHAT_TESTING'),
    'arbitrage': os.getenv('TELEGRAM_CHAT_ARBITRAGE'),
    'betting': os.getenv('TELEGRAM_CHAT_BETTING'),
    'arbitrage_monitoring': (
        os.getenv('TELEGRAM_CHAT_ARBITRAGE_MONITORING') or _TELEGRAM_HEALTH or os.getenv('TELEGRAM_CHAT_OPS')
    ),
    'ops': os.getenv('TELEGRAM_CHAT_OPS') or _TELEGRAM_HEALTH,
    'real_bets': os.getenv('TELEGRAM_CHAT_REAL_BETS'),
    'screenshots': os.getenv('TELEGRAM_CHAT_SCREENSHOTS'),
}

# Delay before posting the single Real Bets summary (seconds; allows both legs to land in Redis).
REAL_BETS_SUMMARY_DELAY_SEC = float(os.getenv('REAL_BETS_SUMMARY_DELAY_SEC', '30'))
REAL_BETS_FAILED_SUMMARY_DELAY_SEC = float(os.getenv('REAL_BETS_FAILED_SUMMARY_DELAY_SEC', '5'))


def telegram_health_chat_id():
    """Chat ID for health / ops / monitoring alerts (not real-money bet confirmations)."""
    return (
        TELEGRAM.get('health')
        or TELEGRAM.get('ops')
        or TELEGRAM.get('monitoring')
        or TELEGRAM.get('arbitrage_monitoring')
    )


def arb_opportunity_alert_chat_ids():
    """Telegram chats for scanner / arb-opportunity alerts (both legs, pre-bet).

    Uses TELEGRAM_CHAT_ARBITRAGE only — not TELEGRAM_CHAT_OPS / REAL_BETS, which are
    reserved for isolated per-book bet confirmations and arb-complete summaries.
    """
    cid = TELEGRAM.get("arbitrage")
    if cid:
        return [cid]
    fallback = TELEGRAM.get("chat_id")
    return [fallback] if fallback else []

# Betting — base amount per arb leg ($20 default).
# Minus odds → fill to-win box; plus odds → fill risk box (see utils/stake_sizing.py).
BET_STAKE = float(os.getenv('BET_STAKE', '20'))
REAL_MONEY_BETTING_ENABLED = os.getenv('REAL_MONEY_BETTING_ENABLED', 'true').lower() in (
    '1', 'true', 'yes',
)
# Per-book real-money gate (Betamapola paused while account/placement issues are investigated).
BETAMAPOLA_REAL_MONEY_BETTING_ENABLED = os.getenv(
    'BETAMAPOLA_REAL_MONEY_BETTING_ENABLED', 'false'
).lower() in ('1', 'true', 'yes')
SEQUENTIAL_ARB_BETTING = os.getenv('SEQUENTIAL_ARB_BETTING', 'false').lower() in (
    '1', 'true', 'yes',
)
# Place both legs at once on exchange pairs (4casters + S411): no wait for pre-position or leg-1 ack.
PARALLEL_EXCHANGE_ARB_BETTING = os.getenv(
    'PARALLEL_EXCHANGE_ARB_BETTING', 'true'
).lower() in ('1', 'true', 'yes')
# 4casters fast path: skip pre-place line-move re-check + orderbook max-risk cap and
# fire the order straight from scan odds (participant id + game come from the warm schedule cache).
FOURCASTERS_FAST_PLACE = os.getenv(
    'FOURCASTERS_FAST_PLACE', 'true'
).lower() in ('1', 'true', 'yes')
# S411 fast path: on the first placement attempt skip the pre-place open-bets
# navigations (we just scanned the line and are on the sport page) — click the
# line and place right away. Post-failure recovery still verifies open bets.
S411_FAST_PLACE = os.getenv(
    'S411_FAST_PLACE', 'true'
).lower() in ('1', 'true', 'yes')
# Legacy sequential path: open S411 betslip while waiting for 4casters fill, then stake+click on ack.
S411_EXCHANGE_HEDGE_PREPOSITION = os.getenv(
    'S411_EXCHANGE_HEDGE_PREPOSITION', 'true'
).lower() in ('1', 'true', 'yes')
# Max seconds 4casters waits for S411 betslip pre-position before placing leg 1.
S411_HEDGE_PREPOSITION_WAIT_SECONDS = float(
    os.getenv('S411_HEDGE_PREPOSITION_WAIT_SECONDS', '10')
)
# One bet per book per arb pair per matchup within this window (seconds).
GAME_PAIR_BET_COOLDOWN_SECONDS = int(os.getenv('GAME_PAIR_BET_COOLDOWN_SECONDS', '3600'))
ARB_TTL_SECONDS = int(os.getenv('ARB_TTL_SECONDS', '300'))
# After first-leg placement starts, block new arb scans/placement for this many seconds.
ARB_EXECUTION_PAUSE_SECONDS = int(os.getenv('ARB_EXECUTION_PAUSE_SECONDS', '300'))
TELEGRAM_ALERTS_ASYNC = os.getenv('TELEGRAM_ALERTS_ASYNC', 'true').lower() in (
    '1', 'true', 'yes',
)
SECOND_LEG_ODDS_TOLERANCE = int(os.getenv('SECOND_LEG_ODDS_TOLERANCE', '2'))
# Wider juice tolerance when completing spread hedges (leg 1 already on book).
SPREAD_SECOND_LEG_ODDS_TOLERANCE = int(
    os.getenv('SPREAD_SECOND_LEG_ODDS_TOLERANCE', '5')
)
# BetWar: reuse My Bets rows only when explicitly enabled (scoped match; default off).
BETWAR_MY_BETS_RECOVERY = os.getenv('BETWAR_MY_BETS_RECOVERY', 'false').lower() in (
    '1', 'true', 'yes',
)


def profit_pct_to_max_total_prob(profit_pct: float) -> float:
    """Convert min profit % to max total implied probability for arb detection."""
    return 1.0 - (profit_pct / 100.0)


# Testing: allow near-miss / slightly negative ML arbs (e.g. -1.02%). Set back to 1.01 for production.
MIN_ARB_PROFIT_PCT = float(os.getenv('MIN_ARB_PROFIT_PCT', '-1.02'))
if os.getenv('ARB_MAX_TOTAL_PROB') is not None:
    ARB_MAX_TOTAL_PROB = float(os.getenv('ARB_MAX_TOTAL_PROB'))
elif MIN_ARB_PROFIT_PCT != 0:
    ARB_MAX_TOTAL_PROB = profit_pct_to_max_total_prob(MIN_ARB_PROFIT_PCT)
else:
    ARB_MAX_TOTAL_PROB = 1.0

# Spread/run-line alert threshold (same min edge as ML by default).
MIN_ARB_PROFIT_PCT_SPREAD = float(os.getenv('MIN_ARB_PROFIT_PCT_SPREAD', '1.01'))
SPREAD_REAL_MONEY_BETTING_ENABLED = os.getenv(
    'SPREAD_REAL_MONEY_BETTING_ENABLED', 'true'
).lower() in ('1', 'true', 'yes')
# Spread/run-line arb detection + Telegram alerts (independent of spread betting).
SPREAD_ARB_SCAN_ENABLED = os.getenv('SPREAD_ARB_SCAN_ENABLED', 'true').lower() in (
    '1', 'true', 'yes',
)
# One book-pair per matchup/event — blocks a second pair on the same game.
SINGLE_PAIR_PER_GAME = os.getenv('SINGLE_PAIR_PER_GAME', 'true').lower() in (
    '1', 'true', 'yes',
)
if os.getenv('ARB_MAX_TOTAL_PROB_SPREAD') is not None:
    ARB_MAX_TOTAL_PROB_SPREAD = float(os.getenv('ARB_MAX_TOTAL_PROB_SPREAD'))
else:
    ARB_MAX_TOTAL_PROB_SPREAD = profit_pct_to_max_total_prob(MIN_ARB_PROFIT_PCT_SPREAD)


def arb_max_total_prob_for_bet_type(bet_type: str) -> float:
    if (bet_type or 'moneyline').lower() == 'spread':
        return ARB_MAX_TOTAL_PROB_SPREAD
    return ARB_MAX_TOTAL_PROB


def min_arb_profit_pct_for_bet_type(bet_type: str) -> float:
    if (bet_type or 'moneyline').lower() == 'spread':
        return MIN_ARB_PROFIT_PCT_SPREAD
    return MIN_ARB_PROFIT_PCT


# Odds watch — shared defaults (ML + spread); override per-book via env if needed.
ODDS_WATCH_POLL_SECONDS = float(os.getenv("ODDS_WATCH_POLL_SEC", "1"))
ODDS_WATCH_FORCE_SCAN_SECONDS = int(os.getenv("ODDS_WATCH_FORCE_SCAN_SEC", "1"))

# Arb scanner loop delay (seconds between DB scans). Backup path when inline scan misses.
# Each scan takes ~2–3s; delay 0 runs back-to-back (fastest backup cadence).
ARB_SCAN_DELAY_SECONDS = float(os.getenv("ARB_SCAN_DELAY_SEC", "0"))

# Inline arb detection on odds persist (Redis cross-book compare, sub-100ms wake).
INLINE_ARB_SCAN_ENABLED = os.getenv("INLINE_ARB_SCAN_ENABLED", "true").lower() in (
    "1", "true", "yes",
)

# Betting loops block on Redis wake queue (ms) instead of fixed multi-second sleeps.
BET_WAKE_BLPOP_MS = int(os.getenv("BET_WAKE_BLPOP_MS", "50"))
BETTING_IDLE_POLL_SECONDS = float(os.getenv("BETTING_IDLE_POLL_SEC", "1"))

# Betamapola: Angular pick + ProcessTicket HTTP (skip DOM bet-slip UI when possible).
BETAMAPOLA_API_PLACEMENT = os.getenv("BETAMAPOLA_API_PLACEMENT", "true").lower() in (
    "1", "true", "yes",
)

# Ops health agent — staleness thresholds and remediation cooldowns.
OPS_ODDS_STALE_SECONDS = int(os.getenv("OPS_ODDS_STALE_SEC", "90"))
OPS_ARB_SCAN_STALE_SECONDS = int(os.getenv("OPS_ARB_SCAN_STALE_SEC", "30"))
OPS_REMEDIATE_COOLDOWN_SECONDS = int(os.getenv("OPS_REMEDIATE_COOLDOWN_SEC", "300"))
OPS_HEALTH_CHECK_ENABLED = os.getenv("OPS_HEALTH_CHECK_ENABLED", "true").lower() in (
    "1", "true", "yes",
)

# Spread arb sanity gates
SPREAD_ARB_MAX_PROFIT_PCT = float(os.getenv("SPREAD_ARB_MAX_PROFIT_PCT", "2.0"))
SPREAD_ODDS_MAX_AGE_SECONDS = int(os.getenv("SPREAD_ODDS_MAX_AGE_SECONDS", "600"))
SPREAD_ODDS_MAX_GAP_SECONDS = int(os.getenv("SPREAD_ODDS_MAX_GAP_SECONDS", "300"))
ACTIVE_ARB_BOOK_PAIRS = frozenset(
    frozenset(b.strip().lower() for b in part.split(":") if b.strip())
    for part in os.getenv(
        "ACTIVE_ARB_BOOK_PAIRS",
        "sports411:betamapola,paradisewager:betamapola,4casters:betamapola,betamapola:3et,"
        "sports411:paradisewager,"
        "sports411:lowvig,paradisewager:lowvig,"
        "sports411:3et,paradisewager:3et,"
        "4casters:sports411,4casters:paradisewager,"
        "4casters:lowvig,4casters:3et",
    ).split(",")
    if part.strip() and ":" in part
)
ACTIVE_ARB_BOOK_PAIR_ORDER = tuple(
    (parts[0].strip().lower(), parts[1].strip().lower())
    for part in os.getenv(
        "ACTIVE_ARB_BOOK_PAIRS",
        "sports411:betamapola,paradisewager:betamapola,4casters:betamapola,betamapola:3et,"
        "sports411:paradisewager,"
        "sports411:lowvig,paradisewager:lowvig,"
        "sports411:3et,paradisewager:3et,"
        "4casters:sports411,4casters:paradisewager,"
        "4casters:lowvig,4casters:3et",
    ).split(",")
    if part.strip() and ":" in part
    for parts in [part.strip().split(":", 1)]
    if len(parts) == 2 and parts[0].strip() and parts[1].strip()
)
ACTIVE_ARB_BOOKMAKERS = frozenset(
    book for pair in ACTIVE_ARB_BOOK_PAIRS for book in pair
)


def is_active_arb_pair(book_1: str, book_2: str) -> bool:
    b1 = (book_1 or "").strip().lower()
    b2 = (book_2 or "").strip().lower()
    if not b1 or not b2 or b1 == b2:
        return False
    return frozenset({b1, b2}) in ACTIVE_ARB_BOOK_PAIRS


def arb_pair_legs(book_1: str, book_2: str) -> tuple[str, str] | None:
    """Return (first_leg, second_leg) for an active pair, preserving env order."""
    b1 = (book_1 or "").strip().lower()
    b2 = (book_2 or "").strip().lower()
    pair = frozenset({b1, b2})
    for first, second in ACTIVE_ARB_BOOK_PAIR_ORDER:
        if frozenset({first, second}) == pair:
            return first, second
    return None


def required_first_leg_book(book_1: str, book_2: str, bookmaker: str) -> str | None:
    """When bookmaker is the configured second leg, return the first leg book to wait for."""
    legs = arb_pair_legs(book_1, book_2)
    if not legs:
        return None
    first, second = legs
    bm = (bookmaker or "").strip().lower()
    if bm == second:
        return first
    return None

# 2Captcha
TWOCAPTCHA_API_URL = os.getenv('TWOCAPTCHA_API_URL')
TWOCAPTCHA_API_KEY = os.getenv('TWOCAPTCHA_API_KEY')

# Zenrow
ZENROWS_WS_URL = os.getenv('ZENROWS_WS_URL')
ZENROWS_API_KEY = os.getenv('ZENROWS_API_KEY')

# Odds Market
ODDSMARKET_API_KEY = os.getenv('ODDSMARKET_API_KEY')
ODDSMARKET_API_PREMATCH_URL = os.getenv('ODDSMARKET_API_PREMATCH_URL')
ODDSMARKET_API_LIVE_URL = os.getenv('ODDSMARKET_API_LIVE_URL')
ODDSMARKET_WS_PREMATCH_URL = os.getenv('ODDSMARKET_WS_PREMATCH_URL')
ODDSMARKET_WS_LIVE_URL = os.getenv('ODDSMARKET_WS_LIVE_URL')

# Proxy
BRIGHTDATA_CUSTOMER = os.getenv('BRIGHTDATA_CUSTOMER', 'hl_70fad530')
BRIGHTDATA_ZONE_PASSWORD = os.getenv('BRIGHTDATA_ZONE_PASSWORD', 'truzviha7wip')
BRIGHTDATA_PROXY_ZONE = os.getenv('BRIGHTDATA_PROXY_ZONE', 'arbitrage_bot')
# Scraping Browser / Browser API zone (NOT the same as proxy zone — create in Bright Data dashboard)
BRIGHTDATA_BROWSER_ZONE = os.getenv('BRIGHTDATA_BROWSER_ZONE')


def brightdata_selenium_endpoint(zone: str = None) -> str | None:
    """Selenium Remote URL for Bright Data Browser API (port 9515)."""
    explicit = os.getenv("BRIGHTDATA_SELENIUM_URL")
    if explicit:
        return explicit
    z = zone or BRIGHTDATA_BROWSER_ZONE
    if not z or not BRIGHTDATA_ZONE_PASSWORD:
        return None
    auth = f"brd-customer-{BRIGHTDATA_CUSTOMER}-zone-{z}:{BRIGHTDATA_ZONE_PASSWORD}"
    return f"https://{auth}@brd.superproxy.io:9515"


def brightdata_cdp_endpoint(zone: str = None) -> str | None:
    """Playwright/Puppeteer CDP URL for Bright Data Browser API (port 9222)."""
    explicit = os.getenv("BRIGHTDATA_SBR_CDP")
    if explicit:
        return explicit
    z = zone or BRIGHTDATA_BROWSER_ZONE
    if not z or not BRIGHTDATA_ZONE_PASSWORD:
        return None
    auth = f"brd-customer-{BRIGHTDATA_CUSTOMER}-zone-{z}:{BRIGHTDATA_ZONE_PASSWORD}"
    return f"wss://{auth}@brd.superproxy.io:9222"


def lowvig_proxy_settings() -> dict | None:
    """HTTP proxy for LowVig (IPRoyal residential/ISP with optional sticky US session)."""
    username = os.getenv("LOWVIG_PROXY_USERNAME") or os.getenv("IPROYAL_PROXY_USERNAME")
    password = os.getenv("LOWVIG_PROXY_PASSWORD") or os.getenv("IPROYAL_PROXY_PASSWORD")
    if not username or not password:
        return None
    host = os.getenv("LOWVIG_PROXY_HOST") or os.getenv("IPROYAL_PROXY_HOST") or "geo.iproyal.com"
    port_raw = os.getenv("LOWVIG_PROXY_PORT") or os.getenv("IPROYAL_PROXY_PORT") or "12321"
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return None
    if "_country-" not in password:
        country = os.getenv("LOWVIG_PROXY_COUNTRY", "us")
        session = os.getenv("LOWVIG_PROXY_SESSION", "lowvig01")
        lifetime = os.getenv("LOWVIG_PROXY_SESSION_LIFETIME", "30m")
        session = "".join(c for c in session if c.isalnum())[:8].ljust(8, "0")
        password = f"{password}_country-{country}_session-{session}_lifetime-{lifetime}"
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


PROXY1 = {
    'host': os.getenv('PROXY1_HOST'),
    'port': os.getenv('PROXY1_PORT')
}

PROXY2 = {
    'host': os.getenv('PROXY2_HOST'),
    'port': os.getenv('PROXY2_PORT'),
    'username': os.getenv('PROXY2_USERNAME'),
    'password': os.getenv('PROXY2_PASSWORD')
}

PROXY3 = {
    'host': os.getenv('PROXY3_HOST'),
    'port': os.getenv('PROXY3_PORT'),
    'username': os.getenv('PROXY3_USERNAME'),
    'password': os.getenv('PROXY3_PASSWORD')
}

# Log
LOG_DIR = os.getenv('LOG_DIR')

########## Websites ##########
LOOSELINES = {
    'website': 'looselines.ag',
    'url': 'https://looselines.ag',
    'bookmaker': 'looselines'
}

BETAMAPOLA = {
    'website': 'betamapola.com',
    'url': 'https://betamapola.com',
    'bookmaker': 'betamapola'
}

BETAMAPOLA_ACCOUNT = os.getenv('BETAMAPOLA_ACCOUNT')
BETAMAPOLA_PASSWORD = os.getenv('BETAMAPOLA_PASSWORD')
BETAMAPOLA_LABEL = os.getenv('BETAMAPOLA_LABEL', 'Bettor')

LOWVIG = {
    'website': 'lowvig.ag',
    'url': 'https://www.lowvig.ag',
    'bookmaker': 'lowvig',
}

LOWVIG_ACCOUNT = os.getenv('LOWVIG_ACCOUNT')
LOWVIG_PASSWORD = os.getenv('LOWVIG_PASSWORD')
LOWVIG_LABEL = os.getenv('LOWVIG_LABEL', 'Bettor')

PARADISEWAGER = {
    'website': 'paradisewager.com',
    'url': 'https://paradisewager.com',
    'bookmaker': 'paradisewager'
}

# Backward-compatible alias (typo in original config)
PRADISEWAGER = PARADISEWAGER

PARADIESWAGER_ACCOUNT = os.getenv('PARADIESWAGER_ACCOUNT')
PARADIESWAGER_PASSWORD = os.getenv('PARADIESWAGER_PASSWORD')
PARADIESWAGER_LABEL = os.getenv('PARADIESWAGER_LABEL', 'Bettor')

_4CASTERS = {
    'website': '4casters.io',
    'url': 'https://api.4casters.io',
    'bookmaker': '4casters'
}
FOURCASTERS = _4CASTERS

FOURCASTERS_ACCOUNT = os.getenv('FOURCASTERS_ACCOUNT')
FOURCASTERS_PASSWORD = os.getenv('FOURCASTERS_PASSWORD')
FOURCASTERS_LABEL = os.getenv('FOURCASTERS_LABEL', 'Bettor')
FOURCASTERS_API_BASE = os.getenv('FOURCASTERS_API_BASE', 'https://api.4casters.io')
FOURCASTERS_MLB_LEAGUE = os.getenv('FOURCASTERS_MLB_LEAGUE', 'MLB')
# Fixed tick haircut on gross API orderbook odds → net scanner odds (4cast UI commission).
FOURCASTERS_SCAN_ODDS_TICK_HAIRCUT = int(os.getenv('FOURCASTERS_SCAN_ODDS_TICK_HAIRCUT', '3'))

PLATINUMWAGER = {
    'website': 'platinumwager.com',
    'url': 'https://platinumwager.com',
    'bookmaker': 'platinumwager'
}

SPORTS411 = {
    'website': 'sports411.ag',
    'url': 'https://www.sports411.ag',
    'bookmaker': 'sports411'
}

PINNACLE = {
    'website': 'pinnacle.com',
    'url': 'https://guest.api.arcadia.pinnacle.com',
    'bookmaker': 'pinnacle'
}

POLYMARKET = {
    'website': 'polymarket.com',
    'url': 'https://polymarket.com',
    'bookmaker': 'polymarket',
    'telegram': {
        'default': '-5148480944',
        '10K': '-1003723757640',
    },
    'users': [
        {'label': 'ST', 'user': '0x492442eab586f242b53bda933fd5de859c8a3782'},
        {'label': 'AP', 'user': '0x37c1874a60d348903594a96703e0507c518fc53a'},
        {'label': 'BeachBoy', 'user': '0xc2e7800b5af46e6093872b177b7a5e7f0563be51'},
        {'label': 'Moon', 'user': '0xbddf61af533ff524d27154e589d2d7a81510c684'},
        {'label': 'Moon2', 'user': '0x8a3aB8120807bD64a3De48695110e390fa2ceB9a'},
        {'label': 'Gator', 'user': '0x93abbc022ce98d6f45d4444b594791cc4b7a9723'},
        {'label': 'Talvez', 'user': '0xa71093cafc0c099b4ccab24c3cb8018d817923c4'},
    ],
}
POLYMARKET_GAMMA_API_URL = os.getenv(
    'POLYMARKET_GAMMA_API_URL', 'https://gamma-api.polymarket.com'
)
POLYMARKET_CLOB_HOST = os.getenv('POLYMARKET_CLOB_HOST', 'https://clob.polymarket.com')
POLYMARKET_CHAIN_ID = int(os.getenv('POLYMARKET_CHAIN_ID', '137'))
POLYMARKET_MLB_TAG_ID = int(os.getenv('POLYMARKET_MLB_TAG_ID', '100381'))
POLYMARKET_PRIVATE_KEY = os.getenv('POLYMARKET_PRIVATE_KEY')
POLYMARKET_FUNDER_ADDRESS = os.getenv('POLYMARKET_FUNDER_ADDRESS')
POLYMARKET_SIGNATURE_TYPE = int(os.getenv('POLYMARKET_SIGNATURE_TYPE', '3'))
POLYMARKET_RELAYER_API_KEY = os.getenv('POLYMARKET_RELAYER_API_KEY')
POLYMARKET_RELAYER_API_KEY_ADDRESS = os.getenv(
    'POLYMARKET_RELAYER_API_KEY_ADDRESS',
    '0x35C8180822f948F2b7Cf9e78514F5bA8F1A21B51',
)
POLYMARKET_MAX_HOURS_AHEAD = int(os.getenv('POLYMARKET_MAX_HOURS_AHEAD', '48'))

# Web1
IMRUINED = {
    'website': 'imruined.ag',
    'url': 'https://imruined.ag',
    'bookmaker': 'imruined'
}
SHIBA = {
    'website': 'shiba.ag',
    'url': 'https://shiba.ag',
    'bookmaker': 'shiba'
}

# Web2
_1ABSOLUTEWAGER = {
    'website': '1absolutewager.com',
    'url': 'https://1absolutewager.com',
    'bookmaker': '1absolutewager'
}

_555DIMES = {
    'website': '555dimes.com',
    'url': 'https://555dimes.com',
    'bookmaker': '555dimes'
}

_1BETVEGAS = {
    'website': '1betvegas.com',
    'url': 'https://1betvegas.com',
    'bookmaker': '1betvegas'
}

ACTION212 = {
    'website': 'action212.com',
    'url': 'https://action212.com',
    'bookmaker': 'action212'
}

# Web3
_608REDZONE = {
    'website': '608redzone.com',
    'url': 'https://608redzone.com',
    'bookmaker': '608redzone'
}

WAGERWIZARD = {
    'website': 'wagerwizard.ag',
    'url': 'https://wagerwizard.ag',
    'bookmaker': 'wagerwizard'
}

ROUNDERS = {
    'website': 'rounders.ag',
    'url': 'https://rounders.ag',
    'bookmaker': 'rounders'
}

B21 = {
    'website': 'b21.ag',
    'url': 'https://b21.ag',
    'bookmaker': 'b21'
}

SUGERFREESPORTS = {
    'website': 'sugarfreesports.com',
    'url': 'https://sugarfreesports.com',
    'bookmaker': 'sugarfreesports'
}


_3BETZ = {
    'website': '3betz.com',
    'url': 'https://3betz.com',
    'bookmaker': '3betz'
}

WISCONSINWAGER = {
    'website': 'wisconsinwager.com',
    'url': 'https://wisconsinwager.com',
    'bookmaker': 'wisconsinwager'
}

# Web4
BETPORKY = {
    'website': 'betporky.com',
    'url': 'https://betporky.com',
    'bookmaker': 'betporky'
}

ONLYBETS = {
    'website': 'onlybets.ag',
    'url': 'https://onlybets.ag',
    'bookmaker': 'onlybets'
}

GAMEON39 = {
    'website': 'gameon39.com',
    'url': 'https://gameon39.com',
    'bookmaker': 'gameon39'
}

EZPLAY247 = {
    'website': 'ezplay247.com',
    'url': 'https://ezplay247.com',
    'bookmaker': 'ezplay247'
}

BLING99 = {
    'website': 'bling99.com',
    'url': 'https://bling99.com',
    'bookmaker': 'bling99'
}

REDDOG77 = {
    'website': 'reddog77.com',
    'url': 'https://reddog77.com',
    'bookmaker': 'reddog77'
} # DEPRECATED - URL CHANGED TO BETLIMON

BACKSTREETBETS = {
    'website': 'backstreetbets.com',
    'url': 'https://backstreetbets.com',
    'bookmaker': 'backstreetbets'
} # DEPRECATED - URL CHANGED TO BETLIMON

SQUARE999 = {
    'website': 'square999.com',
    'url': 'https://square999.com',
    'bookmaker': 'square999'
}

# Web5
BET487 = {
    'website': 'bet487.org',
    'url': 'https://www.bet487.org',
    'bookmaker': 'bet487'
}

PROBET42 = {
    'website': 'probet42.com',
    'url': 'https://www.probet42.com',
    'bookmaker': 'probet42'
}

# Web6
_807WAGERS = {
    'website': '807wagers.com',
    'url': 'https://807wagers.com',
    'bookmaker': '807wagers'
}

_4QUARTERZ = {
    'website': '4quarterz.me',
    'url': 'https://4quarterz.me',
    'bookmaker': '4quarterz'
}

# Web7
WORMSTIFFEDME = {
    'website': 'wormstiffedme.com',
    'url': 'https://wormstiffedme.com',
    'bookmaker': 'worms'
}

BETTORPRICE = {
    'website': 'bettorprice.com',
    'url': 'https://bettorprice.com',
    'bookmaker': 'bettorprice'
}

# Web8
BETWAR = {
    'website': 'betwar.com',
    'url': 'https://betwar.com',
    'bookmaker': 'betwar'
}

BETWAR_ACCOUNT = os.getenv('BETWAR_ACCOUNT')
BETWAR_PASSWORD = os.getenv('BETWAR_PASSWORD')
BETWAR_LABEL = os.getenv('BETWAR_LABEL', 'Bettor')

THREEET = {
    'website': '3et.com',
    'url': 'https://www.3et.com',
    'bookmaker': '3et',
}

THREEET_ACCOUNT = os.getenv('THREEET_ACCOUNT')
THREEET_PASSWORD = os.getenv('THREEET_PASSWORD')
THREEET_LABEL = os.getenv('THREEET_LABEL', 'Bettor')
THREEET_API_BASE = os.getenv('THREEET_API_BASE', 'https://sports.3et.com')
THREEET_MLB_COMPETITION_ID = int(os.getenv('THREEET_MLB_COMPETITION_ID', '4309'))

EBET2 = {
    'website': 'ebet2.com',
    'url': 'https://ebet2.com',
    'bookmaker': 'ebet2'
}

# Web9
BETLIMON = {
    'website': 'betlimon.com',
    'url': 'https://betlimon.com',
    'bookmaker': 'betlimon'
}

SHARP = {
    'website': 'sharp.ag',
    'url': 'https://myaccount.sharp.ag',
    'bookmaker': 'sharp'
}

# Web10
LOVE23 = {
    'website': 'love23.bet',
    'url': 'https://love23.bet',
    'bookmaker': 'love23'
}

PLAYBETNOW = {
    'website': 'playbetnow.com',
    'url': 'https://playbetnow.com',
    'bookmaker': 'playbetnow'
}

GOBETYA = {
    'website': 'gobetya.net',
    'url': 'https://gobetya.net',
    'bookmaker': 'gobetya'
}

# Web11
ACE23 = {
    'website': 'ace23.ag',
    'url': 'https://backend.ace23.ag',
    'bookmaker': 'ace23'
}

BESTBETS = {
    'website': 'bestbets.vip',
    'url': 'https://play.bestbets.vip',
    'bookmaker': 'bestbets'
}