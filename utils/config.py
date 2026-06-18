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

# Telegram
TELEGRAM = {
    'bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
    'chat_id': os.getenv('TELEGRAM_CHAT_ID'),
    'monitoring': os.getenv('TELEGRAM_CHAT_MONITORING'),
    'testing': os.getenv('TELEGRAM_CHAT_TESTING'),
    'arbitrage': os.getenv('TELEGRAM_CHAT_ARBITRAGE'),
    'betting': os.getenv('TELEGRAM_CHAT_BETTING'),
    'arbitrage_monitoring': os.getenv('TELEGRAM_CHAT_ARBITRAGE_MONITORING'),
    'ops': os.getenv('TELEGRAM_CHAT_OPS'),
}

# Betting
BET_STAKE = float(os.getenv('BET_STAKE', '20'))
SEQUENTIAL_ARB_BETTING = os.getenv('SEQUENTIAL_ARB_BETTING', 'true').lower() in (
    '1', 'true', 'yes',
)
MIN_ARB_PROFIT_PCT = float(os.getenv('MIN_ARB_PROFIT_PCT', '0'))
if os.getenv('ARB_MAX_TOTAL_PROB') is not None:
    ARB_MAX_TOTAL_PROB = float(os.getenv('ARB_MAX_TOTAL_PROB'))
elif MIN_ARB_PROFIT_PCT != 0:
    ARB_MAX_TOTAL_PROB = 1.0 - (MIN_ARB_PROFIT_PCT / 100.0)
else:
    ARB_MAX_TOTAL_PROB = 1.0
ACTIVE_ARB_BOOK_PAIRS = frozenset(
    frozenset(b.strip().lower() for b in part.split(":") if b.strip())
    for part in os.getenv(
        "ACTIVE_ARB_BOOK_PAIRS",
        "sports411:betamapola,paradisewager:betamapola,sports411:paradisewager,"
        "sports411:betwar,betamapola:betwar,paradisewager:betwar",
    ).split(",")
    if part.strip() and ":" in part
)
ACTIVE_ARB_BOOK_PAIR_ORDER = tuple(
    (parts[0].strip().lower(), parts[1].strip().lower())
    for part in os.getenv(
        "ACTIVE_ARB_BOOK_PAIRS",
        "sports411:betamapola,paradisewager:betamapola,sports411:paradisewager,"
        "sports411:betwar,betamapola:betwar,paradisewager:betwar",
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
    'url': 'https://data-api.polymarket.com',
    'bookmaker': 'polymarket',
    'telegram': {
        'default': '-5148480944',
        '10K': '-1003723757640',
    },
    'users': [
        {
            'label': 'ST',
            'user': '0x492442eab586f242b53bda933fd5de859c8a3782',
        },
        {
            'label': 'AP',
            'user': '0x37c1874a60d348903594a96703e0507c518fc53a',
        },
        {
            'label': 'BeachBoy',
            'user': '0xc2e7800b5af46e6093872b177b7a5e7f0563be51',
        },
        {
            'label': 'Moon',
            'user': '0xbddf61af533ff524d27154e589d2d7a81510c684',
        },
        {
            'label': 'Moon2',
            'user': '0x8a3aB8120807bD64a3De48695110e390fa2ceB9a',
        },
        {
            'label': 'Gator',
            'user': '0x93abbc022ce98d6f45d4444b594791cc4b7a9723',
        },
        {
            'label': 'Talvez',
            'user': '0xa71093cafc0c099b4ccab24c3cb8018d817923c4',
        },
    ]
}

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