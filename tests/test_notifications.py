"""Bildirim biçimi, outbox (pending/sent), yeniden deneme ve Telegram istemcisi testleri (ağsız)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import requests

import config
import main
from campaign_tracker import Change, Event
from helpers import NOW, FakeNotifier, Site, basic_site, load_state, run
from state_manager import StateManager
from telegram_notifier import (
    TelegramNotifier,
    render_event,
    render_test_message,
    split_message,
)

TOKEN = "123456:SECRET-TOKEN-VALUE"


def add_new_campaign(site: Site, state_path, notifier):
    run(site, state_path, notifier)
    site.add("yeni-kampanya", "Yeni %40 İndirim Kampanyası", detail={"summary": "Yeni %40 İndirim Kampanyası"})


# --------------------------------------------------------------------------- #
# Biçimlendirme
# --------------------------------------------------------------------------- #


def _event(kind, **overrides) -> Event:
    campaign = {
        "id": "x", "url": "https://www.flypgs.com/kampanyali-ucak-biletleri/x",
        "title": "Test <Kampanya> & Ortakları", "discount": "%40", "price": None,
        "start_date": "2026-11-10", "end_date": "2026-11-12", "sale_period": "10–12 Kasım 2026",
        "flight_period": "15 Kasım 2026 – 30 Mart 2027", "bolpuan": [], "promo_codes": [],
        "category": "Yurt Dışı Uçuş",
    }
    campaign.update(overrides.pop("campaign", {}))
    return Event(kind, "x", f"{kind}:x", campaign, **overrides)


def test_new_message_is_turkish_escaped_and_complete():
    text = render_event(_event("new"))
    assert text.startswith("🟢 <b>YENİ PEGASUS KAMPANYASI</b>")
    assert "Test &lt;Kampanya&gt; &amp; Ortakları" in text  # HTML güvenli
    assert "💰 İndirim: %40" in text
    assert "Satış Tarihleri: 10–12 Kasım 2026" in text
    assert "Uçuş Tarihleri: 15 Kasım 2026 – 30 Mart 2027" in text
    assert '<a href="https://www.flypgs.com/kampanyali-ucak-biletleri/x">Kampanyayı İncele</a>' in text


def test_updated_message_lists_only_changes():
    event = _event("updated", changes=[
        Change("discount", "İndirim", "%20", "%40", "📈"),
        Change("end_date", "Bitiş Tarihi", "15 Kasım 2026", "20 Kasım 2026", "📅"),
    ])
    text = render_event(event)
    assert "🟡 <b>PEGASUS KAMPANYASI GÜNCELLENDİ</b>" in text
    assert "📈 İndirim: %20 → %40" in text
    assert "📅 Bitiş Tarihi: 15 Kasım 2026 → 20 Kasım 2026" in text
    assert "Satış Tarihleri" not in text


def test_ended_message():
    text = render_event(_event("ended", reason="date"))
    assert "🔴 <b>PEGASUS KAMPANYASI SONA ERDİ</b>" in text
    assert "📅 Bitiş: 12 Kasım 2026" in text and "Kampanya Detayı" in text


def test_test_message_matches_spec():
    assert render_test_message() == (
        "✅ Pegasus Kampanya Takip Botu Aktif\n"
        "⏱️ Planlanan kontrol aralığı: 5 dakika\n"
        "📡 Takip sistemi çalışıyor.\n"
        "🔔 Yalnızca kampanya değişikliklerinde bildirim gönderilecek."
    )


def test_split_message_respects_telegram_limit():
    text = "\n".join(f"satır {i} " + "x" * 90 for i in range(200))
    parts = split_message(text)
    assert len(parts) > 1 and all(len(p) <= config.TELEGRAM_MAX_MESSAGE_LENGTH for p in parts)
    assert "\n".join(parts) == text
    assert split_message("kısa") == ["kısa"]
    assert all(len(p) <= 100 for p in split_message("y" * 450, limit=100))


# --------------------------------------------------------------------------- #
# 17. Başarılı bildirim kaydı / 9. Telegram hatası / 18. Bekleyen yeniden deneme / 10. Yeniden başlatma
# --------------------------------------------------------------------------- #


def test_successful_notification_is_recorded_as_sent(state_path):
    site, notifier = basic_site(), FakeNotifier()
    add_new_campaign(site, state_path, notifier)
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))

    state = load_state(state_path)
    (item,) = state["outbox"]
    assert item["status"] == "sent" and item["sent_at"] and item["attempts"] == 1
    rec = state["campaigns"]["yeni-kampanya"]
    assert rec["notification_state"] == "sent"
    assert rec["last_notified_hash"] == rec["content_hash"]


def test_telegram_failure_keeps_notification_pending_and_never_marks_sent(state_path):
    site, notifier = basic_site(), FakeNotifier(fail_times=1)
    add_new_campaign(site, state_path, notifier)
    report = run(site, state_path, notifier, now=NOW + timedelta(minutes=5))

    assert notifier.sent == [] and report.sent == 0 and report.pending_left == 1
    state = load_state(state_path)
    (item,) = state["outbox"]
    assert item["status"] == "pending" and item["attempts"] == 1 and item["last_error"]
    rec = state["campaigns"]["yeni-kampanya"]
    assert rec["notification_state"] == "pending"
    assert rec["last_notified_hash"] != rec["content_hash"]


def test_pending_notification_is_retried_without_duplicates(state_path):
    site, notifier = basic_site(), FakeNotifier(fail_times=2)
    add_new_campaign(site, state_path, notifier)
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))  # başarısız
    run(site, state_path, notifier, now=NOW + timedelta(minutes=10))  # başarısız
    assert notifier.sent == []
    run(site, state_path, notifier, now=NOW + timedelta(minutes=15))  # başarılı
    assert len(notifier.sent) == 1 and "YENİ PEGASUS KAMPANYASI" in notifier.sent[0]

    run(site, state_path, notifier, now=NOW + timedelta(minutes=20))
    assert len(notifier.sent) == 1  # Gönderildikten sonra tekrar yok
    state = load_state(state_path)
    assert len(state["outbox"]) == 1 and state["outbox"][0]["attempts"] == 3


def test_restart_between_runs_keeps_pending_and_delivers_once(state_path):
    """GitHub Actions her çalışmada sıfırdan başlar; yalnızca state dosyası taşınır."""
    site = basic_site()
    first = FakeNotifier(fail_times=99)
    add_new_campaign(site, state_path, first)
    run(site, state_path, first, now=NOW + timedelta(minutes=5))
    assert load_state(state_path)["outbox"][0]["status"] == "pending"

    fresh_process_notifier = FakeNotifier()
    run(site, state_path, fresh_process_notifier, now=NOW + timedelta(minutes=10))
    assert len(fresh_process_notifier.sent) == 1
    run(site, state_path, fresh_process_notifier, now=NOW + timedelta(minutes=15))
    assert len(fresh_process_notifier.sent) == 1


def test_permanent_rejection_is_marked_failed_and_not_retried_forever(state_path):
    site, notifier = basic_site(), FakeNotifier(fail_times=99, permanent=True)
    add_new_campaign(site, state_path, notifier)
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    run(site, state_path, notifier, now=NOW + timedelta(minutes=10))
    state = load_state(state_path)
    assert state["outbox"][0]["status"] == "failed"
    assert notifier.attempts == 1


def test_missing_secrets_keep_notifications_pending_and_fail_the_run(state_path):
    site = basic_site()
    add_new_campaign(site, state_path, None)
    report = run(site, state_path, None, now=NOW + timedelta(minutes=5))
    assert report.exit_code == 1 and report.pending_left == 1
    # Secret eklenince bekleyen bildirim gider.
    notifier = FakeNotifier()
    run(site, state_path, notifier, now=NOW + timedelta(minutes=10))
    assert len(notifier.sent) == 1


def test_multiple_changes_produce_separate_orderly_messages(state_path):
    site, notifier = basic_site(), FakeNotifier()
    run(site, state_path, notifier)
    site.add("yeni-a", "Yeni A Kampanyası")
    site.add("yeni-b", "Yeni B Kampanyası")
    site.campaigns["yurt-disi-yuzde-20"].section = "past"
    run(site, state_path, notifier, now=NOW + timedelta(minutes=5))
    kinds = sorted("YENİ" if "YENİ" in m else "SONA" for m in notifier.sent)
    assert kinds == ["SONA", "YENİ", "YENİ"]


def test_enqueue_deduplicates_pending_items_with_same_key():
    state = {"outbox": []}
    item = {"key": "new:a", "event": "new", "campaign_id": "a", "text": "m"}
    assert StateManager.enqueue(state, item, NOW) is True
    assert StateManager.enqueue(state, item, NOW) is False
    assert len(state["outbox"]) == 1


# --------------------------------------------------------------------------- #
# Telegram HTTP istemcisi
# --------------------------------------------------------------------------- #


class FakeTelegramResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {"ok": status == 200} if body is None else body

    def json(self):
        return self._body


class FakeTelegramSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.posts: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": dict(json)})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_notifier(outcomes, sleeps=None):
    session = FakeTelegramSession(outcomes)
    sleeps = sleeps if sleeps is not None else []
    return TelegramNotifier(TOKEN, "42", session, sleep=sleeps.append), session, sleeps


def test_telegram_success_sends_html_to_configured_chat():
    notifier, session, _ = make_notifier([FakeTelegramResponse()])
    assert notifier.send("<b>merhaba</b>").ok
    payload = session.posts[0]["json"]
    assert payload["chat_id"] == "42" and payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True


def test_telegram_429_honours_retry_after():
    body = {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}
    notifier, session, sleeps = make_notifier([FakeTelegramResponse(429, body), FakeTelegramResponse()])
    assert notifier.send("x").ok
    assert len(session.posts) == 2 and sleeps and 3 <= sleeps[0] <= 4


def test_telegram_gives_up_on_very_long_retry_after():
    body = {"ok": False, "parameters": {"retry_after": 900}}
    notifier, session, sleeps = make_notifier([FakeTelegramResponse(429, body)])
    result = notifier.send("x")
    assert not result.ok and sleeps == [] and len(session.posts) == 1


def test_telegram_retries_server_errors_and_network_errors():
    notifier, session, sleeps = make_notifier(
        [FakeTelegramResponse(502, {"ok": False}), requests.ConnectionError(f"{TOKEN} yok"), FakeTelegramResponse()]
    )
    assert notifier.send("x").ok and len(session.posts) == 3 and len(sleeps) == 2


def test_telegram_errors_never_leak_the_token():
    notifier, _, _ = make_notifier([requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")] * 4)
    result = notifier.send("x")
    assert not result.ok and TOKEN not in (result.error or "")


def test_telegram_parse_error_falls_back_to_plain_text():
    bad = FakeTelegramResponse(400, {"ok": False, "description": "Bad Request: can't parse entities"})
    notifier, session, _ = make_notifier([bad, FakeTelegramResponse()])
    assert notifier.send("<b>kalın</b> &amp; düz").ok
    retry_payload = session.posts[1]["json"]
    assert "parse_mode" not in retry_payload and retry_payload["text"] == "kalın & düz"


def test_telegram_auth_error_is_fatal_not_retried():
    notifier, session, _ = make_notifier([FakeTelegramResponse(401, {"ok": False, "description": "Unauthorized"})])
    result = notifier.send("x")
    assert not result.ok and result.fatal and len(session.posts) == 1


def test_telegram_bad_request_is_permanent():
    notifier, _, _ = make_notifier([FakeTelegramResponse(400, {"ok": False, "description": "chat not found"})])
    result = notifier.send("x")
    assert not result.ok and result.permanent


def test_log_filter_redacts_secrets(monkeypatch, capsys):
    import logging

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    main.setup_logging()
    logging.getLogger("test").error("hata %s ve %s", TOKEN, "987654321")
    out = capsys.readouterr().out
    assert TOKEN not in out and "987654321" not in out and "***" in out
