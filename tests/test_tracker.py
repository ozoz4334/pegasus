"""Değişiklik tespiti, durum makinesi ve state dayanıklılığı testleri (ağsız)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
from helpers import (
    NOW,
    FakeNotifier,
    Site,
    basic_site,
    detail_page,
    load_state,
    run,
)
from state_manager import StateManager, new_state


def count(messages: list[str], needle: str) -> int:
    return sum(1 for m in messages if needle in m)


# --------------------------------------------------------------------------- #
# 1. İlk çalıştırma / 2. Değişiklik yok / 14. Tekrar bildirim yok
# --------------------------------------------------------------------------- #


def test_first_run_records_everything_but_sends_nothing(state_path):
    site, notifier = basic_site(), FakeNotifier()
    report = run(site, state_path, notifier)

    assert notifier.sent == []
    assert report.bootstrapped is True
    state = load_state(state_path)
    assert state["meta"]["initialized"] is True
    assert "yurt-disi-yuzde-20" in state["campaigns"]
    rec = state["campaigns"]["yurt-disi-yuzde-20"]
    assert rec["status"] == "active"
    assert rec["content_hash"] and rec["last_notified_hash"] == rec["content_hash"]
    assert rec["discount"] == "%20"
    assert state["outbox"] == []


def test_no_change_sends_nothing_and_does_not_rewrite_state(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    before = state_path.read_bytes()

    later = NOW + timedelta(minutes=5)
    report = run(site, state_path, notifier, now=later)

    assert notifier.sent == []
    assert report.state_written is False
    assert state_path.read_bytes() == before  # Gereksiz commit oluşmaz.


def test_new_campaign_is_notified_exactly_once(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)

    site.add("yeni-kampanya", "Yeni %40 İndirim Kampanyası", detail={"summary": "Yeni %40 İndirim Kampanyası"})
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert len(notifier.sent) == 1
    message = notifier.sent[0]
    assert "YENİ PEGASUS KAMPANYASI" in message
    assert "Yeni %40 İndirim Kampanyası" in message
    assert "İndirim: %40" in message
    assert f"{config.BASE_URL}/kampanyali-ucak-biletleri/yeni-kampanya" in message

    for minute in (10, 15, 20):  # Art arda taramalar aynı kampanyayı tekrar bildirmez.
        run(site, state_path, notifier, now=NOW + timedelta(minutes=minute))
    assert len(notifier.sent) == 1


# --------------------------------------------------------------------------- #
# 4-6. Güncellemeler
# --------------------------------------------------------------------------- #


def test_discount_change_shows_only_changed_field(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)

    camp = site.campaigns["yurt-disi-yuzde-20"]
    camp.title = "Yurt Dışı Uçuşlarda %40 İndirim"
    camp.detail = {"summary": camp.title}
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))

    assert len(notifier.sent) == 1
    message = notifier.sent[0]
    assert "PEGASUS KAMPANYASI GÜNCELLENDİ" in message
    assert "İndirim: %20 → %40" in message
    assert "Uçuş Tarihleri" not in message  # Değişmeyen alanlar tekrar edilmez.
    assert "Satış Tarihleri" not in message


def test_price_change_is_detected(state_path):
    site, notifier = Site(), FakeNotifier()
    site.add("fiyat-kampanyasi", "Yurt dışı 11€+vergilerden başlayan fiyatlarla",
             detail={"summary": "Yurt dışı 11€+vergilerden başlayan fiyatlarla"})
    run(site, state_path, notifier)

    camp = site.campaigns["fiyat-kampanyasi"]
    camp.title = "Yurt dışı 9€+vergilerden başlayan fiyatlarla"
    camp.detail = {"summary": camp.title}
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))

    assert len(notifier.sent) == 1
    assert "Fiyat: 11€ → 9€" in notifier.sent[0]


def test_end_date_extension_is_reported(state_path):
    site, notifier = Site(), FakeNotifier()
    site.add("uzayan-kampanya", "Uzayan Kampanya", start="2026-11-01", end="2026-12-15")
    run(site, state_path, notifier)

    site.campaigns["uzayan-kampanya"].end = "2026-12-20"
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))

    assert len(notifier.sent) == 1
    assert "Bitiş Tarihi: 15 Aralık 2026 → 20 Aralık 2026" in notifier.sent[0]
    state = load_state(state_path)
    assert state["campaigns"]["uzayan-kampanya"]["end_date"] == "2026-12-20"
    assert state["campaigns"]["uzayan-kampanya"]["status"] == "active"


def test_route_and_condition_changes(state_path):
    site, notifier = Site(), FakeNotifier()
    site.add("hat-kampanyasi", "Hat Kampanyası",
             detail={"routes": ("İstanbul-Berlin", "İstanbul-Paris"), "extra": "Toplam 200.000 koltuk için geçerlidir."})
    run(site, state_path, notifier)

    site.campaigns["hat-kampanyasi"].detail = {"routes": ("İstanbul-Berlin", "İstanbul-Roma"),
                                               "extra": "Toplam 200.000 koltuk için geçerlidir."}
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert len(notifier.sent) == 1
    assert "Eklenen hatlar" in notifier.sent[0] and "İstanbul-Roma" in notifier.sent[0]
    assert "Çıkan hatlar" in notifier.sent[0] and "İstanbul-Paris" in notifier.sent[0]

    site.campaigns["hat-kampanyasi"].detail = {"routes": ("İstanbul-Berlin", "İstanbul-Roma"),
                                               "extra": "Toplam 300.000 koltuk için geçerlidir."}
    run(site, state_path, notifier, now=NOW + timedelta(minutes=10))
    assert len(notifier.sent) == 2
    assert "Kampanya Koşulları güncellendi" in notifier.sent[1]


# --------------------------------------------------------------------------- #
# 7. Sona erme
# --------------------------------------------------------------------------- #


def test_campaign_moved_to_past_is_reported_once(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)

    site.campaigns["yurt-disi-yuzde-20"].section = "past"
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert count(notifier.sent, "SONA ERDİ") == 1
    assert "Geçmiş Kampanyalar" in notifier.sent[0]

    for minute in (10, 15):
        run(site, state_path, notifier, now=NOW + timedelta(minutes=minute))
    assert count(notifier.sent, "SONA ERDİ") == 1
    assert load_state(state_path)["campaigns"]["yurt-disi-yuzde-20"]["status"] == "ended"


def test_campaign_ended_by_explicit_end_date(state_path):
    site, notifier = Site(), FakeNotifier()
    site.add("kisa-kampanya", "Kısa Kampanya", start="2026-11-01", end="2026-11-10")
    run(site, state_path, notifier)

    # Bitişten ve tolerans süresinden sonra (Europe/Istanbul gün sonu).
    after = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)
    run(site, state_path, notifier, now=after)
    assert count(notifier.sent, "SONA ERDİ") == 1
    assert "Bitiş: 10 Kasım 2026" in notifier.sent[0]
    assert "Bitiş tarihi geçti" in notifier.sent[0]

    run(site, state_path, notifier, now=after + timedelta(minutes=5))
    assert count(notifier.sent, "SONA ERDİ") == 1  # Kart hâlâ listede olsa da yeniden "aktif" sayılmaz.


def test_end_date_not_reached_is_not_ended(state_path):
    site, notifier = Site(), FakeNotifier()
    site.add("kisa-kampanya", "Kısa Kampanya", start="2026-11-01", end="2026-11-10")
    run(site, state_path, notifier)
    # 10 Kasım 23:00 İstanbul = 20:00 UTC: gün bitmedi.
    run(site, state_path, notifier, now=datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc))
    assert notifier.sent == []


def test_removed_campaign_needs_repeated_scans_and_missing_page(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    gone_slug = "bolbol-ozel-avantaj"
    del site.campaigns[gone_slug]  # Listelerden ve sunucudan kayboldu (detay 404).

    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    run(site, state_path, notifier, now=NOW + timedelta(minutes=10))
    assert notifier.sent == []  # Tek/iki taramada kaybolma bitiş sayılmaz.
    assert load_state(state_path)["campaigns"][gone_slug]["status"] == "active"

    run(site, state_path, notifier, now=NOW + timedelta(minutes=15))
    assert count(notifier.sent, "SONA ERDİ") == 1
    assert "kaldırıldı" in notifier.sent[0]


def test_unlisted_but_still_published_page_is_not_ended(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    slug = "bolbol-ozel-avantaj"
    site.overrides[site.detail_url(slug)] = detail_page("BolBol Özel Avantaj 1000 BolPuan")
    del site.campaigns[slug]
    for minute in (5, 10, 15, 20):
        run(site, state_path, notifier, now=NOW + timedelta(minutes=minute))
    assert notifier.sent == []


# --------------------------------------------------------------------------- #
# 8. İki kaynakta aynı kampanya / 15. Kart sırası
# --------------------------------------------------------------------------- #


def test_same_campaign_in_two_sources_gives_one_notification(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)

    site.add("iki-kaynakli", "İki Kaynaklı Kampanya", bolbol=True, kampanyali=True)
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert count(notifier.sent, "YENİ PEGASUS KAMPANYASI") == 1
    sources = load_state(state_path)["campaigns"]["iki-kaynakli"]["sources"]
    assert {"kampanyali", "bolbol", "sana-ozel"} <= set(sources)


def test_card_reordering_is_not_a_change(state_path):
    site, notifier = basic_site(), FakeNotifier()
    site.add("ucuncu", "Üçüncü Kampanya")
    run(site, state_path, notifier)
    site.campaigns = dict(reversed(list(site.campaigns.items())))
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert notifier.sent == []


# --------------------------------------------------------------------------- #
# 11-12, 16. HTML değişimi, geçici kesinti, başarısız tarama
# --------------------------------------------------------------------------- #


def test_changed_html_structure_causes_no_notifications(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)

    for key in ("kampanyali", "sana-ozel"):
        site.overrides[site.url(key)] = "<html><body><div class='yeni-tasarim'>Kartlar yok</div></body></html>"
    for slug in site.campaigns:
        site.overrides[site.detail_url(slug)] = "<html><body><h1>Yeni tasarım</h1></body></html>"
    report = run(site, state_path, notifier, now=NOW + timedelta(minutes=5))

    assert notifier.sent == []
    assert len(report.failed_sources) == 2
    state = load_state(state_path)
    assert all(rec["status"] == "active" for slug, rec in state["campaigns"].items() if not slug.startswith("zz-dummy-past"))


def test_total_outage_does_not_create_fake_end_notifications(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    for source in config.SOURCES:
        site.overrides[source.url] = 503
    for minute in (5, 10, 15, 20, 25):
        report = run(site, state_path, notifier, now=NOW + timedelta(minutes=minute))
        assert len(report.failed_sources) == len(config.SOURCES)
    assert notifier.sent == []
    state = load_state(state_path)
    assert state["campaigns"]["yurt-disi-yuzde-20"]["status"] == "active"
    assert state["campaigns"]["yurt-disi-yuzde-20"]["missing_scans"] == 0


def test_partial_outage_does_not_count_as_disappearance(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    del site.campaigns["bolbol-ozel-avantaj"]
    site.overrides[site.url("bolbol")] = ConnectionError("internet yok")  # Zorunlu kaynak erişilemiyor.
    for minute in (5, 10, 15, 20, 25):
        run(site, state_path, notifier, now=NOW + timedelta(minutes=minute))
    assert notifier.sent == []
    assert load_state(state_path)["campaigns"]["bolbol-ozel-avantaj"]["missing_scans"] == 0


def test_temporary_detail_failure_is_not_a_change(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    site.overrides[site.detail_url("yurt-disi-yuzde-20")] = 500
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    del site.overrides[site.detail_url("yurt-disi-yuzde-20")]
    run(site, state_path, notifier, now=NOW + timedelta(minutes=10))
    assert notifier.sent == []


def test_new_campaign_with_failing_detail_is_deferred_then_announced(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    site.add("gec-gelen", "Geç Gelen Detay Kampanyası")
    site.overrides[site.detail_url("gec-gelen")] = 503

    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    run(site, state_path, notifier, now=NOW + timedelta(minutes=10))
    assert notifier.sent == []
    assert "gec-gelen" not in load_state(state_path)["campaigns"]

    run(site, state_path, notifier, now=NOW + timedelta(minutes=15))
    assert count(notifier.sent, "YENİ PEGASUS KAMPANYASI") == 1

    del site.overrides[site.detail_url("gec-gelen")]  # Detay sonradan gelince sessiz temel çizgi.
    run(site, state_path, notifier, now=NOW + timedelta(minutes=20))
    assert len(notifier.sent) == 1


def test_bootstrap_is_postponed_when_a_source_is_down(state_path):
    site, notifier = basic_site(), FakeNotifier()
    site.overrides[site.url("bolbol")] = 503
    report = run(site, state_path, notifier)
    assert report.bootstrapped is False
    state = load_state(state_path) if state_path.exists() else new_state()
    assert state["meta"]["initialized"] is False
    assert state["campaigns"] == {}

    del site.overrides[site.url("bolbol")]
    report = run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert report.bootstrapped is True
    assert notifier.sent == []


# --------------------------------------------------------------------------- #
# Gürültü: boşluk, sayaç, tasarım, izleme parametresi, küçük yazım farkı
# --------------------------------------------------------------------------- #


def test_cosmetic_changes_do_not_trigger_notifications(state_path):
    site, notifier = Site(), FakeNotifier()
    site.add("kozmetik", "Kozmetik Kampanya", detail={
        "eligibility": "Kampanyadan yalnızca Pegasus BolBol üyeleri faydalanabilir.",
        "noise": "<div class='countdown'>Kalan süre 01:02:03</div>",
    })
    run(site, state_path, notifier)

    site.campaigns["kozmetik"].detail = {
        "eligibility": "Kampanyadan  yalnızca Pegasus   BolBol üyeleri faydalanabilir",  # boşluk ve nokta
        "noise": "<div class='countdown'>Kalan süre 01:02:02</div><div class='yeni-banner'>x</div>",
        "whitespace": "  \n\t  ",
    }
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert notifier.sent == []


def test_tiny_typo_in_long_text_is_ignored_but_wording_change_is_not(state_path):
    words = ["alfa", "beta", "gama", "delta", "epsilon"]
    base = " ".join(words[i % 5] for i in range(300))
    typo = base.replace("alfa", "alfaa", 1)
    site, notifier = Site(), FakeNotifier()
    site.add("yazim", "Yazım Kampanyası", detail={"extra": base})
    run(site, state_path, notifier)

    site.campaigns["yazim"].detail = {"extra": typo}
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert notifier.sent == []

    site.campaigns["yazim"].detail = {"extra": base + " ".join(["yepyeni"] * 40)}
    run(site, state_path, notifier, now=NOW + timedelta(minutes=10))
    assert len(notifier.sent) == 1


def test_promo_code_and_bolpuan_are_tracked(state_path):
    site, notifier = Site(), FakeNotifier()
    site.add("kod-kampanyasi", "Kod Kampanyası 1000 BolPuan", detail={"extra": "Promosyon kodu: BAHAR25 kullanılabilir."})
    run(site, state_path, notifier)
    rec = load_state(state_path)["campaigns"]["kod-kampanyasi"]
    assert rec["promo_codes"] == ["BAHAR25"]
    assert rec["bolpuan"] == ["1000 BolPuan"]

    site.campaigns["kod-kampanyasi"].detail = {"extra": "Promosyon kodu: YAZ2027 kullanılabilir."}
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    assert len(notifier.sent) == 1
    assert "Promosyon Kodu: BAHAR25 → YAZ2027" in notifier.sent[0]


# --------------------------------------------------------------------------- #
# 13. State bozulması ve state yöneticisi
# --------------------------------------------------------------------------- #


def test_corrupt_state_is_quarantined_and_rebootstrapped_silently(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)

    state_path.write_text('{"campaigns": {"x": ', encoding="utf-8")  # Yarım kalmış JSON
    site.add("bozulma-sonrasi", "Bozulma Sonrası Kampanya")
    report = run(site, state_path, notifier, now=NOW + timedelta(minutes=5))

    assert notifier.sent == []  # Geçmiş toplu bildirim yağmuru yok.
    assert report.bootstrapped is True
    assert list(state_path.parent.glob("campaigns.json.corrupt-*"))
    state = load_state(state_path)
    assert state["meta"]["initialized"] is True
    assert "bozulma-sonrasi" in state["campaigns"]


def test_structurally_invalid_state_is_treated_as_corrupt(state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"schema_version": 1, "campaigns": [], "meta": {}}', encoding="utf-8")
    manager = StateManager(state_path)
    state = manager.load()
    assert manager.status == "recovered"
    assert state["campaigns"] == {}


def test_state_heartbeat_writes_at_most_daily(state_path):
    manager = StateManager(state_path)
    state = manager.load(NOW)
    assert manager.save_if_changed(state, NOW) is True
    assert manager.save_if_changed(state, NOW + timedelta(minutes=5)) is False
    assert manager.save_if_changed(state, NOW + timedelta(hours=12)) is False
    assert manager.save_if_changed(state, NOW + timedelta(hours=25)) is True


def test_dry_run_neither_sends_nor_writes_state(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    before = state_path.read_bytes()

    site.add("kuru-kampanya", "Kuru Çalıştırma Kampanyası")
    report = run(site, state_path, notifier, now=NOW + timedelta(minutes=5), dry_run=True)

    assert notifier.sent == []
    assert state_path.read_bytes() == before
    assert len(report.messages) == 1 and "Kuru Çalıştırma" in report.messages[0]


def test_detail_fetches_are_staggered_with_default_buckets(state_path, monkeypatch):
    monkeypatch.setattr(config, "DETAIL_REFRESH_BUCKETS", 6)
    site, notifier = Site(), FakeNotifier()
    for i in range(12):
        site.add(f"kampanya-{i}", f"Kampanya {i}")
    run(site, state_path, notifier)

    client = site.client()
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5), client=client)
    detail_calls = [c for c in client.calls if "/kampanyali-ucak-biletleri/" in c and "gecmis" not in c]
    assert 0 < len(detail_calls) < 12  # Her çalışmada tüm detaylar çekilmez.
