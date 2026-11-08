"""Değişiklik tespiti: yeni / güncellenen / sona eren kampanyalar.

Önemli ilkeler
* Başarısız veya eksik bir tarama ASLA "kampanya bitti" anlamına gelmez.
* Kampanya kimliği = kalıcı URL slug'ı (resmi kampanya UUID'si ayrıca saklanır).
* Hash yalnızca anlamlı alanlardan üretilir (görsel, kart sırası, boşluk, noktalama,
  izleme parametreleri, sayaçlar hash'e girmez).
* Olaylar önce outbox'a "pending" yazılır; "sent" işareti yalnızca Telegram başarısında konur.
"""

from __future__ import annotations

import copy
import logging
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import config
from scraper import DetailData, DetailResult, Listing, ScanResult
from state_manager import State, iso
from textutil import (
    format_date_tr,
    norm_text,
    similarity,
    stable_hash,
    tr_lower,
)

log = logging.getLogger(__name__)

Record = dict[str, Any]

# --------------------------------------------------------------------------- #
# Olay modeli
# --------------------------------------------------------------------------- #


@dataclass
class Change:
    field: str
    label: str
    old: str | None
    new: str | None
    icon: str = "🔄"
    kind: str = "value"  # value | routes | note


@dataclass
class Event:
    type: str  # "new" | "updated" | "ended"
    campaign_id: str
    key: str
    campaign: Record
    changes: list[Change] = field(default_factory=list)
    reason: str | None = None
    content_hash: str = ""


# --------------------------------------------------------------------------- #
# Sınıflandırma
# --------------------------------------------------------------------------- #

_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Genç BolBol", ("genç bolbol", "genc-bolbol", "genç bolbollu")),
    ("Banka / Kredi Kartı", ("banka", "kredi kart", "kartlı", "ziraat", "papara")),
    ("İş Ortaklığı", ("iş birliği", "is-birligi", "ortaklık")),
    ("Promosyon Kodu", ("promosyon kodu", "indirim kodu")),
    ("Yurt İçi Uçuş", ("yurt içi", "yurt-ici")),
    ("Yurt Dışı Uçuş", ("yurt dışı", "yurt-disi", "uluslararası", "uluslararasi", "kuzey kıbrıs", "kuzey-kibris")),
    ("Bagaj / Ek Hizmet", ("bagaj", "ek hizmet", "yemek sipariş", "cafe", "esim", "koltuk seçim")),
    ("BolPuan", ("bolpuan",)),
    ("Mobil Uygulamaya Özel", ("mobil uygulama",)),
    ("Özel Gün / Dönemsel", ("sezon", "bayram", "yılbaşı", "kara cuma", "sevgililer", "anneler günü", "babalar günü")),
)


def classify(title: str, summary: str, slug: str, promo_codes: list[str]) -> tuple[str, list[str]]:
    haystack = tr_lower(f"{title} {summary} {slug}")
    tags: list[str] = []
    for label, needles in _CATEGORY_RULES:
        if any(n in haystack for n in needles) or (label == "Promosyon Kodu" and promo_codes):
            tags.append(label)
    return (tags[0] if tags else "Diğer Avantajlar"), tags


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def listing_fingerprint(lst: Listing) -> str:
    return stable_hash([norm_text(lst.title), _iso_date(lst.start) or "", _iso_date(lst.end) or ""])


def is_expired(end_date_iso: str | None, now: datetime) -> bool:
    """Bitiş tarihi (gün sonu + tolerans) geçti mi? Europe/Istanbul saatine göre."""
    if not end_date_iso:
        return False
    try:
        end_day = date.fromisoformat(end_date_iso)
    except ValueError:
        return False
    deadline = datetime.combine(end_day, time(23, 59, 59), tzinfo=config.TZ) + timedelta(
        hours=config.END_DATE_GRACE_HOURS
    )
    return now > deadline


def _bucket_due(slug: str, now: datetime) -> bool:
    slot = int(now.timestamp() // 300)
    return (zlib.crc32(slug.encode("utf-8")) + slot) % config.DETAIL_REFRESH_BUCKETS == 0


def _numbers(text: str) -> list[str]:
    return sorted(tok for tok in text.split() if any(ch.isdigit() for ch in tok))


def _text_changed(old: str | None, new: str | None) -> bool:
    """Önemli bir metin farkı var mı? (Küçük yazım farkları ve noktalama yok sayılır.)"""
    a, b = norm_text(old), norm_text(new)
    if a == b:
        return False
    if _numbers(a) != _numbers(b):
        return True  # Rakam/tarih değişimi her zaman önemlidir.
    if min(len(a), len(b)) < 60:
        return True
    return similarity(a, b) < config.TEXT_SIMILARITY_IGNORE


def compute_hash(rec: Record) -> str:
    parts = [
        norm_text(rec.get("title")),
        rec.get("discount") or "",
        rec.get("price") or "",
        rec.get("start_date") or "",
        rec.get("end_date") or "",
        rec.get("sale_period") or "",
        rec.get("flight_period") or "",
        norm_text(rec.get("offer")),
        norm_text(rec.get("eligibility")),
        "|".join(sorted(norm_text(r) for r in rec.get("routes") or [])),
        "|".join(rec.get("promo_codes") or []),
        "|".join(rec.get("bolpuan") or []),
        norm_text(rec.get("conditions")),
    ]
    return stable_hash(parts)


# --------------------------------------------------------------------------- #
# Kayıt oluşturma / güncelleme
# --------------------------------------------------------------------------- #


def _apply_listing(rec: Record, lst: Listing) -> None:
    rec["url"] = lst.url
    rec["slug"] = lst.slug
    rec["promotion_id"] = lst.promotion_id or rec.get("promotion_id")
    rec["sources"] = list(lst.sources)
    rec["list_section"] = lst.section


def _apply_detail(rec: Record, data: DetailData, lst: Listing) -> None:
    rec["title"] = data.title or lst.title
    rec["summary"] = data.summary
    rec["discount"] = data.discount
    rec["price"] = data.price
    rec["start_date"] = _iso_date(data.start_date) or _iso_date(lst.start)
    rec["end_date"] = _iso_date(data.end_date) or _iso_date(lst.end)
    rec["sale_period"] = data.sale_period
    rec["flight_period"] = data.flight_period
    rec["offer"] = data.offer
    rec["eligibility"] = data.eligibility
    rec["routes"] = list(data.routes)
    rec["price_terms"] = data.price_terms
    rec["promo_codes"] = list(data.promo_codes)
    rec["bolpuan"] = list(data.bolpuan)
    rec["conditions"] = data.conditions
    category, tags = classify(rec["title"], data.summary, rec["slug"], data.promo_codes)
    rec["category"], rec["tags"] = category, tags
    rec["detail_ok"] = True
    rec["list_fp"] = listing_fingerprint(lst)
    rec["content_hash"] = compute_hash(rec)


def _apply_list_only(rec: Record, lst: Listing) -> None:
    rec["title"] = lst.title
    rec["start_date"] = _iso_date(lst.start)
    rec["end_date"] = _iso_date(lst.end)
    category, tags = classify(lst.title, "", lst.slug, [])
    rec["category"], rec["tags"] = category, tags
    rec["list_fp"] = listing_fingerprint(lst)
    rec["content_hash"] = compute_hash(rec)


def _blank_record(lst: Listing, now: datetime) -> Record:
    return {
        "id": lst.slug,
        "slug": lst.slug,
        "url": lst.url,
        "promotion_id": lst.promotion_id,
        "title": lst.title,
        "summary": "",
        "category": "Diğer Avantajlar",
        "tags": [],
        "sources": list(lst.sources),
        "list_section": lst.section,
        "discount": None,
        "price": None,
        "start_date": None,
        "end_date": None,
        "sale_period": None,
        "flight_period": None,
        "offer": "",
        "eligibility": "",
        "routes": [],
        "price_terms": "",
        "promo_codes": [],
        "bolpuan": [],
        "conditions": "",
        "content_hash": "",
        "last_notified_hash": None,
        "last_queued_hash": None,
        "status": "active",
        "ended_at": None,
        "end_reason": None,
        "ended_notified": False,
        "notification_state": "baseline",
        "first_seen": iso(now),
        "last_seen": iso(now),
        "last_verified": None,
        "last_detail_fetch": None,
        "list_fp": "",
        "detail_ok": False,
        "missing_scans": 0,
        "suspect_count": 0,
    }


# --------------------------------------------------------------------------- #
# Fark hesaplama
# --------------------------------------------------------------------------- #

_VALUE_FIELDS = (
    ("discount", "İndirim", "📈"),
    ("price", "Fiyat", "💰"),
    ("start_date", "Başlangıç Tarihi", "📅"),
    ("end_date", "Bitiş Tarihi", "📅"),
    ("sale_period", "Satış Tarihleri", "🗓️"),
    ("flight_period", "Uçuş Tarihleri", "🛫"),
)
_DATE_FIELDS = {"start_date", "end_date"}


def _fmt(field_name: str, value: Any) -> str:
    if value in (None, "", []):
        return "—"
    if field_name in _DATE_FIELDS:
        return format_date_tr(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _short_list(items: list[str]) -> str:
    shown = items[: config.MAX_ROUTE_ITEMS_SHOWN]
    extra = len(items) - len(shown)
    text = "; ".join(shown)
    return f"{text} (+{extra} daha)" if extra > 0 else text


def diff_records(old: Record, new: Record) -> list[Change]:
    changes: list[Change] = []
    if norm_text(old.get("title")) != norm_text(new.get("title")):
        changes.append(Change("title", "Başlık", old.get("title"), new.get("title"), "🏷️"))
    for name, label, icon in _VALUE_FIELDS:
        if (old.get(name) or None) != (new.get(name) or None):
            changes.append(Change(name, label, _fmt(name, old.get(name)), _fmt(name, new.get(name)), icon))
    if sorted(old.get("promo_codes") or []) != sorted(new.get("promo_codes") or []):
        changes.append(Change("promo_codes", "Promosyon Kodu", _fmt("promo_codes", old.get("promo_codes")),
                              _fmt("promo_codes", new.get("promo_codes")), "🏷️"))
    if sorted(old.get("bolpuan") or []) != sorted(new.get("bolpuan") or []):
        changes.append(Change("bolpuan", "BolPuan", _fmt("bolpuan", old.get("bolpuan")),
                              _fmt("bolpuan", new.get("bolpuan")), "🎁"))

    old_routes = {norm_text(r): r for r in old.get("routes") or []}
    new_routes = {norm_text(r): r for r in new.get("routes") or []}
    if set(old_routes) != set(new_routes):
        removed = [old_routes[k] for k in old_routes if k not in new_routes]
        added = [new_routes[k] for k in new_routes if k not in old_routes]
        changes.append(Change("routes", "Geçerli Hatlar", _short_list(removed), _short_list(added), "🌍", "routes"))

    for name, label, icon in (("offer", "Kampanya Avantajı", "✨"), ("eligibility", "Katılım Koşulları", "👥")):
        if _text_changed(old.get(name), new.get(name)):
            changes.append(Change(name, label, None, None, icon, "note"))

    if not changes and _text_changed(old.get("conditions"), new.get("conditions")):
        changes.append(Change("conditions", "Kampanya Koşulları", None, None, "📝", "note"))
    return changes


# --------------------------------------------------------------------------- #
# İzleyici
# --------------------------------------------------------------------------- #


def plan_detail_fetches(state: State, scan: ScanResult, now: datetime) -> dict[str, str]:
    """Bu çalışmada detay sayfası çekilmesi gereken kampanyaların {slug: url} listesi."""
    bootstrap = not state["meta"].get("initialized")
    if bootstrap and not scan.complete:
        return {}
    cap = config.MAX_DETAIL_FETCHES_BOOTSTRAP if bootstrap else config.MAX_DETAIL_FETCHES_PER_RUN
    urgent: dict[str, str] = {}
    routine: dict[str, str] = {}
    campaigns = state["campaigns"]

    for slug, lst in scan.listings.items():
        rec = campaigns.get(slug)
        if lst.section == "past":
            continue  # Bitmiş kampanyalar için detay gerekmez.
        if rec is None:
            urgent[slug] = lst.url
        elif not rec.get("detail_ok") or rec.get("list_fp") != listing_fingerprint(lst):
            urgent[slug] = lst.url
        elif _bucket_due(slug, now):
            routine[slug] = lst.url  # Bitmiş görünen ama hâlâ listelenen kayıtlar da burada yeniden doğrulanır.

    if scan.disappearance_reliable:
        for slug, rec in campaigns.items():
            if (
                rec.get("status") == "active"
                and slug not in scan.listings
                and int(rec.get("missing_scans", 0)) + 1 >= config.MISSING_SCANS_FOR_REMOVAL
            ):
                urgent[slug] = rec["url"]

    plan = dict(urgent)
    for slug, url in routine.items():
        if len(plan) >= cap:
            break
        plan[slug] = url
    if len(plan) > cap:
        plan = dict(list(plan.items())[:cap])
        log.warning("Detay istek sınırı (%d) aşıldı; kalanlar sonraki çalışmaya bırakıldı", cap)
    return plan


def process(
    state: State,
    scan: ScanResult,
    details: dict[str, DetailResult],
    now: datetime,
) -> list[Event]:
    """Taramayı state ile karşılaştırır, state'i günceller ve olayları döndürür."""
    meta = state["meta"]
    bootstrap = not meta.get("initialized")
    if bootstrap and not scan.complete:
        failed = [f"{k}: {r.error}" for k, r in scan.sources.items() if not r.ok]
        log.error("İlk kurulum ertelendi; tüm kaynaklar sağlıklı olmalı. Başarısız: %s", "; ".join(failed))
        return []

    campaigns: dict[str, Record] = state["campaigns"]
    events: list[Event] = []

    for slug, lst in scan.listings.items():
        rec = campaigns.get(slug)
        detail = details.get(slug)
        detail_data = detail.data if detail is not None and detail.status == "ok" else None
        if detail is not None and detail.status != "ok":
            log.warning("Detay alınamadı (%s): %s %s", slug, detail.status, detail.error or "")

        if rec is None:
            _handle_new(state, lst, detail_data, bootstrap, now, events)
            continue
        _handle_existing(rec, lst, detail_data, now, events)

    _handle_missing(state, scan, details, now, events)
    _handle_expiry(state, now, events)

    if bootstrap:
        meta["initialized"] = True
        meta["created_at"] = meta.get("created_at") or iso(now)
        log.info("İlk kurulum tamamlandı: %d kampanya kaydedildi, bildirim gönderilmedi", len(campaigns))
    return events


# -- yeni kampanya -----------------------------------------------------------


def _handle_new(
    state: State, lst: Listing, data: DetailData | None, bootstrap: bool, now: datetime, events: list[Event]
) -> None:
    campaigns = state["campaigns"]
    rec = _blank_record(lst, now)

    if lst.section == "past":
        _apply_list_only(rec, lst)
        _mark_ended(rec, now, "past_section", notified=True)
        rec["last_notified_hash"] = rec["content_hash"]
        campaigns[lst.slug] = rec
        return

    if data is not None:
        _apply_detail(rec, data, lst)
        rec["last_verified"] = iso(now)
        rec["last_detail_fetch"] = iso(now)
    else:
        if not bootstrap:
            deferred = state["deferred_new"]
            count = int(deferred.get(lst.slug, 0)) + 1
            if count < config.NEW_CAMPAIGN_DETAIL_RETRIES:
                deferred[lst.slug] = count
                log.warning("Yeni kampanya detayı alınamadı; erteleniyor (%d/%d): %s",
                            count, config.NEW_CAMPAIGN_DETAIL_RETRIES, lst.slug)
                return
        _apply_list_only(rec, lst)
    state["deferred_new"].pop(lst.slug, None)

    if is_expired(rec.get("end_date"), now):
        _mark_ended(rec, now, "date", notified=True)  # Zaten bitmiş yeni kayıt: sessizce sakla.
        rec["last_notified_hash"] = rec["content_hash"]
        campaigns[lst.slug] = rec
        return

    campaigns[lst.slug] = rec
    if bootstrap:
        rec["last_notified_hash"] = rec["content_hash"]
        return
    rec["last_queued_hash"] = rec["content_hash"]
    rec["notification_state"] = "pending"
    events.append(
        Event("new", rec["id"], f"new:{rec['id']}", copy.deepcopy(rec), content_hash=rec["content_hash"])
    )
    log.info("YENİ kampanya: %s", rec["title"])


# -- mevcut kampanya -----------------------------------------------------------


def _handle_existing(
    rec: Record, lst: Listing, data: DetailData | None, now: datetime, events: list[Event]
) -> None:
    rec["last_seen"] = iso(now)
    rec["missing_scans"] = 0
    _apply_listing(rec, lst)

    if lst.section == "past":
        if rec["status"] == "active":
            _mark_ended(rec, now, "past_section")
            _emit_ended(rec, "past_section", events)
        return

    if data is None:
        return  # Detay yok: liste bilgisiyle içerik değişikliği iddia etmeyiz.

    rec["last_detail_fetch"] = iso(now)
    rec["last_verified"] = iso(now)

    if not rec.get("detail_ok"):
        # Liste-only kayıttan ilk detay: sessiz temel çizgi.
        _apply_detail(rec, data, lst)
        rec["last_notified_hash"] = rec["content_hash"]
        return

    candidate = copy.deepcopy(rec)
    _apply_detail(candidate, data, lst)

    old_len, new_len = len(rec.get("conditions") or ""), len(candidate.get("conditions") or "")
    if old_len >= 400 and new_len < old_len * config.SHRINK_GUARD_RATIO:
        suspects = int(rec.get("suspect_count", 0)) + 1
        if suspects < config.SHRINK_GUARD_MAX_SUSPECT:
            rec["suspect_count"] = suspects
            log.warning("Detay metni şüpheli ölçüde kısaldı (%d→%d); HTML değişmiş olabilir, atlandı: %s",
                        old_len, new_len, rec["id"])
            return
    rec["suspect_count"] = 0

    changes = diff_records(rec, candidate)
    was_ended = rec["status"] == "ended"
    for key in ("title", "summary", "category", "tags", "discount", "price", "start_date", "end_date",
                "sale_period", "flight_period", "offer", "eligibility", "routes", "price_terms",
                "promo_codes", "bolpuan", "conditions", "content_hash", "list_fp", "detail_ok"):
        rec[key] = candidate[key]

    if was_ended:
        if lst.section == "current" and not is_expired(rec.get("end_date"), now):
            rec["status"], rec["ended_at"], rec["end_reason"] = "active", None, None
            rec["ended_notified"] = False
            changes = [Change("status", "Durum", "Sona ermişti", "Yeniden aktif", "🔁")] + changes
            rec["last_queued_hash"] = rec["content_hash"]
            rec["notification_state"] = "pending"
            events.append(
                Event("updated", rec["id"], f"reactivated:{rec['id']}:{rec['content_hash']}",
                      copy.deepcopy(rec), changes, content_hash=rec["content_hash"])
            )
            log.info("Kampanya yeniden aktif: %s", rec["title"])
        return

    if changes and rec["content_hash"] != rec.get("last_queued_hash"):
        rec["last_queued_hash"] = rec["content_hash"]
        rec["notification_state"] = "pending"
        events.append(
            Event("updated", rec["id"], f"updated:{rec['id']}:{rec['content_hash']}",
                  copy.deepcopy(rec), changes, content_hash=rec["content_hash"])
        )
        log.info("GÜNCELLENDİ: %s (%s)", rec["title"], ", ".join(c.field for c in changes))


# -- kaybolan / biten kampanya ---------------------------------------------------


def _handle_missing(
    state: State, scan: ScanResult, details: dict[str, DetailResult], now: datetime, events: list[Event]
) -> None:
    if not scan.disappearance_reliable:
        if any(r.get("status") == "active" and s not in scan.listings for s, r in state["campaigns"].items()):
            log.warning("Bazı kaynaklar başarısız; kaybolan kampanyalar hakkında karar verilmedi")
        return
    for slug, rec in state["campaigns"].items():
        if rec.get("status") != "active" or slug in scan.listings:
            continue
        rec["missing_scans"] = int(rec.get("missing_scans", 0)) + 1
        if rec["missing_scans"] < config.MISSING_SCANS_FOR_REMOVAL:
            continue
        detail = details.get(slug)
        if detail is not None and detail.status == "gone":
            _mark_ended(rec, now, "removed")
            _emit_ended(rec, "removed", events)
        else:
            log.warning("Kampanya %d taramadır listede yok ama sayfası silinmedi; bitmiş sayılmadı: %s",
                        rec["missing_scans"], slug)


def _handle_expiry(state: State, now: datetime, events: list[Event]) -> None:
    for rec in state["campaigns"].values():
        if rec.get("status") == "active" and is_expired(rec.get("end_date"), now):
            _mark_ended(rec, now, "date")
            _emit_ended(rec, "date", events)


def _mark_ended(rec: Record, now: datetime, reason: str, notified: bool = False) -> None:
    rec["status"] = "ended"
    rec["ended_at"] = iso(now)
    rec["end_reason"] = reason
    rec["ended_notified"] = notified
    if notified:
        rec["notification_state"] = "baseline"


def _emit_ended(rec: Record, reason: str, events: list[Event]) -> None:
    if rec.get("ended_notified"):
        return
    rec["ended_notified"] = True
    rec["notification_state"] = "pending"
    events.append(
        Event("ended", rec["id"], f"ended:{rec['id']}:{rec['ended_at']}", copy.deepcopy(rec),
              reason=reason, content_hash=rec.get("content_hash", ""))
    )
    log.info("SONA ERDİ (%s): %s", reason, rec["title"])


# -- outbox sonuçları -----------------------------------------------------------


def mark_delivered(state: State, item: dict[str, Any]) -> None:
    rec = state["campaigns"].get(item["campaign_id"])
    if rec is None:
        return
    if item["event"] in ("new", "updated"):
        rec["last_notified_hash"] = item.get("content_hash") or rec.get("content_hash")
    if rec.get("notification_state") == "pending" and rec.get("last_queued_hash") == item.get("content_hash") \
            or item["event"] == "ended":
        rec["notification_state"] = "sent"


def mark_failed(state: State, item: dict[str, Any]) -> None:
    rec = state["campaigns"].get(item["campaign_id"])
    if rec is not None and rec.get("notification_state") == "pending":
        rec["notification_state"] = "failed"
