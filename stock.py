import sqlite3
import time
import traceback
import os
import json
import math
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

=========================

CONFIG

=========================

FINNHUB_API_KEY = "d6p7d41r01qk3chj7i00d6p7d41r01qk3chj7i0g"
TWELVE_API_KEY = "536665a15d214e48a622c80eff1bfa88"
COMMODITY_API_KEY = "81b66f88-22a3-4317-aff7-40d3ee221c70"
ALPHA_VANTAGE_KEY = "2HUZXG0RQSLXVQSZ"
OILPRICE_API_KEY = "71a7c209df5f57d072367f4a09d9985ebcc5e3ed2bbe52e687c007dd23926d6c"
FIXER_API_KEY = "70820ab44387be352ff27fed8e85116d"          # Added

TELEGRAM_BOT_TOKEN = "8537126256:AAFrwFUTmSatD3VUORG44RcBPtiNjUK0P3w"
TELEGRAM_CHAT_IDS = [-1003753296608, 7198809557]

PRICE_SPIKE_PERCENT = 1.0
PRICE_DROP_PERCENT = -1.0

FOREX_SPIKE = 0.2          # Added – separate forex thresholds
FOREX_DROP = -0.2

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

CURRENCIES = [                      # Added – list of forex pairs
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

=========================

DATABASE

=========================

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

=========================

ALERTS STATE PERSISTENCE

=========================

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

=========================

TELEGRAM

=========================

def send_telegram(message):
for chat_id in TELEGRAM_CHAT_IDS:
try:
url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
req = Request(url, data=payload, headers={"Content-Type": "application/json"})
urlopen(req, timeout=10)
except Exception as e:
print("Telegram error:", e)

=========================

LOAD STOCK LIST WITH CACHE + FALLBACKS

=========================

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

=========================

FETCH STOCK PRICE (FAST)

=========================

def get_stock_data(symbol):

Try Finnhub first

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

Fallback to Twelve Data

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

=========================

FETCH VOLUME FROM ALPHA VANTAGE

=========================

last_alpha_call = 0

def get_stock_volume(symbol):
global last_alpha_call

Try Alpha Vantage first

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

Fallback to Twelve Data

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

=========================

FETCH COMMODITY DATA (with fallback)

=========================

def get_commodity_data(symbols_batch):
result = {}
# First try Twelve Data for each symbol
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
time.sleep(1)  # rate limit

# If any symbol failed, fallback to OilPriceAPI for those  
failed = [sym for sym, val in result.items() if val is None]  
if failed:  
    try:  
        url = "https://api.oilpriceapi.com/v1/prices/latest"  
        req = Request(url, headers={"x-api-key": OILPRICE_API_KEY})  
        resp = urlopen(req, timeout=10)  
        data = json.load(resp)  
        prices = data.get("data", data.get("prices", []))  
        if prices:  
            mapping = {"WTIOIL-FUT": "WTI"}  # extend as needed  
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
        else:  
            print("OilPriceAPI returned no prices")  
    except Exception as e:  
        print(f"OilPriceAPI fallback failed: {e}")  

# Remove None entries  
return {sym: price for sym, price in result.items() if price is not None}

=========================

FETCH FOREX DATA (with fallback)

=========================

def get_forex_data(symbols_batch):

First try Fixer.io

try:

Collect all distinct currencies from the pairs

currencies = set()
for pair in symbols_batch:
base, quote = pair.split("/")
currencies.add(base)
currencies.add(quote)
currencies = list(currencies)

Build URL for Fixer (free tier uses EUR base)

url = f"http://data.fixer.io/api/latest?access_key={FIXER_API_KEY}&symbols={','.join(currencies)}"    
resp = urlopen(url, timeout=10)    
data = json.load(resp)    

if data.get("success") and "rates" in data:    
    rates = data["rates"]    
    result = {}    
    for pair in symbols_batch:    
        base, quote = pair.split("/")    
        # rate = rate_eur_quote / rate_eur_base    
        rate_eur_base = rates.get(base)    
        rate_eur_quote = rates.get(quote)    
        if rate_eur_base and rate_eur_quote:    
            price = rate_eur_quote / rate_eur_base    
            result[pair] = price    
    if result:    
        return result

except Exception as e:
print(f"Fixer.io failed: {e}")

Fallback to Twelve Data

result = {}
for pair in symbols_batch:
try:
url = f"https://api.twelvedata.com/quote?symbol={pair}&apikey={TWELVE_API_KEY}"
resp = urlopen(url, timeout=10)
data = json.load(resp)
price = data.get("close")
if price is not None:
result[pair] = float(price)
time.sleep(1)  # basic rate limiting
except Exception as e:
print(f"Twelve Data forex fallback failed for {pair}: {e}")
return result

=========================

PROCESS SYMBOL

=========================

def process_symbol(symbol, price):
if not price:
return

now = int(time.time())

cursor.execute(
"SELECT alerted, baseline_price, baseline_volume, last_alert FROM assets WHERE symbol=?",
(symbol,)
)

row = cursor.fetchone()
first_scan = row is None
alerted, baseline_price, baseline_volume, last_alert = (0, price, 0, 0) if first_scan else row

reset alert after cooldown

if alerted == 1 and last_alert and (now - last_alert) >= COOLDOWN:
cursor.execute("UPDATE assets SET alerted=0 WHERE symbol=?", (symbol,))
conn.commit()
alerted = 0

if last_alert and (now - last_alert) < COOLDOWN:
return

Skip alerts and volume checks on first scan

if first_scan:
cursor.execute(
"INSERT OR REPLACE INTO assets (symbol, baseline_price, baseline_volume) VALUES (?, ?, ?)",
(symbol, price, 0)
)
conn.commit()
return

price_growth = ((price - baseline_price) / baseline_price) * 100

chart = f"https://www.tradingview.com/symbols/{symbol}/"

if symbol in COMMODITIES:

if symbol == "XAU":            
    chart = "https://www.investing.com/commodities/gold"            
elif symbol == "XAG":            
    chart = "https://www.investing.com/commodities/silver"            
elif symbol == "WTIOIL-FUT":            
    chart = "https://www.investing.com/commodities/crude-oil"            

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

elif symbol in CURRENCIES:                                     # Added – currency branch
# Build a chart URL for currencies
chart = f"https://www.tradingview.com/symbols/{symbol.replace('/', '')}/"

if price_growth >= FOREX_SPIKE:            
    message = (            
        f"💱 CURRENCY SPIKE ALERT\n\n"            
        f"Pair: {symbol}\n"            
        f"Rate: {price:.4f}\n"            
        f"Change: {price_growth:+.2f}%\n"            
        f"――――――――――――――――――\n\n"            
        f"Chart: {chart}"            
    )            

elif price_growth <= FOREX_DROP:            
    message = (            
        f"⚠️ CURRENCY DROP ALERT\n\n"            
        f"Pair: {symbol}\n"            
        f"Rate: {price:.4f}\n"            
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

=========================

SCAN STOCKS

=========================

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

=========================

SCAN COMMODITIES

=========================

def scan_commodities():
print("Scanning commodities...")
for i in range(0, len(COMMODITIES), BATCH_SIZE):
batch = COMMODITIES[i:i+BATCH_SIZE]
data = get_commodity_data(batch)
for symbol, price in data.items():
process_symbol(symbol, price)
time.sleep(1)

=========================

SCAN CURRENCIES                                           # Added

=========================

def scan_currencies():
print("Scanning currencies...")
for i in range(0, len(CURRENCIES), BATCH_SIZE):
batch = CURRENCIES[i:i+BATCH_SIZE]
data = get_forex_data(batch)
for symbol, price in data.items():
process_symbol(symbol, price)
time.sleep(1)

=========================

MAIN LOOP

=========================

def main():
print("Starting Liquid Market Scanner")
load_stock_list()
send_telegram(
f"Market scanner started\nStocks loaded: {len(STOCKS)}\nCommodities: {', '.join(COMMODITIES)}\nCurrencies: {', '.join(CURRENCIES)}\n\nALERTS COMING SOON 💎"
)
while True:
try:
scan_stocks()
scan_commodities()
scan_currencies()              # Added
print("Sleeping...\n")
time.sleep(CHECK_INTERVAL)
except:
traceback.print_exc()

if name == "main":
main()
