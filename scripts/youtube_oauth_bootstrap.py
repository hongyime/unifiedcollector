"""
YouTube OAuth bootstrap.

Run this ONCE on the host (NOT in Docker) to mint a refresh-token-bearing
pickle that the collector loads via YOUTUBE_OAUTH_PICKLE.

Usage:
    1. In Google Cloud Console, create OAuth 2.0 Client ID of type
       "Desktop app". Download the JSON. Save it as:
           credentials/youtube/client_secret.json
       (NOT in repo root — keep it under credentials/)
    2. python scripts/youtube_oauth_bootstrap.py
    3. A browser window opens. Sign in with the Google account whose
       YouTube data you want to access. Grant the requested scope.
    4. The script writes data/youtube_oauth.pickle. The collector reads
       this file (path is YOUTUBE_OAUTH_PICKLE in .env).

Required pip packages (already in requirements):
    google-auth, google-auth-oauthlib, google-api-python-client
"""
import os
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET = ROOT / "credentials" / "youtube" / "client_secret.json"
PICKLE_OUT = ROOT / "data" / "youtube_oauth.pickle"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    # Add upload/manage scopes here if you need write access.
]


def main() -> int:
    if not CLIENT_SECRET.exists():
        print(f"ERROR: client_secret.json not found at {CLIENT_SECRET}")
        print("Download it from Google Cloud Console > OAuth client (Desktop app) > Download JSON")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        print("ERROR: missing dependencies. Install with:")
        print("  pip install google-auth google-auth-oauthlib google-api-python-client")
        return 2

    creds = None
    if PICKLE_OUT.exists():
        with open(PICKLE_OUT, "rb") as f:
            creds = pickle.load(f)
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing token …")
            creds.refresh(Request())
        elif creds and creds.valid:
            print(f"Existing pickle at {PICKLE_OUT} is still valid. Re-using.")

    if not creds or not creds.valid:
        print(f"Starting OAuth flow with {CLIENT_SECRET}")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        # run_local_server opens browser, listens on localhost callback, returns creds
        creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    PICKLE_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(PICKLE_OUT, "wb") as f:
        pickle.dump(creds, f)
    print(f"OAuth pickle written: {PICKLE_OUT}")
    print(f"Token expires: {creds.expiry}")
    print(f"Has refresh_token: {bool(creds.refresh_token)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
