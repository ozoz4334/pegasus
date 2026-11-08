"""Pegasus kampanya sayfalarını çeker ve ayrıştırır.

Yalnızca canlı sitede gözlemlenmiş HTML sınıfları kullanılır (2026-11-08):

Liste sayfaları
  * ``.campaign-card``               – "Güncel" kartları (kampanyali-ucak-biletleri, sana-ozel)
  * ``.campaign-swiper-card``        – "Geçmiş" kartları (past-campaigns bölümü)
  * ``.bolbol-campaign-list__item``  – /bolbol/kampanyalar kartları
  * ``.past-cmps-list__item``        – /gecmis-kampanyalar kartları
Detay sayfası
  * ``h1.campaign-header__content__desc__title``
  * ``.campaign-header__content__dates time[datetime]``
  * ``p.campaign-detail__summary``
  * ``.campaign-detail-content__article`` (kalın başlıklı <p> blokları)

Sunucu ETag/Last-Modified döndürmediği ve ``Cache-Control: no-store`` kullandığı için
koşullu GET kullanılmaz; değişiklik içerik hash'iyle bulunur.
"""

from __future__ import annotations

import email.utils
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

import config
from config import Source
from textutil import clean_text, extract_date_range, extract_dates, norm_text, tr_lower, truncate

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# HTTP istemcisi
# --------------------------------------------------------------------------- #


@dataclass
class FetchResult:
    url: str
    status: int | None = None
    text: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text)

    @property
    def gone(self) -> bool:
        """Sayfa kalıcı olarak yok mu (404/410)?"""
        return self.status in (404, 410)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


class HttpClient:
    """Bağlantıyı yeniden kullanan, 429/Retry-After ve geçici hatalara saygılı hafif istemci.

    ``get`` asla istisna fırlatmaz; sonucu ``FetchResult`` olarak döndürür. Böylece tek bir
    başarısız kaynak bütün çalışmayı durdurmaz.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = config.HTTP_MAX_RETRIES,
        timeout: tuple[float, float] = (config.CONNECT_TIMEOUT, config.READ_TIMEOUT),
    ):
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "tr-TR,tr;q=0.9",
            }
        )
        self._sleep = sleep
        self.max_retries = max_retries
        self.timeout = timeout
        self.request_count = 0

    def get(self, url: str) -> FetchResult:
        last = FetchResult(url=url, error="istek yapılmadı")
        for attempt in range(self.max_retries + 1):
            is_last = attempt == self.max_retries
            try:
                self.request_count += 1
                response = self.session.get(url, timeout=self.timeout)
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                last = FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")
                log.warning("Bağlantı hatası (%s) deneme %d/%d: %s", url, attempt + 1,
                            self.max_retries + 1, type(exc).__name__)
                if is_last:
                    return last
                self._sleep(self._backoff(attempt))
                continue
            except requests.RequestException as exc:
                return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")

            status = response.status_code
            if status == 200:
                return FetchResult(url=url, status=200, text=response.text)
            last = FetchResult(url=url, status=status, error=f"HTTP {status}")
            if status in (404, 410):
                return last
            if status == 429:
                wait = _parse_retry_after(response.headers.get("Retry-After"))
                wait = config.BACKOFF_BASE_SECONDS * (2 ** attempt) if wait is None else wait
                log.warning("HTTP 429 (%s); Retry-After=%.0fs", url, wait)
                if is_last or wait > config.MAX_RETRY_AFTER_SECONDS:
                    last.error = f"HTTP 429 (Retry-After {wait:.0f}s)"
                    return last
                self._sleep(wait)
                continue
            if status == 403:
                # Bot koruması olabilir: bir kez nazikçe tekrar dene, ısrar etme.
                log.warning("HTTP 403 (%s); erişim kısıtlaması olabilir", url)
                if attempt >= 1 or is_last:
                    return last
                self._sleep(self._backoff(attempt))
                continue
            if status >= 500:
                log.warning("HTTP %s (%s) deneme %d/%d", status, url, attempt + 1, self.max_retries + 1)
                if is_last:
                    return last
                self._sleep(self._backoff(attempt))
                continue
            return last  # Diğer 4xx/3xx: tekrar denemenin anlamı yok.
        return last

    @staticmethod
    def _backoff(attempt: int) -> float:
        return config.BACKOFF_BASE_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)


# --------------------------------------------------------------------------- #
# Liste ayrıştırma
# --------------------------------------------------------------------------- #

_CARD_CLASSES = frozenset(
    {
        "campaign-card",
        "campaign-swiper-card",
        "bolbol-campaign-list__item",
        "past-cmps-list__item",
    }
)
_PAST_PREFIXES = ("past-campaigns", "past-cmps-list")
_TITLE_SELECTORS = (
    ".campaign-card__title",
    ".bolbol-campaign-list__title",
    ".past-cmps-list__title",
    ".campaign-swiper-card__title",
    ".campaign-swiper-card__desc",
)
_SLUG_RE = re.compile(rf"^{re.escape(config.CAMPAIGN_PATH_PREFIX)}([^/?#]+)/?$")


@dataclass
class Card:
    slug: str
    url: str
    title: str
    promotion_id: str | None
    section: str  # "current" | "past"
    start: date | None
    end: date | None
    description: str
    source: str


@dataclass
class SourceResult:
    key: str
    url: str
    ok: bool
    status: int | None = None
    error: str | None = None
    cards: list[Card] = field(default_factory=list)


@dataclass
class Listing:
    slug: str
    url: str
    title: str
    promotion_id: str | None
    section: str
    start: date | None
    end: date | None
    sources: list[str]


@dataclass
class ScanResult:
    sources: dict[str, SourceResult]
    listings: dict[str, Listing]

    @property
    def complete(self) -> bool:
        """Tüm liste kaynakları sağlıklı mı? (ilk kurulum için gereklidir)"""
        return bool(self.sources) and all(r.ok for r in self.sources.values())

    @property
    def disappearance_reliable(self) -> bool:
        """'Listeden kayboldu' kararı için gerekli kaynaklar sağlıklı mı?"""
        required = [s for s in config.SOURCES if s.required_for_disappearance]
        return all(self.sources.get(s.key) is not None and self.sources[s.key].ok for s in required)


def _classes(tag: Tag) -> list[str]:
    value = tag.get("class")
    if not value:
        return []
    return value if isinstance(value, list) else str(value).split()


def _card_container(anchor: Tag) -> Tag | None:
    for parent in anchor.parents:
        if isinstance(parent, Tag) and _CARD_CLASSES.intersection(_classes(parent)):
            return parent
    return None


def _is_past(anchor: Tag, container: Tag) -> bool:
    for node in [container, *container.parents]:
        if not isinstance(node, Tag):
            continue
        for cls in _classes(node):
            if cls.startswith(_PAST_PREFIXES):
                return True
    area = str(anchor.get("data-gtm-promotion-area") or "")
    return "past" in area.lower()


def _card_title(anchor: Tag, container: Tag) -> str:
    for selector in _TITLE_SELECTORS:
        node = container.select_one(selector)
        if node and clean_text(node.get_text(" ")):
            return clean_text(node.get_text(" "))
    for attr in ("title", "data-gtm-promotion-name"):
        value = clean_text(str(anchor.get(attr) or ""))
        if value:
            return value
    image = container.find("img", alt=True)
    if image and clean_text(str(image.get("alt"))) not in ("", "Pegasus Havayolları"):
        return clean_text(str(image.get("alt")))
    return clean_text(anchor.get_text(" "))


def _card_description(container: Tag) -> str:
    node = container.select_one(".bolbol-campaign-list__description, .past-cmps-list__description")
    return clean_text(node.get_text(" ")) if node else ""


def _card_dates(container: Tag) -> tuple[date | None, date | None]:
    collected: list[date] = []
    for node in container.find_all("time"):
        from_attr = extract_dates(str(node.get("datetime") or ""))
        collected.extend(from_attr or extract_dates(node.get_text(" ")))
    if not collected:
        return None, None
    if len(collected) == 1:
        return collected[0], None
    return collected[0], collected[-1]


def parse_listing(html: str, source_key: str) -> list[Card]:
    """Bir liste sayfasındaki kampanya kartlarını çıkarır (aynı slug'ı tekilleştirir)."""
    soup = BeautifulSoup(html, "html.parser")
    cards: dict[str, Card] = {}
    for anchor in soup.find_all("a", href=True):
        path = urlparse(str(anchor["href"])).path
        match = _SLUG_RE.match(path)
        if not match or match.group(1) in config.NON_CAMPAIGN_SLUGS:
            continue
        container = _card_container(anchor)
        if container is None:
            continue  # Menü/altbilgi bağlantısı; kampanya kartı değil.
        slug = match.group(1)
        past = _is_past(anchor, container)
        start, end = _card_dates(container)
        card = Card(
            slug=slug,
            url=urljoin(config.BASE_URL, f"{config.CAMPAIGN_PATH_PREFIX}{slug}"),
            title=_card_title(anchor, container),
            promotion_id=clean_text(str(anchor.get("data-gtm-promotion-id") or "")) or None,
            section="past" if past else "current",
            start=start,
            end=end,
            description=_card_description(container),
            source=source_key,
        )
        previous = cards.get(slug)
        if previous is None:
            cards[slug] = card
        elif previous.section == "past" and card.section == "current":
            cards[slug] = card  # Aynı sayfada hem güncel hem geçmişte görünüyorsa güncel sayılır.
    return list(cards.values())


def _fetch_source(client: HttpClient, source: Source) -> SourceResult:
    result = client.get(source.url)
    if not result.ok:
        log.error("Kaynak alınamadı: %s (%s)", source.key, result.error)
        return SourceResult(source.key, source.url, ok=False, status=result.status, error=result.error)
    try:
        cards = parse_listing(result.text, source.key)
    except Exception as exc:  # noqa: BLE001 - ayrıştırıcı hatası bütün çalışmayı durdurmamalı
        log.exception("Kaynak ayrıştırılamadı: %s", source.key)
        return SourceResult(source.key, source.url, ok=False, status=200, error=f"ayrıştırma hatası: {exc}")
    if not cards:
        msg = "kampanya kartı bulunamadı (HTML yapısı değişmiş olabilir)"
        log.error("Kaynak %s: %s", source.key, msg)
        return SourceResult(source.key, source.url, ok=False, status=200, error=msg)
    log.info("Kaynak %s: %d kart (%d güncel)", source.key, len(cards),
             sum(1 for c in cards if c.section == "current"))
    return SourceResult(source.key, source.url, ok=True, status=200, cards=cards)


def scan_sources(
    client: HttpClient,
    sources: Iterable[Source] = config.SOURCES,
    workers: int = config.LIST_FETCH_WORKERS,
) -> ScanResult:
    sources = list(sources)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda s: _fetch_source(client, s), sources))
    by_key = {r.key: r for r in results}

    listings: dict[str, Listing] = {}
    for source in sources:  # Kaynak önceliği: config sırası.
        for card in by_key[source.key].cards:
            existing = listings.get(card.slug)
            if existing is None:
                listings[card.slug] = Listing(
                    slug=card.slug, url=card.url, title=card.title,
                    promotion_id=card.promotion_id, section=card.section,
                    start=card.start, end=card.end, sources=[source.key],
                )
                continue
            if source.key not in existing.sources:
                existing.sources.append(source.key)
            if card.section == "current":
                existing.section = "current"
            if not existing.title and card.title:
                existing.title = card.title
            existing.promotion_id = existing.promotion_id or card.promotion_id
            existing.start = existing.start or card.start
            existing.end = existing.end or card.end
    return ScanResult(sources=by_key, listings=listings)


# --------------------------------------------------------------------------- #
# Detay ayrıştırma
# --------------------------------------------------------------------------- #


@dataclass
class DetailData:
    title: str
    summary: str
    start_date: date | None
    end_date: date | None
    offer: str
    sale_text: str
    flight_text: str
    eligibility: str
    routes: list[str]
    price_terms: str
    conditions: str
    discount: str | None
    price: str | None
    promo_codes: list[str]
    bolpuan: list[str]

    @property
    def sale_period(self) -> str | None:
        return extract_date_range(self.sale_text)

    @property
    def flight_period(self) -> str | None:
        return extract_date_range(self.flight_text)


_PERCENT_RE = re.compile(r"%\s*(\d{1,3})|(\d{1,3})\s*%")
_PRICE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(€|euro|eur\b|₺|tl\b)", re.IGNORECASE)
_PROMO_RE = re.compile(
    r"(?i:(?:promosyon|indirim|kampanya)\s+kodu)\s*(?:[:：]|olan|ile)?\s*[\"“‘']?"
    r"([A-Z0-9ÇĞİÖŞÜ]{4,20})\b"
)
_BOLPUAN_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)\s*(?:adet\s+)?bolpuan")
_BOLPUAN_KAT_RE = re.compile(r"(\d+)\s*kat\s+bolpuan")


def _extract_discount(*texts: str) -> str | None:
    for text in texts:
        values: list[str] = []
        for m in _PERCENT_RE.finditer(text or ""):
            number = m.group(1) or m.group(2)
            if number not in values:
                values.append(number)
        if values:
            return "/".join(f"%{v}" for v in values)
    return None


def _extract_price(*texts: str) -> str | None:
    for text in texts:
        m = _PRICE_RE.search(text or "")
        if m:
            unit = m.group(2).lower()
            symbol = {"€": "€", "euro": "€", "eur": "€", "₺": "TL", "tl": "TL"}[unit]
            amount = m.group(1).replace(",", ".")
            return f"{amount}{symbol}" if symbol == "€" else f"{amount} TL"
    return None


def _extract_promo_codes(text: str) -> list[str]:
    codes = {m.group(1) for m in _PROMO_RE.finditer(text or "") if any(ch.isalpha() for ch in m.group(1))}
    return sorted(codes)


def _extract_bolpuan(text: str) -> list[str]:
    lowered = tr_lower(text or "")
    found: set[str] = set()
    for m in _BOLPUAN_RE.finditer(lowered):
        found.add(f"{m.group(1).replace('.', '')} BolPuan")
    for m in _BOLPUAN_KAT_RE.finditer(lowered):
        found.add(f"{m.group(1)} kat BolPuan")
    return sorted(found)


_SECTION_KEYS: tuple[tuple[str, str], ...] = (
    ("satış tarih", "sale"),
    ("uçuş tarih", "flight"),
    ("yararlanabilecek", "eligibility"),
    ("geçerli olduğu hat", "routes"),
    ("kampanya teklifi", "offer"),
    ("ücret", "price_terms"),
)


def _section_for(heading: str) -> str:
    lowered = tr_lower(heading)
    for needle, key in _SECTION_KEYS:
        if needle in lowered:
            return key
    return "other"


_KNOWN_HEADING_RE = re.compile(
    r"^(?P<h>Kampanya Teklifi"
    r"|Kampanyanın Geçerli Olduğu [^:•]{2,60}"
    r"|Kampanyadan Yararlanabilecek [^:•]{2,40}"
    r"|Kampanya Kuralları|Kampanya Koşulları"
    r"|Ücret|Koltuk Adedi"
    r"|Kampanyalı [^:•]{2,60})\s*:",
    re.IGNORECASE,
)
_ROUTE_ITEM_MAX_CHARS = 100
_TIER_RE = re.compile(r"^\d[\d.,]*\s*(?:€|euro|eur|tl|₺)", re.IGNORECASE)


def _has_block_ancestor(el: Tag, article: Tag) -> bool:
    for parent in el.parents:
        if parent is article:
            return False
        if isinstance(parent, Tag) and parent.name in ("p", "li"):
            return True
    return False


def _article_blocks(article: Tag) -> list[tuple[str | None, str, str]]:
    """Makale metnini (başlık, metin, etiket) bloklarına böler.

    Başlık: ``<p><strong>Başlık:</strong> metin</p>`` kalıbındaki kalın etiket.
    """
    blocks: list[tuple[str | None, str, str]] = []
    for el in article.find_all(["p", "li", "h2", "h3", "h4"]):
        if _has_block_ancestor(el, article):
            continue  # İç içe bloklar üst bloğun metnine zaten dahildir.
        text = clean_text(el.get_text(" "))
        if not text:
            continue
        heading: str | None = None
        if el.name == "p":
            first = next(
                (c for c in el.children if getattr(c, "name", None) or str(c).strip()), None
            )
            if getattr(first, "name", None) in ("strong", "b"):
                candidate = clean_text(first.get_text(" "))
                if candidate.endswith(":") and len(candidate) <= 150:
                    heading = candidate
                    text = clean_text(text[len(candidate):]) if text.startswith(candidate) else text
            if heading is None:
                known = _KNOWN_HEADING_RE.match(text)
                if known:
                    heading = clean_text(known.group("h")) + ":"
                    text = clean_text(text[known.end():])
        blocks.append((heading, text, el.name))
    return blocks


def parse_detail(html: str) -> DetailData | None:
    """Kampanya detay sayfasını ayrıştırır. Beklenen yapı yoksa None döner."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("h1.campaign-header__content__desc__title") or soup.select_one(
        "h2.campaign-detail__title"
    )
    article = soup.select_one(".campaign-detail-content__article")
    if h1 is None or article is None:
        return None
    title = clean_text(h1.get_text(" "))
    if not title:
        return None
    for junk in article.find_all(["script", "style", "form", "input", "noscript"]):
        junk.decompose()

    summary_node = soup.select_one("p.campaign-detail__summary")
    summary = clean_text(summary_node.get_text(" ")) if summary_node else ""

    date_nodes = soup.select(".campaign-header__content__dates time")
    collected: list[date] = []
    for node in date_nodes:
        collected.extend(extract_dates(str(node.get("datetime") or "")) or extract_dates(node.get_text(" ")))
    if not collected:
        for node in soup.select(".campaign-detail__date__info time"):
            collected.extend(
                extract_dates(str(node.get("datetime") or "")) or extract_dates(node.get_text(" "))
            )
    start = collected[0] if collected else None
    end = collected[-1] if len(collected) >= 2 else None

    sections: dict[str, list[str]] = {}
    route_items: list[str] = []
    route_tier: str | None = None
    current_key = "other"
    all_lines: list[str] = []
    for heading, text, _kind in _article_blocks(article):
        if heading is not None:
            current_key = _section_for(heading)
            if text:
                sections.setdefault(current_key, []).append(text)
            all_lines.append(f"{heading} {text}".strip())
            continue
        if (
            current_key == "routes"
            and len(text) <= _ROUTE_ITEM_MAX_CHARS
            and not text.startswith("•")
        ):
            if _TIER_RE.match(text):
                route_tier = text  # "11€ + vergilerle" gibi fiyat başlığı; sonraki hatlara eklenir.
            else:
                route_items.append(f"{text} ({route_tier})" if route_tier else text)
            continue
        sections.setdefault(current_key, []).append(text)
        all_lines.append(text)

    conditions = truncate(" ".join(all_lines), config.MAX_CONDITIONS_CHARS)
    if len(norm_text(conditions)) < 40:
        return None  # Makale boş/anlamsız: HTML yapısı değişmiş olabilir.

    routes = route_items

    return DetailData(
        title=title,
        summary=summary,
        start_date=start,
        end_date=end,
        offer=" ".join(sections.get("offer", [])),
        sale_text=" ".join(sections.get("sale", [])),
        flight_text=" ".join(sections.get("flight", [])),
        eligibility=" ".join(sections.get("eligibility", [])),
        routes=routes,
        price_terms=" ".join(sections.get("price_terms", [])),
        conditions=conditions,
        discount=_extract_discount(title, summary),
        price=_extract_price(title, summary),
        promo_codes=_extract_promo_codes(conditions),
        bolpuan=_extract_bolpuan(f"{title} {summary} {' '.join(sections.get('offer', []))}"),
    )


# --------------------------------------------------------------------------- #
# Detay çekme
# --------------------------------------------------------------------------- #


@dataclass
class DetailResult:
    slug: str
    status: str  # "ok" | "gone" | "error" | "unparsable"
    data: DetailData | None = None
    error: str | None = None


def fetch_details(
    client: HttpClient,
    urls: dict[str, str],
    *,
    delay: float = config.DETAIL_REQUEST_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, DetailResult]:
    """Detay sayfalarını sırayla (nazikçe) çeker."""
    results: dict[str, DetailResult] = {}
    for index, (slug, url) in enumerate(urls.items()):
        if index and delay:
            sleep(delay)
        fetched = client.get(url)
        if fetched.gone:
            results[slug] = DetailResult(slug, "gone", error=fetched.error)
            continue
        if not fetched.ok:
            results[slug] = DetailResult(slug, "error", error=fetched.error)
            continue
        try:
            data = parse_detail(fetched.text)
        except Exception as exc:  # noqa: BLE001
            log.exception("Detay ayrıştırılamadı: %s", slug)
            results[slug] = DetailResult(slug, "unparsable", error=str(exc))
            continue
        if data is None:
            log.error("Detay yapısı tanınmadı (HTML değişmiş olabilir): %s", slug)
            results[slug] = DetailResult(slug, "unparsable", error="beklenen alanlar bulunamadı")
        else:
            results[slug] = DetailResult(slug, "ok", data=data)
    return results
