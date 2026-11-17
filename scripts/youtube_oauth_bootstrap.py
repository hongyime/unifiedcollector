"""
YouTube OAuth bootstrap.

Run this ONCE on the host (NOT in Docker) to mint a refresh-token-bearing
JSON credential file that the collector loads alongside YOUTUBE_OAUTH_PICKLE.

Usage:
    1. In Google Cloud Console, create OAuth 2.0 Client ID of type
       "Desktop app". Download the JSON. Save it as:
           credentials/youtube/client_secret.json
       (NOT in repo root — keep it under credentials/)
    2. python scripts/youtube_oauth_bootstrap.py
    3. A browser window opens. Sign in with the Google account whose
       YouTube data you want to access. Grant the requested scope.
    4. The script writes data/youtube_oauth.json. The collector reads this
       adjacent JSON file; it only uses YOUTUBE_OAUTH_PICKLE for one-time,
       restricted migration of pre-existing legacy credentials.

Required pip packages (already in requirements):
    google-auth, google-auth-oauthlib, google-api-python-client
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET = ROOT / "credentials" / "youtube" / "client_secret.json"
JSON_OUT = ROOT / "data" / "youtube_oauth.json"
LEGACY_PICKLE = ROOT / "data" / "youtube_oauth.pickle"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    # Add upload/manage scopes here if you need write access.
]


def _write_private_json(path: Path, content: str) -> None:
    """Atomically write credentials with owner-only permissions."""
    json.loads(content)  # fail before replacing a valid credential file
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def main() -> int:
    if not CLIENT_SECRET.exists():
        print(f"ERROR: client_secret.json not found at {CLIENT_SECRET}")
        print("Download it from Google Cloud Console > OAuth client (Desktop app) > Download JSON")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("ERROR: missing dependencies. Install with:")
        print("  pip install google-auth google-auth-oauthlib google-api-python-client")
        return 2

    creds = None
    if JSON_OUT.exists():
        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(JSON_OUT.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            print(f"WARNING: could not load OAuth JSON at {JSON_OUT}: {exc}")
            creds = None
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing JSON token …")
            creds.refresh(Request())
        elif creds and creds.valid:
            print(f"Existing JSON credential at {JSON_OUT} is still valid. Re-using.")
    elif LEGACY_PICKLE.exists():
        print(
            "Legacy OAuth pickle found but will not be loaded by bootstrap. "
            "The collector can migrate it with its restricted reader."
        )

    if not creds or not creds.valid:
        print(f"Starting OAuth flow with {CLIENT_SECRET}")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        # run_local_server opens browser, listens on localhost callback, returns creds
        creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    _write_private_json(JSON_OUT, creds.to_json())
    print(f"OAuth JSON written: {JSON_OUT}")
    print(f"Token expires: {creds.expiry}")
    print(f"Has refresh_token: {bool(creds.refresh_token)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
