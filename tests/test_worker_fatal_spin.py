from __future__ import annotations

import logging
import os

import pytest


def _record(message: str, *, logger_name: str = "telethon.network.mtprotosender") -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name,
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_fatal_spin_watcher_persists_and_notifies_before_exit(monkeypatch):
    from src.worker import _FatalSpinLogWatcher

    monkeypatch.setenv("COLLECTOR_SELFHEAL_LOG_THRESHOLD", "2")
    monkeypatch.setenv("COLLECTOR_SELFHEAL_LOG_WINDOW", "60")
    monkeypatch.setenv("COLLECTOR_SELF_HEAL_RESTART", "true")

    persisted: list[tuple[str, str, int]] = []
    notified: list[tuple[str, str, int]] = []

    watcher = _FatalSpinLogWatcher(["telegram"])
    monkeypatch.setattr(watcher, "_persist_self_heal_event", lambda s, r, c: persisted.append((s, r, c)))
    monkeypatch.setattr(watcher, "_notify_self_heal", lambda s, r, c: notified.append((s, r, c)))
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))

    message = "Too many messages had to be ignored consecutively"
    watcher.emit(_record(message))
    with pytest.raises(SystemExit) as exc:
        watcher.emit(_record(message))

    assert exc.value.code == 42
    assert persisted and persisted[0][0] == "telegram"
    assert notified and notified[0][0] == "telegram"
    assert persisted[0][2] == 2
    assert "Too many messages" in persisted[0][1]


def test_fatal_spin_watcher_ignores_recoverable_wrong_session_id(monkeypatch):
    from src.worker import _FatalSpinLogWatcher

    monkeypatch.setenv("COLLECTOR_SELFHEAL_LOG_THRESHOLD", "2")
    monkeypatch.setenv("COLLECTOR_SELFHEAL_LOG_WINDOW", "60")
    monkeypatch.setenv("COLLECTOR_SELF_HEAL_RESTART", "true")

    persisted: list[tuple[str, str, int]] = []
    watcher = _FatalSpinLogWatcher(["telegram"])
    monkeypatch.setattr(watcher, "_persist_self_heal_event", lambda s, r, c: persisted.append((s, r, c)))
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))

    message = "Security error while unpacking a received message: Server replied with a wrong session ID"
    watcher.emit(_record(message))
    watcher.emit(_record(message))
    watcher.emit(_record(message))

    assert persisted == []


def test_fatal_spin_watcher_infers_telegram_from_telethon_logger(monkeypatch):
    from src.worker import _FatalSpinLogWatcher

    watcher = _FatalSpinLogWatcher()
    record = _record("Too many messages had to be ignored consecutively")

    assert watcher._infer_source(record, record.getMessage().lower()) == "telegram"
