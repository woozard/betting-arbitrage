import os
import time
import traceback
import requests
from time import sleep
import random
import configparser
from datetime import datetime, timedelta
from decimal import Decimal
import pytz
import asyncio
import tracemalloc
from utils.config import TELEGRAM, LOG_DIR


# engine = create_engine('mysql://root@localhost/lockz')
# metadata = MetaData()
config = configparser.ConfigParser()

authorization = None
config.read("constants.ini")

# Create a request session
session = requests.Session()
    
def get_session():
    return session

def random_sleep(min_time=2, max_time=5):
    sleep_time = random.uniform(min_time, max_time)
    sleep(sleep_time)


def get_debug_dir():
    """Directory for HTML/JSON debug artifacts (under logs/debug, not project root)."""
    debug_dir = os.path.join(LOG_DIR or "logs", "debug")
    os.makedirs(debug_dir, exist_ok=True)
    return debug_dir


def debug_filepath(name: str) -> str:
    """Build a timestamped path under logs/debug/. Pass name without extension."""
    return os.path.join(get_debug_dir(), f"{name}_{int(time.time())}.html")


def prune_debug_files(max_age_hours: int = 24):
    """Remove debug artifacts older than max_age_hours from logs/debug/."""
    debug_dir = get_debug_dir()
    cutoff = time.time() - max_age_hours * 3600
    try:
        for fname in os.listdir(debug_dir):
            path = os.path.join(debug_dir, fname)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception:
        pass


def parse_game_datetime(value):
    """Parse game_datetime from str or datetime; returns naive UTC datetime or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(text[:19], fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(pytz.UTC).replace(tzinfo=None)
    return dt


def is_game_pregame(game_datetime) -> bool:
    """Return True when game_datetime is in the future (game has not started yet)."""
    dt = parse_game_datetime(game_datetime)
    if dt is None:
        return False
    return dt > datetime.utcnow()

def run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop → safe to use asyncio.run
        return asyncio.run(coro)
    else:
        # Already in an event loop → schedule task
        return asyncio.create_task(coro)

def set_authorization(token):
    config.set("Authorization", "token", "Bearer {}".format(token))


def get_authorization():
    return config.get("Authorization", "token")


def get_csv_headers():
    return [
        "Book Ticket ID",
        "Game #",
        "Team 1",
        "Team 2",
        "Bet Type",
        "Odds",
        "Risk",
        "Win",
        "Status",
        "Final Score",
        "Accepted",
        "Placed On",
        "Sport",
        "Period",
        "Date",
        "Time",
        "Timezone",
        "MASTER BET ID",
        "WORKER",
        "WORKER FREEROLL %",
        "HANDICAPPER",
        "HANDICAPPER RISK %",
        "PLAY TYPE",
        "AGENT ID",
        "SUB AGENT ID",
        "BOOK ID",
        "MASTER AGENT RISK %",
        "SUB AGENT RISK %",
        "SUB AGENT FREEROLL %",
        "MASTER AGENT FREEROLL %",
        "NOTES",
        "UPLOAD FILES (PHOTO/VIDEO)",
    ]


def get_last_5_weeks_dates(format):
    # Get the current date and time
    now = datetime.now()
    # Create a list to store the dates
    last_5_weeks_dates = []
    # Get the start of the week (Monday) for the current date
    start_of_week = now - timedelta(days=now.weekday())
    for i in range(7):
        # Get the start and end date of the current week
        start_date = start_of_week - timedelta(days=7 * i)
        end_date = start_date + timedelta(days=6)

        # Loop through the dates in the current week
        current_date = start_date
        while current_date <= end_date:
            last_5_weeks_dates.append(current_date.strftime(format))
            current_date += timedelta(days=1)

    return last_5_weeks_dates


def get_last_2_weeks_dates(format):
    # Get the current date and time
    now = datetime.now()
    # Create a list to store the dates
    last_2_weeks_dates = []
    # Get the start of the week (Monday) for the current date
    start_of_week = now - timedelta(days=now.weekday())
    for i in range(7):
        # Get the start and end date of the current week
        start_date = start_of_week - timedelta(days=7 * i)
        end_date = start_date + timedelta(days=6)

        # Loop through the dates in the current week
        current_date = start_date
        while current_date <= end_date:
            last_2_weeks_dates.append(current_date.strftime(format))
            current_date += timedelta(days=1)

    return last_2_weeks_dates


def get_constants():
    config = configparser.ConfigParser()
    config.read("constants.ini")
    return config


def get_credentials():
    # credentials = {
    #     'customerID': 'doc1105',
    #     'password': '**********',
    #     'login.x': '42',
    #     'login.y': '5'
    # }
    # customer_id = 'tv111'
    # password = '**********'
    # login_x = '42'
    # login_y = '5'
    customer_id = "doc1105"
    password = "**********"
    login_x = "42"
    login_y = "5"
    return customer_id, password, login_x, login_y


def get_headers():
    headers = {
        "Host": "lockz.co",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/111.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": "56",
        "Origin": "https://lockz.co",
        "Connection": "keep-alive",
        "Referer": "https://lockz.co/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    return headers

def determine_wager_on_spread(spread, wager_on=None):
    """
    Process the spread to remove prefixes like u, o, under, or over
    and determine the value of wager_on.

    Args:
        spread (str): The original spread value.
        wager_on (str, optional): The initial wager_on value. Defaults to None.

    Returns:
        tuple: A tuple containing the cleaned spread and wager_on value.
    """
    spread = str(spread).strip().lower()  # Normalize the spread string

    # Check and process prefix
    if spread.startswith(("u", "under")):
        wager_on = "UNDER"
        spread = spread.removeprefix("under").removeprefix("u").strip()
    elif spread.startswith(("o", "over")):
        wager_on = "OVER"
        spread = spread.removeprefix("over").removeprefix("o").strip()

    return spread, wager_on

def format_website(url):
    # Remove http, https, and www
    website = url.replace('http://', '').replace('https://', '').replace('www.', '')
    return website.split('/')[0]  # Return only the domain


def format_utc_timestamp(ts=None) -> str:
    """Format a Unix timestamp (or now) for Telegram/log alerts."""
    from datetime import datetime, timezone

    if ts is None:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

async def send_telegram_alert(alert, chat_id = None) -> None:
    tracemalloc.start()
    try:
        from telegram import Bot
        token = TELEGRAM.get('bot_token')
        if not token:
            print("Telegram alerts disabled (no bot_token) - skipping")
            return
        chat_id = chat_id or TELEGRAM.get('chat_id')
        if not chat_id:
            print("No chat_id for telegram alert - skipping")
            return
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=alert)
    except Exception as e:
        print(f"Telegram alert failed (non-fatal) for chat_id={chat_id}: {e}")

async def send_monitoring_alert(website, account, ex, chat_id = None) -> None:
    tracemalloc.start()
    try:
        from telegram import Bot
        token = TELEGRAM.get('bot_token')
        if not token:
            print("Telegram monitoring disabled (no bot_token) - skipping alert for error")
            return
        chat_id = chat_id or TELEGRAM.get('monitoring')
        if not chat_id:
            print("No monitoring chat_id - skipping telegram error alert")
            return
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        exception_msg = f"{str(ex)}"
        tb_list = traceback.format_exception(type(ex), ex, ex.__traceback__, limit=3)
        trace = "".join(tb_list)
        
        if len(exception_msg) + len(trace) > 3600:
            exception_msg = exception_msg[:1800]
            trace = trace[:1800] + "... [truncated]"

        alert = (
            f"Website: {website}\n"
            f"Account: {account}\n" 
            f"Timestamp: {timestamp}\n"
            f"Exception: {exception_msg}\n"
            f"Traceback:\n{trace}"
        )
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=alert)
    except Exception as e:
        print(f"Telegram monitoring alert failed (non-fatal) for chat_id={chat_id}: {e}")


async def send_testing_alert(alert, chat_id = None) -> None:
    tracemalloc.start()
    try:
        from telegram import Bot
        token = TELEGRAM.get('bot_token')
        if not token:
            print("Telegram testing disabled (no bot_token) - skipping")
            return
        chat_id = chat_id or TELEGRAM.get('testing')
        if not chat_id:
            print("No testing chat_id - skipping")
            return
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=alert)
    except Exception as e:
        print(f"Telegram testing alert failed (non-fatal) for chat_id={chat_id}: {e}")


async def discover_telegram_chats() -> None:
    """Utility to discover chat IDs the bot can send to.

    Consistent with colleague feedback: create a new group chat, add the bot
    (kleyman_arb_bot) + the people who should receive arb alerts, send a test
    message in the group, then run this to get the numeric chat ID (required for
    groups; the bot must be a member).

    Run with:
        python -c '
        import asyncio
        from utils.helpers import discover_telegram_chats
        asyncio.run(discover_telegram_chats())
        '

    Paste the printed Chat ID into .env:
      TELEGRAM_CHAT_ARBITRAGE=-100...   (===== Arbitrage ===== alerts)
      TELEGRAM_CHAT_BETTING=-100...     (===== Moneyline Bet ===== alerts)
    Groups/channels use large negative IDs. Then restart the service.

    The bot created for this project is kleyman_arb_bot (token goes in TELEGRAM_BOT_TOKEN).

    See TELEGRAM_SETUP.md (in the repo root) for the full tailored instructions, including the exact steps
    after you received the BotFather token screenshot.
    """
    tracemalloc.start()
    try:
        from telegram import Bot
        token = TELEGRAM.get('bot_token')
        if not token:
            print("No TELEGRAM_BOT_TOKEN - cannot discover chats")
            return
        bot = Bot(token=token)
        updates = await bot.get_updates(limit=100, timeout=5)
        if not updates:
            print("No recent updates seen by the bot.")
            print("Steps:")
            print("  1. Create the Telegram group (or use existing).")
            print("  2. Add the bot by searching for its username: kleyman_arb_bot")
            print("  3. Add the recipient(s) to the group.")
            print("  4. Post a message in the group from one of the members.")
            print("  5. Re-run this discover function.")
            return
        seen = {}
        for update in updates:
            msg = update.message or update.channel_post or update.edited_channel_post
            if not msg or not msg.chat:
                continue
            chat = msg.chat
            cid = chat.id
            if cid in seen:
                continue
            seen[cid] = True
            name = chat.title or chat.username or (f"{chat.first_name or ''} {chat.last_name or ''}".strip()) or "unnamed"
            print(f"Chat ID: {cid}")
            print(f"  Type: {chat.type}")
            print(f"  Name/Title: {name}")
            print(f"  Suggested .env lines:")
            print(f"    TELEGRAM_CHAT_ARBITRAGE={cid}  # arb opportunity alerts")
            print(f"    TELEGRAM_CHAT_BETTING={cid}    # confirmed moneyline bet alerts")
            print()
        if not seen:
            print("No chats with messages found in updates.")
    except Exception as e:
        print(f"discover_telegram_chats failed (non-fatal): {e}")


# -----------------------------------
# Numbers
# -----------------------------------
def currency_to_float(currency):
    """
    Convert a currency to a float.
    Handles:
    - Dollar signs ($)
    - Commas as thousand separators
    - Parentheses for negative values (accounting format)
    - Negative signs
    - Whitespace
    
    Args:
        currency (str): The currency string to convert (e.g., "$-69,170.00")
    
    Returns:
        float: The numeric value
    
    Raises:
        ValueError: If the string cannot be converted to a float
    """
    # Remove all non-numeric characters except digits, minus sign, and decimal point
    currency = str(currency)
    cleaned = (currency.replace('(', '-')  # Replace opening parenthesis with minus
               .replace(')', '')  # Remove closing parenthesis
               .replace('$', '')  # Remove dollar signs
               .replace(',', '')  # Remove thousand separators
               .strip())  # Remove any whitespace
    
    try:
        return float(cleaned)
    except ValueError as e:
        raise ValueError(f"Could not convert string '{currency}' to float") from e

def to_decimal(value):
    if value is None or value == "":
        return None
    return Decimal(str(value))

def format_money(amount, symbol="$", decimals=2):
    """
    Format a numeric value as a currency string.
    
    Args:
        amount (float): The numeric value to format
        symbol (str): The currency symbol to prepend (default: "$")
        decimals (int): Number of decimal places (default: 2)
    
    Returns:
        str: Formatted currency string, e.g., "$1,234.56"
    """
    try:
        formatted = f"{amount:,.{decimals}f}"  # Adds commas and fixes decimals
        return f"{symbol}{formatted}"
    except Exception as e:
        raise ValueError(f"Could not format value '{amount}' as currency") from e

# -----------------------------------
# Date
# -----------------------------------
def parse_to_mysql_datetime(date_str, time_str=None, tz_name=None):
    """
    Parse date and time strings to MySQL DATETIME format.

    Supports flexible calling:
      - parse_to_mysql_datetime("12/16", "17:30")
      - parse_to_mysql_datetime("12/16/2026 17:30")
      - parse_to_mysql_datetime("2026-06-01 18:40:00")
      - parse_to_mysql_datetime("06/01/2026 18:40")

    If tz_name is provided (e.g. 'US/Eastern'), the parsed datetime is treated as
    local time in that timezone, then converted to UTC before returning the string.
    This ensures consistent game_datetime across bookmakers with different TZ displays.

    Args:
        date_str: Date part or combined "date time" string
        time_str: Optional time part
        tz_name: Optional pytz timezone name for the source time (e.g. 'US/Eastern', 'US/Pacific')

    Returns:
        String in MySQL DATETIME format (YYYY-MM-DD HH:MM:SS) in UTC if tz_name given, else naive as parsed.
    """
    try:
        if time_str is None and date_str:
            s = str(date_str).strip()
            if ' ' in s:
                # combined "MM/DD/YYYY HH:MM[:SS]" or "YYYY-MM-DD HH:MM[:SS]"
                date_part, time_part = s.split(' ', 1)
                date_str = date_part
                time_str = time_part
            else:
                time_str = "00:00"

        if tz_name:
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
        else:
            now = datetime.now()
        current_year = now.year
        current_date = now

        # Parse date (support MM/DD, MM/DD/YYYY, YYYY-MM-DD)
        year = None
        month = day = None
        ds = str(date_str).strip()
        if '/' in ds:
            dparts = [p for p in ds.split('/') if p]
            if len(dparts) == 3:
                month, day, yr = int(dparts[0]), int(dparts[1]), int(dparts[2])
                if yr < 100:
                    yr += 2000
                year = yr
            elif len(dparts) == 2:
                month, day = int(dparts[0]), int(dparts[1])
        elif '-' in ds:
            dparts = [p for p in ds.split('-') if p]
            if len(dparts) == 3:
                year, month, day = int(dparts[0]), int(dparts[1]), int(dparts[2])

        if month is None or day is None:
            month, day = 1, 1

        if year is None:
            year = current_year
            game_date = datetime(year, month, day)
            # Handle year rollover for short dates
            if game_date.month == 1 and current_date.month == 12:
                game_date = game_date.replace(year=year + 1)
            elif game_date < current_date - timedelta(days=30):
                game_date = game_date.replace(year=year + 1)
        else:
            game_date = datetime(year, month, day)

        # Parse time (handle "7:10", "19:10", "7:10 PM", "19:10:00")
        time_str_lower = str(time_str).lower().strip()
        is_pm = 'pm' in time_str_lower
        is_am = 'am' in time_str_lower

        if is_pm or is_am:
            time_clean = time_str_lower.replace('am', '').replace('pm', '').strip()
            tparts = time_clean.split(':')
            hour = int(tparts[0])
            minute = int(tparts[1]) if len(tparts) > 1 else 0
            if is_pm and hour != 12:
                hour += 12
            elif is_am and hour == 12:
                hour = 0
        else:
            tparts = time_str_lower.split(':')
            hour = int(tparts[0])
            minute = int(tparts[1]) if len(tparts) > 1 else 0

        final_datetime = game_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if tz_name:
            tz = pytz.timezone(tz_name)
            dt_local = tz.localize(final_datetime)
            final_datetime = dt_local.astimezone(pytz.UTC).replace(tzinfo=None)

        return final_datetime.strftime('%Y-%m-%d %H:%M:%S')

    except Exception as e:
        print(f"Error converting to MySQL datetime: {date_str} {time_str} - {e}")
        return None


def epoch_to_mysql_datetime(epoch_time, is_milliseconds=False):
    """
    Convert epoch time to MySQL DATETIME format.
    
    Args:
        epoch_time: Unix timestamp (int or float)
        is_milliseconds: True if epoch_time is in milliseconds, False for seconds
    
    Returns:
        String in MySQL DATETIME format (YYYY-MM-DD HH:MM:SS)
    """
    try:
        if is_milliseconds:
            # Convert milliseconds to seconds
            timestamp = epoch_time / 1000.0
        else:
            timestamp = float(epoch_time)
        
        # Convert to datetime object
        dt = datetime.fromtimestamp(timestamp)
        
        # Format as MySQL DATETIME
        return dt.strftime('%Y-%m-%d %H:%M:%S')
        
    except Exception as e:
        print(f"Error converting epoch {epoch_time} to MySQL datetime: {e}")
        return None

# -----------------------------------
# Odds
# -----------------------------------
def detect_odds_type(odds):
    odds = float(odds)

    if odds >= 1.01 and odds < 10:
        return "decimal"
    if odds <= -100 or odds >= 100:
        return "american"

    raise ValueError(f"Cannot detect odds type: {odds}")

def decimal_to_american(decimal_odds, precision=0):
    """
    Convert Decimal odds to American odds
    precision: decimals to round American odds (0 = sportsbook standard)
    """
    d = float(decimal_odds)

    if d < 1:
        raise ValueError("Decimal odds must be >= 1.0")

    if d >= 2:
        american = (d - 1) * 100
    else:
        american = -100 / (d - 1)

    return round(american, precision)

def american_to_decimal(american_odds, precision=3):
    """
    Convert American odds to Decimal odds
    precision: decimals to round Decimal odds
    """
    a = float(american_odds)

    if a == 0:
        raise ValueError("American odds cannot be 0")

    if a > 0:
        decimal = (a / 100) + 1
    else:
        decimal = (100 / abs(a)) + 1

    return round(decimal, precision)

def american_to_probability(odds):
    odds = float(odds)
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def decimal_to_probability(odds):
    return 1 / float(odds)


def implied_probability(odds):
    odds_type = detect_odds_type(odds)
    return (
        american_to_probability(odds)
        if odds_type == "american"
        else decimal_to_probability(odds)
    )

def odds_equal(odds1, odds2, tolerance=1e-6):
    """
    Returns True if odds represent the same implied probability
    tolerance handles rounding differences between bookmakers
    """
    p1 = implied_probability(odds1)
    p2 = implied_probability(odds2)

    return abs(p1 - p2) <= tolerance


def is_plausible_moneyline_pair(ml_1, ml_2) -> bool:
    """Reject obvious scrape glitches for 2-way American moneylines."""
    try:
        a = float(ml_1)
        b = float(ml_2)
    except (TypeError, ValueError):
        return False
    if a == 0.0 or b == 0.0:
        return False
    # One side must be the underdog (+) and the other the favorite (-).
    return (a > 0 and b < 0) or (a < 0 and b > 0)

def parse_odds(payload: dict) -> list[dict]:
    rows = []

    for match in payload.get("matches", []):
        base = {
            "sport": match["sport"],
            "league": match["league"],
            "game_id": match["game_id"],
            "game_datetime": datetime.strptime(
                match["game_datetime"], "%Y-%m-%d %H:%M:%S"
            ),
            "team_1": match["team_1"],
            "team_2": match["team_2"],
            "bookmaker": match["bookmaker"],
        }

        # -------------------
        # MONEYLINE
        # -------------------
        if "moneyline" in match:
            rows.append({
                **base,
                "bet_type": "moneyline",
                "moneyline_team_1": to_decimal(match["moneyline"].get("team_1")),
                "moneyline_team_2": to_decimal(match["moneyline"].get("team_2")),
                "moneyline_draw": None,
                "spread_team_1": None,
                "spread_team_2": None,
                "spread_value": None,
                "total_points": None,
                "over_odds": None,
                "under_odds": None,
            })

        # -------------------
        # SPREAD
        # -------------------
        if "spread" in match:
            spread = match["spread"]
            rows.append({
                **base,
                "bet_type": "spread",
                "moneyline_team_1": None,
                "moneyline_team_2": None,
                "moneyline_draw": None,
                "spread_team_1": to_decimal(spread.get("team_1_odds")),
                "spread_team_2": to_decimal(spread.get("team_2_odds")),
                "spread_value": to_decimal(spread.get("team_1_spread")),
                "total_points": None,
                "over_odds": None,
                "under_odds": None,
            })

        # -------------------
        # TOTAL
        # -------------------
        if "total" in match:
            total = match["total"]
            rows.append({
                **base,
                "bet_type": "total",
                "moneyline_team_1": None,
                "moneyline_team_2": None,
                "moneyline_draw": None,
                "spread_team_1": None,
                "spread_team_2": None,
                "spread_value": None,
                "total_points": to_decimal(total.get("over_total")),
                "over_odds": to_decimal(total.get("over_odds")),
                "under_odds": to_decimal(total.get("under_odds")),
            })

    return rows



