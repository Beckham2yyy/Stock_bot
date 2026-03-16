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
COMMODITY_API_KEY = "81b66f88-22a3-4317-aff7-40d3ee221c70"
ALPHA_VANTAGE_KEY = "2HUZXG0RQSLXVQSZ"

TELEGRAM_BOT_TOKEN = "8759682838:AAFVFNMA2kFLgAQDgzOTVSMmRhWkUk6Hxn8"
TELEGRAM_CHAT_IDS = [-1003753296608]

PRICE_SPIKE_PERCENT = 2
PRICE_DROP_PERCENT = -1.6

MIN_VOLUME = 1_000_000
MIN_DAILY_VALUE = 10_000_000

CHECK_INTERVAL = 60
COOLDOWN = 3600

MAX_STOCKS = 300
BATCH_SIZE = 30

COMMODITIES = [
    "XAU",
    "XAG",
    "WTIOIL-FUT"
]

STOCKS = []

ALERTS_STATE_FILE = "alerts_state.json"

# =========================
# DATABASE
# =========================

conn = sqlite3.connect("market.db")
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
# TELEGRAM
# =========================

def send_telegram(message):
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
            req = Request(url, data=payload, headers={"Content-Type": "application/json"})
            urlopen(req, timeout=10)
        except Exception as e:
            print("Telegram error:", e)

# =========================
# LOAD STOCK LIST WITH CACHE
# =========================

def load_stock_list():
    global STOCKS
    cache_file = "stocks_cache.json"

    fallback_stocks = [
        "AAPL","MSFT","TSLA","NVDA","AMZN","META","AMD","INTC","NFLX","GOOGL",
        "BABA","UBER","PYPL","SHOP","COIN","PLTR","SNOW","BA","DIS","NKE"
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

            print("Loaded", len(STOCKS), "stocks")
            return
        except Exception as e:
            print(f"Attempt {attempt+1} failed to load stock list: {e}")
            time.sleep(2 ** attempt)

    print("Failed to load stock list after 3 attempts, using fallback")
    STOCKS = fallback_stocks

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
    return None

# =========================
# FETCH VOLUME FROM ALPHA VANTAGE
# =========================

last_alpha_call = 0

def get_stock_volume(symbol):
    global last_alpha_call
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
    return 0

# =========================
# FETCH COMMODITY DATA
# =========================

def get_commodity_data(symbols_batch):
    symbols_str = ",".join(symbols_batch)
    for attempt in range(3):
        try:
            url = f"https://api.commoditypriceapi.com/v2/rates/latest?symbols={symbols_str}"
            req = Request(url, headers={"x-api-key": COMMODITY_API_KEY})
            resp = urlopen(req, timeout=10)
            data = json.load(resp)
            rates = data.get("rates", {})
            result = {}
            for sym in symbols_batch:
                price = rates.get(sym)
                if price is not None:
                    result[sym] = float(price)
            return result
        except:
            time.sleep(2 ** attempt)
    return {}

# =========================
# PROCESS SYMBOL
# =========================

def process_symbol(symbol, price):
    if not price:
        return

    now = int(time.time())

    cursor.execute(
        "SELECT alerted, baseline_price, baseline_volume, last_alert FROM assets WHERE symbol=?",
        (symbol,)
    )

    row = cursor.fetchone()
    alerted, baseline_price, baseline_volume, last_alert = (0, price, 0, 0) if not row else row

    if alerted == 1 and last_alert and (now - last_alert) >= COOLDOWN:
        cursor.execute("UPDATE assets SET alerted=0 WHERE symbol=?", (symbol,))
        conn.commit()
        alerted = 0

    if last_alert and (now - last_alert) < COOLDOWN:
        return

    price_growth = ((price - baseline_price) / baseline_price) * 100

    chart = f"https://www.tradingview.com/symbols/{symbol}/"

    if symbol in COMMODITIES:

        if price_growth >= PRICE_SPIKE_PERCENT:
            message = (
                f"⛏️ COMMODITY SPIKE ALERT\n\n"
                f"Asset: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )

        elif price_growth <= PRICE_DROP_PERCENT:
            message = (
                f"⚠️ COMMODITY DROP ALERT\n\n"
                f"Asset: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )
        else:
            return

        volume = 0

    else:

        if price_growth < PRICE_SPIKE_PERCENT and price_growth > PRICE_DROP_PERCENT:
            return

        volume = get_stock_volume(symbol)

        if volume < MIN_VOLUME:
            return

        avg_daily_value = price * volume

        if avg_daily_value < MIN_DAILY_VALUE:
            return

        if price_growth >= PRICE_SPIKE_PERCENT:
            message = (
                f"📈 STOCK SPIKE ALERT\n\n"
                f"Symbol: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"Volume: {volume:,}\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )

        else:
            message = (
                f"📉 STOCK DROP ALERT\n\n"
                f"Symbol: {symbol}\n"
                f"Price: ${price:.2f}\n"
                f"Change: {price_growth:+.2f}%\n"
                f"Volume: {volume:,}\n"
                f"――――――――――――――――――\n\n"
                f"Chart: {chart}"
            )

    cursor.execute(
        "UPDATE assets SET alerted=1, baseline_price=?, baseline_volume=?, last_alert=? WHERE symbol=?",
        (price, volume, now, symbol)
    )
    conn.commit()

    alerts_state[symbol] = now
    save_alerts_state()

    send_telegram(message)

# =========================
# SCAN STOCKS
# =========================

def scan_stocks():
    print("Scanning liquid stocks...")
    for i in range(0, len(STOCKS), BATCH_SIZE):
        batch = STOCKS[i:i+BATCH_SIZE]
        for symbol in batch:
            price = get_stock_data(symbol)
            if not price:
                continue
            process_symbol(symbol, price)
        time.sleep(1)

# =========================
# SCAN COMMODITIES
# =========================

def scan_commodities():
    print("Scanning commodities...")
    for i in range(0, len(COMMODITIES), BATCH_SIZE):
        batch = COMMODITIES[i:i+BATCH_SIZE]
        data = get_commodity_data(batch)
        for symbol, price in data.items():
            process_symbol(symbol, price)
        time.sleep(1)

# =========================
# MAIN LOOP
# =========================

def main():
    print("Starting Liquid Market Scanner")
    load_stock_list()
    send_telegram(
        f"Market scanner started\nStocks loaded: {len(STOCKS)}\nCommodities: {', '.join(COMMODITIES)}\n\nALERTS COMING SOON 💎"
    )
    while True:
        try:
            scan_stocks()
            scan_commodities()
            print("Sleeping...\n")
            time.sleep(CHECK_INTERVAL)
        except:
            traceback.print_exc()

if __name__ == "__main__":
    main()
