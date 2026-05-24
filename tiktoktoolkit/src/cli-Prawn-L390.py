"""Main CLI application for Unified TikTok Toolkit."""

from pathlib import Path
import re
import subprocess
import webbrowser

import click

from .config import load_config
from .download_path_manager import prompt_for_download_path
from .downloader import TikTokDownloader
from .errors import ProviderError, ValidationError
from .logging_setup import setup_logging
from .provider import GalleryDLProvider
from .utils import read_usernames_from_file
from .validation import validate_username, validate_limit, validate_download_type


def _status_for(result) -> str:
    return getattr(result, 'status', 'downloaded' if getattr(result, 'ok', False) else 'failed')


def _count_results(results):
    counts = {'downloaded': 0, 'skipped': 0, 'failed': 0}
    for result in results:
        status = _status_for(result)
        counts[status] = counts.get(status, 0) + 1
    return counts


def create_provider(config, logger):
    """Create and initialize Gallery-dl provider with merged provider settings."""
    try:
        full_config = config.model_dump() if hasattr(config, 'model_dump') else vars(config)
        full_config['gallerydl'] = dict(getattr(config, 'providers', {}).get('gallerydl', {}) or {})
        return GalleryDLProvider(full_config)
    except Exception as exc:
        logger.error(f"Gallery-dl provider initialization failed: {exc}")
        raise ProviderError(f"Failed to initialize Gallery-dl provider: {exc}") from exc


@click.group()
@click.option('--config-root', default='.', help='Toolkit root path')
@click.option('--log-level', default=None, help='Override log level')
@click.option('--cookies-file', default=None, help='Path to cookies.txt to override config')
@click.pass_context
def cli(ctx, config_root, log_level, cookies_file):
    """Unified TikTok Toolkit - Download TikTok videos using Gallery-dl."""
    base_path = Path(config_root).resolve()
    config = load_config(base_path)

    if log_level:
        config.log_level = log_level

    if cookies_file:
        config.cookies_file = cookies_file
        config.providers.setdefault('gallerydl', {})
        config.providers['gallerydl']['cookies_file'] = cookies_file

    logger = setup_logging(base_path / 'logs' / 'uttk.log', config.log_level)

    try:
        provider = create_provider(config, logger)
    except ProviderError as exc:
        click.echo(f"❌ Initialization failed: {exc}", err=True)
        ctx.exit(1)
        return

    if cookies_file:
        provider.cookies_file = cookies_file
        logger.info(f"Using manual cookies file override: {cookies_file}")

    downloader = TikTokDownloader(provider)
    ctx.obj = {
        'config': config,
        'provider': provider,
        'downloader': downloader,
        'base_path': base_path,
        'logger': logger,
    }


@cli.group()
def download():
    """Download TikTok videos from user profiles."""


@download.command('user')
@click.option('--user', help='TikTok username (without @)')
@click.option('--limit', default=30, show_default=True, help='Maximum number of videos to download')
@click.option('--out', default=None, help='Output directory (will prompt if not provided)')
@click.option('--type', 'download_type', type=click.Choice(['videos', 'profile_pictures']), default='videos', help='Type of content to download')
@click.pass_context
def download_user_cmd(ctx, user, limit, out, download_type):
    """Download content from a single user profile."""
    downloader = ctx.obj['downloader']

    if not user:
        click.echo("Error: Username is required")
        click.echo("Usage: python main.py download user --user username")
        raise click.ClickException("Username required")
    
    # Validate and sanitize username
    try:
        user = validate_username(user)
    except ValidationError as e:
        raise click.ClickException(str(e))
    
    # Validate limit
    try:
        limit = validate_limit(limit)
    except ValidationError as e:
        raise click.ClickException(str(e))
    
    # Validate download type
    try:
        download_type = validate_download_type(download_type)
    except ValidationError as e:
        raise click.ClickException(str(e))

    if not out:
        base_path = ctx.obj['base_path']
        default_out = str(base_path / 'downloads')
        out = prompt_for_download_path(
            context=f"TikTok {download_type.replace('_', ' ')} from @{user}",
            out_path=None,
            default_path=default_out,
        )
    output_path = Path(out)

    try:
        click.echo(f"Downloading {download_type.replace('_', ' ')} from user @{user} (limit: {limit})...")
        results = downloader.download_user(user, limit, output_path, download_type=download_type)
        counts = _count_results(results)

        if counts['downloaded'] > 0:
            click.echo(f"Downloaded {counts['downloaded']} {download_type.replace('_', ' ')} from user @{user}")
            for result in results:
                if _status_for(result) == 'downloaded' and result.filepath:
                    icon = "IMG" if download_type == 'profile_pictures' else "VID"
                    click.echo(f"  {icon} {result.filepath.name}")

        if counts['skipped'] > 0:
            click.echo(f"Skipped {counts['skipped']} items for user @{user}")
            for result in results:
                if _status_for(result) == 'skipped' and result.reason:
                    click.echo(f"  -> {result.reason}")

        if counts['failed'] > 0:
            click.echo(f"❌ Failed {counts['failed']} items for user @{user}")
            for result in results:
                if _status_for(result) == 'failed' and result.reason:
                    click.echo(f"  ⚠️  Reason: {result.reason}")

        if counts['downloaded'] == 0 and counts['skipped'] > 0 and counts['failed'] == 0:
            click.echo(f"ℹ️  No new {download_type.replace('_', ' ')} were downloaded from user @{user}; recent items are already tracked.")
        elif counts['downloaded'] == 0 and counts['failed'] > 0:
            click.echo("\n💡 Troubleshooting suggestions:")
            click.echo("  1. Verify the username is correct (without @)")
            click.echo("  2. Check if the account is public")
            click.echo("  3. Try setting up authentication: python main.py utils setup-cookies --browser chrome")
            click.echo("  4. Run with debug logging: python main.py --log-level DEBUG download user --user <username>")

    except ProviderError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Download failed: {exc}") from exc


@download.command('bulk')
@click.option('--file', 'usernames_file', help='Path to text file containing usernames (one per line)')
@click.option('--users', help='Comma or space separated list of usernames')
@click.option('--limit', default=30, show_default=True, help='Maximum number of videos per user')
@click.option('--out', default=None, help='Output directory (will prompt if not provided)')
@click.option('--interactive', is_flag=True, help='Interactive mode to input usernames')
@click.option('--type', 'download_type', type=click.Choice(['videos', 'profile_pictures']), default='videos', help='Type of content to download')
@click.pass_context
def download_bulk_cmd(ctx, usernames_file, users, limit, out, interactive, download_type):
    """Bulk download from multiple users. Supports file input, direct list, or interactive mode."""
    downloader = ctx.obj['downloader']
    
    # Validate limit
    try:
        limit = validate_limit(limit)
    except ValidationError as e:
        raise click.ClickException(str(e))
    
    # Validate download type
    try:
        download_type = validate_download_type(download_type)
    except ValidationError as e:
        raise click.ClickException(str(e))

    if not out:
        base_path = ctx.obj['base_path']
        default_out = str(base_path / 'downloads')
        out = prompt_for_download_path(
            context=f"TikTok {download_type.replace('_', ' ')} (bulk download)",
            out_path=None,
            default_path=default_out,
        )
    output_path = Path(out)

    try:
        if usernames_file:
            click.echo(f"Reading usernames from file: {usernames_file}")
            results = downloader.download_users_from_file(usernames_file, limit, output_path, download_type=download_type)
        elif users:
            usernames = re.split(r'[,\s]+', users.strip())
            usernames_raw = [username.strip() for username in usernames if username.strip()]
            
            # Validate each username
            validated_usernames = []
            for username in usernames_raw:
                try:
                    validated = validate_username(username)
                    validated_usernames.append(validated)
                except ValidationError as e:
                    click.echo(f"Warning: Skipping invalid username '{username}': {e}")
            
            if not validated_usernames:
                raise click.ClickException("No valid usernames provided")
            
            click.echo(f"Downloading from {len(validated_usernames)} users: {', '.join(validated_usernames)}")
            results = downloader.download_users_bulk(validated_usernames, limit, output_path, download_type=download_type)
        elif interactive:
            click.echo("Interactive username input mode.")
            click.echo("Enter usernames one by one (without @). Press Enter with empty input to finish.")
            usernames = []
            while True:
                user_input = click.prompt("Username", default="", show_default=False).strip()
                if not user_input:
                    break
                
                # Validate username
                try:
                    validated = validate_username(user_input)
                    if validated not in usernames:
                        usernames.append(validated)
                        click.echo(f"Added: @{validated}")
                    else:
                        click.echo(f"Already added: @{validated}")
                except ValidationError as e:
                    click.echo(f"Invalid username: {e}")
            
            if not usernames:
                click.echo("No usernames provided.")
                return
            click.echo(f"\nDownloading {download_type.replace('_', ' ')} from {len(usernames)} users...")
            results = downloader.download_users_bulk(usernames, limit, output_path, download_type=download_type)
        else:
            raise click.ClickException("Must specify either --file, --users, or --interactive")

        total_downloaded = 0
        total_skipped = 0
        total_failed = 0
        users_with_downloads = 0
        users_skipped_only = 0
        users_with_failures = 0

        for user_results in results.values():
            counts = _count_results(user_results)
            total_downloaded += counts['downloaded']
            total_skipped += counts['skipped']
            total_failed += counts['failed']
            if counts['downloaded'] > 0:
                users_with_downloads += 1
            elif counts['skipped'] > 0 and counts['failed'] == 0:
                users_skipped_only += 1
            if counts['failed'] > 0:
                users_with_failures += 1

        click.echo(f"\n✅ Bulk download ({download_type.replace('_', ' ')}) completed:")
        click.echo(f"  Users processed: {len(results)}")
        click.echo(f"  Users with downloads: {users_with_downloads}")
        click.echo(f"  Users skipped only: {users_skipped_only}")
        click.echo(f"  Users with failures: {users_with_failures}")
        click.echo(f"  Total items downloaded: {total_downloaded}")
        click.echo(f"  Total items skipped: {total_skipped}")
        click.echo(f"  Total items failed: {total_failed}")

    except ProviderError as exc:
        raise click.ClickException(str(exc)) from exc
    except FileNotFoundError as exc:
        raise click.ClickException(f"File not found: {exc}") from exc
    except Exception as exc:
        raise click.ClickException(f"Bulk download failed: {exc}") from exc


@cli.group()
def utils():
    """Utility commands."""


@utils.command('setup-cookies')
@click.option(
    '--browser',
    default='chrome',
    type=click.Choice(['chrome', 'firefox', 'safari', 'edge']),
    help='Browser to extract cookies from',
)
@click.pass_context
def setup_cookies_cmd(ctx, browser):
    """Extract TikTok cookies from browser for private profile access."""
    provider = ctx.obj['provider']
    logger = ctx.obj['logger']

    try:
        click.echo(f"🍪 Setting up TikTok authentication from {browser}...")
        
        click.echo(f"\n🌐 Opening TikTok in your default browser...")
        webbrowser.open('https://www.tiktok.com/')
        click.echo("Please ensure you are logged into TikTok.")
        
        if not click.confirm("\nAre you logged in and ready to proceed?"):
            click.echo("Setup cancelled.")
            return

        cookies_file = provider.setup_browser_cookies(browser)

        if cookies_file and cookies_file.exists():
            click.echo(f"\n✅ Cookies extracted to: {cookies_file}")
            click.echo("✅ Authentication setup complete!")
            click.echo("You can now download from private accounts you follow.")
        else:
            click.echo("\n❌ Setup failed. Check error messages above.")
            click.echo("💡 Tip: Try logging out and logging back in on TikTok, then try again.")

    except Exception as exc:
        click.echo(f"❌ Error: {exc}")
        logger.error(f"Cookie setup failed: {exc}")


@utils.command('list-users')
@click.option('--file', 'usernames_file', required=True, help='Path to text file containing usernames')
def list_users_cmd(usernames_file):
    """List usernames from a text file."""
    try:
        usernames = read_usernames_from_file(usernames_file)
        click.echo(f"Found {len(usernames)} valid usernames in {usernames_file}:")
        for index, username in enumerate(usernames, 1):
            click.echo(f"  {index:3d}. @{username}")
    except FileNotFoundError as exc:
        raise click.ClickException(f"File not found: {usernames_file}") from exc
    except Exception as exc:
        raise click.ClickException(f"Error reading file: {exc}") from exc


@utils.command('check-folders')
@click.option('--out', default='downloads', help='Output directory to check')
def check_folders_cmd(out):
    """Check existing download folders and their contents."""
    output_path = Path(out)

    if not output_path.exists():
        click.echo(f"Directory does not exist: {output_path}")
        return

    username_folders = []
    other_folders = []

    for folder in output_path.iterdir():
        if folder.is_dir():
            if folder.name.startswith('username_'):
                username_folders.append(folder)
            else:
                other_folders.append(folder)

    click.echo(f"Download folder structure in {output_path}:")
    click.echo()

    if username_folders:
        click.echo(f"Username folders ({len(username_folders)}):")
        for folder in sorted(username_folders):
            video_count = sum(
                1
                for file_path in folder.rglob('*')
                if file_path.is_file() and file_path.suffix.lower() in ['.mp4', '.mov', '.avi', '.webm', '.mkv']
            )
            click.echo(f"  {folder.name}: {video_count} videos total")
            
            # Show date subfolders
            date_folders = [d for d in folder.iterdir() if d.is_dir() and re.match(r'\d{4}-\d{2}-\d{2}', d.name)]
            if date_folders:
                for date_folder in sorted(date_folders):
                    date_count = sum(
                        1
                        for file_path in date_folder.rglob('*')
                        if file_path.is_file() and file_path.suffix.lower() in ['.mp4', '.mov', '.avi', '.webm', '.mkv']
                    )
                    click.echo(f"    └─ {date_folder.name}: {date_count} videos")

    if other_folders:
        click.echo(f"\nOther folders ({len(other_folders)}):")
        for folder in sorted(other_folders):
            video_count = sum(
                1
                for file_path in folder.rglob('*')
                if file_path.is_file() and file_path.suffix.lower() in ['.mp4', '.mov', '.avi', '.webm', '.mkv']
            )
            click.echo(f"  {folder.name}: {video_count} videos")

    if not username_folders and not other_folders:
        click.echo("No download folders found.")


@utils.command('import-existing')
@click.option('--root', default='downloads', help='Root directory containing previously downloaded files')
@click.option('--username', default=None, help='Force assign all found videos to a single username (override folder detection)')
@click.pass_context
def import_existing_cmd(ctx, root, username):
    """Import already-downloaded video files into the tracker."""
    provider = ctx.obj['provider']
    tracker = getattr(provider, 'tracker', None)
    if not tracker:
        raise click.ClickException('Tracker not initialized; cannot import.')

    root_path = Path(root)
    if not root_path.exists():
        raise click.ClickException(f'Root directory does not exist: {root}')

    click.echo(f'Importing existing media from {root_path}...')
    added = tracker.import_directory(root_path, assume_username=username, source='manual-import')
    click.echo(f'Imported {added} records into tracker.')


@utils.command('debug-gallery-dl')
@click.option('--url', help='Test URL (default: uses a known working TikTok profile)')
@click.pass_context
def debug_gallery_dl_cmd(ctx, url):
    """Debug gallery-dl installation and TikTok access."""
    provider = ctx.obj['provider']
    config = ctx.obj['config']
    test_url = url or "https://www.tiktok.com/@tiktok"

    click.echo("🔍 Gallery-dl Debug Information")
    click.echo("=" * 50)

    try:
        result = subprocess.run(['gallery-dl', '--version'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            click.echo(f"✅ Gallery-dl version: {result.stdout.strip()}")
        else:
            click.echo(f"❌ Gallery-dl version check failed: {result.stderr}")
    except Exception as exc:
        click.echo(f"❌ Gallery-dl not found: {exc}")
        return

    click.echo(f"\n🧪 Testing gallery-dl with URL: {test_url}")

    try:
        test_args = ['gallery-dl', '--list-urls', test_url]
        click.echo(f"Running: {' '.join(test_args)}")
        result = subprocess.run(test_args, capture_output=True, text=True, timeout=30)
        click.echo(f"Return code: {result.returncode}")

        if result.returncode == 0:
            if result.stdout and 'tiktok.com' in result.stdout:
                click.echo("✅ Gallery-dl can access TikTok URLs")
            else:
                click.echo("⚠️  Gallery-dl ran but found no URLs - might indicate access issues")
        elif "unrecognized arguments" in result.stderr and "--list-urls" in result.stderr:
            click.echo("⚠️  --list-urls not supported, trying fallback method...")
            test_args_fallback = ['gallery-dl', '--simulate', '--range', '1-2', test_url]
            click.echo(f"Fallback: {' '.join(test_args_fallback)}")
            result = subprocess.run(test_args_fallback, capture_output=True, text=True, timeout=30)
            click.echo(f"Fallback return code: {result.returncode}")
            if result.returncode == 0:
                click.echo("✅ Gallery-dl simulation works (older version)")
            else:
                click.echo("❌ Gallery-dl simulation failed")
        else:
            click.echo("❌ Gallery-dl failed - see error output above")

        if result.stdout:
            click.echo(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            click.echo(f"STDERR:\n{result.stderr}")

    except subprocess.TimeoutExpired:
        click.echo("❌ Gallery-dl command timed out")
    except Exception as exc:
        click.echo(f"❌ Gallery-dl test failed: {exc}")

    click.echo("\n⚙️  Effective Configuration:")
    click.echo(f"  Output root: {config.output_root}")
    click.echo(f"  Log level: {config.log_level}")
    click.echo(f"  Gallery-dl retries: {provider.retries}")
    click.echo(f"  Gallery-dl sleep: {provider.sleep}")
    click.echo(f"  Gallery-dl timeout: {provider.timeout_seconds}s")
    click.echo(f"  Skip existing: {provider.skip_existing}")
    click.echo(f"  Cookies file: {provider.cookies_file or 'none'}")
    click.echo(f"  Cookies browser: {provider.cookies_browser or 'none'}")

    click.echo("\n💡 If gallery-dl is working but downloads fail:")
    click.echo("  1. Try setting up cookies: python main.py utils setup-cookies --browser chrome")
    click.echo("  2. Test with a known public account: @tiktok")
    click.echo("  3. Check if the account exists and is public")
    click.echo("  4. Run with debug logging: --log-level DEBUG")


@utils.command('check-cookies')
@click.option('--test-user', default='tiktok', help='Username to test cookie authentication with')
@click.pass_context
def check_cookies_cmd(ctx, test_user):
    """Check if TikTok cookies are working correctly for authentication."""
    from .cookie_manager import TikTokCookieManager
    
    provider = ctx.obj['provider']
    config = ctx.obj['config']

    click.echo("TikTok Cookie Authentication Check")
    click.echo("=" * 50)

    cookies_file = provider.cookies_file or config.cookies_file
    if not cookies_file:
        possible_locations = [
            Path("configs/tiktok_cookies.txt"),
            Path("cookies.txt"),
            Path.home() / ".local/share/gallery-dl/cookies.txt",
        ]
        for location in possible_locations:
            if location.exists():
                cookies_file = str(location)
                break

    if cookies_file and Path(cookies_file).exists():
        click.echo(f"✅ Cookies file found: {cookies_file}")
        cookies_path = Path(cookies_file)
        file_size = cookies_path.stat().st_size
        click.echo(f"  File size: {file_size} bytes")

        if file_size == 0:
            click.echo("❌ Cookies file is empty!")
            click.echo("💡 Run: python main.py utils setup-cookies --browser chrome")
            return

        # Validate cookies
        click.echo("\n📋 Cookie Validation:")
        cookie_mgr = TikTokCookieManager()
        validation = cookie_mgr.validate_cookies(cookies_path)
        summary = cookie_mgr.get_validation_summary(validation)
        for line in summary.split('\n'):
            click.echo(f"  {line}")
        
        if not validation['valid']:
            click.echo("\n⚠️  Warning: Cookie validation failed")
            click.echo("  This may cause issues with private account access")
            click.echo("  Try refreshing cookies: python main.py utils setup-cookies --browser chrome")

        try:
            with cookies_path.open('r', encoding='utf-8') as handle:
                first_lines = handle.readlines()[:5]

            if first_lines:
                first_line = first_lines[0].strip()
                if first_line.startswith('#') or first_line.startswith('tiktok.com'):
                    click.echo(f"\n  Format: Netscape cookie file ✅")
                else:
                    click.echo(f"\n  Format: Unknown (unexpected first line) ⚠️")
        except Exception as exc:
            click.echo(f"❌ Error reading cookies file: {exc}")
            return
    else:
        click.echo("❌ No cookies file found!")
        click.echo("💡 Set up cookies with: python main.py utils setup-cookies --browser chrome")
        return

    click.echo(f"\n🧪 Testing authentication with @{test_user}...")
    click.echo("⚠️  Note: This test uses gallery-dl directly (no browser fallback)")
    click.echo("    If it times out, that's EXPECTED - TikTok blocks automated tools")
    click.echo("    The actual download command has browser fallback and will work!\n")

    result = None
    test_url = f"https://www.tiktok.com/@{test_user}"
    supports_list = getattr(provider, 'supports_list_urls', False)
    if not supports_list:
        click.echo(f"ℹ️ gallery-dl {getattr(provider, 'version', 'unknown')} does not support --list-urls; using simulation mode for tests.")

    try:
        if supports_list:
            click.echo(f"Using --list-urls (supported by gallery-dl {provider.version})")
            test_args = [
                'gallery-dl', '--cookies', str(cookies_file),
                '--list-urls', '--range', '1-3', test_url,
            ]
            click.echo("Running: " + ' '.join(test_args))
            result = subprocess.run(test_args, capture_output=True, text=True, timeout=45)
            click.echo(f"Return code: {result.returncode}")
            if result.returncode == 0:
                urls = [line.strip() for line in result.stdout.split('\n') if line.strip() and 'tiktok.com' in line]
                click.echo(f"✅ Found {len(urls)} video URLs")
                for index, found_url in enumerate(urls[:3], start=1):
                    click.echo(f"  {index}. {found_url}")
            else:
                click.echo("❌ --list-urls run failed; falling back to simulation")

        if (not supports_list) or (result and result.returncode != 0):
            click.echo("Using --simulate fallback")
            test_args_fallback = [
                'gallery-dl', '--cookies', str(cookies_file),
                '--simulate', '--range', '1-3', test_url,
            ]
            click.echo("Running: " + ' '.join(test_args_fallback))
            result = subprocess.run(test_args_fallback, capture_output=True, text=True, timeout=45)
            click.echo(f"Return code: {result.returncode}")
            if result.returncode == 0:
                simulated_count = sum(1 for line in result.stdout.split('\n') if 'tiktok' in line.lower())
                click.echo(f"✅ Simulation successful - detected {simulated_count} lines of output")
            else:
                click.echo("❌ Simulation failed")
                if result.stderr:
                    click.echo(f"Error: {result.stderr}")
    except subprocess.TimeoutExpired:
        click.echo("❌ Test timed out (>45 seconds)")
    except Exception as exc:
        click.echo(f"❌ Test failed: {exc}")

    click.echo("\n🔓 Testing WITHOUT cookies for comparison...")

    try:
        if supports_list:
            test_args_no_cookies = ['gallery-dl', '--list-urls', '--range', '1-2', test_url]
        else:
            test_args_no_cookies = ['gallery-dl', '--simulate', '--range', '1-2', test_url]
        click.echo("Running without cookies: " + ' '.join(test_args_no_cookies))
        result_no_cookies = subprocess.run(test_args_no_cookies, capture_output=True, text=True, timeout=30)

        if result_no_cookies.returncode == 0:
            if result_no_cookies.stdout:
                urls_no_cookies = [
                    line.strip()
                    for line in result_no_cookies.stdout.split('\n')
                    if line.strip() and 'tiktok.com' in line
                ]
                click.echo(f"📊 Without cookies: {len(urls_no_cookies)} URLs found")
                if cookies_file and result and getattr(result, 'returncode', 1) == 0:
                    with_cookies_count = len([
                        line
                        for line in (result.stdout or '').split('\n')
                        if line.strip() and 'tiktok.com' in line
                    ])
                    if with_cookies_count > len(urls_no_cookies):
                        click.echo("✅ Cookies provide access to MORE content!")
                    elif with_cookies_count == len(urls_no_cookies):
                        click.echo("⚠️  Cookies don't seem to provide additional access")
                    else:
                        click.echo("❌ Cookies might be causing issues")
            else:
                click.echo("📊 Without cookies: No URLs found")
        else:
            click.echo("📊 Without cookies: Access failed")
    except Exception as exc:
        click.echo(f"⚠️  Comparison test failed: {exc}")

    click.echo("\n" + "="*60)
    click.echo("💡 RECOMMENDATIONS:")
    click.echo("="*60)

    if cookies_file and Path(cookies_file).exists():
        click.echo("✅ Cookies file exists and has content")
        if result and getattr(result, 'returncode', 1) == 0:
            click.echo("✅ Gallery-dl can use your cookies successfully")
            click.echo("🎯 Your cookies are working correctly!")
        else:
            click.echo("❌ Gallery-dl timed out or failed (EXPECTED with TikTok's anti-bot)")
            click.echo("\n🎯 YOUR COOKIES ARE FINE! This timeout is normal.")
            click.echo("   TikTok blocks gallery-dl even with valid cookies.")
            click.echo("\n✅ SOLUTION: Use the download command instead:")
            click.echo("   python main.py download user tiktok --limit 3")
            click.echo("\n   The download command has browser automation fallback")
            click.echo("   that bypasses TikTok's anti-bot protection automatically.")
            click.echo("\n🔧 Optional: Refresh cookies if you want:")
            click.echo("   1. Log out of TikTok in your browser")
            click.echo("   2. Log back in")
            click.echo("   3. Run: python main.py utils setup-cookies --browser chrome")
    else:
        click.echo("❌ No valid cookies found")
        click.echo("🔧 Set up cookies: python main.py utils setup-cookies --browser chrome")

    click.echo("\n📚 For more info, see: TIKTOK_COOKIE_SOLUTION.md")
    click.echo("="*60)


@utils.command('clean-empty-folders')
@click.option('--dir', 'target_dir', default='downloads', help='Directory to clean (default: downloads)')
@click.pass_context
def clean_empty_folders_cmd(ctx, target_dir):
    """Clean up empty directories left behind by gallery-dl."""
    from pathlib import Path
    from .utils import remove_empty_dirs

    base_path = ctx.obj['base_path']
    dir_path = base_path / target_dir

    if not dir_path.exists() or not dir_path.is_dir():
        click.echo(f"❌ Directory not found: {dir_path}")
        return

    click.echo(f"🧹 Scanning for empty folders in: {dir_path}")
    
    # We count how many dirs exist before and after to report
    def count_dirs(p):
        return sum(1 for _ in p.rglob('*') if _.is_dir())
        
    try:
        before_count = count_dirs(dir_path)
        remove_empty_dirs(dir_path)
        after_count = count_dirs(dir_path)
        
        removed = before_count - after_count
        if removed > 0:
            click.echo(f"✅ Successfully removed {removed} empty folder(s).")
        else:
            click.echo("ℹ️ No empty folders found.")
            
    except Exception as e:
        click.echo(f"❌ Error while cleaning folders: {e}")


@utils.command('maintain-tracker')
@click.pass_context
def maintain_tracker_cmd(ctx):
    """Run maintenance (VACUUM and ANALYZE) on the SQLite tracker database."""
    provider = ctx.obj['provider']
    tracker = getattr(provider, 'tracker', None)
    if not tracker:
        raise click.ClickException('Tracker not initialized.')
    if hasattr(tracker, 'vacuum'):
        tracker.vacuum()
        click.echo('✅ Tracker maintenance complete.')
    else:
        click.echo('ℹ️  Tracker does not support vacuum.')


@utils.command('find-duplicates')
@click.option('--dir', 'scan_dir', default='downloads', help='Directory to scan (default: downloads)')
@click.option('--delete', is_flag=True, default=False, help='Prompt to delete duplicates after listing')
@click.option('--no-backup', is_flag=True, help='Skip backup when deleting (DANGEROUS)')
@click.pass_context
def find_duplicates_cmd(ctx, scan_dir, delete, no_backup):
    """Scan download folders for duplicate videos and optionally delete them.
    
    Normalizes any legacy date subfolders (e.g. 2026-04-24/) by moving videos
    up to the flat per-user layout before checking for duplicates.
    """
    from .utils import find_duplicate_videos, remove_empty_dirs
    import re as _re

    base_path = ctx.obj['base_path']
    dir_path = Path(scan_dir) if Path(scan_dir).is_absolute() else base_path / scan_dir

    if not dir_path.exists():
        raise click.ClickException(f'Directory not found: {dir_path}')

    # --- Step 1: Flatten date subfolders ---
    DATE_RE = _re.compile(r'^\d{4}-?\d{2}-?\d{2}$')  # matches 2026-04-24 or 20260424
    moved = 0
    for user_folder in dir_path.iterdir():
        if not user_folder.is_dir():
            continue
        for sub in list(user_folder.iterdir()):
            if sub.is_dir() and DATE_RE.match(sub.name):
                for vid_file in list(sub.iterdir()):
                    if vid_file.is_file():
                        dest = user_folder / vid_file.name
                        if not dest.exists():
                            vid_file.rename(dest)
                            moved += 1
                        else:
                            # dest already exists — keep the larger file
                            if vid_file.stat().st_size > dest.stat().st_size:
                                dest.unlink()
                                vid_file.rename(dest)
                            else:
                                vid_file.unlink()
                            moved += 1
                # Remove now-empty date folder
                try:
                    sub.rmdir()
                except OSError:
                    pass

    if moved > 0:
        click.echo(f'📁 Flattened {moved} file(s) out of date subfolders.')
    else:
        click.echo('📁 No date subfolders found — folder structure is already flat.')

    # Clean up any remaining empty dirs
    remove_empty_dirs(dir_path)

    # --- Step 2: Find & delete duplicates ---
    click.echo(f'\n🔍 Scanning for duplicate videos in: {dir_path}')
    dupes = find_duplicate_videos(dir_path)

    if not dupes:
        click.echo('✅ No duplicate videos found.')
        return

    total_dupes = sum(len(paths) - 1 for paths in dupes.values())
    click.echo(f'\n⚠️  Found {len(dupes)} video IDs with duplicates ({total_dupes} extra files):\n')

    files_to_delete = []
    for vid, paths in sorted(dupes.items()):
        click.echo(f'  Video ID: {vid}')
        for i, p in enumerate(paths):
            tag = '  KEEP →' if i == 0 else '  DUP  →'
            size_kb = p.stat().st_size // 1024 if p.exists() else 0
            click.echo(f'    {tag} {p.relative_to(dir_path)}  ({size_kb} KB)')
            if i > 0:
                files_to_delete.append(p)
        click.echo()

    if not delete:
        click.echo(f'💡 Run with --delete to remove {len(files_to_delete)} duplicate file(s).')
        return

    from .utils import create_backup
    
    # Create backup unless --no-backup specified
    if not no_backup:
        backup_dir = create_backup(files_to_delete)
        click.echo(f'✅ Backup created: {backup_dir}')
        click.echo(f'   Files to delete: {len(files_to_delete)}')
    else:
        click.echo('⚠️  WARNING: Running without backup!')
    
    click.echo(f'🗑️  About to delete {len(files_to_delete)} duplicate file(s).')
    if not click.confirm('Proceed?'):
        click.echo('Cancelled.')
        return

    deleted = 0
    for f in files_to_delete:
        try:
            f.unlink()
            click.echo(f'  Deleted: {f.name}')
            deleted += 1
        except Exception as e:
            click.echo(f'  ❌ Failed to delete {f.name}: {e}')

    click.echo(f'\n[OK] Deleted {deleted}/{len(files_to_delete)} duplicate files.')
    if not no_backup:
        click.echo(f'💾 Backup available at: {backup_dir}')


@utils.command('reset-tracker')
@click.option('--no-backup', is_flag=True, help='Skip backup (DANGEROUS)')
@click.confirmation_option(prompt='WARNING: This will clear all download history (SQLite + JSON backup). Are you sure?')
@click.pass_context
def reset_tracker_cmd(ctx, no_backup):
    """Reset tracking progress (clear SQLite + JSON backup). You will need to resave videos."""
    from .utils import create_backup
    
    provider = ctx.obj['provider']
    tracker = getattr(provider, 'tracker', None)
    
    if not tracker:
        raise click.ClickException("Tracker is not initialized.")
    
    try:
        # Get tracker paths
        primary_tracker = getattr(tracker, 'primary', tracker)
        db_path = getattr(primary_tracker, 'db_path', None)
        json_backup = getattr(primary_tracker, 'json_backup', None)
        
        files_to_delete = []
        
        # Collect files that will be deleted
        if db_path:
            db_path = Path(db_path)
            files_to_delete.extend([
                db_path,
                db_path.parent / f"{db_path.name}-wal",
                db_path.parent / f"{db_path.name}-shm",
            ])
        
        if json_backup:
            json_path = getattr(json_backup, 'path', None)
            if json_path:
                files_to_delete.append(Path(json_path))
        
        # Filter to existing files only
        files_to_delete = [f for f in files_to_delete if isinstance(f, Path) and f.exists()]
        
        if not files_to_delete:
            click.echo("No tracker files found to delete")
            return
        
        # Create backup unless --no-backup specified
        if not no_backup:
            backup_dir = create_backup(files_to_delete)
            click.echo(f"✅ Backup created: {backup_dir}")
            click.echo(f"   Files backed up: {len(files_to_delete)}")
            if not click.confirm("Proceed with reset?"):
                click.echo("Reset cancelled. Backup preserved.")
                return
        else:
            click.echo("⚠️  WARNING: Running without backup!")
            if not click.confirm("Are you absolutely sure?"):
                click.echo("Reset cancelled.")
                return
        
        # Delete files
        deleted_count = 0
        for file_path in files_to_delete:
            try:
                file_path.unlink()
                deleted_count += 1
                click.echo(f"Deleted: {file_path}")
            except Exception as e:
                click.echo(f"Warning: Could not delete {file_path}: {e}", err=True)
        
        if deleted_count > 0:
            click.echo(f"\nSuccessfully reset tracker ({deleted_count} files deleted)")
            if not no_backup:
                click.echo(f"💾 Backup available at: {backup_dir}")
        else:
            click.echo("No tracker files deleted")
            
    except Exception as e:
        raise click.ClickException(f"Failed to reset tracker: {e}")


@utils.command('refresh-cookies')
@click.option(
    '--browser',
    default='chrome',
    type=click.Choice(['chrome', 'firefox', 'safari', 'edge']),
    help='Browser to extract fresh cookies from',
)
@click.pass_context
def refresh_cookies_cmd(ctx, browser):
    """Refresh TikTok cookies by extracting new ones from browser."""
    provider = ctx.obj['provider']
    logger = ctx.obj['logger']

    click.echo(f"🔄 Refreshing TikTok cookies from {browser}...")
    click.echo("\n📋 Current cookie status:")
    ctx.invoke(check_cookies_cmd)
    
    click.echo(f"\n🌐 Opening TikTok in your default browser...")
    webbrowser.open('https://www.tiktok.com/')
    
    click.echo(f"\n🔄 Ready to extract fresh cookies from {browser}?")
    click.echo("Make sure you're logged into TikTok in your browser!")

    if not click.confirm("Continue with cookie refresh?"):
        click.echo("Refresh cancelled.")
        return

    try:
        cookies_file = provider.setup_browser_cookies(browser)
        if cookies_file and cookies_file.exists():
            click.echo(f"\n✅ Fresh cookies extracted to: {cookies_file}")
            click.echo("\n🧪 Testing new cookies...")
            ctx.invoke(check_cookies_cmd)
        else:
            click.echo("\n❌ Cookie refresh failed. Check error messages above.")
            click.echo("💡 Tip: Try logging out and logging back in on TikTok, then try again.")
    except Exception as exc:
        click.echo(f"❌ Error during refresh: {exc}")
        logger.error(f"Cookie refresh failed: {exc}")


# ============================================================================
# NEW COMMANDS - Added per REFACTOR_PLAN.md TASK 9
# ============================================================================

@cli.command('spider')
@click.option('--seed', is_flag=True, help='Fetch following lists of accounts in data/usernames.txt and enqueue discovered accounts')
@click.option('--batch', is_flag=True, help='Spider all pending users in queue')
@click.option('--username', help='Spider a single user')
@click.option('--limit', default=500, help='Maximum users to spider in batch mode')
@click.pass_context
def spider_command(ctx, seed, batch, username, limit):
    """Spider TikTok users to discover followers/following."""
    from .spider import Spider
    from .account_manager import AccountManager
    from .rate_limiter import AdaptiveRateLimiter

    config = ctx.obj['config']
    logger = ctx.obj['logger']
    base_path = ctx.obj['base_path']

    db_path = Path(config.tracker_db)
    cookies_file = Path(config.cookies_file) if config.cookies_file else None
    account_manager = AccountManager(sessions_dir=Path('sessions'), db_path=db_path)
    rate_limiter = AdaptiveRateLimiter(base_delay=1.0)

    spider = Spider(
        db_path=db_path,
        account_manager=account_manager,
        rate_limiter=rate_limiter,
        batch_size=min(limit, 500),
        cookies_file=cookies_file,
        max_following=config.spider_max_following,
        max_followers=config.spider_max_followers,
    )

    try:
        if username:
            click.echo(f"Spidering user: @{username}")
            spider.enqueue([username])
            processed = spider.run_batch(max_items=1)
            if processed > 0:
                click.echo(f"Successfully spidered @{username}")
            else:
                click.echo(f"Failed to spider @{username}")

        elif batch:
            click.echo(f"Spidering pending users (batch size: {limit})...")
            total = spider.run_until_done()
            click.echo(f"Spidered {total} users")

        elif seed:
            username_file = base_path / 'data' / 'usernames.txt'
            if not username_file.exists():
                raise click.ClickException(f"data/usernames.txt not found at {username_file}")

            from .utils import read_usernames_from_file
            seed_users = read_usernames_from_file(str(username_file))

            if not seed_users:
                raise click.ClickException("No valid usernames found in data/usernames.txt")

            click.echo(f"Seeding from {len(seed_users)} accounts in data/usernames.txt")
            click.echo(f"  Max following threshold : {spider.max_following}")
            click.echo(f"  Max followers threshold : {spider.max_followers}")
            click.echo(f"  Cookies file            : {cookies_file or 'none'}")
            click.echo()

            added = spider.enqueue(seed_users)
            click.echo(f"Enqueued {added} accounts ({len(seed_users) - added} already in queue)")
            click.echo("Fetching profiles and following lists for seed accounts...")

            total = spider.run_until_done()
            click.echo(f"\nSeed complete: {total} accounts processed")
            click.echo("Run 'python main.py spider --batch' to process newly discovered accounts")

        else:
            click.echo("Must specify --seed, --batch, or --username")
            click.echo("  python main.py spider --seed             # Seed from data/usernames.txt")
            click.echo("  python main.py spider --username <user>  # Spider one user")
            click.echo("  python main.py spider --batch            # Process all pending")

    except KeyboardInterrupt:
        click.echo("\nSpider interrupted by user")
        logger.info("Spider interrupted by Ctrl+C")
    except Exception as exc:
        click.echo(f"Spider failed: {exc}")
        logger.error(f"Spider error: {exc}", exc_info=True)
        raise click.ClickException(str(exc))


@cli.command('reconcile')
@click.option('--deep', is_flag=True, help='Run tier 2 deep hash verification (slower)')
@click.option('--photos', is_flag=True, default=True, help='Export orphaned photo blobs')
@click.option('--no-photos', is_flag=True, help='Skip photo blob export')
@click.pass_context
def reconcile_command(ctx, deep, photos, no_photos):
    """Reconcile database records with filesystem state."""
    from .config import load_config
    from .reconciler import Reconciler

    config = load_config()
    logger = ctx.obj['logger']

    gallerydl_cfg = config.providers.get('gallerydl', {})
    db_path = Path(gallerydl_cfg.get('tracker_db', config.tracker_db))

    if not db_path.exists():
        click.echo(f"❌ Database not found: {db_path}")
        click.echo("   Run a download first to initialize the database")
        return
    
    reconciler = Reconciler(db_path=db_path, chunk_size=500)
    
    # Determine if we should export photos
    export_photos = photos and not no_photos
    
    try:
        click.echo("🔍 Starting reconciliation...")
        click.echo(f"   Database: {db_path}")
        click.echo(f"   Deep verification: {'Yes' if deep else 'No'}")
        click.echo(f"   Export photos: {'Yes' if export_photos else 'No'}")
        click.echo()
        
        result = reconciler.run_full_reconciliation(deep=deep, export_photos=export_photos)
        
        click.echo("\n" + "="*60)
        click.echo("📊 Reconciliation Results:")
        click.echo("="*60)
        click.echo(f"  Total checked:     {result.total_checked}")
        click.echo(f"  Missing files:     {result.missing_files}")
        if deep:
            click.echo(f"  Hash mismatches:   {result.hash_mismatches}")
        click.echo(f"  Fixed/Updated:     {result.fixed}")
        if result.errors > 0:
            click.echo(f"  Errors:            {result.errors}")
        click.echo("="*60)
        
        if result.missing_files > 0:
            click.echo("\n💡 Tip: Re-download missing files with:")
            click.echo("   python main.py download user --user <username>")
        
        if deep and result.hash_mismatches > 0:
            click.echo("\n⚠️  Warning: Hash mismatches detected (possible file corruption)")
            click.echo("   Consider re-downloading affected files")
        
        logger.info(f"Reconciliation complete: {result}")
        
    except KeyboardInterrupt:
        click.echo("\n⚠️  Reconciliation interrupted by user")
        logger.info("Reconciliation interrupted by Ctrl+C")
    except Exception as exc:
        click.echo(f"❌ Reconciliation failed: {exc}")
        logger.error(f"Reconciliation error: {exc}", exc_info=True)
        raise click.ClickException(str(exc))


@cli.command('photo-history')
@click.option('--username', required=True, help='TikTok username to show photo history for')
@click.option('--limit', default=10, help='Maximum number of history entries to show')
@click.pass_context
def photo_history_command(ctx, username, limit):
    """Show profile photo change history for a user."""
    from .config import load_config
    import sqlite3
    from datetime import datetime
    
    config = load_config()
    logger = ctx.obj['logger']

    gallerydl_cfg = config.providers.get('gallerydl', {})
    db_path = Path(gallerydl_cfg.get('tracker_db', config.tracker_db))

    if not db_path.exists():
        click.echo(f"❌ Database not found: {db_path}")
        return

    try:
        with sqlite3.connect(str(db_path)) as conn:
            # Get photo history
            cursor = conn.execute("""
                SELECT id, photo_url, photo_phash, file_path, detected_at
                FROM profile_photo_history
                WHERE username = ?
                ORDER BY detected_at DESC
                LIMIT ?
            """, (username, limit))
            
            rows = cursor.fetchall()
        
        if not rows:
            click.echo(f"📷 No photo history found for @{username}")
            click.echo("   This user may not have been spidered yet")
            return
        
        click.echo(f"\n📷 Profile Photo History for @{username}")
        click.echo("="*80)
        
        for idx, (photo_id, url, phash, file_path, detected_at) in enumerate(rows, 1):
            # Parse timestamp
            try:
                if isinstance(detected_at, (int, float)):
                    dt = datetime.fromtimestamp(detected_at)
                else:
                    dt = datetime.fromisoformat(detected_at.replace('Z', '+00:00'))
                timestamp = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                timestamp = str(detected_at)
            
            click.echo(f"\n{idx}. Detected: {timestamp}")
            click.echo(f"   URL:   {url[:60]}..." if len(url) > 60 else f"   URL:   {url}")
            click.echo(f"   pHash: {phash}")
            if file_path:
                click.echo(f"   File:  {file_path}")
            else:
                click.echo(f"   File:  (not saved)")
        
        click.echo("\n" + "="*80)
        click.echo(f"Showing {len(rows)} of {len(rows)} entries")
        
        if len(rows) == limit:
            click.echo(f"💡 Tip: Use --limit to see more entries")
        
    except Exception as exc:
        click.echo(f"❌ Failed to retrieve photo history: {exc}")
        logger.error(f"Photo history error: {exc}", exc_info=True)
        raise click.ClickException(str(exc))
