"""Metin normalizasyonu, Türkçe tarih ayrıştırma/biçimlendirme ve yardımcılar."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Iterable

from config import TZ

TR_MONTHS = (
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)


def tr_lower(text: str) -> str:
    """Türkçe'ye duyarlı küçük harfe çevirme (İ→i, I→ı); karakter sayısını korur."""
    return text.replace("İ", "i").replace("I", "ı").lower()


_MONTH_LOOKUP: dict[str, int] = {}
for _i, _name in enumerate(TR_MONTHS, start=1):
    _MONTH_LOOKUP[tr_lower(_name)] = _i
_ASCII_FOLD = str.maketrans("şğüöçı", "sguoci")
for _i, _name in enumerate(TR_MONTHS, start=1):
    _MONTH_LOOKUP[tr_lower(_name).translate(_ASCII_FOLD)] = _i

_MONTH_ALT = "|".join(sorted(_MONTH_LOOKUP, key=len, reverse=True))
_DATE_TEXT_RE = re.compile(rf"(\d{{1,2}})\s+({_MONTH_ALT})\s+(\d{{4}})")
_DATE_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")
_DATE_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

_DASH = r"[-–—]"
_RANGE_FULL = re.compile(
    rf"(\d{{1,2}})\s+({_MONTH_ALT})\s+(\d{{4}})\s*{_DASH}\s*(\d{{1,2}})\s+({_MONTH_ALT})\s+(\d{{4}})"
)
_RANGE_MIXED = re.compile(
    rf"(\d{{1,2}})\s+({_MONTH_ALT})\s*{_DASH}\s*(\d{{1,2}})\s+({_MONTH_ALT})\s+(\d{{4}})"
)
_RANGE_SAME_MONTH = re.compile(
    rf"(\d{{1,2}})\s*{_DASH}\s*(\d{{1,2}})\s+({_MONTH_ALT})\s+(\d{{4}})"
)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_dates(text: str | None) -> list[date]:
    """Metindeki tüm tarihleri (ISO, gg.aa.yyyy, '27 Ekim 2026') görünme sırasıyla döndürür."""
    if not text:
        return []
    lowered = tr_lower(unicodedata.normalize("NFKC", text))
    found: list[tuple[int, date]] = []
    for m in _DATE_TEXT_RE.finditer(lowered):
        d = _safe_date(int(m.group(3)), _MONTH_LOOKUP[m.group(2)], int(m.group(1)))
        if d:
            found.append((m.start(), d))
    for m in _DATE_NUM_RE.finditer(lowered):
        d = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d:
            found.append((m.start(), d))
    for m in _DATE_ISO_RE.finditer(lowered):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
    found.sort(key=lambda item: item[0])
    return [d for _, d in found]


def format_date_tr(value: date | datetime | str | None) -> str:
    """'2026-10-27' veya date nesnesini '27 Ekim 2026' biçimine çevirir."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        parsed = extract_dates(value)
        if not parsed:
            return value
        value = parsed[0]
    if isinstance(value, datetime):
        value = value.astimezone(TZ).date() if value.tzinfo else value.date()
    return f"{value.day} {TR_MONTHS[value.month - 1]} {value.year}"


def format_datetime_tr(value: datetime) -> str:
    """Europe/Istanbul saatiyle 'gg.aa.yyyy SS:DD'."""
    local = value.astimezone(TZ)
    return local.strftime("%d.%m.%Y %H:%M")


def extract_date_range(text: str | None) -> str | None:
    """Metindeki ilk tarih/tarih aralığını okunabilir kısa biçimde döndürür.

    Örnek: '21-23 Ekim 2026' → '21–23 Ekim 2026'.
    """
    if not text:
        return None
    lowered = tr_lower(unicodedata.normalize("NFKC", text))
    candidates: list[tuple[int, int, str]] = []

    def name(token: str) -> str:
        return TR_MONTHS[_MONTH_LOOKUP[token] - 1]

    m = _RANGE_FULL.search(lowered)
    if m:
        candidates.append((m.start(), -(m.end() - m.start()),
                           f"{int(m.group(1))} {name(m.group(2))} {m.group(3)} – "
                           f"{int(m.group(4))} {name(m.group(5))} {m.group(6)}"))
    m = _RANGE_MIXED.search(lowered)
    if m:
        candidates.append((m.start(), -(m.end() - m.start()),
                           f"{int(m.group(1))} {name(m.group(2))} – "
                           f"{int(m.group(3))} {name(m.group(4))} {m.group(5)}"))
    m = _RANGE_SAME_MONTH.search(lowered)
    if m:
        candidates.append((m.start(), -(m.end() - m.start()),
                           f"{int(m.group(1))}–{int(m.group(2))} {name(m.group(3))} {m.group(4)}"))
    m = _DATE_TEXT_RE.search(lowered)
    if m:
        candidates.append((m.start(), -(m.end() - m.start()),
                           f"{int(m.group(1))} {name(m.group(2))} {m.group(3)}"))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w%€₺$+]+", re.UNICODE)
_QUOTE_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', " ": " "})


def clean_text(text: str | None) -> str:
    """Görüntüleme için: boşlukları sadeleştirir, büyük/küçük harfi korur."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_MAP)
    return _WS_RE.sub(" ", text).strip()


def norm_text(text: str | None) -> str:
    """Karşılaştırma/hash için: NFKC, Türkçe küçük harf, noktalama ve boşluk farklarını siler."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_MAP)
    text = tr_lower(text)
    text = _NON_WORD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def similarity(a: str, b: str) -> float:
    """Kelime düzeyinde benzerlik oranı (0..1). Girdiler önceden norm_text'ten geçmiş olmalı."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.split(), b.split(), autojunk=False).ratio()


def stable_hash(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:32]


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"
