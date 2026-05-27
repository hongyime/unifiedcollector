# API Setup Guide

## Serper API key

1. Go to `https://serper.dev`.
2. Create account or sign in.
3. Generate an API key from dashboard.
4. Copy `.env.example` to `.env`.
5. Set:

```env
SERPER_API_KEY=your_real_serper_api_key
```

## Windows current session

```bat
set SERPER_API_KEY=your_real_serper_api_key
```

## PowerShell current session

```powershell
$env:SERPER_API_KEY="your_real_serper_api_key"
```

## Persistent local setup

Keep key in local `.env` or shell profile. Never commit real key.

## Verify

```bat
.venv\Scripts\python.exe main.py --stats
```
