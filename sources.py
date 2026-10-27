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
from urllib.parse import urlparse, urlunparse


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
                page = requests.get("https://www.google.com/search", params={"q": term, "num": "10"},
                                    headers=headers, timeout=12)
                if page.status_code != 200:
                    continue
                soup_results = BeautifulSoup(page.text, "html.parser")
                for anchor in soup_results.select("a[href]"):
                    href = str(anchor.get("href", ""))
                    match = re.search(r"(?:https?://)?(?:www\\.)?(?:t\\.me|telegram\\.me)/(?:s/)?([A-Za-z0-9_]{5,64})(?:[/?#]|$)", href)
                    if match and match.group(1).lower() not in {"share", "proxy", "joinchat", "addstickers", "iv"}:
                        channel = match.group(1)
                        if channel not in channels:
                            channels.append(channel)
            except Exception as exc:
                print("Telegram channel discovery failed:", exc)
        for channel in channels:
            if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", channel):
                continue
            try:
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
                    if not title or not re.search(r"خانه|آپارتمان|ملک|فروش|خواب|متر|مسکونی", title):
                        continue
                    post_id = str(post.get("data-post") or "")
                    if "/" not in post_id:
                        continue
                    url = "https://t.me/" + post_id
                    dt = post.select_one("time[datetime]")
                    created_at = str(dt.get("datetime", "")).strip() if dt else ""
                    price_match = re.search(r"([\d۰-۹٠-٩,.٬]+\s*(?:میلیارد|میلیون)?\s*تومان|\d+(?:\.\d+)?\s*میلیارد)", title)
                    area_match = re.search(r"(\d{2,4})\s*متر", normalize_digits(title))
                    location_match = re.search(r"(?:محله|منطقه|محدوده)\s*[:：-]?\s*([^،\n]+)", title)
                    output.append(Listing(source="telegram", id=post_id, title=title[:500], url=url,
                        price=price_match.group(1) if price_match else "",
                        area=area_match.group(1) if area_match else "",
                        location=location_match.group(1).strip() if location_match else channel,
                        created_at=created_at))
            except Exception as exc:
                print(f"Telegram source @{channel} failed: {exc}")
        return output


def normalize_digits(value: str) -> str:
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(value or "").translate(table)
