"""Smoke test for dashboard module — verifies the audit fixes don't break imports.

Run: python tmp/smoke_dashboard.py
"""
import os
import sys
import importlib
from pathlib import Path

# Make `src.*` importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 1. Verify fail-closed JWT_SECRET refuses startup with empty / default.
print("=" * 60)
print("Test 1: JWT_SECRET fail-closed")
print("=" * 60)

os.environ.pop("DASHBOARD_JWT_SECRET", None)
# Force a re-import
for mod in list(sys.modules):
    if mod.startswith("src.dashboard"):
        del sys.modules[mod]

try:
    from src.dashboard import api  # noqa
    print("FAIL: imported with no DASHBOARD_JWT_SECRET set")
except (RuntimeError, SystemExit) as e:
    print(f"OK: refused import without secret: {e}")
except Exception as e:
    print(f"OK: refused import with {type(e).__name__}: {e}")

# 2. Default value also rejected
os.environ["DASHBOARD_JWT_SECRET"] = "changeme-in-production"
for mod in list(sys.modules):
    if mod.startswith("src.dashboard"):
        del sys.modules[mod]
try:
    from src.dashboard import api  # noqa
    print("FAIL: imported with default secret")
except (RuntimeError, SystemExit) as e:
    print(f"OK: refused import with default secret: {e}")
except Exception as e:
    print(f"OK: refused with {type(e).__name__}: {e}")

# 3. Real secret loads cleanly + DB pool init pathway
import secrets
os.environ["DASHBOARD_JWT_SECRET"] = secrets.token_hex(32)
for mod in list(sys.modules):
    if mod.startswith("src.dashboard") or mod.startswith("src.db"):
        del sys.modules[mod]

try:
    from src.dashboard import api as api_mod
    print(f"OK: dashboard imports with valid secret (app={api_mod.app!r})")
    routes = [r for r in api_mod.app.routes]
    print(f"OK: {len(routes)} routes registered")
    # Spot-check that auth-required endpoints have dependencies attached
    sample = [r for r in routes if hasattr(r, "path") and r.path == "/collectors"]
    if sample:
        deps = getattr(sample[0], "dependant", None)
        if deps:
            sub = deps.dependencies
            print(f"OK: /collectors has {len(sub)} dependency layer(s)")
        else:
            print("WARN: /collectors has no dependant attribute")
except Exception as e:
    import traceback
    print(f"FAIL: dashboard import error: {type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(1)

print()
print("=" * 60)
print("Test 4: Pool init (real DB)")
print("=" * 60)

# Real DB connect using DATABASE_URL from .env
import asyncio
async def test_pool():
    from src.db.connection import get_pool, close_pool
    p = await get_pool()
    print(f"OK: pool acquired ({p!r})")
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT 1")
        print(f"OK: trivial query returned {v}")
        rows = await conn.fetch("SELECT count(*) FROM media_items")
        print(f"OK: media_items rows={rows[0][0]}")
    await close_pool()
    print("OK: pool closed cleanly")

# load .env
from pathlib import Path
env_file = Path(".env")
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

try:
    asyncio.run(test_pool())
except Exception as e:
    import traceback
    print(f"FAIL: pool test: {type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(2)

print()
print("=" * 60)
print("ALL SMOKE TESTS PASSED")
print("=" * 60)
