"""Telegram mesaj biçimlendirme ve gönderim.

* Tüm kullanıcı metni HTML-escape edilir (parse_mode=HTML).
* Mesajlar 4096 karakter sınırına göre satır sınırlarından bölünür.
* 429 ``retry_after`` ve geçici hatalarda sınırlı, artan beklemeli yeniden deneme yapılır.
* Bot token'ı hiçbir log satırına veya hata metnine yazılmaz.
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import requests

import config
from campaign_tracker import Change, Event
from textutil import clean_text, format_date_tr, truncate

log = logging.getLogger(__name__)

_MAX_VALUE_CHARS = 160


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _val(text: str | None) -> str:
    return _esc(truncate(clean_text(text), _MAX_VALUE_CHARS))


def _link(url: str, label: str) -> str:
    return f'🔗 <a href="{html.escape(url, quote=True)}">{_esc(label)}</a>'


# --------------------------------------------------------------------------- #
# Mesaj biçimlendirme
# --------------------------------------------------------------------------- #


def _date_line(rec: dict) -> str | None:
    start, end = rec.get("start_date"), rec.get("end_date")
    if start and end:
        return f"📅 Kampanya Tarihi: {format_date_tr(start)} – {format_date_tr(end)}"
    if end:
        return f"📅 Bitiş Tarihi: {format_date_tr(end)}"
    if start:
        return f"📅 Başlangıç Tarihi: {format_date_tr(start)}"
    return None


def _render_new(event: Event) -> str:
    rec = event.campaign
    lines = ["🟢 <b>YENİ PEGASUS KAMPANYASI</b>", f"✈️ {_esc(rec.get('title'))}"]
    if rec.get("discount"):
        lines.append(f"💰 İndirim: {_esc(rec['discount'])}")
    if rec.get("price"):
        lines.append(f"💶 Fiyat: {_esc(rec['price'])}")
    date_line = _date_line(rec)
    if date_line:
        lines.append(date_line)
    if rec.get("sale_period"):
        lines.append(f"🗓️ Satış Tarihleri: {_esc(rec['sale_period'])}")
    if rec.get("flight_period"):
        lines.append(f"🛫 Uçuş Tarihleri: {_esc(rec['flight_period'])}")
    if rec.get("bolpuan"):
        lines.append(f"🎁 BolPuan: {_esc(', '.join(rec['bolpuan']))}")
    if rec.get("promo_codes"):
        lines.append(f"🏷️ Promosyon Kodu: {_esc(', '.join(rec['promo_codes']))}")
    if rec.get("category"):
        lines.append(f"📂 Kategori: {_esc(rec['category'])}")
    lines.append(_link(rec["url"], "Kampanyayı İncele"))
    return "\n".join(lines)


def _render_change(change: Change) -> str:
    if change.kind == "note":
        return f"{change.icon} {_esc(change.label)} güncellendi"
    if change.kind == "routes":
        parts = []
        if change.new:
            parts.append(f"➕ Eklenen hatlar: {_val(change.new)}")
        if change.old:
            parts.append(f"➖ Çıkan hatlar: {_val(change.old)}")
        return "\n".join(parts) or f"{change.icon} {_esc(change.label)} güncellendi"
    return f"{change.icon} {_esc(change.label)}: {_val(change.old)} → {_val(change.new)}"


def _render_updated(event: Event) -> str:
    rec = event.campaign
    lines = ["🟡 <b>PEGASUS KAMPANYASI GÜNCELLENDİ</b>", f"✈️ {_esc(rec.get('title'))}"]
    lines.extend(_render_change(c) for c in event.changes)
    lines.append(_link(rec["url"], "Kampanyayı İncele"))
    return "\n".join(lines)


_END_REASONS = {
    "date": "Bitiş tarihi geçti",
    "past_section": "Pegasus kampanyayı “Geçmiş Kampanyalar” bölümüne taşıdı",
    "removed": "Kampanya sayfası Pegasus sitesinden kaldırıldı",
}


def _render_ended(event: Event) -> str:
    rec = event.campaign
    lines = ["🔴 <b>PEGASUS KAMPANYASI SONA ERDİ</b>", f"✈️ {_esc(rec.get('title'))}"]
    if rec.get("end_date"):
        lines.append(f"📅 Bitiş: {format_date_tr(rec['end_date'])}")
    reason = _END_REASONS.get(event.reason or "")
    if reason:
        lines.append(f"ℹ️ {_esc(reason)}")
    lines.append(_link(rec["url"], "Kampanya Detayı"))
    return "\n".join(lines)


def render_event(event: Event) -> str:
    if event.type == "new":
        return _render_new(event)
    if event.type == "updated":
        return _render_updated(event)
    if event.type == "ended":
        return _render_ended(event)
    raise ValueError(f"Bilinmeyen olay türü: {event.type}")


def render_test_message() -> str:
    return (
        "✅ Pegasus Kampanya Takip Botu Aktif\n"
        "⏱️ Planlanan kontrol aralığı: 5 dakika\n"
        "📡 Takip sistemi çalışıyor.\n"
        "🔔 Yalnızca kampanya değişikliklerinde bildirim gönderilecek."
    )


def split_message(text: str, limit: int = config.TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Metni satır sınırlarından bölerek her parçayı ``limit`` karakterin altında tutar."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # Tek satır bile sığmıyorsa sert böl.
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# Gönderim
# --------------------------------------------------------------------------- #


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    permanent: bool = False  # Bu mesaj hiçbir zaman gönderilemez (örn. 400).
    fatal: bool = False  # Yapılandırma sorunu (token/izin); bu çalışmada tekrar denemeyin.


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        session: requests.Session | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = config.TELEGRAM_MAX_ATTEMPTS,
    ):
        self._token = token
        self._chat_id = chat_id
        self._session = session or requests.Session()
        self._sleep = sleep
        self._max_attempts = max_attempts

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def send(self, text: str) -> SendResult:
        for chunk in split_message(text):
            result = self._send_chunk(chunk)
            if not result.ok:
                return result
        return SendResult(ok=True)

    def _send_chunk(self, text: str) -> SendResult:
        url = f"{config.TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        payload: dict = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        last_error = "bilinmeyen hata"
        for attempt in range(self._max_attempts):
            is_last = attempt == self._max_attempts - 1
            try:
                response = self._session.post(url, json=payload, timeout=config.TELEGRAM_TIMEOUT)
            except requests.RequestException as exc:
                last_error = self._redact(f"{type(exc).__name__}: {exc}")
                log.warning("Telegram bağlantı hatası (deneme %d/%d): %s", attempt + 1,
                            self._max_attempts, last_error)
                if not is_last:
                    self._sleep(min(2.0 * (2 ** attempt), 20.0))
                continue

            status = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = {}
            description = str(body.get("description") or "")
            if status == 200 and body.get("ok", True):
                return SendResult(ok=True)

            last_error = self._redact(f"HTTP {status}: {description}".strip())
            if status == 429:
                retry_after = float((body.get("parameters") or {}).get("retry_after") or 2 ** (attempt + 1))
                log.warning("Telegram 429; retry_after=%.0fs", retry_after)
                if retry_after > config.TELEGRAM_MAX_RETRY_AFTER or is_last:
                    return SendResult(False, error=last_error)
                self._sleep(retry_after + 0.5)
                continue
            if status in (401, 403, 404):
                log.error("Telegram yetki/yapılandırma hatası: %s", last_error)
                return SendResult(False, error=last_error, fatal=True)
            if status == 400:
                if "parse entities" in description.lower() and payload.get("parse_mode"):
                    log.warning("Telegram HTML ayrıştırma hatası; düz metinle yeniden deneniyor")
                    payload.pop("parse_mode", None)
                    payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", text))
                    continue
                log.error("Telegram mesajı reddetti (kalıcı): %s", last_error)
                return SendResult(False, error=last_error, permanent=True)
            if status >= 500:
                log.warning("Telegram sunucu hatası (deneme %d/%d): %s", attempt + 1,
                            self._max_attempts, last_error)
                if not is_last:
                    self._sleep(min(2.0 * (2 ** attempt), 20.0))
                continue
            return SendResult(False, error=last_error, permanent=True)
        return SendResult(False, error=last_error)
