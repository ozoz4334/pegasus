"""Ayrıştırıcı, tarih biçimleri ve HTTP istemcisi testleri (ağsız)."""

from __future__ import annotations

from datetime import date

import pytest
import requests

import config
import scraper
from helpers import (
    PATH,
    Site,
    bolbol_item,
    bolbol_page,
    collab_card,
    current_card,
    detail_page,
    gecmis_page,
    list_page,
    past_card,
    past_list_item,
)
from textutil import (
    extract_date_range,
    extract_dates,
    format_date_tr,
    norm_text,
    similarity,
    tr_lower,
)


# --------------------------------------------------------------------------- #
# Tarihler (12. farklı tarih biçimleri)
# --------------------------------------------------------------------------- #


def test_extract_dates_handles_all_observed_formats():
    assert extract_dates("29.07.2026 13:29:00") == [date(2026, 7, 29)]
    assert extract_dates("2026-10-27") == [date(2026, 10, 27)]
    assert extract_dates("Kampanya Tarihi : 31 Aralık 2026 / 05 Eylül 2026") == [
        date(2026, 12, 31),
        date(2026, 9, 5),
    ]
    assert extract_dates("1 KASIM 2026 ve 3 ŞUBAT 2027") == [date(2026, 11, 1), date(2027, 2, 3)]
    assert extract_dates("31 Subat 2026") == []  # Geçersiz tarih yok sayılır
    assert extract_dates("") == [] and extract_dates(None) == []


def test_extract_date_range_variants():
    assert extract_date_range("Kampanya, 21-23 Ekim 2026 (saat 10.30)") == "21–23 Ekim 2026"
    assert extract_date_range("23 Kasım 2026 – 27 Mart 2027 tarihleri") == "23 Kasım 2026 – 27 Mart 2027"
    assert extract_date_range("30 Ekim - 2 Kasım 2026") == "30 Ekim – 2 Kasım 2026"
    assert extract_date_range("sadece 5 Mayıs 2027 günü") == "5 Mayıs 2027"
    assert extract_date_range("tarih yok") is None


def test_format_date_tr_and_text_normalization():
    assert format_date_tr("2026-10-27") == "27 Ekim 2026"
    assert format_date_tr(None) == "—"
    assert norm_text("  İNDİRİM,  %40!! ") == norm_text("indirim %40")
    assert tr_lower("IŞIK İLE") == "ışık ile"
    assert similarity("a b c", "a b c") == 1.0


# --------------------------------------------------------------------------- #
# Liste ayrıştırma
# --------------------------------------------------------------------------- #


def test_parse_listing_current_collab_and_past_cards():
    html = list_page(
        current=[current_card("aktif-1", "Aktif Kampanya 1", "2026-07-29", "2026-12-31", promo_id="uuid-1")],
        collab=[collab_card("ortak-1", "Ortak Kampanya", "2026-10-29", "2026-11-30")],
        past=[past_card("eski-1", "Eski Kampanya")],
    )
    cards = {c.slug: c for c in scraper.parse_listing(html, "kampanyali")}
    assert set(cards) == {"aktif-1", "ortak-1", "eski-1"}  # Menü ve "gecmis-kampanyalar" bağlantıları yok
    assert cards["aktif-1"].section == "current"
    assert cards["aktif-1"].promotion_id == "uuid-1"
    assert cards["aktif-1"].title == "Aktif Kampanya 1"
    assert (cards["aktif-1"].start, cards["aktif-1"].end) == (date(2026, 7, 29), date(2026, 12, 31))
    assert cards["ortak-1"].title == "Ortak Kampanya"  # swiper-card başlığı
    assert cards["ortak-1"].section == "current"
    assert cards["eski-1"].section == "past"
    assert cards["eski-1"].url == f"{config.BASE_URL}{PATH}eski-1"  # İzleme parametresi yok


def test_parse_listing_deduplicates_same_slug_and_prefers_current():
    html = list_page(
        current=[current_card("ayni", "Aynı Kampanya", "2026-07-01", "2026-12-31")],
        collab=[collab_card("ayni", "Aynı Kampanya", "2026-07-01", "2026-12-31")],
        past=[past_card("ayni", "Aynı Kampanya")],
    )
    cards = scraper.parse_listing(html, "kampanyali")
    assert len(cards) == 1 and cards[0].section == "current"


def test_parse_listing_bolbol_and_past_archive_pages():
    bolbol = bolbol_page([bolbol_item("opet", "Pegasus BolBol & Opet İş Birliği", "01 Temmuz 2026", "31 Aralık 2026")])
    (card,) = scraper.parse_listing(bolbol, "bolbol")
    assert card.slug == "opet" and card.section == "current"
    assert (card.start, card.end) == (date(2026, 7, 1), date(2026, 12, 31))
    assert card.promotion_id is None

    archive = gecmis_page([past_list_item("bitti", "Bitmiş", "21 Ekim 2026 / 23 Ekim 2026")])
    (old,) = scraper.parse_listing(archive, "gecmis")
    assert old.slug == "bitti" and old.section == "past"
    assert (old.start, old.end) == (date(2026, 10, 21), date(2026, 10, 23))


def test_parse_listing_ignores_non_card_links():
    html = f'<html><body><a href="{PATH}menu-linki">Menü</a><a href="/baska/sayfa">x</a></body></html>'
    assert scraper.parse_listing(html, "kampanyali") == []


# --------------------------------------------------------------------------- #
# Detay ayrıştırma
# --------------------------------------------------------------------------- #


def test_parse_detail_extracts_structured_fields():
    html = detail_page(
        "Yurt dışı biletlerim 11€ + vergilerden başlayan fiyatlarla! %30 indirim",
        start="2026-10-21",
        end="2026-10-23",
        offer="Mobil uygulamaya özel 1.000 BolPuan ve 2 kat BolPuan fırsatı.",
        sale="Kampanya, 21-23 Ekim 2026 tarihleri arasında geçerlidir.",
        flight="Kampanya, 23 Kasım 2026 – 27 Mart 2027 uçuşları için geçerlidir.",
        routes=("11€ + vergilerle", "İstanbul-Atina", "İstanbul-Berlin", "19€ + vergilerle", "İstanbul-Paris"),
        extra="Promosyon kodu: PEGASUS26 geçerlidir.",
    )
    data = scraper.parse_detail(html)
    assert data is not None
    assert data.start_date == date(2026, 10, 21) and data.end_date == date(2026, 10, 23)
    assert data.discount == "%30" and data.price == "11€"
    assert data.sale_period == "21–23 Ekim 2026"
    assert data.flight_period == "23 Kasım 2026 – 27 Mart 2027"
    assert data.routes == ["İstanbul-Atina (11€ + vergilerle)", "İstanbul-Berlin (11€ + vergilerle)",
                           "İstanbul-Paris (19€ + vergilerle)"]
    assert data.promo_codes == ["PEGASUS26"]
    assert data.bolpuan == ["1000 BolPuan", "2 kat BolPuan"]
    assert "yalnızca Pegasus BolBol" in data.eligibility
    assert "Giriş Yapın" not in data.conditions  # Giriş formu içeriğe karışmaz


def test_parse_detail_returns_none_when_structure_is_missing():
    assert scraper.parse_detail("<html><body><h1>Başka sayfa</h1></body></html>") is None
    assert scraper.parse_detail("") is None
    broken = detail_page("Başlık").replace("campaign-detail-content__article", "yeni-sinif")
    assert scraper.parse_detail(broken) is None


def test_fetch_details_classifies_results():
    site = Site()
    site.add("iyi", "İyi Kampanya")
    site.overrides[site.detail_url("bozuk")] = "<html>yapı değişti</html>"
    site.overrides[site.detail_url("hatali")] = 503
    client = site.client()
    urls = {s: site.detail_url(s) for s in ("iyi", "bozuk", "hatali", "yok")}
    results = scraper.fetch_details(client, urls, delay=0)
    assert {k: v.status for k, v in results.items()} == {
        "iyi": "ok", "bozuk": "unparsable", "hatali": "error", "yok": "gone",
    }


# --------------------------------------------------------------------------- #
# Kaynak taraması
# --------------------------------------------------------------------------- #


def test_scan_marks_failing_or_empty_sources_unhealthy_without_stopping_others():
    site = Site()
    site.add("a", "A")
    site.overrides[site.url("bolbol")] = 503
    site.overrides[site.url("sana-ozel")] = "<html><body>kart yok</body></html>"
    scan = scraper.scan_sources(site.client())
    assert scan.sources["kampanyali"].ok and scan.sources["gecmis"].ok
    assert not scan.sources["bolbol"].ok and "503" in scan.sources["bolbol"].error
    assert not scan.sources["sana-ozel"].ok and "kart" in scan.sources["sana-ozel"].error
    assert "a" in scan.listings and not scan.complete and not scan.disappearance_reliable


def test_scan_merges_sources_for_same_campaign():
    site = Site()
    site.add("ortak", "Ortak", bolbol=True)
    scan = scraper.scan_sources(site.client())
    assert sorted(scan.listings["ortak"].sources) == ["bolbol", "kampanyali", "sana-ozel"]


# --------------------------------------------------------------------------- #
# HTTP istemcisi (3. Retry, 12. Geçici internet kesintisi)
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status=200, text="<html>ok</html>", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.headers = {}

    def get(self, url, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(outcomes, sleeps):
    return scraper.HttpClient(FakeSession(outcomes), sleep=sleeps.append)


def test_client_retries_server_errors_then_succeeds():
    sleeps: list[float] = []
    client = make_client([FakeResponse(503), FakeResponse(502), FakeResponse(200)], sleeps)
    result = client.get("https://x/")
    assert result.ok and client.session.calls == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0]  # Artan bekleme


def test_client_survives_temporary_network_outage():
    sleeps: list[float] = []
    client = make_client([requests.ConnectionError("yok"), requests.Timeout("zaman aşımı"), FakeResponse()], sleeps)
    assert client.get("https://x/").ok


def test_client_gives_up_after_max_retries_and_never_raises():
    client = make_client([requests.ConnectionError("yok")] * (config.HTTP_MAX_RETRIES + 1), [])
    result = client.get("https://x/")
    assert not result.ok and "ConnectionError" in result.error
    assert client.session.calls == config.HTTP_MAX_RETRIES + 1


def test_client_respects_retry_after_on_429():
    sleeps: list[float] = []
    client = make_client([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse()], sleeps)
    assert client.get("https://x/").ok
    assert sleeps == [7.0]


def test_client_does_not_wait_for_very_long_retry_after():
    sleeps: list[float] = []
    client = make_client([FakeResponse(429, headers={"Retry-After": "3600"})], sleeps)
    result = client.get("https://x/")
    assert not result.ok and result.status == 429 and sleeps == []
    assert client.session.calls == 1


def test_client_does_not_retry_404_and_limits_403_retries():
    client = make_client([FakeResponse(404)], [])
    result = client.get("https://x/")
    assert result.gone and client.session.calls == 1

    client = make_client([FakeResponse(403)] * 5, [])
    assert client.get("https://x/").status == 403
    assert client.session.calls == 2  # Bot koruması karşısında ısrar edilmez
