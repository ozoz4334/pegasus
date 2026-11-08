"""Sabitler ve yapılandırma.

Bu dosyada yalnızca canlı sitede doğrulanmış kaynak adresleri bulunur.
Gizli bilgiler (token, chat id) ASLA burada tutulmaz; ortam değişkenlerinden okunur.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Genel
# --------------------------------------------------------------------------- #
BASE_URL = "https://www.flypgs.com"
TZ = ZoneInfo("Europe/Istanbul")  # Kullanıcıya gösterilen tüm tarihler bu saat dilimindedir.

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = PROJECT_ROOT / ".state" / "campaigns.json"

CAMPAIGN_PATH_PREFIX = "/kampanyali-ucak-biletleri/"
# Liste/arşiv sayfasının kendisi bir kampanya değildir.
NON_CAMPAIGN_SLUGS = frozenset({"gecmis-kampanyalar"})

USER_AGENT = "Mozilla/5.0 (compatible; PegasusCampaignBot/1.0; kisisel kampanya takibi)"


# --------------------------------------------------------------------------- #
# Kaynaklar (2026-11-08 tarihinde canlı olarak doğrulandı; hepsi giriş gerektirmez)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Source:
    key: str
    url: str
    label: str
    # Bu kaynağın "bir kampanya listeden kayboldu" kararına katkısı var mı?
    # (Geçmiş arşivi yalnızca "sona erdi" kanıtıdır, kaybolma kanıtı değildir.)
    required_for_disappearance: bool = True


SOURCES: tuple[Source, ...] = (
    Source("kampanyali", f"{BASE_URL}/kampanyali-ucak-biletleri", "Kampanyalı Uçak Biletleri"),
    Source("bolbol", f"{BASE_URL}/bolbol/kampanyalar", "BolBol Kampanyaları"),
    Source("sana-ozel", f"{BASE_URL}/sana-ozel-kampanyalar", "Sana Özel Kampanyalar (herkese açık kısım)"),
    Source(
        "gecmis",
        f"{BASE_URL}/kampanyali-ucak-biletleri/gecmis-kampanyalar",
        "Geçmiş Kampanyalar",
        required_for_disappearance=False,
    ),
)

# --------------------------------------------------------------------------- #
# HTTP davranışı
# --------------------------------------------------------------------------- #
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 25
HTTP_MAX_RETRIES = 3  # ilk deneme + 3 yeniden deneme
BACKOFF_BASE_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 60  # Retry-After bundan uzunsa bu çalışmada beklemeyiz, kaynağı atlarız.
LIST_FETCH_WORKERS = 2  # Pegasus'u yormamak için eşzamanlı liste isteği sınırı.
DETAIL_REQUEST_DELAY_SECONDS = 0.4

# --------------------------------------------------------------------------- #
# Detay sayfası tarama stratejisi
# --------------------------------------------------------------------------- #
# Her kampanyanın detayı yaklaşık DETAIL_REFRESH_BUCKETS * 5 dk'da bir yenilenir
# (6 * 5 = 30 dk). Kampanyalar kovalara dağıtılır; böylece her çalışmada yalnızca
# birkaç detay isteği yapılır. Yeni kampanya veya liste kartı değişirse detay hemen çekilir.
DETAIL_REFRESH_BUCKETS = 6
MAX_DETAIL_FETCHES_PER_RUN = 12
MAX_DETAIL_FETCHES_BOOTSTRAP = 80
NEW_CAMPAIGN_DETAIL_RETRIES = 3  # Detayı alınamayan yeni kampanya bu kadar çalışma ertelenir.

# --------------------------------------------------------------------------- #
# Değişiklik tespiti
# --------------------------------------------------------------------------- #
MISSING_SCANS_FOR_REMOVAL = 3  # Üst üste bu kadar sağlıklı taramada listede yoksa "kayboldu" sayılır.
END_DATE_GRACE_HOURS = 2  # Bitiş tarihinin gün sonundan sonra tanınan tolerans.
TEXT_SIMILARITY_IGNORE = 0.985  # Uzun metinlerde bu benzerlik üstü fark "önemsiz yazım farkı" sayılır.
SHRINK_GUARD_RATIO = 0.4  # Detay metni bu orandan fazla küçülürse HTML bozulmuş sayılır.
SHRINK_GUARD_MAX_SUSPECT = 3  # Bu kadar çalışma üst üste küçük kalırsa gerçek değişiklik kabul edilir.
MAX_CONDITIONS_CHARS = 30_000
MAX_ROUTE_ITEMS_SHOWN = 6

# --------------------------------------------------------------------------- #
# State / outbox
# --------------------------------------------------------------------------- #
STATE_SCHEMA_VERSION = 1
HEARTBEAT_HOURS = 24  # Hiçbir şey değişmese bile günde bir kez state yazılır (Actions'ın zamanlamayı kapatmaması için).
OUTBOX_RETENTION_DAYS = 14
OUTBOX_MAX_ITEMS = 300

# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_MAX_ATTEMPTS = 4
TELEGRAM_MAX_RETRY_AFTER = 30
TELEGRAM_MESSAGE_GAP_SECONDS = 1.1  # Aynı sohbete saniyede ~1 mesaj sınırına saygı.
TELEGRAM_TIMEOUT = 20
OUTBOX_MAX_ATTEMPTS = 50  # Kalıcı olarak gönderilemeyen bildirimi sonsuza dek denememek için.


def get_telegram_credentials() -> tuple[str | None, str | None]:
    """Token ve chat id'yi yalnızca ortam değişkenlerinden okur."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip() or None
    return token, chat_id
