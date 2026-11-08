"""Kalıcı hafıza: .state/campaigns.json okuma/yazma, bozuk dosya kurtarma ve outbox.

Tasarım ilkeleri
* Yazma atomiktir (geçici dosya + os.replace); yarım kalan yazma dosyayı bozmaz.
* Yalnızca "kararlı" içerik değiştiğinde (veya günde bir kez heartbeat) dosya yazılır;
  böylece her 5 dakikada gereksiz commit oluşmaz.
* Bozuk JSON asla sessizce silinmez; ``.corrupt-<zaman>`` olarak yedeklenir ve bot
  sessiz bir "ilk kurulum" moduna döner (geçmiş kampanyalar için bildirim gitmez).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config

log = logging.getLogger(__name__)

State = dict[str, Any]

# Her çalışmada değişen ve tek başına commit gerektirmeyen alanlar.
_VOLATILE_CAMPAIGN_KEYS = ("last_seen", "last_verified", "last_detail_fetch")
_VOLATILE_META_KEYS = ("updated_at", "last_run_at")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def new_state() -> State:
    return {
        "schema_version": config.STATE_SCHEMA_VERSION,
        "meta": {
            "initialized": False,
            "created_at": None,
            "updated_at": None,
            "last_run_at": None,
            "last_heartbeat": None,
            "recovered_from_corruption": False,
        },
        "campaigns": {},
        "deferred_new": {},
        "outbox": [],
    }


class StateCorruptError(ValueError):
    """State dosyası okunabilir ama beklenen yapıda değil."""


def validate_state(data: Any) -> State:
    """Yapıyı doğrular; eksik üst düzey anahtarları güvenle tamamlar."""
    if not isinstance(data, dict):
        raise StateCorruptError("kök nesne sözlük değil")
    version = data.get("schema_version")
    if not isinstance(version, int) or version > config.STATE_SCHEMA_VERSION:
        raise StateCorruptError(f"desteklenmeyen schema_version: {version!r}")
    campaigns = data.get("campaigns")
    if not isinstance(campaigns, dict):
        raise StateCorruptError("'campaigns' sözlük değil")
    for cid, rec in campaigns.items():
        if not isinstance(rec, dict) or rec.get("id") != cid or not rec.get("url"):
            raise StateCorruptError(f"geçersiz kampanya kaydı: {cid!r}")
    outbox = data.get("outbox", [])
    if not isinstance(outbox, list) or not all(isinstance(i, dict) and "key" in i for i in outbox):
        raise StateCorruptError("'outbox' geçersiz")
    base = new_state()
    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise StateCorruptError("'meta' sözlük değil")
    base["meta"].update(meta)
    base["campaigns"] = campaigns
    base["outbox"] = outbox
    deferred = data.get("deferred_new", {})
    base["deferred_new"] = deferred if isinstance(deferred, dict) else {}
    base["schema_version"] = config.STATE_SCHEMA_VERSION
    return base


def stable_digest(state: State) -> str:
    """Değişken zaman damgaları hariç state'in özeti; gereksiz yazmayı önler."""
    snapshot = copy.deepcopy(state)
    for key in _VOLATILE_META_KEYS:
        snapshot["meta"].pop(key, None)
    for rec in snapshot["campaigns"].values():
        for key in _VOLATILE_CAMPAIGN_KEYS:
            rec.pop(key, None)
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class StateManager:
    def __init__(self, path: Path | str = config.DEFAULT_STATE_PATH):
        self.path = Path(path)
        self._loaded_digest: str | None = None
        self.status = "unloaded"  # new | ok | recovered

    # ------------------------------------------------------------------ #
    # Okuma
    # ------------------------------------------------------------------ #
    def load(self, now: datetime | None = None) -> State:
        now = now or utcnow()
        if not self.path.exists():
            log.info("State dosyası yok; ilk kurulum modu (%s)", self.path)
            self.status = "new"
            state = new_state()
            self._loaded_digest = stable_digest(state)
            return state
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                # Boş dosya (örn. ilk commit için yer tutucu) bozuk sayılmaz.
                log.info("State dosyası boş; ilk kurulum modu")
                self.status = "new"
                state = new_state()
            else:
                state = validate_state(json.loads(raw))
                self.status = "ok"
        except (json.JSONDecodeError, StateCorruptError, UnicodeDecodeError, OSError) as exc:
            log.error("State dosyası bozuk (%s); yedeklenip yeniden kurulacak: %s", self.path, exc)
            self._quarantine(now)
            self.status = "recovered"
            state = new_state()
            state["meta"]["recovered_from_corruption"] = True
        self._loaded_digest = stable_digest(state)
        return state

    def _quarantine(self, now: datetime) -> None:
        stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.replace(self.path, target)
            log.error("Bozuk state dosyası şuraya taşındı: %s", target)
        except OSError as exc:  # pragma: no cover - dosya sistemi hatası
            log.error("Bozuk state yedeklenemedi: %s", exc)

    # ------------------------------------------------------------------ #
    # Yazma
    # ------------------------------------------------------------------ #
    def save_if_changed(self, state: State, now: datetime | None = None, force: bool = False) -> bool:
        """Anlamlı bir değişiklik veya heartbeat zamanı varsa yazar. Yazıldıysa True döner."""
        now = now or utcnow()
        meta = state["meta"]
        meta["last_run_at"] = iso(now)
        digest = stable_digest(state)
        changed = digest != self._loaded_digest
        last_beat = parse_iso(meta.get("last_heartbeat"))
        heartbeat_due = last_beat is None or now - last_beat >= timedelta(hours=config.HEARTBEAT_HOURS)
        file_missing = not self.path.exists()
        if not (changed or heartbeat_due or force or file_missing):
            return False
        if heartbeat_due and not changed and last_beat is not None:
            log.info("Günlük heartbeat için state yazılıyor")
        meta["updated_at"] = iso(now)
        if heartbeat_due or force or last_beat is None:
            meta["last_heartbeat"] = iso(now)
        if not meta.get("created_at"):
            meta["created_at"] = iso(now)
        self._write(state)
        self._loaded_digest = stable_digest(state)
        return True

    def _write(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        json.loads(payload)  # Yazmadan önce geçerliliği doğrula.
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    # Outbox
    # ------------------------------------------------------------------ #
    @staticmethod
    def enqueue(state: State, item: dict[str, Any], now: datetime) -> bool:
        """Aynı anahtarlı bekleyen öğe varsa eklemez. Eklendiyse True."""
        for existing in state["outbox"]:
            if existing["key"] == item["key"] and existing.get("status") == "pending":
                return False
        entry = {
            "key": item["key"],
            "event": item["event"],
            "campaign_id": item["campaign_id"],
            "content_hash": item.get("content_hash"),
            "text": item["text"],
            "status": "pending",
            "attempts": 0,
            "created_at": iso(now),
            "sent_at": None,
            "last_error": None,
        }
        state["outbox"].append(entry)
        return True

    @staticmethod
    def pending(state: State) -> list[dict[str, Any]]:
        return [i for i in state["outbox"] if i.get("status") == "pending"]

    @staticmethod
    def prune_outbox(state: State, now: datetime) -> None:
        cutoff = now - timedelta(days=config.OUTBOX_RETENTION_DAYS)
        kept: list[dict[str, Any]] = []
        for item in state["outbox"]:
            if item.get("status") == "pending":
                kept.append(item)
                continue
            stamp = parse_iso(item.get("sent_at") or item.get("created_at"))
            if stamp is None or stamp >= cutoff:
                kept.append(item)
        done = [i for i in kept if i.get("status") != "pending"]
        if len(done) > config.OUTBOX_MAX_ITEMS:
            drop = {id(i) for i in done[: len(done) - config.OUTBOX_MAX_ITEMS]}
            kept = [i for i in kept if id(i) not in drop]
        state["outbox"] = kept
