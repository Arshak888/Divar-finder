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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
        c.execute("CREATE TABLE IF NOT EXISTS listings (id TEXT PRIMARY KEY, title TEXT, price TEXT, area TEXT, location TEXT, url TEXT, seen_at TEXT)")
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


def extract_api_listings(payload, base_url):
    """Parse Divar's web-search response (web_widgets.post_list)."""
    widgets = payload.get("web_widgets", {}) if isinstance(payload, dict) else {}
    cards = widgets.get("post_list", []) if isinstance(widgets, dict) else []
    found, seen = [], set()
    for card in cards:
        data = card.get("data", {}) if isinstance(card, dict) else {}
        if not isinstance(data, dict):
            continue
        token = str(data.get("token") or "")
        action = data.get("action") or {}
        payload_data = action.get("payload") or {} if isinstance(action, dict) else {}
        web_info = payload_data.get("web_info") or {} if isinstance(payload_data, dict) else {}
        token = token or str(web_info.get("token") or "")
        if not token:
            continue
        href = "https://divar.ir/v/" + token
        if token in seen:
            continue
        seen.add(token)
        title = str(data.get("title") or "").strip()
        descriptions = [
            str(data.get(k) or "").strip()
            for k in ("top_description_text", "middle_description_text", "bottom_description_text")
        ]
        descriptions = [x for x in descriptions if x]
        detail = " · ".join(descriptions)
        raw_text = " ".join([title, *descriptions])
        price = ""
        # API fields vary by category and may omit price for some ads.
        for key in ("price", "price_text", "bottom_description_text"):
            val = data.get(key)
            if isinstance(val, (str, int, float)) and re.search(r"تومان|میلیارد|میلیون|توافقی", str(val)):
                price = str(val).strip()
                break
        area_match = re.search(r"(\d{2,4})\s*متر", normalize_digits(raw_text))
        area = area_match.group(1) if area_match else ""
        location = str(web_info.get("district_persian") or web_info.get("district") or "").strip()
        if not location:
            location = str(data.get("subtitle") or "").strip()
        found.append({
            "id": token, "title": title or detail[:180], "price": price,
            "area": area, "location": location[:100], "url": href
        })
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
    # Text price extraction can be incomplete; don't discard listing solely on unknown price.
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
    text = "🏠 آگهی جدید دیوار\n\n" + item["title"] + "\n"
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
    api_url = divar_search_api_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.5",
        "Referer": url if url.startswith("https://divar.ir/") else "https://divar.ir/",
        "Origin": "https://divar.ir",
    }
    response = requests.get(api_url, headers=headers, timeout=20)
    if response.status_code in (401,403,429):
        raise RuntimeError(f"دیوار دسترسی را محدود کرده (HTTP {response.status_code}). CAPTCHA یا محدودیت را دور نزنید؛ بعداً دوباره بررسی کنید.")
    response.raise_for_status()
    items = []
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type or response.text.lstrip().startswith("{"):
        try:
            items = extract_api_listings(response.json(), url)
        except (ValueError, TypeError, AttributeError) as e:
            raise RuntimeError(f"پاسخ API دیوار قابل پردازش نیست: {e}")
    if not items:
        # Compatibility fallback for responses that still contain server-rendered cards.
        items = extract_listings(response.text, url)
    if not items:
        raise RuntimeError("API جست‌وجوی دیوار پاسخ داد اما آگهی قابل استخراجی پیدا نشد؛ ممکن است قالب پاسخ تغییر کرده یا فیلترهای URL نیاز به بررسی داشته باشند.")
    new_items = []
    with db() as c:
        for item in items:
            if not matches(item,s): continue
            cur = c.execute("INSERT OR IGNORE INTO listings(id,title,price,area,location,url,seen_at) VALUES(?,?,?,?,?,?,?)",
                (item["id"],item["title"],item["price"],item["area"],item["location"],item["url"],datetime.now(timezone.utc).isoformat()))
            if cur.rowcount: new_items.append(item)
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("دسترسی ندارید. شناسه تلگرام خود را در پنل مدیر اضافه کنید.")
        return
    chat = str(update.effective_chat.id)
    with db() as c: c.execute("INSERT OR IGNORE INTO subscribers(chat_id,created_at) VALUES(?,?)",(chat,datetime.now(timezone.utc).isoformat()))
    await update.effective_message.reply_text("ربات جست‌وجوی خانه فعال شد 🏠\nآگهی‌های جدید به‌صورت خودکار برایت ارسال می‌شوند.\n/latest آخرین آگهی‌ها\n/filters فیلترهای فعلی\n/help راهنما")

async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("دسترسی ندارید. شناسه تلگرام خود را در پنل مدیر اضافه کنید."); return
    with db() as c: rows = c.execute("SELECT * FROM listings ORDER BY seen_at DESC LIMIT 10").fetchall()
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
    if not allowed(update): await update.effective_message.reply_text("دسترسی ندارید."); return
    s=get_settings()
    await update.effective_message.reply_text(
        "⚙️ فیلترهای فعلی\n"
        f"شهر: شیراز\nحداکثر قیمت: {money(s.get('max_price'))}\n"
        f"حداقل قیمت: {money(s.get('min_price')) if s.get('min_price') else 'بدون محدودیت'}\n"
        f"متراژ: {s.get('min_area') or '—'} تا {s.get('max_area') or '—'} متر\n"
        f"اتاق خواب: {s.get('bedrooms') or 'همه'}\n"
        f"پارکینگ: { {'any':'مهم نیست','yes':'دارد','no':'ندارد'}.get(s.get('parking'),'مهم نیست')}\n"
        f"محله‌ها: {s.get('neighborhoods') or 'همه'}\nفاصله بررسی: {s.get('interval')} ثانیه"
    )

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

