"""Test yardımcıları: Pegasus'un gerçek HTML yapısını taklit eden sayfa üreticileri ve sahte istemciler."""

from __future__ import annotations

import html
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config
import main
from scraper import FetchResult
from telegram_notifier import SendResult

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
BASE = config.BASE_URL
PATH = config.CAMPAIGN_PATH_PREFIX


def esc(text: str) -> str:
    return html.escape(text, quote=True)


# --------------------------------------------------------------------------- #
# HTML üreticileri (canlı sitede gözlemlenen sınıf adlarıyla)
# --------------------------------------------------------------------------- #


def _dotted(iso_date: str, time_part: str = "00:00:00") -> str:
    year, month, day = iso_date.split("-")
    return f"{day}.{month}.{year} {time_part}"


def current_card(slug, title, start, end, promo_id=None):
    return f"""
<div class="active-campaigns__item js-active-campaigns__item">
  <div class="campaign-card">
    <figure class="campaign-card__figure"><img src="https://mediap.flypgs.com/x/{uuid.uuid4().hex}.png" alt="Pegasus Havayolları"></figure>
    <article class="campaign-card__article"><strong class="campaign-card__title">{esc(title)}</strong></article>
    <div class="campaign-card__footer">
      <div class="campaign-card__date"><span>TARİH: </span>
        <time datetime="{_dotted(start, '13:29:00')}">x</time><span> / </span>
        <time datetime="{_dotted(end, '23:59:00')}">y</time></div>
    </div>
    <a href="{PATH}{slug}?utm_source=test" class="campaign-card__streched-link" data-gtm-event
       data-gtm-promotion-id="{promo_id or uuid.uuid5(uuid.NAMESPACE_URL, slug)}"
       data-gtm-promotion-area="Campaigns Page - Current"></a>
  </div>
</div>"""


def collab_card(slug, title, start, end):
    return f"""
<div class="swiper-slide collab__slide">
  <div class="campaign-swiper-card">
    <article class="campaign-swiper-card__article">
      <strong class="campaign-swiper-card__title">{esc(title)}</strong>
      <p class="campaign-swiper-card__desc">Açıklama</p></article>
    <div class="campaign-swiper-card__footer"><div class="campaign-swiper-card__date">
      <time datetime="{_dotted(start)}">a</time><span>/</span><time datetime="{_dotted(end)}">b</time></div></div>
    <a href="{PATH}{slug}" class="campaign-swiper-card__streched-link"
       data-gtm-promotion-area="Campaigns Page - Collabrations"></a>
  </div>
</div>"""


def past_card(slug, title):
    return f"""
<div class="swiper-slide past-campaigns__slide">
  <div class="campaign-swiper-card">
    <article class="campaign-swiper-card__article"><p class="campaign-swiper-card__desc">{esc(title)}</p></article>
    <a href="{PATH}{slug}" class="campaign-swiper-card__streched-link"
       data-gtm-promotion-area="Campaigns Page - Past Campaigns"></a>
  </div>
</div>"""


NAV = f"""
<nav><a href="/kampanyali-ucak-biletleri">Kampanyalar</a><a href="/bolbol/kampanyalar">BolBol</a>
<a href="{PATH}gecmis-kampanyalar">Geçmiş Kampanyalar</a></nav>"""


def list_page(current=(), collab=(), past=(), noise=""):
    return f"""<!DOCTYPE html><html><body>{NAV}{noise}
<section class="active-campaigns"><div class="active-campaigns__list">{''.join(current)}</div></section>
<section class="collab"><div class="swiper-wrapper">{''.join(collab)}</div></section>
<section class="past-campaigns"><div class="swiper-wrapper past-campaigns__swiper__wrapper">{''.join(past)}</div></section>
</body></html>"""


def bolbol_item(slug, title, start_text, end_text):
    return f"""
<div class="bolbol-campaign-list__item">
  <div class="bolbol-campaign-list__content">
    <h2 class="bolbol-campaign-list__title">{esc(title)}</h2>
    <p class="bolbol-campaign-list__description">Açıklama</p>
    <time class="bolbol-campaign-list__date">Kampanya Tarihi : {start_text} / {end_text}</time>
    <a class="bolbol-campaign-list__detail__link" href="{PATH}{slug}">DETAY</a>
  </div>
</div>"""


def bolbol_page(items=()):
    return f"<html><body>{NAV}<div class='bolbol-campaign-list'>{''.join(items)}</div></body></html>"


def past_list_item(slug, title, dates_text):
    return f"""
<div class="past-cmps-list__item js-past-cmps-list__item">
  <div class="past-cmps-list__content"><h2 class="past-cmps-list__title">{esc(title)}</h2>
    <time class="past-cmps-list__date" datetime="{dates_text}">{dates_text}</time>
    <a class="past-cmps-list__link" href="{PATH}{slug}">DETAY</a></div>
</div>"""


def gecmis_page(items=()):
    return f"<html><body>{NAV}<div class='past-cmps-list'>{''.join(items)}</div></body></html>"


FILLER = (
    "Kampanya kapsamında geçerli olan kurallar ve koşullar aşağıda belirtilmiştir. "
    "Pegasus kampanya süresince kurallarda değişiklik yapma hakkını saklı tutar. "
)


def detail_page(
    title,
    start="2026-11-01",
    end="2026-12-31",
    summary=None,
    offer="Seçili uçuşlarda geçerli kampanya teklifi.",
    sale="Kampanya, 10-12 Kasım 2026 tarihleri arasında geçerlidir.",
    flight="Kampanya, 15 Kasım 2026 – 30 Mart 2027 tarihleri arasındaki uçuşlar için geçerlidir.",
    eligibility="Kampanyadan yalnızca Pegasus BolBol üyeleri faydalanabilir.",
    routes=("İstanbul Sabiha Gökçen-Berlin-İstanbul Sabiha Gökçen",),
    extra="",
    noise="",
    whitespace="",
):
    route_html = "".join(f"<p>{esc(r)}</p>" for r in routes)
    return f"""<!DOCTYPE html><html lang="tr"><head><title>x</title></head><body>
{noise}
<h1 class="campaign-header__content__desc__title">{esc(title)}</h1>
<div class="campaign-header__content__dates"><span class="campaign-header__content__dates__info">
  <time datetime="{start}" class="campaign-header__content__timestamp">{start}</time> /
  <time datetime="{end}" class="campaign-header__content__timestamp">{end}</time></span></div>
<section class="campaign-detail"><h2 class="campaign-detail__title">{esc(title)}</h2>
<p class="campaign-detail__summary">{esc(summary if summary is not None else title)}</p>
<div class="campaign-detail__login__zone">Giriş Yapın Parola Beni Hatırla</div>
<div class="campaign-detail-content__article">
<p><strong>Kampanya Detayları</strong></p>
<p><strong>Kampanya Teklifi: </strong>{whitespace}{esc(offer)}</p>
<p><strong>Kampanyanın Geçerli Olduğu Satış Tarihleri: </strong>{esc(sale)}</p>
<p><strong>Kampanyanın Geçerli Olduğu Uçuş Tarihi: </strong>{esc(flight)}</p>
<p>Kampanyadan Yararlanabilecek Kişiler: {esc(eligibility)}</p>
<p>Kampanyanın Geçerli Olduğu Hatlar:</p>
{route_html}
<p>{esc(FILLER)}{esc(extra)}</p>
</div></section></body></html>"""


# --------------------------------------------------------------------------- #
# Sahte site, istemci, bildirici
# --------------------------------------------------------------------------- #


@dataclass
class SiteCampaign:
    slug: str
    title: str
    section: str = "current"  # current | past
    start: str = "2026-11-01"
    end: str = "2026-12-31"
    bolbol: bool = False
    kampanyali: bool = True
    detail: dict = field(default_factory=dict)
    detail_title: str | None = None


class Site:
    """Küçük bir sahte Pegasus: kampanya listesi değiştirilir, sayfalar buradan üretilir."""

    def __init__(self):
        self.campaigns: dict[str, SiteCampaign] = {}
        self.overrides: dict[str, object] = {}  # url -> int | Exception | str(html)

    def add(self, slug, title, **kwargs) -> SiteCampaign:
        camp = SiteCampaign(slug=slug, title=title, **kwargs)
        self.campaigns[slug] = camp
        return camp

    def url(self, key: str) -> str:
        return next(s.url for s in config.SOURCES if s.key == key)

    def detail_url(self, slug: str) -> str:
        return f"{BASE}{PATH}{slug}"

    def pages(self) -> dict[str, object]:
        cur = [c for c in self.campaigns.values() if c.section == "current"]
        past = [c for c in self.campaigns.values() if c.section == "past"]
        main_cards = [current_card(c.slug, c.title, c.start, c.end) for c in cur if c.kampanyali]
        main_cards.append(current_card("zz-dummy-main", "Sabit Ana Kart", "2026-01-01", "2027-12-31"))
        past_cards = [past_card(c.slug, c.title) for c in past]
        pages: dict[str, object] = {
            self.url("kampanyali"): list_page(main_cards, past=past_cards),
            self.url("sana-ozel"): list_page(main_cards, past=past_cards),
            self.url("bolbol"): bolbol_page(
                [bolbol_item(c.slug, c.title, c.start, c.end) for c in cur if c.bolbol]
                + [bolbol_item("zz-dummy-bolbol", "Sabit BolBol Kartı", "1 Ocak 2026", "31 Aralık 2027")]
            ),
            self.url("gecmis"): gecmis_page(
                [past_list_item(c.slug, c.title, "1 Ekim 2026 / 3 Ekim 2026") for c in past]
                + [past_list_item("zz-dummy-past", "Eski Kampanya", "1 Ocak 2025 / 3 Ocak 2025")]
            ),
        }
        for camp in self.campaigns.values():
            kwargs = dict(start=camp.start, end=camp.end)
            kwargs.update(camp.detail)
            pages[self.detail_url(camp.slug)] = detail_page(camp.detail_title or camp.title, **kwargs)
        pages[self.detail_url("zz-dummy-bolbol")] = detail_page("Sabit BolBol Kartı", end="2027-12-31")
        pages[self.detail_url("zz-dummy-main")] = detail_page("Sabit Ana Kart", end="2027-12-31")
        pages.update(self.overrides)
        return pages

    def client(self) -> "FakeClient":
        return FakeClient(self.pages())


class FakeClient:
    """scraper.HttpClient yerine geçer; ağa çıkmaz."""

    def __init__(self, pages: dict[str, object]):
        self.pages = pages
        self.request_count = 0
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.request_count += 1
        self.calls.append(url)
        value = self.pages.get(url)
        if value is None:
            return FetchResult(url, 404, error="HTTP 404")
        if isinstance(value, Exception):
            return FetchResult(url, None, error=f"{type(value).__name__}: {value}")
        if isinstance(value, int):
            return FetchResult(url, value, error=f"HTTP {value}")
        return FetchResult(url, 200, text=str(value))


class FakeNotifier:
    def __init__(self, fail_times: int = 0, permanent: bool = False):
        self.sent: list[str] = []
        self.fail_times = fail_times
        self.permanent = permanent
        self.attempts = 0

    def send(self, text: str) -> SendResult:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            return SendResult(False, error="Telegram erişilemiyor", permanent=self.permanent)
        self.sent.append(text)
        return SendResult(True)


def run(site: Site, state_path: Path, notifier=None, *, now: datetime = NOW, dry_run=False, client=None, sources=None):
    kwargs = {} if sources is None else {"sources": sources}
    return main.run_once(
        state_path=state_path,
        client=client or site.client(),
        notifier=notifier,
        dry_run=dry_run,
        now_fn=lambda: now,
        sleep=lambda _s: None,
        **kwargs,
    )


def load_state(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def basic_site() -> Site:
    site = Site()
    site.add("yurt-disi-yuzde-20", "Yurt Dışı Uçuşlarda %20 İndirim", bolbol=False,
             detail={"summary": "Yurt Dışı Uçuşlarda %20 İndirim"})
    site.add("bolbol-ozel-avantaj", "BolBol Özel Avantaj 1000 BolPuan", bolbol=True)
    return site
