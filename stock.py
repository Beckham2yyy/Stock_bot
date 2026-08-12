import sqlite3
import time
import traceback
import os
import json
import math
import threading
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# =========================
# CONFIG
# =========================

FINNHUB_API_KEY = "d6p7d41r01qk3chj7i00d6p7d41r01qk3chj7i0g"
TWELVE_API_KEY = "536665a15d214e48a622c80eff1bfa88"
COMMODITY_API_KEY = "81b66f88-22a3-4317-aff7-40d3ee221c70"
ALPHA_VANTAGE_KEY = "2HUZXG0RQSLXVQSZ"
OILPRICE_API_KEY = "71a7c209df5f57d072367f4a09d9985ebcc5e3ed2bbe52e687c007dd23926d6c"
FIXER_API_KEY = "70820ab44387be352ff27fed8e85116d"

TELEGRAM_BOT_TOKEN = "8537126256:AAFrwFUTmSatD3VUORG44RcBPtiNjUK0P3w"
TELEGRAM_CHAT_IDS = [-1003753296608, 7198809557]

# Normal thresholds
PRICE_SPIKE_PERCENT = 1.0
PRICE_DROP_PERCENT = -1.0
FOREX_SPIKE = 0.2
FOREX_DROP = -0.2

# Watchlist gets more sensitive thresholds (priority)
WATCHLIST_SPIKE = 0.5
WATCHLIST_DROP = -0.5
WATCHLIST_FOREX_SPIKE = 0.1
WATCHLIST_FOREX_DROP = -0.1

MIN_VOLUME = 500_000
MIN_DAILY_VALUE = 3_000_000

CHECK_INTERVAL = 60
COOLDOWN = 3600

MAX_STOCKS = 300
BATCH_SIZE = 30

COMMODITIES = [
    "XAU",
    "XAG",
    "WTIOIL-FUT"
]

CURRENCIES = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
    "USD/CHF",
    "EUR/GBP",
    "EUR/JPY",
    "GBP/JPY"
]

STOCKS = []

ALERTS_STATE_FILE = "alerts_state.json"

# =========================
# DATABASE
# =========================

conn = sqlite3.connect("market.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS assets (
    symbol TEXT PRIMARY KEY,
    alerted INTEGER DEFAULT 0,
    baseline_price REAL DEFAULT 0,
    baseline_volume REAL DEFAULT 0,
    last_alert INTEGER DEFAULT 0
)
""")

# Per-user watchlist
cursor.execute("""
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    added_at INTEGER,
    PRIMARY KEY (user_id, symbol)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bookmarks (
    symbol TEXT PRIMARY KEY,
    added_at INTEGER,
    last_price REAL,
    last_change REAL,
    last_direction TEXT,
    last_alert_time INTEGER,
    note TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bookmark_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    alert_time INTEGER,
    price REAL,
    change_pct REAL,
    direction TEXT,
    volume REAL
)
""")

conn.commit()

# =========================
# ALERTS STATE PERSISTENCE
# =========================

alerts_state = {}

if os.path.exists(ALERTS_STATE_FILE):
    try:
        with open(ALERTS_STATE_FILE, "r") as f:
            alerts_state = json.load(f)
    except:
        alerts_state = {}

def save_alerts_state():
    try:
        with open(ALERTS_STATE_FILE, "w") as f:
            json.dump(alerts_state, f)
    except Exception as e:
        print("Failed to save alerts state:", e)

# =========================
# WATCHLIST HELPERS (per-user)
# =========================

def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()

def is_watched(symbol: str) -> bool:
    """True if ANY user has this symbol on their personal watchlist (used for prioritization)"""
    cursor.execute("SELECT 1 FROM watchlist WHERE symbol=? LIMIT 1", (normalize_symbol(symbol),))
    return cursor.fetchone() is not None

def get_watchlist(user_id: int) -> list:
    cursor.execute(
        "SELECT symbol FROM watchlist WHERE user_id=? ORDER BY added_at",
        (user_id,)
    )
    return [row[0] for row in cursor.fetchall()]

def add_to_watchlist(user_id: int, symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return False
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, symbol, added_at) VALUES (?, ?, ?)",
            (user_id, symbol, int(time.time()))
        )
        conn.commit()
        return cursor.rowcount > 0
    except:
        return False

def remove_from_watchlist(user_id: int, symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    cursor.execute(
        "DELETE FROM watchlist WHERE user_id=? AND symbol=?",
        (user_id, symbol)
    )
    conn.commit()
    return cursor.rowcount > 0

def get_total_watchlist_entries() -> int:
    cursor.execute("SELECT COUNT(*) FROM watchlist")
    return cursor.fetchone()[0]

# =========================
# BOOKMARK HELPERS
# =========================

def is_bookmarked(symbol: str) -> bool:
    cursor.execute("SELECT 1 FROM bookmarks WHERE symbol=?", (normalize_symbol(symbol),))
    return cursor.fetchone() is not None

def get_bookmarks() -> list:
    cursor.execute("""
        SELECT symbol, last_price, last_change, last_direction, last_alert_time, note
        FROM bookmarks ORDER BY added_at
    """)
    return cursor.fetchall()

def add_to_bookmarks(symbol: str, price=None, change=None, direction=None, note=None) -> bool:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return False
    try:
        now = int(time.time())
        cursor.execute("""
            INSERT OR REPLACE INTO bookmarks
            (symbol, added_at, last_price, last_change, last_direction, last_alert_time, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (symbol, now, price, change, direction, now if price else None, note))
        conn.commit()
        return True
    except:
        return False

def remove_from_bookmarks(symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    cursor.execute("DELETE FROM bookmarks WHERE symbol=?", (symbol,))
    cursor.execute("DELETE FROM bookmark_history WHERE symbol=?", (symbol,))
    conn.commit()
    return cursor.rowcount > 0

def record_bookmark_history(symbol, price, change_pct, direction, volume=0):
    symbol = normalize_symbol(symbol)
    now = int(time.time())
    cursor.execute("""
        INSERT INTO bookmark_history (symbol, alert_time, price, change_pct, direction, volume)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (symbol, now, price, change_pct, direction, volume))
    # Keep only last 10 entries per symbol
    cursor.execute("""
        DELETE FROM bookmark_history
        WHERE symbol = ? AND id NOT IN (
            SELECT id FROM bookmark_history
            WHERE symbol = ?
            ORDER BY alert_time DESC LIMIT 10
        )
    """, (symbol, symbol))
    conn.commit()

def get_bookmark_history(symbol, limit=5):
    symbol = normalize_symbol(symbol)
    cursor.execute("""
        SELECT alert_time, price, change_pct, direction, volume
        FROM bookmark_history
        WHERE symbol = ?
        ORDER BY alert_time DESC
        LIMIT ?
    """, (symbol, limit))
    return cursor.fetchall()

def check_multi_day_confirmation(symbol, current_direction):
    """
    Check last few history entries for multi-day same-direction moves.
    Returns (confirmed: bool, summary: str, suggestion: str)
    """
    history = get_bookmark_history(symbol, limit=5)
    if len(history) < 2:
        return False, "", ""

    # history is newest first
    same_dir_count = 0
    days_seen = set()
    summary_lines = []

    for entry in history:
        alert_time, price, change_pct, direction, volume = entry
        day = time.strftime("%A", time.localtime(alert_time))
        if direction == current_direction:
            same_dir_count += 1
            days_seen.add(day)
            if symbol in CURRENCIES:
                summary_lines.append(f"{day}\nPair: {symbol}\nRate: {price:.4f}\nChange: {change_pct:+.2f}%")
            else:
                vol_str = f"\nVolume: {int(volume):,}" if volume else ""
                summary_lines.append(f"{day}\nSymbol: {symbol}\nPrice: ${price:.2f}\nChange: {change_pct:+.2f}%{vol_str}")
        else:
            break  # stop at first opposite direction

    if same_dir_count >= 2 and len(days_seen) >= 2:
        if current_direction == "spike":
            suggestion = "➡️ Multi-day SPIKE confirmation → consider LONG"
        else:
            suggestion = "➡️ Multi-day DROP confirmation → consider SHORT"
        summary = "\n\n".join(reversed(summary_lines))  # oldest first
        return True, summary, suggestion

    return False, "", ""

# =========================
# TELEGRAM
# =========================

def send_telegram(message, reply_markup=None, chat_ids=None):
    targets = chat_ids if chat_ids else TELEGRAM_CHAT_IDS
    for chat_id in targets:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            data = json.dumps(payload).encode("utf-8")
            req = Request(url, data=data, headers={"Content-Type": "application/json"})
            urlopen(req, timeout=10)
        except Exception as e:
            print("Telegram error:", e)

def answer_callback(callback_query_id, text=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=5)
    except Exception as e:
        print("answerCallbackQuery error:", e)

def build_watchlist_keyboard(symbols: list):
    """Inline keyboard with Unwatch buttons"""
    keyboard = []
    row = []
    for i, sym in enumerate(symbols):
        row.append({"text": f"❌ {sym}", "callback_data": f"unwatch:{sym}"})
        if len(row) == 2 or i == len(symbols) - 1:
            keyboard.append(row)
            row = []
    if not symbols:
        keyboard = [[{"text": "Watchlist is empty", "callback_data": "noop"}]]
    return {"inline_keyboard": keyboard}

def build_bookmarks_keyboard(rows: list):
    """Inline keyboard with Unbookmark buttons"""
    keyboard = []
    row = []
    for i, r in enumerate(rows):
        sym = r[0]
        row.append({"text": f"❌ {sym}", "callback_data": f"unbookmark:{sym}"})
        if len(row) == 2 or i == len(rows) - 1:
            keyboard.append(row)
            row = []
    if not rows:
        keyboard = [[{"text": "Bookmarks empty", "callback_data": "noop"}]]
    return {"inline_keyboard": keyboard}

# =========================
# TELEGRAM COMMAND HANDLER (background thread)
# =========================

def process_update(update):
    try:
        # ---- Callback queries (inline buttons) ----
        if "callback_query" in update:
            cq = update["callback_query"]
            data_str = cq.get("data", "")
            chat_id = cq["message"]["chat"]["id"]
            user_id = cq["from"]["id"]
            cq_id = cq["id"]

            if data_str.startswith("unwatch:"):
                symbol = data_str.split(":", 1)[1]
                if remove_from_watchlist(user_id, symbol):
                    answer_callback(cq_id, f"Removed {symbol}")
                    wl = get_watchlist(user_id)
                    text = "⭐ <b>Your Watchlist</b>\n\n" + ("\n".join(f"• {s}" for s in wl) if wl else "Empty")
                    send_telegram(text, reply_markup=build_watchlist_keyboard(wl), chat_ids=[chat_id])
                else:
                    answer_callback(cq_id, "Not found")
            elif data_str.startswith("watch:"):
                symbol = data_str.split(":", 1)[1]
                if add_to_watchlist(user_id, symbol):
                    answer_callback(cq_id, f"Added {symbol} ⭐")
                else:
                    answer_callback(cq_id, "Already on your watchlist")
            elif data_str.startswith("unbookmark:"):
                symbol = data_str.split(":", 1)[1]
                if remove_from_bookmarks(symbol):
                    answer_callback(cq_id, f"Removed bookmark {symbol}")
                    bms = get_bookmarks()
                    if not bms:
                        send_telegram("📌 Bookmarks is empty.", chat_ids=[chat_id])
                    else:
                        lines = []
                        for r in bms:
                            sym, price, change, direction, ts, note = r
                            extra = ""
                            if price is not None:
                                extra = f" | last {change:+.2f}% @ {price}"
                            lines.append(f"• {sym}{extra}")
                        text = "📌 <b>Your Bookmarks</b>\n\n" + "\n".join(lines)
                        send_telegram(text, reply_markup=build_bookmarks_keyboard(bms), chat_ids=[chat_id])
                else:
                    answer_callback(cq_id, "Not found")
            elif data_str.startswith("bookmark:"):
                symbol = data_str.split(":", 1)[1]
                if add_to_bookmarks(symbol):
                    answer_callback(cq_id, f"Bookmarked {symbol} 📌")
                else:
                    answer_callback(cq_id, "Already bookmarked")
            else:
                answer_callback(cq_id)
            return

        # ---- Regular messages / commands ----
        message = update.get("message")
        if not message:
            return

        text = (message.get("text") or "").strip()
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        print(f"[Telegram] Received command: {cmd} {arg} from user {user_id} in chat {chat_id}")

        if cmd in ("/start", "/help"):
            help_text = (
                "🤖 <b>Market Scanner Bot</b>\n\n"
                "<b>Commands:</b>\n"
                "/watch SYMBOL – Add to <b>your</b> watchlist (priority alerts)\n"
                "/unwatch SYMBOL – Remove from your watchlist\n"
                "/watchlist – Show your personal watchlist\n"
                "/bookmark SYMBOL – Bookmark for multi-day tracking\n"
                "/unbookmark SYMBOL – Remove bookmark\n"
                "/bookmarks – Show bookmarks + history\n"
                "/help – This message\n\n"
                "⭐ Watchlist is personal (each member has their own)\n"
                "📌 Bookmark = multi-day confirmation tracking\n"
                "   (consecutive same-direction moves → LONG/SHORT suggestion)"
            )
            send_telegram(help_text, chat_ids=[chat_id])

        elif cmd == "/watch":
            if not arg:
                send_telegram("Usage: /watch AAPL  or  /watch EUR/USD", chat_ids=[chat_id])
                return
            symbol = normalize_symbol(arg)
            if add_to_watchlist(user_id, symbol):
                send_telegram(f"✅ Added <b>{symbol}</b> to <b>your</b> watchlist ⭐", chat_ids=[chat_id])
            else:
                send_telegram(f"{symbol} is already on your watchlist.", chat_ids=[chat_id])

        elif cmd == "/unwatch":
            if not arg:
                send_telegram("Usage: /unwatch AAPL", chat_ids=[chat_id])
                return
            symbol = normalize_symbol(arg)
            if remove_from_watchlist(user_id, symbol):
                send_telegram(f"🗑 Removed <b>{symbol}</b> from your watchlist", chat_ids=[chat_id])
            else:
                send_telegram(f"{symbol} was not on your watchlist.", chat_ids=[chat_id])

        elif cmd == "/watchlist":
            wl = get_watchlist(user_id)
            if not wl:
                send_telegram("⭐ Your watchlist is empty.\nUse /watch SYMBOL to add one.", chat_ids=[chat_id])
            else:
                text = "⭐ <b>Your Watchlist</b>\n\n" + "\n".join(f"• {s}" for s in wl)
                send_telegram(text, reply_markup=build_watchlist_keyboard(wl), chat_ids=[chat_id])

        elif cmd == "/bookmark":
            if not arg:
                send_telegram("Usage: /bookmark GBP/JPY  or  /bookmark SUN", chat_ids=[chat_id])
                return
            symbol = normalize_symbol(arg)
            if add_to_bookmarks(symbol):
                send_telegram(
                    f"📌 Bookmarked <b>{symbol}</b>\n\n"
                    f"Bot will now track multi-day same-direction moves.\n"
                    f"When confirmed → you get a LONG / SHORT suggestion.",
                    chat_ids=[chat_id]
                )
            else:
                send_telegram(f"{symbol} is already bookmarked.", chat_ids=[chat_id])

        elif cmd == "/unbookmark":
            if not arg:
                send_telegram("Usage: /unbookmark GBP/JPY", chat_ids=[chat_id])
                return
            symbol = normalize_symbol(arg)
            if remove_from_bookmarks(symbol):
                send_telegram(f"🗑 Removed bookmark <b>{symbol}</b>", chat_ids=[chat_id])
            else:
                send_telegram(f"{symbol} was not bookmarked.", chat_ids=[chat_id])

        elif cmd == "/bookmarks":
            bms = get_bookmarks()
            if not bms:
                send_telegram("📌 Bookmarks is empty.\nUse /bookmark SYMBOL to start multi-day tracking.", chat_ids=[chat_id])
            else:
                lines = []
                for r in bms:
                    sym, price, change, direction, ts, note = r
                    extra = ""
                    if price is not None and change is not None:
                        extra = f"\n  last: {change:+.2f}% @ {price}"
                        if direction:
                            extra += f" ({direction})"
                    hist = get_bookmark_history(sym, limit=3)
                    if hist:
                        extra += "\n  history:"
                        for h in reversed(hist):
                            day = time.strftime("%a", time.localtime(h[0]))
                            extra += f"\n    {day}: {h[2]:+.2f}%"
                    lines.append(f"• <b>{sym}</b>{extra}")
                text = "📌 <b>Your Bookmarks</b> (multi-day tracking)\n\n" + "\n\n".join(lines)
                send_telegram(text, reply_markup=build_bookmarks_keyboard(bms), chat_ids=[chat_id])

    except Exception as e:
        print("Error processing update:", e)
        traceback.print_exc()

def telegram_polling_loop():
    """Dedicated background thread – always listens for commands"""
    offset = 0
    print("Telegram polling thread started")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30"
            resp = urlopen(url, timeout=35)
            data = json.load(resp)

            if not data.get("ok"):
                time.sleep(3)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                process_update(update)

        except Exception as e:
            print("Telegram polling error:", e)
            time.sleep(5)

# =========================
# LOAD STOCK LIST WITH CACHE + FALLBACKS
# =========================

def load_stock_list():
    global STOCKS
    cache_file = "stocks_cache.json"

    static_fallback_stocks = [
        "AAPL","MSFT","TSLA","NVDA","AMZN","META","AMD","INTC","NFLX","GOOGL",
        "BABA","UBER","PYPL","SHOP","COIN","PLTR","SNOW","BA","DIS","NKE",
        "V","JPM","GS","HD","MCD","KO","PEP","PFE","MRK","CVX","XOM","IBM",
        "ORCL","ADBE","CRM","ABNB","SQ","SPOT","SNAP","TWTR","UBER","LYFT",
        "T","VZ","CSCO","QCOM","TXN","LMT","BA","CAT","DE","GE","MMM","HON",
        "RTX","NKE","SBUX","WMT","LOW","CVS","TGT","AMAT","NOW","WDAY","ZM",
        "DOCU","F","GM","TM","NSANY","SONY","BIDU","JD","IQ","MELI","SEA","PDD",
        "SHOP","ETSY","ROKU","NET","CRWD","OKTA","ZS","PLAN","DOCU","TWLO","DDOG"
    ][:MAX_STOCKS]

    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                STOCKS = json.load(f)
            print(f"Loaded {len(STOCKS)} stocks from cache")
            return
        except:
            print("Cache corrupted, downloading fresh list")

    print("Downloading stock list from Finnhub...")
    for attempt in range(3):
        try:
            url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_API_KEY}"
            resp = urlopen(url, timeout=10)
            data = json.load(resp)
            STOCKS = [item["symbol"] for item in data if item["symbol"].isalpha()][:MAX_STOCKS]

            with open(cache_file, "w") as f:
                json.dump(STOCKS, f)

            print("Loaded", len(STOCKS), "stocks from Finnhub")
            return
        except Exception as e:
            print(f"Attempt {attempt+1} failed to load stock list from Finnhub: {e}")
            time.sleep(2 ** attempt)

    print("Finnhub failed, trying Twelve Data...")
    for attempt in range(3):
        try:
            url = f"https://api.twelvedata.com/stocks?exchange=NYSE&apikey={TWELVE_API_KEY}"
            resp = urlopen(url, timeout=10)
            data = json.load(resp)
            STOCKS = [item["symbol"] for item in data.get("data", []) if item.get("symbol")][:MAX_STOCKS]

            if STOCKS:
                with open(cache_file, "w") as f:
                    json.dump(STOCKS, f)
                print("Loaded", len(STOCKS), "stocks from Twelve Data")
                return
        except Exception as e:
            print(f"Twelve Data attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)

    print("Both APIs failed, using static fallback")
    STOCKS = static_fallback_stocks

# =========================
# FETCH STOCK PRICE (FAST)
# =========================

def get_stock_data(symbol):
    for attempt in range(3):
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
            resp = urlopen(url, timeout=10)
            data = json.load(resp)
            price = data.get("c")
            if price is None or price == 0:
                return None
            return price
        except:
            time.sleep(2 ** attempt)

    for attempt in range(3):
        try:
            url = f"https://api.twelvedata.com/quote?symbol={symbol}&apikey={TWELVE_API_KEY}"
            resp = urlopen(url, timeout=10)
            data = json.load(resp)
            price = data.get("close")
            if price is None or price == 0:
                return None
            return float(price)
        except:
            time.sleep(2 ** attempt)

    return None

# =========================
# FETCH VOLUME FROM ALPHA VANTAGE
# =========================

last_alpha_call = 0

def get_stock_volume(symbol):
    global last_alpha_call
    for attempt in range(3):
        now = time.time()
        if now - last_alpha_call < 12:
            time.sleep(12 - (now - last_alpha_call))

        last_alpha_call = time.time()
        try:
            url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={ALPHA_VANTAGE_KEY}"
            resp = urlopen(url, timeout=10)
            data = json.load(resp)
            volume = data.get("Global Quote", {}).get("06. volume")
            if volume:
                return int(volume)
        except:
            pass

    for attempt in range(3):
        try:
            url = f"https://api.twelvedata.com/quote?symbol={symbol}&apikey={TWELVE_API_KEY}"
            resp = urlopen(url, timeout=10)
            data = json.load(resp)
            volume = data.get("volume")
            if volume is not None:
                return int(volume)
        except:
            time.sleep(2 ** attempt)

    return 0

# =========================
# FETCH COMMODITY DATA
# =========================

def get_commodity_data(symbols_batch):
    result = {}
    for symbol in symbols_batch:
        try:
            url = f"https://api.twelvedata.com/quote?symbol={symbol}&apikey={TWELVE_API_KEY}"
            resp = urlopen(url, timeout=10)
            data = json.load(resp)
            price = data.get("close")
            if price is not None:
                result[symbol] = float(price)
            else:
                result[symbol] = None
        except Exception as e:
            print(f"Twelve Data commodity failed for {symbol}: {e}")
            result[symbol] = None
        time.sleep(1)

    failed = [sym for sym, val in result.items() if val is None]
    if failed:
        try:
            url = "https://api.oilpriceapi.com/v1/prices/latest"
            req = Request(url, headers={"x-api-key": OILPRICE_API_KEY})
            resp = urlopen(req, timeout=10)
            data = json.load(resp)
            prices = data.get("data", data.get("prices", []))
            if prices:
                mapping = {"WTIOIL-FUT": "WTI"}
                for sym in failed:
                    api_code = mapping.get(sym)
                    if api_code:
                        for item in prices:
                            code = item.get("code") or item.get("symbol")
                            if code == api_code:
                                price = item.get("price")
                                if price:
                                    result[sym] = float(price)
                                    break
        except Exception as e:
            print(f"OilPriceAPI fallback failed: {e}")

    return {sym: price for sym, price in result.items() if price is not None}

# =========================
# FETCH FOREX DATA
# =========================

def get_forex_data(symbols_batch):
    try:
        currencies = set()
        for pair in symbols_batch:
            base, quote = pair.split("/")
            currencies.add(base)
            currencies.add(quote)
        currencies = list(currencies)

        url = f"http://data.fixer.io/api/latest?access_key={FIXER_API_KEY}&symbols={','.join(currencies)}"
        resp = urlopen(url, timeout=10)
        data = json.load(resp)

        if data.get("success") and "rates" in data:
            rates = data["rates"]
            result = {}
            for pair in symbols_batch:
                base, quote = pair.split("/")
                rate_eur_base = rates.get(base)
                rate_eur_quote = rates.get(quote)
                if rate_eur_base and rate_eur_quote:
                    price = rate_eur_quote / rate_eur_base
                    result[pair] = price
            if result:
                return result
    except Exception as e:
        print(f"Fixer.io failed: {e}")

    result = {}
    for pair in symbols_batch:
        try:
            url = f"https://api.twelvedata.com/quote?symbol={pair}&apikey={TWELVE_API_KEY}"
            resp = urlopen(url, timeout=10)
            data = json.load(resp)
            price = data.get("close")
            if price is not None:
                result[pair] = float(price)
            time.sleep(1)
        except Exception as e:
            print(f"Twelve Data forex fallback failed for {pair}: {e}")
    return result

# =========================
# PROCESS SYMBOL (with watchlist priority)
# =========================

def process_symbol(symbol, price):
    if not price:
        return

    now = int(time.time())
    watched = is_watched(symbol)
    bookmarked = is_bookmarked(symbol)

    cursor.execute(
        "SELECT alerted, baseline_price, baseline_volume, last_alert FROM assets WHERE symbol=?",
        (symbol,)
    )

    row = cursor.fetchone()
    first_scan = row is None
    alerted, baseline_price, baseline_volume, last_alert = (0, price, 0, 0) if first_scan else row

    if alerted == 1 and last_alert and (now - last_alert) >= COOLDOWN:
        cursor.execute("UPDATE assets SET alerted=0 WHERE symbol=?", (symbol,))
        conn.commit()
        alerted = 0

    if last_alert and (now - last_alert) < COOLDOWN:
        return

    if first_scan:
        cursor.execute(
            "INSERT OR REPLACE INTO assets (symbol, baseline_price, baseline_volume) VALUES (?, ?, ?)",
            (symbol, price, 0)
        )
        conn.commit()
        return

    price_growth = ((price - baseline_price) / baseline_price) * 100

    chart = f"https://www.tradingview.com/symbols/{symbol}/"

    # Decide thresholds based on watchlist
    if watched:
        spike_th = WATCHLIST_SPIKE if symbol not in CURRENCIES else WATCHLIST_FOREX_SPIKE
        drop_th  = WATCHLIST_DROP  if symbol not in CURRENCIES else WATCHLIST_FOREX_DROP
        prefix = "⭐ WATCHLIST "
    else:
        spike_th = PRICE_SPIKE_PERCENT if symbol not in CURRENCIES else FOREX_SPIKE
        drop_th  = PRICE_DROP_PERCENT  if symbol not in CURRENCIES else FOREX_DROP
        prefix = ""

    direction = None
    volume = 0

    if symbol in COMMODITIES:
        if symbol == "XAU":
            chart = "https://www.investing.com/commodities/gold"
        elif symbol == "XAG":
            chart = "https://www.investing.com/commodities/silver"
        elif symbol == "WTIOIL-FUT":
            chart = "https://www.investing.com/commodities/crude-oil"

        if price_growth >= spike_th:
            direction = "spike"
            message = (
                f"{prefix}⛏️ COMMODITY SPIKE ALERT\n\n"
                f"Asset: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )
        elif price_growth <= drop_th:
            direction = "drop"
            message = (
                f"{prefix}⚠️ COMMODITY DROP ALERT\n\n"
                f"Asset: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )
        else:
            return

    elif symbol in CURRENCIES:
        chart = f"https://www.tradingview.com/symbols/{symbol.replace('/', '')}/"

        if price_growth >= spike_th:
            direction = "spike"
            message = (
                f"{prefix}💱 CURRENCY SPIKE ALERT\n\n"
                f"Pair: {symbol}\n"
                f"Rate: {price:.4f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )
        elif price_growth <= drop_th:
            direction = "drop"
            message = (
                f"{prefix}⚠️ CURRENCY DROP ALERT\n\n"
                f"Pair: {symbol}\n"
                f"Rate: {price:.4f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )
        else:
            return

    else:  # Stocks
        if price_growth < spike_th and price_growth > drop_th:
            return

        volume = get_stock_volume(symbol)

        if volume < MIN_VOLUME:
            return

        avg_daily_value = price * volume
        if avg_daily_value < MIN_DAILY_VALUE:
            return

        if price_growth >= spike_th:
            direction = "spike"
            message = (
                f"{prefix}📈 STOCK SPIKE ALERT\n\n"
                f"Symbol: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"Volume: {volume:,}\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )
        else:
            direction = "drop"
            message = (
                f"{prefix}📉 STOCK DROP ALERT\n\n"
                f"Symbol: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"Volume: {volume:,}\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )

    # Build buttons
    buttons = []
    if not watched:
        buttons.append({"text": f"⭐ Watch {symbol}", "callback_data": f"watch:{symbol}"})
    if not bookmarked:
        buttons.append({"text": f"📌 Bookmark {symbol}", "callback_data": f"bookmark:{symbol}"})

    reply_markup = None
    if buttons:
        reply_markup = {"inline_keyboard": [buttons]}

    # Update assets
    cursor.execute(
        "UPDATE assets SET alerted=1, baseline_price=?, baseline_volume=?, last_alert=? WHERE symbol=?",
        (price, volume, now, symbol)
    )
    conn.commit()

    alerts_state[symbol] = now
    save_alerts_state()

    send_telegram(message, reply_markup=reply_markup)

    # ===== BOOKMARK multi-day logic =====
    if bookmarked and direction:
        # Update bookmark record
        add_to_bookmarks(symbol, price=price, change=price_growth, direction=direction)

        # Record in history
        record_bookmark_history(symbol, price, price_growth, direction, volume)

        # Check for multi-day confirmation
        confirmed, summary, suggestion = check_multi_day_confirmation(symbol, direction)

        if confirmed:
            confirm_msg = (
                f"📌 <b>MULTI-DAY CONFIRMATION</b>\n\n"
                f"{summary}\n\n"
                f"{suggestion}\n\n"
                f"Chart: {chart}"
            )
            send_telegram(confirm_msg)

# =========================
# SCAN FUNCTIONS (watchlist first)
# =========================

def scan_stocks():
    print("Scanning liquid stocks...")
    # Prioritize symbols that ANY user has on their personal watchlist
    watched = set()
    cursor.execute("SELECT DISTINCT symbol FROM watchlist")
    for row in cursor.fetchall():
        watched.add(row[0])

    ordered = [s for s in STOCKS if s in watched] + [s for s in STOCKS if s not in watched]

    for i in range(0, len(ordered), BATCH_SIZE):
        batch = ordered[i:i+BATCH_SIZE]
        for symbol in batch:
            price = get_stock_data(symbol)
            if not price:
                continue
            process_symbol(symbol, price)
            time.sleep(1)

def scan_commodities():
    print("Scanning commodities...")
    watched = set()
    cursor.execute("SELECT DISTINCT symbol FROM watchlist")
    for row in cursor.fetchall():
        watched.add(row[0])

    ordered = [s for s in COMMODITIES if s in watched] + [s for s in COMMODITIES if s not in watched]

    for i in range(0, len(ordered), BATCH_SIZE):
        batch = ordered[i:i+BATCH_SIZE]
        data = get_commodity_data(batch)
        for symbol, price in data.items():
            process_symbol(symbol, price)
            time.sleep(1)

def scan_currencies():
    print("Scanning currencies...")
    watched = set()
    cursor.execute("SELECT DISTINCT symbol FROM watchlist")
    for row in cursor.fetchall():
        watched.add(row[0])

    ordered = [s for s in CURRENCIES if s in watched] + [s for s in CURRENCIES if s not in watched]

    for i in range(0, len(ordered), BATCH_SIZE):
        batch = ordered[i:i+BATCH_SIZE]
        data = get_forex_data(batch)
        for symbol, price in data.items():
            process_symbol(symbol, price)
            time.sleep(1)

# =========================
# MAIN LOOP
# =========================

def main():
    print("Starting Liquid Market Scanner + Watchlist + Bookmarks")
    load_stock_list()

    # Start Telegram listener in background thread
    t = threading.Thread(target=telegram_polling_loop, daemon=True)
    t.start()

    total_wl = get_total_watchlist_entries()
    bms = get_bookmarks()
    send_telegram(
        f"Scanner online — {len(STOCKS)} stocks | {total_wl} watchlist entries | {len(bms)} bookmarks"
    )

    while True:
        try:
            scan_stocks()
            scan_commodities()
            scan_currencies()
            print("Sleeping...\n")
            time.sleep(CHECK_INTERVAL)
        except Exception:
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    main()
