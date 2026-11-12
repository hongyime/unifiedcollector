from __future__ import annotations

from src.core import recon_seed
from src.scheduler.handlers import recon_seed as recon_seed_handler
from src.scheduler.handlers.recon_seed import ReconSeedHandler


async def test_scheduler_seeds_domain_and_username_sources(monkeypatch):
    calls = []

    async def fake_seed(conn, **kwargs):
        calls.append((conn, kwargs))
        return {"candidates": 5, "queued": 5, "skipped": 0}

    class Acquire:
        async def __aenter__(self):
            return "isolated-connection"

        async def __aexit__(self, *args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class Context:
        pool = Pool()

        @staticmethod
        def get_env_int(name, default, *, min_value):
            return default

    monkeypatch.setattr(recon_seed, "seed_recon_targets_from_collector", fake_seed)
    await ReconSeedHandler().run(Context())

    assert calls[0][0] == "isolated-connection"
    assert calls[0][1]["include_domains"] is True
    assert calls[0][1]["include_usernames"] is True
    assert calls[0][1]["include_urls"] is False
    assert calls[0][1]["approved_source_identities"] is None


async def test_scheduler_seed_producer_has_no_admission_gate(monkeypatch):
    calls = []

    async def seeded(_conn, **_kwargs):
        calls.append(True)
        return {"candidates": 0, "queued": 0, "skipped": 0}

    class Acquire:
        async def __aenter__(self): return object()
        async def __aexit__(self, *_args): return False

    class Context:
        class Pool:
            def acquire(self): return Acquire()
        pool = Pool()

        @staticmethod
        def get_env_int(_name, default, *, min_value): return default

    monkeypatch.setattr(recon_seed, "seed_recon_targets_from_collector", seeded)
    await ReconSeedHandler().run(Context())
    assert calls == [True]
