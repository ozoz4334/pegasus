"""Pegasus Kampanya Takip Botu – giriş noktası.

Kullanım:
    python main.py                 # normal çalışma (GitHub Actions bunu çağırır)
    python main.py --dry-run       # Telegram'a göndermeden, state'e yazmadan değişiklikleri göster
    python main.py --send-test     # Telegram bağlantısını test eden tek bir mesaj gönder
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import campaign_tracker as tracker
import config
import scraper
from state_manager import StateManager, iso
from telegram_notifier import TelegramNotifier, render_event, render_test_message

log = logging.getLogger("pegasus")


# --------------------------------------------------------------------------- #
# Loglama
# --------------------------------------------------------------------------- #


class _RedactFilter(logging.Filter):
    """Gizli değerleri (token vb.) log satırlarından siler."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            for secret in self._secrets:
                message = message.replace(secret, "***")
            record.msg, record.args = message, None
        return True


def setup_logging(verbose: bool = False) -> None:
    token, chat_id = config.get_telegram_credentials()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    formatter.converter = lambda ts: datetime.fromtimestamp(ts, config.TZ).timetuple()  # Europe/Istanbul
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(_RedactFilter([token or "", chat_id or ""]))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _annotate(level: str, message: str) -> None:
    """GitHub Actions'ta çalışma özetinde görünen uyarı/hata notu."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        safe = re.sub(r"[\r\n]+", " ", message)
        print(f"::{level}::{safe}")


# --------------------------------------------------------------------------- #
# Çalışma
# --------------------------------------------------------------------------- #


@dataclass
class RunReport:
    events: list[tracker.Event] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)  # dry-run çıktısı
    sent: int = 0
    pending_left: int = 0
    failed_sources: list[str] = field(default_factory=list)
    state_written: bool = False
    bootstrapped: bool = False
    exit_code: int = 0


def enqueue_events(state: dict, events: list[tracker.Event], now: datetime) -> int:
    added = 0
    for event in events:
        item = {
            "key": event.key,
            "event": event.type,
            "campaign_id": event.campaign_id,
            "content_hash": event.content_hash,
            "text": render_event(event),
        }
        if StateManager.enqueue(state, item, now):
            added += 1
    return added


def deliver_outbox(
    state: dict,
    notifier: TelegramNotifier,
    manager: StateManager,
    now_fn: Callable[[], datetime],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Bekleyen bildirimleri sırayla gönderir; her başarıdan sonra state'i diske yazar."""
    sent = 0
    first = True
    for item in StateManager.pending(state):
        if not first:
            sleep(config.TELEGRAM_MESSAGE_GAP_SECONDS)
        first = False
        item["attempts"] = int(item.get("attempts", 0)) + 1
        result = notifier.send(item["text"])
        if result.ok:
            item["status"] = "sent"
            item["sent_at"] = iso(now_fn())
            item["last_error"] = None
            tracker.mark_delivered(state, item)
            sent += 1
            manager.save_if_changed(state, now_fn())  # Çökme halinde tekrarı en aza indirir.
            continue
        item["last_error"] = result.error
        log.error("Bildirim gönderilemedi (%s): %s", item["key"], result.error)
        if result.permanent or item["attempts"] >= config.OUTBOX_MAX_ATTEMPTS:
            item["status"] = "failed"
            tracker.mark_failed(state, item)
            _annotate("error", f"Bildirim kalıcı olarak gönderilemedi: {item['key']}")
            continue
        if result.fatal:
            _annotate("error", "Telegram yetki hatası: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID değerlerini kontrol edin")
        break  # Geçici hata: kalanları sonraki çalışmaya bırak (sıra korunur).
    manager.save_if_changed(state, now_fn())
    return sent


def run_once(
    *,
    state_path: Path | str,
    client: scraper.HttpClient,
    notifier: TelegramNotifier | None,
    dry_run: bool = False,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    sources=config.SOURCES,
) -> RunReport:
    report = RunReport()
    manager = StateManager(state_path)
    now = now_fn()
    state = manager.load(now)
    if dry_run:
        state = copy.deepcopy(state)  # Gerçek state asla değişmesin.
    was_initialized = bool(state["meta"].get("initialized"))
    if state["meta"].get("recovered_from_corruption") and not was_initialized:
        _annotate("warning", "State dosyası bozuktu; yedeklendi ve sessiz ilk kurulum yapılıyor")

    scan = scraper.scan_sources(client, sources)
    report.failed_sources = [f"{k}: {r.error}" for k, r in scan.sources.items() if not r.ok]
    for failure in report.failed_sources:
        _annotate("warning", f"Kaynak başarısız – {failure}")

    to_fetch = tracker.plan_detail_fetches(state, scan, now)
    details = scraper.fetch_details(client, to_fetch, sleep=sleep) if to_fetch else {}
    log.info("Tarama: %d kampanya listede, %d detay isteği, toplam %d HTTP isteği",
             len(scan.listings), len(to_fetch), client.request_count)

    events = tracker.process(state, scan, details, now)
    report.events = events
    report.bootstrapped = (not was_initialized) and bool(state["meta"].get("initialized"))
    enqueue_events(state, events, now)
    StateManager.prune_outbox(state, now)

    if dry_run:
        report.messages = [i["text"] for i in StateManager.pending(state)]
        return report

    report.state_written = manager.save_if_changed(state, now)  # Önce "pending" kaydı kalıcı olsun.

    pending = StateManager.pending(state)
    if pending:
        if notifier is None:
            log.error("%d bekleyen bildirim var ama TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil", len(pending))
            _annotate("error", "Telegram bilgileri (secrets) tanımlı değil; bildirimler bekletiliyor")
            report.exit_code = 1
        else:
            report.sent = deliver_outbox(state, notifier, manager, lambda: now_fn(), sleep)
            report.state_written = True
    report.pending_left = len(StateManager.pending(state))
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pegasus kampanya takip botu")
    parser.add_argument("--dry-run", action="store_true",
                        help="Telegram'a göndermeden ve state'e yazmadan değişiklikleri terminalde göster")
    parser.add_argument("--send-test", action="store_true",
                        help="Yalnızca Telegram test mesajı gönder ve çık")
    parser.add_argument("--state-path", default=str(config.DEFAULT_STATE_PATH),
                        help="State dosyası yolu (varsayılan: .state/campaigns.json)")
    parser.add_argument("--verbose", action="store_true", help="Ayrıntılı log")
    return parser


def _make_notifier() -> TelegramNotifier | None:
    token, chat_id = config.get_telegram_credentials()
    if not token or not chat_id:
        return None
    return TelegramNotifier(token, chat_id)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    if args.send_test:
        notifier = _make_notifier()
        if notifier is None:
            log.error("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ortam değişkenleri tanımlı olmalı")
            return 1
        result = notifier.send(render_test_message())
        if result.ok:
            log.info("Test mesajı gönderildi")
            return 0
        log.error("Test mesajı gönderilemedi: %s", result.error)
        return 1

    notifier = None if args.dry_run else _make_notifier()
    try:
        report = run_once(
            state_path=args.state_path,
            client=scraper.HttpClient(),
            notifier=notifier,
            dry_run=args.dry_run,
        )
    except Exception:  # noqa: BLE001 - beklenmeyen hata: ayrıntılı log + başarısız çıkış
        log.exception("Beklenmeyen hata")
        _annotate("error", "Pegasus takip botu beklenmeyen bir hatayla durdu; ayrıntı için logları açın")
        return 1

    if args.dry_run:
        print()
        if report.bootstrapped:
            print("[dry-run] İlk kurulum: mevcut kampanyalar kaydedilirdi, bildirim gönderilmezdi.")
        if not report.messages:
            print("[dry-run] Bildirim gerektiren değişiklik yok.")
        for text in report.messages:
            print("-" * 60)
            print(re.sub(r"<[^>]+>", "", text))
        print("-" * 60 if report.messages else "", end="")
        print(f"[dry-run] {len(report.messages)} bildirim üretildi (Telegram'a gönderilmedi).")
    else:
        log.info("Bitti: gönderilen=%d bekleyen=%d state_yazıldı=%s",
                 report.sent, report.pending_left, report.state_written)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
