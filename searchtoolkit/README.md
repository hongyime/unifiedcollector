# 🔍 Unified Search Toolkit v2

A powerful multi-engine search and file extraction tool with enhanced features for reliability, rate limit avoidance, and cost reduction.

## ✨ Key Features

### Core Modes
- **🔍 Search & Extract** — Multi-engine search → download images/PDFs → convert to JPG
- **🖼️ Bing Image Downloader** — Download images from Bing with format/quality filters
- **🎯 Dork Runner** — Run Google dorks across multiple engines, save URL lists

### Enhanced Features (NEW!)
- **🔒 Tor Proxy Support** — Automatic circuit rotation to avoid rate limits
- **💾 State Persistence** — SQLite + JSON backup for resume capability
- **⚡ Smart Rate Limiting** — Per-domain throttling with exponential backoff
- **🗄️ Search Result Caching** — TTL-based caching to reduce API costs

## 🚀 Quick Start

### Setup (first time only)
```bash
scripts\setup.bat
```

### Run the toolkit
```bash
scripts\start_toolkit.bat
```

Or use CLI flags for automation:
```bash
python main.py --mode 1 --query "yearbook 2024" --use-tor --resume
```

## 📋 Requirements

- Python 3.7+
- Internet connection
- Windows (for .bat files) or any OS with Python

### Dependencies
- `beautifulsoup4` - Web scraping
- `requests` - HTTP requests
- `Pillow` - Image processing
- `colorama` - Colored terminal output
- `lxml` - HTML/XML parsing
- `ddgs` - DuckDuckGo search API

## 🎛️ CLI Arguments

The toolkit now supports full CLI automation with the following flags:

### Core Options
- `--mode {1,2,3}` — Operation mode (1=Search&Extract, 2=BingImages, 3=DorkRunner)
- `--query QUERY` — Search query for mode 1/2 (comma-separated for multiple)
- `--dorks-file FILE` — Path to dorks file for mode 3 (one per line)

### Enhanced Features
- `--use-tor` — Enable Tor proxy for request rotation and circuit rotation on rate limits
- `--resume` — Resume from last checkpoint using state persistence
- `--state-dir PATH` — Custom state directory path (default: `./state`)
- `--cache-ttl HOURS` — Cache TTL in hours (default: 24)
- `--no-cache` — Disable search result caching
- `--rate-limit-delay SECS` — Base delay between requests to same domain (default: 2.0)
- `--output-dir PATH` — Output directory for downloads (overrides interactive prompt)

### Examples

```bash
# Basic search with Tor protection
python main.py --mode 1 --query "yearbook 2024" --use-tor

# Resume interrupted download
python main.py --mode 1 --query "nature wallpapers" --resume

# Run dorks with custom state directory
python main.py --mode 3 --dorks-file data/search.txt --state-dir ./mystate

# Disable caching for fresh results
python main.py --mode 2 --query "cats" --no-cache

# Interactive mode (no arguments)
python main.py
```

## 📁 State Persistence

The toolkit automatically tracks progress in a SQLite database with JSON backup:

- **Downloads**: Tracks completed, failed, and pending downloads
- **Query Progress**: Monitors search query completion status
- **API Usage**: Records API calls and costs for budget tracking

### Resume from Checkpoint
Use `--resume` to continue from where you left off:
```bash
python main.py --mode 1 --query "test" --resume
```

The toolkit will:
1. Load the previous state from `./state/state.db`
2. Identify pending/failed downloads
3. Skip already completed items
4. Continue from the checkpoint

## 🔒 Tor Proxy Integration

The toolkit includes automatic Tor Expert Bundle management:

1. **Auto-extraction**: If Tor archive is present, it's automatically extracted
2. **Daemon lifecycle**: Starts/stops Tor as needed
3. **Circuit rotation**: Automatically rotates on 429 rate limit errors
4. **SOCKS5 proxy**: Routes all requests through 127.0.0.1:9050

### Using Tor
```bash
python main.py --mode 1 --query "test" --use-tor
```

**Note**: You need the Tor Expert Bundle archive in the searchtoolkit directory:
- `tor-expert-bundle-*.tar.gz` or
- `tor-expert-bundle-*.zip`

## 🗄️ Search Result Caching

Reduce API costs by caching search results:

- **TTL-based**: Default 24-hour cache
- **Per-engine**: Separate caches for DuckDuckGo, Bing, Serper
- **Automatic cleanup**: Expired entries removed automatically
- **Atomic writes**: Prevents corruption

### Cache Management
```bash
# Use default 24h cache
python main.py --mode 1 --query "test"

# Custom TTL
python main.py --mode 1 --query "test" --cache-ttl 48

# Disable caching
python main.py --mode 1 --query "test" --no-cache
```

## ⚡ Smart Rate Limiting

Prevents 429 errors with intelligent throttling:

- **Per-domain delays**: Separate throttling for each domain
- **Exponential backoff**: Increases delay on repeated failures
- **Adaptive adjustment**: Learns from server responses
- **Tor integration**: Rotates circuit on rate limit errors

### Rate Limit Configuration
```bash
# Increase base delay (default: 2.0 seconds)
python main.py --mode 1 --query "test" --rate-limit-delay 5.0
```

## 📂 Project Structure

```
searchtoolkit/
├── searchtoolkit/        # Python package
│   ├── app.py            # Main CLI application
│   ├── tor_manager.py    # Tor daemon management
│   ├── state_manager.py  # SQLite + JSON state persistence
│   ├── rate_limiter.py   # Smart rate limiting
│   ├── search_cache.py   # Search result caching
│   └── download_path_manager.py
├── main.py               # Entry point wrapper
├── requirements.txt      # Python dependencies
├── scripts/              # Windows helper scripts
├── data/                 # Static input files
├── state/                # State directory (auto-created)
│   ├── state.db          # SQLite database
│   ├── state_backup.json # JSON backup
│   └── cache/            # Search result cache
└── downloads/            # Default download location
```

## 🛠️ Advanced Usage

### Mode 1: Search & Extract
Multi-engine search with file extraction:
```bash
python main.py --mode 1 --query "yearbook 2024,prom photos" --use-tor --resume
```

Features:
- Multi-engine search (DuckDuckGo, Bing, Serper)
- Page spidering for linked files
- Parallel downloads with quality gates
- Automatic PDF→JPG conversion
- Deduplication

### Mode 2: Bing Image Downloader
Download images from Bing with filters:
```bash
python main.py --mode 2 --query "nature wallpapers" --output-dir ./wallpapers
```

Features:
- Format filtering (JPG, PNG, etc.)
- Quality control (resolution thresholds)
- Organized subfolders per keyword
- Progress tracking

### Mode 3: Dork Runner
Run Google dorks across engines:
```bash
python main.py --mode 3 --dorks-file data/search.txt --state-dir ./dork_state
```

Features:
- Multi-engine fallback (DDG→Bing→Serper→Chrome)
- URL list export to text files
- Automatic result deduplication

## 🚨 Troubleshooting

### "Tor not found"
Download the Tor Expert Bundle and place the archive in the searchtoolkit directory:
- https://www.torproject.org/download/tor-bundles/

### "Rate limit errors"
Enable Tor proxy or increase rate limit delay:
```bash
python main.py --mode 1 --query "test" --use-tor --rate-limit-delay 5.0
```

### "Resume not working"
Ensure you're using the same `--state-dir` and query as the original run.

### "Cache not clearing"
Manually delete the `./state/cache/` directory or use `--no-cache`.

## 📄 License

This project is for educational and personal use. Please respect image copyrights and terms of service.

## 🤝 Contributing

Feel free to submit issues, feature requests, or improvements!

---

**Happy Searching! 🎉**
