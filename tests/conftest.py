"""Ortak test ayarları. Testler gerçek Pegasus sitesine veya Telegram'a ASLA bağlanmaz."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_config(monkeypatch):
    """Testlerde her kampanya her çalışmada yenilensin ve beklemeler atlansın."""
    monkeypatch.setattr(config, "DETAIL_REFRESH_BUCKETS", 1)
    monkeypatch.setattr(config, "MAX_DETAIL_FETCHES_PER_RUN", 100)
    monkeypatch.setattr(config, "DETAIL_REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "TELEGRAM_MESSAGE_GAP_SECONDS", 0)


@pytest.fixture()
def state_path(tmp_path):
    return tmp_path / ".state" / "campaigns.json"
