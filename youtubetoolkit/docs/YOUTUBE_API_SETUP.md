# YouTube API Setup Guide

This guide will help you get your YouTube API credentials to use with the toolkit.

## 📋 Prerequisites

- A Google account
- Access to Google Cloud Console

## 🔑 Step-by-Step Guide

### Step 1: Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click on the project dropdown at the top
3. Click "New Project"
4. Enter a project name (e.g., "YouTube Toolkit")
5. Click "Create"

### Step 2: Enable YouTube Data API v3

1. In your project, go to **APIs & Services** > **Library**
2. Search for "YouTube Data API v3"
3. Click on it
4. Click "Enable"

### Step 3: Create OAuth 2.0 Credentials

1. Go to **APIs & Services** > **Credentials**
2. Click "Create Credentials" > "OAuth client ID"
3. If prompted, configure the OAuth consent screen:
   - Choose "External" user type
   - Fill in required fields:
     - App name: "YouTube Toolkit"
     - User support email: your email
     - Developer contact: your email
   - Click "Save and Continue"
   - Skip scopes (click "Save and Continue")
   - Add yourself as a test user
   - Click "Save and Continue"

4. Back at Create OAuth client ID:
   - Application type: **Desktop app**
   - Name: "YouTube Toolkit Desktop"
   - Click "Create"

5. Download the credentials:
   - Click "Download JSON" on the popup
   - Or click the download icon next to your OAuth 2.0 Client ID

### Step 4: Install Credentials

1. Rename the downloaded file to `client_secret.json`
2. Move it to your toolkit's `data/` folder:
   ```
   youtubetoolkit/
   └── data/
       └── client_secret.json  ← Place here
   ```

### Step 5: First Authentication

1. Run any scraping command:
   ```bash
   python scrape_liked_videos_enhanced.py
   ```

2. A browser window will open asking you to:
   - Choose your Google account
   - Click "Continue" (it may show a warning - this is normal for test apps)
   - Click "Continue" again to grant permissions
   - You'll see "The authentication flow has completed"

3. Your credentials are now cached in `data/oauth_credentials.pickle`

## 🔒 Security Notes

### What Gets Stored

- `data/client_secret.json` - Your OAuth app credentials (not your password)
- `data/oauth_credentials.pickle` - Your access/refresh tokens

### Important

- ✅ These files are in `.gitignore` - they won't be committed to git
- ✅ The toolkit only requests **read-only** access to your YouTube data
- ✅ You can revoke access anytime at [Google Account Permissions](https://myaccount.google.com/permissions)
- ⚠️ Never share these files publicly
- ⚠️ Keep backups in a secure location

## 🔄 Re-Authentication

If you need to re-authenticate:

1. Run the logout script:
   ```bash
   python logout_account.py
   ```

2. Or manually delete:
   ```bash
   del data\oauth_credentials.pickle
   del data\subscriptions.json
   ```

3. Run any scraping command again to re-authenticate

## ❓ Troubleshooting

### "Access blocked: This app's request is invalid"

**Solution:** Make sure you:
- Selected "Desktop app" (not "Web application")
- Added yourself as a test user in OAuth consent screen

### "The OAuth client was not found"

**Solution:** 
- Download the credentials file again
- Make sure it's named exactly `client_secret.json`
- Place it in the `data/` folder

### "Invalid grant" or "Token has been expired or revoked"

**Solution:**
```bash
python logout_account.py
```
Then run your scraping command again to re-authenticate.

### "Quota exceeded"

**Solution:** YouTube Data API has daily quotas:
- Default quota: 10,000 units/day
- Each video list request: ~3 units
- Each playlist items request: ~1 unit

If you hit the limit:
- Wait until the next day (quota resets at midnight Pacific Time)
- Or request a quota increase in Google Cloud Console

## 📊 API Quota Management

### Understanding Quota Costs

| Operation | Cost (units) |
|-----------|--------------|
| List liked videos | ~3 per page (50 videos) |
| List subscriptions | ~1 per page (50 channels) |
| Get video details | ~1 per video |
| List playlist items | ~1 per page (50 videos) |

### Tips to Save Quota

1. **Use the `--days` flag** to limit scraping:
   ```bash
   python scrape_liked_videos_enhanced.py --days 7
   ```

2. **Use subscription cache** (automatically cached for 24 hours)

3. **Use target channels** instead of subscriptions:
   ```bash
   python scrape_targets.py
   ```
   This uses yt-dlp instead of the API (no quota cost!)

## 🎯 Alternative: No API Key Required

You can use the toolkit **without** YouTube API credentials:

### What Works Without API

- ✅ Scraping target channels (`scrape_targets.py`)
- ✅ Scraping custom playlists (`scrape_custom_playlist.py`)
- ✅ Downloading videos (`batch_downloader.py`)
- ✅ All download features

### What Requires API

- ❌ Scraping liked videos
- ❌ Scraping subscriptions

### Using Without API

1. Create `data/target_channels.txt`:
   ```
   # My favorite channels
   UCxxxxxxxxxxxxxxxxxxxxxx
   https://www.youtube.com/@channelname
   ```

2. Run:
   ```bash
   python scrape_targets.py
   python batch_downloader.py
   ```

## 📚 Additional Resources

- [YouTube Data API Documentation](https://developers.google.com/youtube/v3)
- [OAuth 2.0 for Desktop Apps](https://developers.google.com/identity/protocols/oauth2/native-app)
- [API Quota Calculator](https://developers.google.com/youtube/v3/determine_quota_cost)
- [Google Cloud Console](https://console.cloud.google.com/)

## 💡 Did You Have API Credentials Before?

If you previously had API credentials:

1. **Check your old project:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Look for your old project in the dropdown
   - Go to **APIs & Services** > **Credentials**
   - Download the OAuth 2.0 Client ID JSON

2. **Check your old toolkit folder:**
   - Look for `client_secret.json` or `token.json`
   - Copy to new `data/` folder

3. **Reuse existing credentials:**
   - If you find `client_secret.json`, just copy it to `data/`
   - If you find `oauth_credentials.pickle`, copy that too
   - The toolkit will use them automatically

---

**Need Help?** Open an issue on GitHub or check the troubleshooting section above.
