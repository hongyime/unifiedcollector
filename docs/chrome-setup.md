# Chrome/CDP Setup for UnifiedCollector

## Overview

The `browser_cookie_vault` service snapshots cookies from a long-lived Chrome instance via CDP (Chrome DevTools Protocol). This document describes the required Chrome setup.

## Required Configuration

### Chrome Launch Command

```bash
google-chrome \
  --headless \
  --remote-debugging-port=9222 \
  --user-data-dir=/home/bryan/.config/chrome-collector \
  --no-sandbox
```

### Key Requirements

| Setting | Value | Why |
|---------|-------|-----|
| Profile | `/home/bryan/.config/chrome-collector` | Dedicated profile — do NOT use the main browser profile |
| CDP Port | `9222` | The vault connects to `http://localhost:9222` |
| Headless | Yes | No UI needed; runs in background |
| Extension | Auto-loaded from `~/Projects/unifiedcollector/extension/dist/` | Provides cookie access for Meta/X/TikTok domains |

### Extension Setup

The UnifiedCollector Bridge extension (v1.23.104) must be loaded:

**Manual (one-time):**
1. Open `chrome://extensions/` in the collector Chrome profile
2. Enable Developer Mode
3. Load unpacked: `/home/bryan/Projects/unifiedcollector/extension/dist/`

**Note:** Enterprise policy install via `/etc/opt/chrome/policies/managed/` does NOT work for local `file://` CRX files. Use manual load.

## What Happens If Chrome Is Not Running

The `browser_cookie_vault` will:
1. Attempt CDP connection for up to 30 seconds on startup
2. If unreachable, log: `Chrome not reachable — launch with: google-chrome --headless --remote-debugging-port=9222 --user-data-dir=/home/bryan/.config/chrome-collector`
3. **Exit with error** (fail-fast) — it will NOT retry 70 times

This is intentional (Fudan pattern): fail fast with a clear message rather than retrying indefinitely against a missing dependency.

## Verification

```bash
# Check CDP is responding
curl -s http://localhost:9222/json/version | head -c 100

# Check vault logs
docker logs unifiedcollector_browser_cookie_vault --tail 20
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Chrome not reachable" on startup | Chrome not running | Launch with the command above |
| "Chrome unreachable, attempt=N" in logs | Chrome died or port blocked | Restart Chrome, check port 9222 |
| Extension not loading | Wrong profile | Ensure `--user-data-dir` points to chrome-collector |

## Architecture Notes

- The vault runs in Docker and connects via `host.docker.internal:9222`
- Chrome ≥128 enforces DNS-rebinding protection: the vault sends `Host: localhost:9222` header
- Cookie snapshots are stored in `credentials/browser_cookies/latest.json`
- Restore is explicit only (`python -m src.tools.browser_cookie_vault restore`)
