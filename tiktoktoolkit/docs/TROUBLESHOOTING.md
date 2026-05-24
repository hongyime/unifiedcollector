# TikTok Download Troubleshooting Guide

## Error: "could not extract rehydration data"

This error means TikTok's anti-bot protection is blocking gallery-dl. **This is expected and normal!**

Your toolkit has a 3-tier fallback system that handles this automatically:

```
gallery-dl (fast) → yt-dlp (medium) → Playwright browser (slow but reliable)
```

## Quick Fixes

### 1. Refresh Your Cookies (Most Common Fix)

Stale cookies are the #1 cause of download failures.

**Windows:**
```bash
scripts\ops\refresh_cookies.bat
```

**Manual:**
```bash
python main.py utils setup-cookies --browser chrome
```

**Steps:**
1. Make sure you're logged into TikTok in Chrome
2. Run the command above
3. Try downloading again

### 2. Test the Fallback Chain

```bash
scripts\ops\test_fallback.bat
```

Or manually:
```bash
python main.py download user --user tiktok --limit 1 --out downloads/test
```

Watch the logs to see:
- ❌ gallery-dl fails (expected)
- ⏳ yt-dlp attempts (may work)
- ✅ Playwright browser succeeds (should always work)

### 3. Enable Visual Browser Mode

If downloads are failing silently, enable visual mode to see what's happening.

Edit `configs/providers.yaml`:
```yaml
browser_fallback_headless: false  # Show browser window
browser_fallback_timeout: 180     # Increase timeout
```

### 4. Run Diagnostics

```bash
scripts\diagnostics\diagnose.bat
```

This checks:
- Python environment
- Installed packages
- Playwright browsers
- Cookie validity
- gallery-dl version

## Common Issues

### Issue: "Playwright not installed"

**Fix:**
```bash
.venv\Scripts\python -m pip install playwright
.venv\Scripts\python -m playwright install chromium
```

### Issue: "curl-cffi not installed"

**Fix:**
```bash
.venv\Scripts\python -m pip install curl-cffi
```

### Issue: "Cookies file empty or missing"

**Fix:**
```bash
python main.py utils setup-cookies --browser chrome
```

### Issue: Downloads timeout

**Increase timeouts in `configs/providers.yaml`:**
```yaml
timeout_seconds: 600  # 10 minutes for gallery-dl
ytdlp_fallback_timeout: 180  # 3 minutes for yt-dlp
browser_fallback_timeout: 180  # 3 minutes for browser
```

### Issue: Private accounts fail

**You need to:**
1. Follow the account on TikTok (in your browser)
2. Refresh cookies: `python main.py utils setup-cookies --browser chrome`
3. Try downloading again

## Understanding the Fallback Chain

### Method 1: gallery-dl (Primary)
- **Speed:** ⚡ Very fast
- **Success Rate:** ~30% (TikTok blocks it)
- **When it fails:** "could not extract rehydration data"

### Method 2: yt-dlp with curl-cffi (Secondary)
- **Speed:** ⚡ Fast
- **Success Rate:** ~60% (better TLS fingerprinting)
- **When it fails:** Similar errors to gallery-dl

### Method 3: Playwright Browser (Last Resort)
- **Speed:** 🐌 Slow (launches real browser)
- **Success Rate:** ~95% (bypasses anti-bot)
- **When it fails:** Captchas, network issues

## Logs

Check logs for detailed error messages:

```bash
logs/uttk.log
```

Look for:
- `Attempting yt-dlp fallback...` - yt-dlp is trying
- `Attempting browser automation fallback...` - Playwright is trying
- `Browser automation complete: X/Y successful` - Final result

## Still Not Working?

1. **Update everything:**
   ```bash
   .venv\Scripts\python -m pip install --upgrade gallery-dl yt-dlp playwright curl-cffi
   .venv\Scripts\python -m playwright install chromium
   ```

2. **Try a known working account:**
   ```bash
   python main.py download user --user tiktok --limit 1
   ```

3. **Check if the account exists:**
   - Visit `https://www.tiktok.com/@username` in your browser
   - Make sure it's not private (unless you follow it)
   - Make sure it has videos

4. **Enable debug logging:**
   ```bash
   python main.py --log-level DEBUG download user --user username --limit 1
   ```

## Expected Behavior

✅ **Normal:** gallery-dl fails with "could not extract rehydration data"  
✅ **Normal:** yt-dlp attempts fallback  
✅ **Normal:** Playwright browser opens and downloads successfully  
❌ **Problem:** All three methods fail

If all three methods fail, the issue is likely:
- Stale/invalid cookies
- Private account you don't follow
- Account doesn't exist
- Network/firewall blocking TikTok
