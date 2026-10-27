"""Shared contracts for future multi-source real-estate listing adapters.

The existing Divar integration remains in app.py. New sources should implement
ListingSource.fetch() and return normalized records; this module intentionally
does not scrape sites before their current public access path is verified.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from hashlib import sha256
import re
from typing import Iterable, Protocol
from urllib.parse import urlparse, urlunparse, parse_qs, unquote


@dataclass
class Listing:
    source: str
    id: str
    title: str
    url: str
    price: str = ""
    area: str = ""
    bedrooms: str = ""
    location: str = ""
    created_at: str = ""
    fingerprint: str = ""

    def __post_init__(self):
        self.source = (self.source or "unknown").strip().lower()
        self.id = str(self.id or "").strip()
        self.title = re.sub(r"\s+", " ", str(self.title or "")).strip()
        self.url = str(self.url or "").strip()
        self.price = str(self.price or "").strip()
        self.area = str(self.area or "").strip()
        self.bedrooms = str(self.bedrooms or "").strip()
        self.location = re.sub(r"\s+", " ", str(self.location or "")).strip()
        self.created_at = str(self.created_at or "").strip()
        if not self.fingerprint:
            self.fingerprint = cross_source_fingerprint(self.title, self.price, self.area, self.location)

    def to_dict(self):
        return asdict(self)


class ListingSource(Protocol):
    key: str

    def fetch(self, search_context: dict) -> Iterable[Listing]:
        """Return listings from one authorized and verified source."""
        ...


def canonical_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))
    except (ValueError, AttributeError):
        return ""


def cross_source_fingerprint(title: str, price: str = "", area: str = "", location: str = "") -> str:
    """Conservative fingerprint. Similarity is a hint, not proof of identity."""
    normalized = lambda value: re.sub(r"[^\w\d]", "", str(value or "").casefold())
    material = "|".join(normalized(x) for x in (title, price, area, location))
    return sha256(material.encode("utf-8")).hexdigest()


def deduplicate(listings: Iterable[Listing]) -> list[Listing]:
    """Deduplicate exact source IDs/URLs and conservative normalized fingerprints."""
    output, seen_ids, seen_urls, seen_fingerprints = [], set(), set(), set()
    for item in listings:
        source_id = (item.source, item.id) if item.id else None
        url = canonical_url(item.url)
        fp = item.fingerprint
        if (source_id and source_id in seen_ids) or (url and url in seen_urls):
            continue
        enough_detail = bool(item.title and (item.price or item.area) and item.location)
        if enough_detail and fp in seen_fingerprints:
            continue
        output.append(item)
        if source_id:
            seen_ids.add(source_id)
        if url:
            seen_urls.add(url)
        if enough_detail:
            seen_fingerprints.add(fp)
    return output


class PublicTelegramSource:
    """Read public Telegram channel preview pages (t.me/s/<channel>) only."""
    key = "telegram"

    def fetch(self, search_context: dict) -> list[Listing]:
        import os
        import requests
        from bs4 import BeautifulSoup
        configured = [x.strip().lstrip("@") for x in os.getenv("PUBLIC_TELEGRAM_CHANNELS", "").split(",") if x.strip()]
        # Known public real-estate Telegram sources discovered from public web listings.
        seeds = [
            "shirazamlak1400", "Villa_sadracity", "Moshaveramlakshiraz",
            "Divar_shiiiraz", "Bazyar_Gem", "moshaverinmaskanshiraz"
        ]
        channels = list(dict.fromkeys(configured + seeds))
        output = []
        self.channels_discovered = len(channels)
        self.channels_scanned = 0
        headers = {"User-Agent": "Mozilla/5.0 (compatible; DivarFinder/1.0)", "Accept-Language": "fa,en;q=0.8"}
        # Discover additional public channel usernames from ordinary public search results.
        # This is best-effort and never bypasses login, private links, or anti-bot controls.
        search_terms = [
            'site:t.me/s/ املاک شیراز فروش آپارتمان',
            'site:telegram.me/s/ مشاور املاک شیراز خرید فروش',
            'site:t.me/ شیراز املاک آپارتمان فروش'
        ]
        for term in search_terms:
            try:
                page = requests.get("https://www.google.com/search", params={"q": term, "num": "100"},
                                    headers=headers, timeout=12)
                if page.status_code != 200:
                    continue
                soup_results = BeautifulSoup(page.text, "html.parser")
                for anchor in soup_results.select("a[href]"):
                    href = str(anchor.get("href", ""))
                    parsed_href = urlparse(href)
                    if parsed_href.path == "/url":
                        href = parse_qs(parsed_href.query).get("q", [href])[0]
                    href = unquote(href)
                    match = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{5,64})(?:[/?#]|$)", href)
                    if match and match.group(1).lower() not in {"share", "proxy", "joinchat", "addstickers", "iv"}:
                        channel = match.group(1)
                        if channel not in channels:
                            channels.append(channel)
            except Exception as exc:
                print("Telegram channel discovery failed:", exc)
        # Hard ceiling is 1000 public channels per run; the actual count depends on discovery results.
        channels = channels[:1000]
        self.channels_discovered = len(channels)
        for channel in channels:
            if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", channel):
                continue
            try:
                self.channels_scanned += 1
                response = requests.get(f"https://t.me/s/{channel}", headers=headers, timeout=15)
                if response.status_code in (401, 403, 404, 429):
                    print(f"Telegram source @{channel}: HTTP {response.status_code}")
                    continue
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                for post in soup.select(".tgme_widget_message"):
                    body = post.select_one(".tgme_widget_message_text")
                    if not body:
                        continue
                    title = " ".join(body.stripped_strings).strip()
                    if not title:
                        continue
                    # Strict intent: for-sale only. Reject rent, deposit, lease and mortgage posts.
                    rent_terms = r"اجاره|رهن|ودیعه|اجاره\s*ماهانه|رهن\s*کامل|رهن\s*و\s*اجاره|برای\s*اجاره|اجاره\s*داده\s*می‌شود"
                    sale_terms = r"فروش|فروشی|خرید|قیمت\s*کل|قیمت\s*تومان|قابل\s*فروش"
                    if re.search(rent_terms, title):
                        continue
                    if not re.search(sale_terms, title):
                        continue
                    # Exclude Sadra and other locations outside Shiraz. Require an explicit
                    # Shiraz mention or a recognized Shiraz neighborhood in the post.
                    shiraz_places = (
                        "شیراز", "قصردشت", "معالی آباد", "معالی‌آباد", "عفیف آباد", "عفیف‌آباد",
                        "فرهنگ شهر", "فرهنگ‌شهر", "قدوسی", "چمران", "زرگری", "ارم", "ستارخان",
                        "قصرالدشت", "تاچارا", "کوی وحدت", "شهرک گلستان", "شهرک صدرا"
                    )
                    # Sadra is a separate city and must not pass as Shiraz.
                    if re.search(r"صدرا|سعادت\s*شهر|مرودشت|کوار|زرقان|فسا|کازرون", title):
                        continue
                    if not any(place in title for place in shiraz_places):
                        continue
                    post_id = str(post.get("data-post") or "")
                    if "/" not in post_id:
                        continue
                    url = "https://t.me/" + post_id
                    dt = post.select_one("time[datetime]")
                    created_at = str(dt.get("datetime", "")).strip() if dt else ""
                    price_match = re.search(r"([\d۰-۹٠-٩,.٬]+\s*(?:میلیارد|میلیون)?\s*تومان|[\d۰-۹٠-٩]+(?:[.,٫]\d+)?\s*میلیارد)", title)
                    area_match = re.search(r"(\d{2,4})\s*متر", normalize_digits(title))
                    location_match = re.search(r"(?:محله|منطقه|محدوده)\s*[:：-]?\s*([^،\n]+)", title)
                    inferred_location = location_match.group(1).strip() if location_match else next(
                        (place for place in shiraz_places if place in title), "شیراز")
                    output.append(Listing(source="telegram", id=post_id, title=title[:500], url=url,
                        price=price_match.group(1) if price_match else "",
                        area=area_match.group(1) if area_match else "",
                        location=inferred_location,
                        created_at=created_at))
            except Exception as exc:
                print(f"Telegram source @{channel} failed: {exc}")
        return output


def normalize_digits(value: str) -> str:
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(value or "").translate(table)


class PublicPropertySearchSource:
    """Best-effort adapter for public, search-engine-indexed property listings.
    It never logs in or bypasses site restrictions. Each source is isolated so
    a blocked site does not stop the other sources.
    """
    key = "property-search"
    source_name = "property"
    domain = ""
    search_query = ""

    def fetch(self, search_context: dict) -> list[Listing]:
        import requests
        from bs4 import BeautifulSoup
        output = []
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                   "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.5"}
        urls = []
        query = f"site:{self.domain} {self.search_query}"
        # Search engines may rate-limit server-side requests. Try ordinary public
        # result pages in sequence; do not bypass CAPTCHAs, authentication, or blocks.
        engines = [
            ("Google", "https://www.google.com/search", {"q": query, "num": "20"}),
            ("DuckDuckGo", "https://html.duckduckgo.com/html/", {"q": query}),
            ("Bing", "https://www.bing.com/search", {"q": query, "count": "20"}),
        ]
        for engine_name, engine_url, params in engines:
            try:
                response = requests.get(engine_url, params=params, headers=headers, timeout=12)
                if response.status_code != 200:
                    print(f"{self.source_name} discovery via {engine_name}: HTTP {response.status_code}")
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                before = len(urls)
                for anchor in soup.select("a[href]"):
                    href = str(anchor.get("href", "")).strip()
                    parsed = urlparse(href)
                    if parsed.path == "/url":
                        href = parse_qs(parsed.query).get("q", [""])[0]
                    href = unquote(href)
                    p = urlparse(href)
                    host = p.netloc.lower().split(":")[0]
                    if p.scheme not in ("http", "https") or not (host == self.domain or host.endswith("." + self.domain)):
                        continue
                    if any(x in p.path.lower() for x in ("/login", "/register", "/search", "/contact", "/about")):
                        continue
                    canonical = canonical_url(href)
                    if canonical and canonical not in urls:
                        urls.append(canonical)
                    if len(urls) >= 8:
                        break
                if len(urls) > before:
                    print(f"{self.source_name} discovery via {engine_name}: candidate_pages={len(urls)}")
                    break
            except Exception as exc:
                print(f"{self.source_name} discovery via {engine_name} failed: {exc}")
        for url in urls:
                try:
                    page = requests.get(url, headers=headers, timeout=10)
                    if page.status_code in (401, 403, 404, 429):
                        continue
                    page.raise_for_status()
                    detail = BeautifulSoup(page.text, "html.parser")
                    for node in detail.select("script,style,nav,footer,header"):
                        node.decompose()
                    title_node = detail.find("h1") or detail.find("title")
                    title = " ".join(title_node.stripped_strings) if title_node else ""
                    body = " ".join(detail.stripped_strings)
                    if len(title) < 8:
                        title = body[:180]
                    searchable = (title + " " + body[:12000])
                    # Keep these adapters aligned with the user's intent: Shiraz city,
                    # residential sale listings only. Do not mistake rent/deposit values
                    # for purchase prices.
                    if not re.search(r"شیراز", searchable):
                        continue
                    if re.search(r"اجاره|رهن\s*و\s*اجاره|ودیعه|رهن\s*کامل|اجاره\s*ماهانه", title):
                        continue
                    if not re.search(r"فروش|فروشی|خرید|قیمت\s*کل", title):
                        continue
                    if re.search(r"مرودشت|کوار|زرقان|فسا|کازرون|صدرا", title):
                        continue
                    price_match = re.search(r"([\d۰-۹٠-٩,.٬]+\s*(?:میلیارد|میلیون)?\s*تومان|[\d۰-۹٠-٩,.٬]+\s*میلیارد)", searchable)
                    area_match = re.search(r"(\d{2,4})\s*متر", normalize_digits(searchable))
                    bedroom_match = re.search(r"(\d)\s*خواب", title)
                    location_match = re.search(r"(?:محله|محدوده|منطقه)\s*[:：-]?\s*([^،؛|]+)", title)
                    identifier = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                    output.append(Listing(source=self.source_name, id=identifier,
                        title=title[:300], url=url,
                        price=price_match.group(1) if price_match else "",
                        area=area_match.group(1) if area_match else "",
                        bedrooms=bedroom_match.group(1) if bedroom_match else "",
                        location=location_match.group(1).strip()[:100] if location_match else "شیراز"))
                except Exception as exc:
                    print(f"{self.source_name} listing fetch failed: {exc}")
        print(f"Source {self.source_name}: candidate_pages={min(len(urls), 8)}; listings_found={len(output)}")
        return output


class SheypoorSource(PublicPropertySearchSource):
    key = "sheypoor"
    source_name = "sheypoor"
    domain = "sheypoor.com"
    search_query = "خرید آپارتمان فروش خانه شیراز"


class KilidSource(PublicPropertySearchSource):
    key = "kilid"
    source_name = "kilid"
    domain = "kilid.com"
    search_query = "خرید آپارتمان شیراز"


class IranfileSource(PublicPropertySearchSource):
    key = "iranfile"
    source_name = "iranfile"
    domain = "iranfile.ir"
    search_query = "خرید آپارتمان شیراز"


class MelkRadarSource(PublicPropertySearchSource):
    key = "melkradar"
    source_name = "melkradar"
    domain = "melkradar.com"
    search_query = "شیراز فروش آپارتمان"


class ZoomilaSource(PublicPropertySearchSource):
    key = "zoomila"
    source_name = "zoomila"
    domain = "zoomila.com"
    search_query = "property-for-sale/shiraz آپارتمان"


class InstagramBusinessSource:
    """Official Instagram Graph API adapter for configured public professional accounts.

    Requires INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ID, and
    INSTAGRAM_PROPERTY_ACCOUNTS (comma-separated usernames). It does not scrape
    Instagram HTML or bypass login/anti-bot controls.
    """
    key = "instagram"
    source_name = "instagram"

    def fetch(self, search_context: dict) -> list[Listing]:
        import os
        import requests

        token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
        business_id = os.getenv("INSTAGRAM_BUSINESS_ID", "").strip()
        accounts = [x.strip().lstrip("@") for x in
                    os.getenv("INSTAGRAM_PROPERTY_ACCOUNTS", "").split(",") if x.strip()]
        if not token or not business_id or not accounts:
            print("Instagram source skipped: configure INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ID, and INSTAGRAM_PROPERTY_ACCOUNTS in .env")
            return []

        api_version = os.getenv("INSTAGRAM_GRAPH_API_VERSION", "v23.0").strip()
        endpoint = f"https://graph.facebook.com/{api_version}/{business_id}"
        output = []
        headers = {"Accept": "application/json"}
        for username in accounts[:25]:
            if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
                print(f"Instagram source: invalid username skipped: {username!r}")
                continue
            try:
                response = requests.get(endpoint, params={
                    "fields": f"business_discovery.username({username}){{username,media.limit(50){{id,caption,permalink,timestamp,media_type}}}}",
                    "access_token": token,
                }, headers=headers, timeout=20)
                if response.status_code != 200:
                    try:
                        detail = response.json().get("error", {}).get("message", response.text[:200])
                    except Exception:
                        detail = response.text[:200]
                    print(f"Instagram source @{username}: Graph API HTTP {response.status_code}: {detail}")
                    continue
                payload = response.json()
                discovery = payload.get("business_discovery") or {}
                media = (discovery.get("media") or {}).get("data") or []
                for post in media:
                    caption = re.sub(r"\s+", " ", str(post.get("caption") or "")).strip()
                    if not caption:
                        continue
                    # Only Shiraz home-sale posts; reject rent and non-Shiraz locations.
                    if not re.search(r"فروش|فروشی|خرید|قیمت\s*کل|قابل\s*فروش", caption):
                        continue
                    if re.search(r"اجاره|ودیعه|رهن\s*و\s*اجاره|رهن\s*کامل|اجاره\s*ماهانه", caption):
                        continue
                    if not re.search(r"شیراز|قصردشت|قصرالدشت|معالی[\s‌]*آباد|عفیف[\s‌]*آباد|فرهنگ[\s‌]*شهر|قدوسی|چمران|زرگری|ستارخان|تاچارا", caption):
                        continue
                    if re.search(r"صدرا|سعادت\s*شهر|مرودشت|کوار|زرقان|فسا|کازرون", caption):
                        continue
                    price_match = re.search(r"([\d۰-۹٠-٩,.٬]+\s*(?:میلیارد|میلیون)?\s*تومان|[\d۰-۹٠-٩]+(?:[.,٫]\d+)?\s*میلیارد)", caption)
                    area_match = re.search(r"(\d{2,4})\s*متر", normalize_digits(caption))
                    bedroom_match = re.search(r"(\d)\s*خواب", caption)
                    post_id = str(post.get("id") or post.get("permalink") or "")
                    permalink = str(post.get("permalink") or "").strip()
                    if not permalink.startswith("https://www.instagram.com/"):
                        continue
                    output.append(Listing(
                        source="instagram",
                        id=post_id,
                        title=caption[:500],
                        url=permalink,
                        price=price_match.group(1) if price_match else "",
                        area=area_match.group(1) if area_match else "",
                        bedrooms=bedroom_match.group(1) if bedroom_match else "",
                        location="شیراز",
                        created_at=str(post.get("timestamp") or "")
                    ))
                print(f"Instagram source @{username}: media_checked={len(media)}; listings_found={len(output)}")
            except Exception as exc:
                print(f"Instagram source @{username} failed: {exc}")
        return output
