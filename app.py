import os, re, json, time, hmac, hashlib, sqlite3, threading, asyncio
from urllib.parse import urlparse, urljoin
from datetime import datetime, timezone
from contextlib import contextmanager

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from starlette.middleware.sessions import SessionMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from sources import PublicTelegramSource

load_dotenv()
DB_PATH = os.getenv("DATABASE_PATH", "./divar.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key-now")
ALLOWED_IDS = {x.strip() for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",") if x.strip()}
BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
DEFAULTS = {
    "search_url": "https://divar.ir/s/shiraz/buy-residential",
    "max_price": "11000000000", "min_price": "", "min_area": "", "max_area": "",
    "bedrooms": "", "parking": "any", "neighborhoods": "", "interval": "20",
    "enabled": "1", "status": "هنوز بررسی نشده"
}
app = FastAPI(title="Divar Finder")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=BASE_URL.startswith("https://"), same_site="lax")
stop_event = threading.Event()
worker_started = False
bot_started = False
last_error = ""
last_check = ""
bot_loop = None
bot_app = None

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS listings (id TEXT PRIMARY KEY, title TEXT, price TEXT, area TEXT, location TEXT, url TEXT, seen_at TEXT, created_at TEXT)")
        columns = {row["name"] for row in c.execute("PRAGMA table_info(listings)")}
        if "created_at" not in columns:
            c.execute("ALTER TABLE listings ADD COLUMN created_at TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS subscribers (chat_id TEXT PRIMARY KEY, created_at TEXT)")
        for k,v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k,v))

def get_settings():
    with db() as c:
        out = dict(DEFAULTS)
        out.update({r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")})
        return out

def set_settings(values):
    with db() as c:
        for k,v in values.items():
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))

def allowed(update):
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and (not ALLOWED_IDS or str(user.id) in ALLOWED_IDS or str(chat.id) in ALLOWED_IDS))

def money(value):
    try: return f"{int(value):,} تومان"
    except: return value or "قیمت درج نشده"

def parse_price(text):
    if not text: return ""
    t = normalize_digits(text).replace(",", "").replace("٬", "").replace("تومان", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m: return ""
    try:
        number = float(m.group(0))
        if "میلیارد" in t: number *= 1_000_000_000
        elif "میلیون" in t: number *= 1_000_000
        return str(int(number))
    except (TypeError, ValueError):
        return ""

def divar_search_api_url(search_url):
    """Translate a normal Divar search URL into the web-search JSON endpoint."""
    parsed = urlparse(search_url)
    if parsed.netloc.lower() == "api.divar.ir" and parsed.path.startswith("/v8/web-search/"):
        return search_url
    path = parsed.path
    if not path.startswith("/s/"):
        raise ValueError("آدرس باید صفحه جست‌وجوی دیوار با مسیر /s/ باشد.")
    api_path = "/v8/web-search/" + path[len("/s/"):]
    return "https://api.divar.ir" + api_path + (("?" + parsed.query) if parsed.query else "")


def normalize_digits(value):
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(value or "").translate(table)


def _listing_from_widget(widget, base_url):
    """Convert a Divar POST_ROW widget to the bot's normalized listing shape."""
    data = widget.get("data", {}) if isinstance(widget, dict) else {}
    if not isinstance(data, dict):
        return None
    action = data.get("action") or {}
    payload_data = action.get("payload") or {} if isinstance(action, dict) else {}
    web_info = payload_data.get("web_info") or {} if isinstance(payload_data, dict) else {}
    token = str(data.get("token") or payload_data.get("token") or "").strip()
    if not token:
        return None
    title = str(data.get("title") or web_info.get("title") or "").strip()
    descriptions = [str(data.get(k) or "").strip() for k in
                   ("top_description_text", "middle_description_text", "bottom_description_text")]
    descriptions = [x for x in descriptions if x]
    raw_text = " ".join([title, *descriptions])
    price = ""
    for key in ("price", "price_text", "middle_description_text", "bottom_description_text", "top_description_text"):
        val = data.get(key)
        if isinstance(val, (str, int, float)) and re.search(r"تومان|میلیارد|میلیون|توافقی", str(val)):
            price = str(val).strip()
            break
    area_match = re.search(r"(\d{2,4})\s*متر", normalize_digits(raw_text))
    location = str(web_info.get("district_persian") or web_info.get("district") or "").strip()
    if not location:
        location = str(data.get("bottom_description_text") or "").strip()
    created_at = ""
    # Divar may expose a timestamp under nested metadata rather than at the card's top level.
    def find_date_value(obj, depth=0):
        if depth > 5 or not isinstance(obj, dict):
            return ""
        date_keys = ("creation_date", "created_at", "published_at", "publication_date",
                     "post_date", "date_created", "date")
        for key in date_keys:
            value = obj.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                candidate = str(value).strip()
                if re.search(r"\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}|\\d{10,13}", normalize_digits(candidate)):
                    return candidate
        for value in obj.values():
            if isinstance(value, dict):
                result = find_date_value(value, depth + 1)
                if result:
                    return result
        return ""
    created_at = find_date_value(widget) or find_date_value(data) or find_date_value(payload_data)
    # Normalize Unix timestamps and common ISO date strings.
    if created_at:
        normalized = normalize_digits(created_at)
        if normalized.isdigit() and len(normalized) in (10, 13):
            try:
                stamp = int(normalized)
                if len(normalized) == 13: stamp //= 1000
                created_at = datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError, OverflowError):
                pass
        else:
            try:
                parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
                if parsed.tzinfo is None: parsed = parsed.astimezone()
                created_at = parsed.astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
    # Fallback: extract Divar's relative publication age if shown in card text.
    if not created_at:
        age_match = re.search(r"(لحظاتی پیش|دقایقی پیش|\\d+\\s*(?:دقیقه|ساعت|روز|هفته|ماه)\\s*پیش|امروز|دیروز)", normalize_digits(raw_text))
        if age_match:
            created_at = "حدوداً " + age_match.group(1)
    return {
        "id": token, "title": title or "آگهی دیوار", "price": price,
        "area": area_match.group(1) if area_match else "",
        "location": location[:100], "url": "https://divar.ir/v/" + token,
        "created_at": created_at
    }


def extract_api_listings(payload, base_url):
    """Parse supported JSON response shapes returned by Divar."""
    found, seen = [], set()
    if not isinstance(payload, dict):
        return found
    widgets = payload.get("web_widgets", {})
    cards = widgets.get("post_list", []) if isinstance(widgets, dict) else []
    if not cards and isinstance(payload.get("widget_list"), list):
        cards = [w for w in payload["widget_list"]
                 if isinstance(w, dict) and w.get("widget_type") == "POST_ROW"]
    for card in cards:
        item = _listing_from_widget(card, base_url)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            found.append(item)
    return found


def extract_html_embedded_listings(source, base_url):
    """Extract server-rendered POST_ROW JSON objects embedded in Divar's HTML."""
    import html as html_lib
    source = html_lib.unescape(source or "")
    decoder = json.JSONDecoder()
    found, seen = [], set()
    for match in re.finditer(r'"widget_type"\s*:\s*"POST_ROW"', source):
        # widget_type is usually not the first key, so the opening brace is
        # earlier than the character immediately before this match.
        start = source.rfind("{", 0, match.start())
        if start < 0:
            continue
        try:
            widget, _ = decoder.raw_decode(source[start:])
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(widget, dict) or widget.get("widget_type") != "POST_ROW":
            continue
        item = _listing_from_widget(widget, base_url)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            found.append(item)
    return found


def extract_listings(html, base_url):
    # Fallback parser retained for older HTML responses and fixtures.
    soup = BeautifulSoup(html, "html.parser")
    found, seen = [], set()
    for node in soup.select('a[href*="/v/"]'):
        href = urljoin(base_url, node.get("href", ""))
        if "/v/" not in urlparse(href).path or href in seen:
            continue
        seen.add(href)
        text = " ".join(node.stripped_strings) or node.get("aria-label", "")
        if not text:
            continue
        title = text[:180].strip()
        price_match = re.search(r"([\d,٬۰-۹]+)\s*(?:تومان|میلیارد|میلیون)", text)
        price = price_match.group(0) if price_match else ""
        area_match = re.search(r"(\d{2,4})\s*متر", normalize_digits(text))
        area = area_match.group(1) if area_match else ""
        parts = [x.strip() for x in text.split("·") if x.strip()]
        location = parts[-1][:100] if len(parts) > 1 else ""
        ident = re.search(r"/v/([^/?#]+)", href)
        lid = ident.group(1) if ident else hashlib.sha256(href.encode()).hexdigest()[:24]
        found.append({"id":lid,"title":title,"price":price,"area":area,"location":location,"url":href})
    return found

def matches(item, s):
    price_num = parse_price(item["price"])
    # Enforce the budget strictly: unknown or negotiable prices cannot be
    # confirmed to fit under the configured maximum.
    if s.get("max_price") and not price_num: return False
    if price_num and s.get("max_price") and int(price_num) > int(s["max_price"]): return False
    if price_num and s.get("min_price") and int(price_num) < int(s["min_price"]): return False
    area = int(item["area"]) if item["area"].isdigit() else None
    if area is not None and s.get("min_area") and area < int(s["min_area"]): return False
    if area is not None and s.get("max_area") and area > int(s["max_area"]): return False
    if s.get("bedrooms"):
        # Only filter if bedroom count is discernible from listing title/snippet.
        m = re.search(r"(\d+)\s*خواب", item["title"])
        if m and int(m.group(1)) != int(s["bedrooms"]): return False
    neighborhoods = [x.strip() for x in s.get("neighborhoods","").split(",") if x.strip()]
    if neighborhoods and not any(n in (item["title"]+" "+item["location"]) for n in neighborhoods): return False
    if s.get("parking") == "yes" and not re.search(r"پارکینگ", item["title"]): return False
    if s.get("parking") == "no" and re.search(r"پارکینگ", item["title"]): return False
    return True

async def notify_new(item):
    if not bot_app: return
    with db() as c:
        chats = [r["chat_id"] for r in c.execute("SELECT chat_id FROM subscribers")]
    text = "🏠 آگهی جدید " + ("تلگرام" if item.get("source") == "telegram" else "دیوار") + "\n\n" + item["title"] + "\n"
    text += "🗓 تاریخ ایجاد آگهی: " + (item.get("created_at") or "تاریخ دقیق در اطلاعات دریافتی دیوار موجود نیست") + "\n"
    if item["price"]: text += "💰 " + item["price"] + "\n"
    if item["area"]: text += "📐 " + item["area"] + " متر\n"
    if item["location"]: text += "📍 " + item["location"] + "\n"
    text += "\n🔗 " + item["url"]
    for chat in chats:
        try: await bot_app.bot.send_message(chat_id=chat, text=text, disable_web_page_preview=False)
        except Exception as e: print("Telegram send failed:", e)

def poll_once():
    global last_error, last_check
    s = get_settings()
    if s.get("enabled") != "1": return
    url = s.get("search_url","").strip()
    if not url.startswith("https://divar.ir/"):
        raise ValueError("آدرس جست‌وجو باید یک URL معتبر از divar.ir باشد.")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json,application/xhtml+xml,*/*",
        "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.5",
        "Referer": "https://divar.ir/",
        "Origin": "https://divar.ir",
    }
    items = []
    # First try the public search endpoint; some responses contain a blocking widget.
    try:
        api_url = divar_search_api_url(url)
        api_response = requests.get(api_url, headers=headers, timeout=20)
        if api_response.status_code not in (401, 403, 429):
            api_response.raise_for_status()
            if "json" in api_response.headers.get("content-type", "").lower() or api_response.text.lstrip().startswith("{"):
                try:
                    items = extract_api_listings(api_response.json(), url)
                except (ValueError, TypeError, AttributeError):
                    items = []
    except (requests.RequestException, ValueError):
        items = []
    # The current website server-renders POST_ROW widgets into its HTML.
    if not items:
        html_response = requests.get(url, headers=headers, timeout=25)
        if html_response.status_code in (401, 403, 429):
            raise RuntimeError(f"دیوار دسترسی صفحه را محدود کرده (HTTP {html_response.status_code}).")
        html_response.raise_for_status()
        items = extract_html_embedded_listings(html_response.text, url)
        if not items:
            items = extract_listings(html_response.text, url)
    if not items:
        raise RuntimeError("صفحه جست‌وجوی دیوار دریافت شد، اما آگهی قابل استخراجی پیدا نشد؛ ساختار صفحه ممکن است تغییر کرده باشد.")
    # Optional extra source: public Telegram channel previews configured by the operator.
    # Failure of this source must never break the existing Divar poll.
    telegram_items = []
    try:
        telegram_items = [x.to_dict() for x in PublicTelegramSource().fetch(s)]
        for item in telegram_items:
            item["id"] = "telegram:" + item["id"]
            item["source"] = "telegram"
    except Exception as exc:
        print("Telegram source failed; continuing with Divar only:", exc)
    items.extend(telegram_items)
    print("Poll filters:", {k: s.get(k) for k in
          ("max_price", "min_price", "min_area", "max_area", "bedrooms", "parking", "neighborhoods", "enabled")})
    print(f"Poll: extracted={len(items)}; source=Divar")
    # Process up to 100 cards when the fetched Divar response contains them.
    items = items[:100]
    for sample in items[:8]:
        print("Poll sample:", repr(sample.get("title", "")[:70]),
              "price=", repr(sample.get("price", "")),
              "parsed_price=", repr(parse_price(sample.get("price", ""))),
              "area=", repr(sample.get("area", "")),
              "location=", repr(sample.get("location", "")),
              "matches=", matches(sample, s))
    new_items = []
    matched_count = 0
    with db() as c:
        for item in items:
            if not matches(item,s): continue
            matched_count += 1
            cur = c.execute("INSERT OR IGNORE INTO listings(id,title,price,area,location,url,seen_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (item["id"],item["title"],item["price"],item["area"],item["location"],item["url"],datetime.now(timezone.utc).isoformat(),item.get("created_at", "")))
            if cur.rowcount: new_items.append(item)
    print(f"Poll: matched={matched_count}; newly_saved={len(new_items)}")
    last_check = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    last_error = ""
    if new_items and bot_loop and bot_loop.is_running():
        for item in new_items[:30]:
            asyncio.run_coroutine_threadsafe(notify_new(item), bot_loop)

def worker():
    global last_error
    while not stop_event.is_set():
        try: poll_once()
        except Exception as e:
            last_error = str(e)
            print("Poll error:", last_error)
        s = get_settings()
        try: delay = max(15, min(3600, int(s.get("interval","20"))))
        except: delay = 20
        stop_event.wait(delay)

def main_keyboard():
    active = get_settings().get("enabled") == "1"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ تنظیم فیلترها", callback_data="menu_filters"),
         InlineKeyboardButton("📋 فیلترهای فعلی", callback_data="show_filters")],
        [InlineKeyboardButton("🆕 آخرین آگهی‌ها", callback_data="latest"),
         InlineKeyboardButton("🔎 بررسی همین الان", callback_data="check")],
        [InlineKeyboardButton("⏸ توقف پایش" if active else "▶️ فعال‌کردن پایش", callback_data="toggle"),
         InlineKeyboardButton("ℹ️ راهنما", callback_data="help")]
    ])

SHIRAZ_NEIGHBORHOODS = [
    "معالی‌آباد", "قصرالدشت", "فرهنگ‌شهر", "عفیف‌آباد", "ستارخان",
    "قدوسی غربی", "قدوسی شرقی", "زرگری", "چمران", "نیایش",
    "ارم", "ملاصدرا", "رحمت‌آباد", "عادل‌آباد", "تاچارا",
    "بلوار پاسداران", "بلوار مدرس", "بلوار رحمت", "بلوار عدالت", "بلوار امیرکبیر",
    "شهرک گلستان", "شهرک صدرا", "شهرک پرواز", "شهرک بهار", "شهرک بزین",
    "شهرک گلها", "شهرک والفجر", "شهرک سجادیه", "شهرک رضوان", "شهرک امام حسین",
    "زرگون", "کوی وحدت", "کوی زهرا", "کوی آزادگان", "کوی طلاب",
    "دروازه کازرون", "دروازه سعدی", "دروازه قرآن", "شاهچراغ", "لب آب",
    "سنگ سیاه", "بازار وکیل", "سعدیه", "حافظیه", "اطلسی",
    "پودنک", "سهل‌آباد", "دینکان", "کفترک", "سروستان",
    "باغ حوض", "ابوذر", "فضیلت", "شریف‌آباد", "فرگاز",
    "بلوار مدرس", "بلوار مدرس شمالی", "بلوار مدرس جنوبی", "بلوار استقلال", "بلوار جمهوری",
    "بلوار چمران", "بلوار شهید بهشتی", "بلوار نصر", "بلوار رحمت جنوبی", "بلوار تخت جمشید",
]

def filters_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 حداقل قیمت", callback_data="edit_min_price"),
         InlineKeyboardButton("💰 حداکثر قیمت", callback_data="edit_max_price")],
        [InlineKeyboardButton("📐 حداقل متراژ", callback_data="edit_min_area"),
         InlineKeyboardButton("📐 حداکثر متراژ", callback_data="edit_max_area")],
        [InlineKeyboardButton("🛏 تعداد خواب", callback_data="choose_bedrooms"),
         InlineKeyboardButton("🚗 پارکینگ", callback_data="choose_parking")],
        [InlineKeyboardButton("📍 انتخاب محله‌ها ☑️", callback_data="neighborhoods_menu"),
         InlineKeyboardButton("⏱ فاصله بررسی", callback_data="edit_interval")],
        [InlineKeyboardButton("🧹 پاک‌کردن محدودیت متراژ", callback_data="clear_area")],
        [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu_home")]
    ])

def filters_text():
    s = get_settings()
    return ("⚙️ فیلترهای فعلی\n"
        f"شهر: شیراز | نوع: خرید مسکونی\n"
        f"حداکثر قیمت: {money(s.get('max_price')) if s.get('max_price') else 'بدون محدودیت'}\n"
        f"حداقل قیمت: {money(s.get('min_price')) if s.get('min_price') else 'بدون محدودیت'}\n"
        f"متراژ: {s.get('min_area') or 'بدون حداقل'} تا {s.get('max_area') or 'بدون حداکثر'} متر\n"
        f"اتاق خواب: {s.get('bedrooms') or 'همه'}\n"
        f"پارکینگ: { {'any':'مهم نیست','yes':'حتماً داشته باشد','no':'نداشته باشد'}.get(s.get('parking'),'مهم نیست')}\n"
        f"محله‌ها: {s.get('neighborhoods') or 'همه محله‌ها'}\n"
        f"فاصله بررسی: {s.get('interval')} ثانیه\n"
        f"پایش: {'فعال ✅' if s.get('enabled') == '1' else 'متوقف ⏸'}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("دسترسی ندارید. شناسه تلگرام خود را در پنل مدیر اضافه کنید.")
        return
    chat = str(update.effective_chat.id)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO subscribers(chat_id,created_at) VALUES(?,?)",
                  (chat,datetime.now(timezone.utc).isoformat()))
    await update.effective_message.reply_text(
        "سلام! 🏠 به دیوار فایندر خوش اومدی.\nاز دکمه‌های زیر برای تنظیم فیلترها و مدیریت آگهی‌ها استفاده کن.",
        reply_markup=main_keyboard())

async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("دسترسی ندارید. شناسه تلگرام خود را در پنل مدیر اضافه کنید."); return
    with db() as c: rows = c.execute("SELECT * FROM listings ORDER BY seen_at DESC LIMIT 100").fetchall()
    if not rows:
        await update.effective_message.reply_text("هنوز آگهی‌ای ثبت نشده. آدرس جست‌وجو و وضعیت جمع‌آوری را در پنل بررسی کن."); return
    for r in rows:
        text = "🏠 " + (r["title"] or "آگهی") + "\n"
        if r["price"]: text += "💰 " + r["price"] + "\n"
        if r["area"]: text += "📐 " + r["area"] + " متر\n"
        if r["location"]: text += "📍 " + r["location"] + "\n"
        text += "🔗 " + r["url"]
        await update.effective_message.reply_text(text, disable_web_page_preview=False)

async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("دسترسی ندارید.")
        return
    await update.effective_message.reply_text(filters_text(), reply_markup=filters_keyboard())

def neighborhood_keyboard(page=0):
    s = get_settings()
    selected = {x.strip() for x in s.get("neighborhoods", "").split(",") if x.strip()}
    per_page = 12
    total_pages = (len(SHIRAZ_NEIGHBORHOODS) + per_page - 1) // per_page
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page
    subset = SHIRAZ_NEIGHBORHOODS[start:start + per_page]
    rows = []
    for n in range(0, len(subset), 2):
        row = []
        for name in subset[n:n + 2]:
            mark = "☑️" if name in selected else "⬜"
            row.append(InlineKeyboardButton(f"{mark} {name}", callback_data="hood_toggle:" + str(SHIRAZ_NEIGHBORHOODS.index(name))))
        rows.append(row)
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"hood_page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="hood_page_info"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"hood_page:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("✅ ثبت انتخاب‌ها", callback_data="hood_done"),
                 InlineKeyboardButton("🧹 همه محله‌ها", callback_data="hood_clear")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت به فیلترها", callback_data="menu_filters")])
    return InlineKeyboardMarkup(rows)

def neighborhood_prompt(page=0):
    selected = [x.strip() for x in get_settings().get("neighborhoods", "").split(",") if x.strip()]
    summary = "، ".join(selected) if selected else "همه محله‌ها (بدون محدودیت)"
    return f"📍 محله‌های شیراز\nبرای انتخاب چند محله، روی هرکدام بزن تا ☑️ شود.\nانتخاب‌شده: {summary[:500]}"

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return
    await q.answer()
    data = q.data or ""
    if data == "menu_home":
        await q.edit_message_text("🏠 منوی اصلی دیوار فایندر", reply_markup=main_keyboard())
    elif data == "menu_filters":
        await q.edit_message_text("⚙️ تنظیم فیلترها\nیکی از گزینه‌ها را انتخاب کن:", reply_markup=filters_keyboard())
    elif data == "show_filters":
        await q.edit_message_text(filters_text(), reply_markup=filters_keyboard())
    elif data == "latest":
        with db() as c:
            rows = c.execute("SELECT * FROM listings ORDER BY seen_at DESC LIMIT 100").fetchall()
        if not rows:
            await q.message.reply_text("هنوز آگهی‌ای ثبت نشده است.", reply_markup=main_keyboard())
        else:
            for r in rows:
                msg = "🏠 " + (r["title"] or "آگهی") + "\n"
                msg += "🗓 تاریخ ایجاد آگهی: " + (r["created_at"] or "برای این آگهی ذخیره نشده؛ آگهی تازه‌ای را بررسی کن") + "\n"
                if r["price"]: msg += "💰 " + r["price"] + "\n"
                if r["area"]: msg += "📐 " + r["area"] + " متر\n"
                if r["location"]: msg += "📍 " + r["location"] + "\n"
                await q.message.reply_text(msg + "🔗 " + r["url"], disable_web_page_preview=False)
            await q.message.reply_text("منوی اصلی:", reply_markup=main_keyboard())
    elif data == "check":
        await q.edit_message_text("🔎 دارم دیوار را بررسی می‌کنم...")
        try:
            await asyncio.to_thread(poll_once)
            await q.message.reply_text("✅ بررسی تمام شد.\n" + filters_text(), reply_markup=main_keyboard())
        except Exception as e:
            await q.message.reply_text("❌ بررسی ناموفق بود: " + str(e), reply_markup=main_keyboard())
    elif data == "toggle":
        enabled = "0" if get_settings().get("enabled") == "1" else "1"
        set_settings({"enabled": enabled})
        await q.edit_message_text(("▶️ پایش فعال شد." if enabled == "1" else "⏸ پایش متوقف شد.") + "\n\n" + filters_text(), reply_markup=main_keyboard())
    elif data == "neighborhoods_menu":
        await q.edit_message_text(neighborhood_prompt(), reply_markup=neighborhood_keyboard(0))
    elif data.startswith("hood_page:"):
        page = int(data.split(":", 1)[1])
        await q.edit_message_text(neighborhood_prompt(page), reply_markup=neighborhood_keyboard(page))
    elif data == "hood_page_info":
        await q.answer("برای رفتن بین صفحه‌ها از دکمه‌های قبلی و بعدی استفاده کن.")
    elif data.startswith("hood_toggle:"):
        try:
            index = int(data.split(":", 1)[1])
            name = SHIRAZ_NEIGHBORHOODS[index]
        except (ValueError, IndexError):
            await q.answer("این گزینه معتبر نیست.", show_alert=True)
            return
        selected = [x.strip() for x in get_settings().get("neighborhoods", "").split(",") if x.strip()]
        if name in selected:
            selected.remove(name)
        else:
            selected.append(name)
        set_settings({"neighborhoods": ",".join(selected)})
        page = index // 12
        await q.edit_message_text(neighborhood_prompt(page), reply_markup=neighborhood_keyboard(page))
    elif data == "hood_clear":
        set_settings({"neighborhoods": ""})
        await q.edit_message_text("همه محله‌ها انتخاب شدند، یعنی محدودیت محله خاموش است.\n" + neighborhood_prompt(), reply_markup=neighborhood_keyboard(0))
    elif data == "hood_done":
        await q.edit_message_text("✅ انتخاب محله‌ها ذخیره شد.\n\n" + filters_text(), reply_markup=filters_keyboard())
    elif data == "clear_area":
        set_settings({"min_area": "", "max_area": ""})
        await q.edit_message_text("✅ محدودیت متراژ پاک شد.\n\n" + filters_text(), reply_markup=filters_keyboard())
    elif data == "choose_bedrooms":
        await q.edit_message_text("تعداد اتاق خواب را انتخاب کن:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("همه", callback_data="bedrooms_any"), InlineKeyboardButton("بدون خواب", callback_data="bedrooms_0")],
            [InlineKeyboardButton("۱ خواب", callback_data="bedrooms_1"), InlineKeyboardButton("۲ خواب", callback_data="bedrooms_2")],
            [InlineKeyboardButton("۳ خواب", callback_data="bedrooms_3"), InlineKeyboardButton("۴ خواب", callback_data="bedrooms_4")],
            [InlineKeyboardButton("۵ خواب", callback_data="bedrooms_5"), InlineKeyboardButton("⬅️ بازگشت", callback_data="menu_filters")]
        ]))
    elif data.startswith("bedrooms_"):
        val = data[len("bedrooms_"):]
        set_settings({"bedrooms": "" if val == "any" else val})
        await q.edit_message_text("✅ تعداد خواب ذخیره شد.\n\n" + filters_text(), reply_markup=filters_keyboard())
    elif data == "choose_parking":
        await q.edit_message_text("وضعیت پارکینگ را انتخاب کن:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("مهم نیست", callback_data="parking_any")],
            [InlineKeyboardButton("حتماً پارکینگ داشته باشد", callback_data="parking_yes")],
            [InlineKeyboardButton("بدون پارکینگ", callback_data="parking_no")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu_filters")]
        ]))
    elif data.startswith("parking_"):
        set_settings({"parking": data[len("parking_"):]})
        await q.edit_message_text("✅ فیلتر پارکینگ ذخیره شد.\n\n" + filters_text(), reply_markup=filters_keyboard())
    elif data.startswith("edit_"):
        field = data[len("edit_"):]
        labels = {"min_price":"حداقل قیمت به تومان", "max_price":"حداکثر قیمت به تومان",
                  "min_area":"حداقل متراژ به متر", "max_area":"حداکثر متراژ به متر",
                  "neighborhoods":"نام محله‌ها", "interval":"فاصله بررسی به ثانیه (۱۵ تا ۳۶۰۰)"}
        if field in labels:
            context.user_data["awaiting_filter"] = field
            await q.message.reply_text(f"مقدار جدید {labels[field]} را بفرست. برای حذف این فیلتر «پاک» و برای لغو «لغو» را بفرست.")
    elif data == "help":
        await q.message.reply_text("🏠 راهنما\nاز «تنظیم فیلترها» برای تغییر قیمت، متراژ، خواب، پارکینگ و محله‌ها استفاده کن.\nدستورها: /start، /latest و /filters", reply_markup=main_keyboard())

async def handle_filter_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    field = context.user_data.get("awaiting_filter")
    msg = update.effective_message
    if not field or not msg or not msg.text: return
    raw = msg.text.strip()
    if raw.lower() in ("لغو", "cancel", "بیخیال", "بی‌خیال"):
        context.user_data.pop("awaiting_filter", None)
        await msg.reply_text("ویرایش لغو شد.", reply_markup=main_keyboard())
        return
    value = "" if raw.lower() in ("پاک", "حذف", "خالی", "clear", "-") else raw
    if field in ("min_price", "max_price", "min_area", "max_area", "interval") and value:
        digits = normalize_digits(value).replace(",", "").replace("٬", "").replace(" ", "")
        if not digits.isdigit():
            await msg.reply_text("لطفاً فقط عدد بفرست. برای لغو «لغو» را بفرست.")
            return
        number = int(digits)
        if field == "interval" and not 15 <= number <= 3600:
            await msg.reply_text("فاصله بررسی باید بین ۱۵ تا ۳۶۰۰ ثانیه باشد.")
            return
        value = str(number)
    if field == "neighborhoods" and value:
        value = ",".join(part.strip() for part in value.replace("،", ",").split(",") if part.strip())
    set_settings({field: value})
    context.user_data.pop("awaiting_filter", None)
    await msg.reply_text("✅ فیلتر ذخیره شد.\n\n" + filters_text(), reply_markup=filters_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("/start ثبت عضویت و دریافت آگهی جدید\n/latest آخرین ۱۰ آگهی\n/filters نمایش فیلترها\n/help راهنما")

def start_bot():
    global bot_loop, bot_app, bot_started
    if not BOT_TOKEN: print("BOT_TOKEN missing; Telegram bot disabled."); return
    async def runner():
        global bot_loop, bot_app, bot_started
        bot_loop=asyncio.get_running_loop()
        bot_app=Application.builder().token(BOT_TOKEN).build()
        bot_app.add_handler(CommandHandler("start",cmd_start))
        bot_app.add_handler(CommandHandler("latest",cmd_latest))
        bot_app.add_handler(CommandHandler("filters",cmd_filters))
        bot_app.add_handler(CommandHandler("help",cmd_help))
        bot_app.add_handler(CallbackQueryHandler(handle_button))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_filter_input))
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=False)
        bot_started=True
        while not stop_event.is_set(): await asyncio.sleep(1)
        await bot_app.updater.stop(); await bot_app.stop(); await bot_app.shutdown()
    try: asyncio.run(runner())
    except Exception as e: print("Telegram bot failed:", repr(e))

def esc(v): 
    import html
    return html.escape(str(v or ""), quote=True)

def page(body, title="Divar Finder"):
    return HTMLResponse(f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f4f6fa;color:#172033;font-family:Tahoma,Arial,sans-serif;line-height:1.8}}main{{max-width:1000px;margin:24px auto;padding:16px}}.card{{background:white;border:1px solid #e1e6ef;border-radius:16px;padding:20px;margin:14px 0;box-shadow:0 4px 18px #14213d08}}h1{{font-size:24px}}h2{{font-size:18px}}label{{display:block;font-size:13px;margin:10px 0 4px}}input,select{{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:9px;background:white;font:inherit}}button,.btn{{display:inline-block;background:#155eef;color:white;border:0;border-radius:9px;padding:11px 16px;font:inherit;text-decoration:none;cursor:pointer;margin-top:12px}}.muted{{color:#64748b;font-size:13px}}.error{{background:#fff1f2;color:#9f1239;padding:12px;border-radius:10px}}.ok{{background:#ecfdf5;color:#065f46;padding:12px;border-radius:10px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.listing{{padding:14px 0;border-bottom:1px solid #e2e8f0;overflow-wrap:anywhere}}a{{color:#155eef}}.top{{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}}small{{color:#64748b}} </style></head><body><main>{body}</main></body></html>""")

@app.on_event("startup")
def startup():
    global worker_started
    init_db()
    if not worker_started:
        threading.Thread(target=start_bot,daemon=True).start()
        threading.Thread(target=worker,daemon=True).start()
        worker_started=True

@app.on_event("shutdown")
def shutdown(): stop_event.set()

@app.get("/health")
def health(): return {"ok":True,"bot_configured":bool(BOT_TOKEN),"bot_started":bot_started,"last_check":last_check,"error":last_error}

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not request.session.get("logged_in"): return RedirectResponse("/login",303)
    s=get_settings()
    with db() as c:
        rows=c.execute("SELECT * FROM listings ORDER BY seen_at DESC LIMIT 20").fetchall()
        total=c.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"]
        subs=c.execute("SELECT COUNT(*) n FROM subscribers").fetchone()["n"]
    status = f'<div class="error">خطا در آخرین بررسی: {esc(last_error)}</div>' if last_error else '<div class="ok">خطای فعلی ثبت نشده است.</div>'
    listing_html = "".join(f'<div class="listing"><b>{esc(r["title"])}</b><br>{esc(r["price"])} · {esc(r["area"])} متر · {esc(r["location"])}<br><a target="_blank" rel="noopener" href="{esc(r["url"])}">مشاهده آگهی</a><br><small>{esc(r["seen_at"])}</small></div>' for r in rows) or '<p class="muted">هنوز آگهی‌ای ذخیره نشده است.</p>'
    body=f"""<div class="top"><h1>🏠 دیوار فایندر</h1><a href="/logout">خروج</a></div>
    <p class="muted">پنل فارسی پایش آگهی‌های خرید خانه در شیراز</p>
    <div class="card"><h2>وضعیت سرویس</h2>{status}<p>آخرین بررسی: {esc(last_check or 'هنوز اجرا نشده')} · کل آگهی‌های ذخیره‌شده: {total} · کاربران تلگرام: {subs}</p><p class="muted">فاصله بررسی حداقل ۱۵ ثانیه است. سرعت واقعی به دسترسی و ساختار صفحه دیوار بستگی دارد؛ ارسال کاملاً آنی تضمین‌شدنی نیست.</p><form method="post" action="/check"><button>بررسی همین الان</button></form></div>
    <div class="card"><h2>تنظیمات جست‌وجو</h2><form method="post" action="/settings">
    <label>آدرس صفحه جست‌وجوی دیوار</label><input name="search_url" type="url" required value="{esc(s['search_url'])}" placeholder="https://divar.ir/s/...">
    <div class="grid"><div><label>حداقل قیمت (تومان)</label><input name="min_price" type="number" min="0" value="{esc(s['min_price'])}"></div><div><label>حداکثر قیمت (تومان)</label><input name="max_price" type="number" min="0" value="{esc(s['max_price'])}"></div><div><label>حداقل متراژ</label><input name="min_area" type="number" min="0" value="{esc(s['min_area'])}"></div><div><label>حداکثر متراژ</label><input name="max_area" type="number" min="0" value="{esc(s['max_area'])}"></div><div><label>تعداد اتاق خواب</label><select name="bedrooms"><option value="">همه</option>{''.join(f'<option value="{n}" {"selected" if s["bedrooms"]==str(n) else ""}>{n} اتاق</option>' for n in range(0,6))}</select></div><div><label>پارکینگ</label><select name="parking">{''.join(f'<option value="{v}" {"selected" if s["parking"]==v else ""}>{t}</option>' for v,t in [('any','مهم نیست'),('yes','حتماً داشته باشد'),('no','بدون پارکینگ')])}</select></div><div><label>فاصله بررسی (ثانیه، حداقل ۱۵)</label><input name="interval" type="number" min="15" max="3600" value="{esc(s['interval'])}"></div><div><label>پایش آگهی‌ها</label><select name="enabled"><option value="1" {"selected" if s["enabled"]=="1" else ""}>فعال</option><option value="0" {"selected" if s["enabled"]!="1" else ""}>متوقف</option></select></div></div>
    <label>محله‌ها (با ویرگول جدا کن، خالی یعنی همه)</label><input name="neighborhoods" value="{esc(s['neighborhoods'])}" placeholder="مثلاً معالی‌آباد، قصرالدشت">
    <button type="submit">ذخیره تنظیمات</button></form></div>
    <div class="card"><div class="top"><h2>آخرین آگهی‌های ذخیره‌شده</h2><a href="/export">خروجی JSON</a></div>{listing_html}</div>
    <div class="card"><h2>راه‌اندازی تلگرام</h2><p>به رباتت پیام <code>/start</code> بده تا عضو دریافت آگهی‌های جدید شوی. سپس <code>/latest</code> برای ۱۰ آگهی اخیر و <code>/filters</code> برای دیدن فیلترها.</p><p class="muted">اگر ربات پاسخ نمی‌دهد، BOT_TOKEN را در .env بررسی کن. شناسه عددی خودت را به ALLOWED_TELEGRAM_IDS اضافه کن و کانتینر را مجدداً راه‌اندازی کن.</p></div>"""
    return page(body)

@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    if request.session.get("logged_in"): return RedirectResponse("/",303)
    return page('<div class="card"><h1>ورود به پنل</h1><form method="post"><label>رمز مدیر</label><input type="password" name="password" required autofocus><button>ورود</button></form><p class="muted">رمز را در متغیر ADMIN_PASSWORD تنظیم کن.</p></div>','ورود')

@app.post("/login")
def login_post(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD: return PlainTextResponse("ADMIN_PASSWORD is not configured. Set it in .env.",500)
    if not hmac.compare_digest(password,ADMIN_PASSWORD):
        return page('<div class="card"><div class="error">رمز اشتباه است.</div><a href="/login">تلاش مجدد</a></div>','خطای ورود')
    request.session["logged_in"]=True
    return RedirectResponse("/",303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login",303)

@app.post("/settings")
def save_settings(request: Request, search_url: str = Form(...), min_price: str = Form(""), max_price: str = Form(""), min_area: str = Form(""), max_area: str = Form(""), bedrooms: str = Form(""), parking: str = Form("any"), interval: str = Form("20"), enabled: str = Form("1"), neighborhoods: str = Form("")):
    if not request.session.get("logged_in"): return RedirectResponse("/login",303)
    if not search_url.startswith("https://divar.ir/"): return PlainTextResponse("Only https://divar.ir/ search URLs are allowed.",400)
    try: interval=str(max(15,min(3600,int(interval))))
    except: interval="20"
    vals={"search_url":search_url,"min_price":min_price,"max_price":max_price,"min_area":min_area,"max_area":max_area,"bedrooms":bedrooms,"parking":parking,"interval":interval,"enabled":"1" if enabled=="1" else "0","neighborhoods":neighborhoods}
    set_settings(vals)
    return RedirectResponse("/",303)

@app.post("/check")
def check_now(request: Request):
    if not request.session.get("logged_in"): return RedirectResponse("/login",303)
    try: poll_once()
    except Exception as e:
        global last_error
        last_error=str(e)
    return RedirectResponse("/",303)

@app.get("/export")
def export(request: Request):
    if not request.session.get("logged_in"): return RedirectResponse("/login",303)
    with db() as c: rows=[dict(r) for r in c.execute("SELECT * FROM listings ORDER BY seen_at DESC LIMIT 500").fetchall()]
    return HTMLResponse("<pre>"+esc(json.dumps(rows,ensure_ascii=False,indent=2))+"</pre>")

