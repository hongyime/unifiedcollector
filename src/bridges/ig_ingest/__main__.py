"""Entry-point shim so ``python -m src.bridges.ig_ingest`` still starts the aiohttp server.

The compose command (`docker/docker-compose.yml`) invokes the package via
``python -m src.bridges.ig_ingest``. Python resolves that to this file
(``__main__.py``) which imports the fully-wired ``app`` from the package
and hands it to ``aiohttp.web.run_app``.

Keeping this file tiny keeps the import surface stable: any refactor that
moves route wiring into ``app.py`` only needs ``__init__.py`` to re-export
``app``; this shim never has to change.
"""
from aiohttp import web

from src.bridges.ig_ingest import PORT, app


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
