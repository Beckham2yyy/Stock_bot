import sqlite3
import time
import traceback
import os
import json
import math
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

cursor.execute("""
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    added_at INTEGER
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
# WATCHLIST HELPERS
# =========================

def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()

def is_watched(symbol: str) -> bool:
    cursor.execute("SELECT 1 FROM watchlist WHERE symbol=?", (normalize_symbol(symbol),))
    return cursor.fetchone() is not None

def get_watchlist() -> list:
    cursor.execute("SELECT symbol FROM watchlist ORDER BY added_at")
    return [row[0] for row in cursor.fetchall()]

def add_to_watchlist(symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return False
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)",
            (symbol, int(time.time()))
        )
        conn.commit()
        return cursor.rowcount > 0
    except:
        return False

def remove_from_watchlist(symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    cursor.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
    conn.commit()
    return cursor.rowcount > 0

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

# =========================
# TELEGRAM COMMAND HANDLER
# =========================

last_update_id = 0

def handle_telegram_updates():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=5"
        resp = urlopen(url, timeout=10)
        data = json.load(resp)
        if not data.get("ok"):
            return

        for update in data.get("result", []):
            last_update_id = update["update_id"]

            # ---- Callback queries (inline buttons) ----
            if "callback_query" in update:
                cq = update["callback_query"]
                data_str = cq.get("data", "")
                chat_id = cq["message"]["chat"]["id"]
                cq_id = cq["id"]

                if data_str.startswith("unwatch:"):
                    symbol = data_str.split(":", 1)[1]
                    if remove_from_watchlist(symbol):
                        answer_callback(cq_id, f"Removed {symbol}")
                        # Refresh the message
                        wl = get_watchlist()
                        text = "⭐ <b>Your Watchlist</b>\n\n" + ("\n".join(f"• {s}" for s in wl) if wl else "Empty")
                        send_telegram(text, reply_markup=build_watchlist_keyboard(wl), chat_ids=[chat_id])
                    else:
                        answer_callback(cq_id, "Not found")
                elif data_str.startswith("watch:"):
                    symbol = data_str.split(":", 1)[1]
                    if add_to_watchlist(symbol):
                        answer_callback(cq_id, f"Added {symbol} ⭐")
                    else:
                        answer_callback(cq_id, "Already on watchlist")
                else:
                    answer_callback(cq_id)
                continue

            # ---- Regular messages / commands ----
            message = update.get("message")
            if not message:
                continue

            text = (message.get("text") or "").strip()
            chat_id = message["chat"]["id"]

            if not text.startswith("/"):
                continue

            parts = text.split(maxsplit=1)
            cmd = parts[0].lower().split("@")[0]   # remove @BotName if present
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("/start", "/help"):
                help_text = (
                    "🤖 <b>Market Scanner Bot</b>\n\n"
                    "<b>Commands:</b>\n"
                    "/watch SYMBOL – Add to watchlist (priority alerts)\n"
                    "/unwatch SYMBOL – Remove from watchlist\n"
                    "/watchlist – Show current watchlist\n"
                    "/help – This message\n\n"
                    "Watchlist symbols are scanned first and use tighter thresholds."
                )
                send_telegram(help_text, chat_ids=[chat_id])

            elif cmd == "/watch":
                if not arg:
                    send_telegram("Usage: /watch AAPL  or  /watch EUR/USD", chat_ids=[chat_id])
                    continue
                symbol = normalize_symbol(arg)
                if add_to_watchlist(symbol):
                    send_telegram(f"✅ Added <b>{symbol}</b> to watchlist ⭐", chat_ids=[chat_id])
                else:
                    send_telegram(f"{symbol} is already on the watchlist.", chat_ids=[chat_id])

            elif cmd == "/unwatch":
                if not arg:
                    send_telegram("Usage: /unwatch AAPL", chat_ids=[chat_id])
                    continue
                symbol = normalize_symbol(arg)
                if remove_from_watchlist(symbol):
                    send_telegram(f"🗑 Removed <b>{symbol}</b> from watchlist", chat_ids=[chat_id])
                else:
                    send_telegram(f"{symbol} was not on the watchlist.", chat_ids=[chat_id])

            elif cmd == "/watchlist":
                wl = get_watchlist()
                if not wl:
                    send_telegram("⭐ Watchlist is empty.\nUse /watch SYMBOL to add one.", chat_ids=[chat_id])
                else:
                    text = "⭐ <b>Your Watchlist</b>\n\n" + "\n".join(f"• {s}" for s in wl)
                    send_telegram(text, reply_markup=build_watchlist_keyboard(wl), chat_ids=[chat_id])

    except Exception as e:
        print("Telegram update error:", e)

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

    if symbol in COMMODITIES:
        if symbol == "XAU":
            chart = "https://www.investing.com/commodities/gold"
        elif symbol == "XAG":
            chart = "https://www.investing.com/commodities/silver"
        elif symbol == "WTIOIL-FUT":
            chart = "https://www.investing.com/commodities/crude-oil"

        if price_growth >= spike_th:
            message = (
                f"{prefix}⛏️ COMMODITY SPIKE ALERT\n\n"
                f"Asset: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )
        elif price_growth <= drop_th:
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
        volume = 0

    elif symbol in CURRENCIES:
        chart = f"https://www.tradingview.com/symbols/{symbol.replace('/', '')}/"

        if price_growth >= spike_th:
            message = (
                f"{prefix}💱 CURRENCY SPIKE ALERT\n\n"
                f"Pair: {symbol}\n"
                f"Rate: {price:.4f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )
        elif price_growth <= drop_th:
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
        volume = 0

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
            message = (
                f"{prefix}📉 STOCK DROP ALERT\n\n"
                f"Symbol: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"Volume: {volume:,}\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )

    # Add quick "Add to watchlist" button if not already watched
    reply_markup = None
    if not watched:
        reply_markup = {
            "inline_keyboard": [[
                {"text": f"⭐ Watch {symbol}", "callback_data": f"watch:{symbol}"}
            ]]
        }

    cursor.execute(
        "UPDATE assets SET alerted=1, baseline_price=?, baseline_volume=?, last_alert=? WHERE symbol=?",
        (price, volume, now, symbol)
    )
    conn.commit()

    alerts_state[symbol] = now
    save_alerts_state()

    send_telegram(message, reply_markup=reply_markup)

# =========================
# SCAN FUNCTIONS (watchlist first)
# =========================

def scan_stocks():
    print("Scanning liquid stocks...")
    watched = set(get_watchlist())
    # Prioritize: watched stocks first
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
    watched = set(get_watchlist())
    ordered = [s for s in COMMODITIES if s in watched] + [s for s in COMMODITIES if s not in watched]

    for i in range(0, len(ordered), BATCH_SIZE):
        batch = ordered[i:i+BATCH_SIZE]
        data = get_commodity_data(batch)
        for symbol, price in data.items():
            process_symbol(symbol, price)
            time.sleep(1)

def scan_currencies():
    print("Scanning currencies...")
    watched = set(get_watchlist())
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
    print("Starting Liquid Market Scanner + Watchlist")
    load_stock_list()

    wl = get_watchlist()
    send_telegram(
        f"🚀 Market scanner started\n"
        f"Stocks loaded: {len(STOCKS)}\n"
        f"Commodities: {', '.join(COMMODITIES)}\n"
        f"Currencies: {', '.join(CURRENCIES)}\n"
        f"Watchlist: {len(wl)} symbols\n\n"
        f"Use /watch SYMBOL to prioritize any asset 💎"
    )

    while True:
        try:
            # Handle Telegram commands / buttons frequently
            handle_telegram_updates()

            scan_stocks()
            handle_telegram_updates()          # keep responsive

            scan_commodities()
            handle_telegram_updates()

            scan_currencies()
            handle_telegram_updates()

            print("Sleeping...\n")
            # During sleep, keep checking for commands every few seconds
            for _ in range(CHECK_INTERVAL // 5):
                handle_telegram_updates()
                time.sleep(5)

        except Exception:
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    main()
