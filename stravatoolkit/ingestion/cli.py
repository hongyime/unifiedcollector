"""Interactive menu for Strava Toolkit."""
from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path

try:
    import questionary
    from rich.console import Console
    from rich.table import Table
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

_ROOT = Path(__file__).resolve().parent.parent
_PYTHON = sys.executable
console = None

if _RICH_AVAILABLE:
    console = Console()


def _print(msg: str) -> None:
    if console:
        console.print(msg)
    else:
        print(msg)


def _run(args: list[str]) -> None:
    """Run a subprocess command from toolkit root."""
    try:
        subprocess.run([_PYTHON] + args, cwd=str(_ROOT))
    except KeyboardInterrupt:
        pass


def _today() -> str:
    return date.today().isoformat()


def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


# ── Menu 1: Sync & Scrape ──────────────────────────────────────────────────


def _menu_sync() -> None:
    choices = [
        "Sync today",
        "Sync a specific date",
        "Refresh following roster + sync today",
        "Keep today fresh (watch mode)",
        "Seed explore (discover new athletes)",
        "Promote discovered athletes",
        "Backfill only (no feed sync)",
        "← Back",
    ]
    choice = questionary.select("Sync & Scrape", choices=choices).ask()
    if choice is None or choice == "← Back":
        return

    if choice == "Sync today":
        _run(["-m", "ingestion.main", "--date", _today(), "--sync-only"])
    elif choice == "Sync a specific date":
        target = questionary.text("Date (YYYY-MM-DD):", default=_yesterday()).ask()
        if target:
            _run(["-m", "ingestion.main", "--date", target, "--sync-only"])
    elif choice == "Refresh following roster + sync today":
        _run(["-m", "ingestion.main", "--date", _today(), "--sync-only", "--refresh-following-roster"])
    elif choice == "Keep today fresh (watch mode)":
        interval_str = questionary.text("Refresh interval in minutes:", default="15").ask() or "15"
        steps_str = questionary.text("Backfill steps per cycle:", default="2").ask() or "2"
        try:
            interval = max(1, int(interval_str))
            steps = max(1, int(steps_str))
        except ValueError:
            _print("[red]Invalid input — using defaults (15 min, 2 steps).[/red]")
            interval, steps = 15, 2
        _print(f"[cyan]Keeping today fresh every {interval} minute(s). Press Ctrl+C to stop.[/cyan]")
        cookies_file = str(_ROOT / "cookies.txt")
        while True:
            try:
                today = _today()
                _print(f"[dim]{time.strftime('%Y-%m-%d %H:%M:%S')} — syncing {today}...[/dim]")
                subprocess.run(
                    [_PYTHON, "-m", "ingestion.main",
                     "--date", today,
                     "--backfill-steps", str(steps),
                     "--auth-mode", "cookiestxt", "--auth-fallback", "auto",
                     "--cookies-file", cookies_file],
                    cwd=str(_ROOT),
                )
                _print(f"[dim]Waiting {interval} minute(s)...[/dim]")
                time.sleep(interval * 60)
            except KeyboardInterrupt:
                _print("[yellow]Watch mode stopped.[/yellow]")
                break
    elif choice == "Seed explore (discover new athletes)":
        _print("[yellow]Explore scraper — requires cookies.txt to be valid.[/yellow]")
        try:
            from ingestion.core.scrapers.explore_scraper import run_explore_scraper
            from ingestion.config import load_settings
            from ingestion.db import connect, init_db
            from ingestion.session import StravaSession
            import threading
            settings = load_settings()
            init_db(settings.db_path)
            session = StravaSession.from_sources(settings, auth_mode="cookiestxt",
                                                  auth_fallback="none",
                                                  cookies_file=str(_ROOT / "cookies.txt"))
            conn = connect(settings.db_path)
            shutdown = threading.Event()
            try:
                result = run_explore_scraper(session, conn, shutdown)
                _print(f"[green]Explore complete: {result.added} new athlete stubs added ({result.total_discovered} unique IDs found across {result.pages_fetched} pages).[/green]")
                if result.added > 0:
                    _print(f"[dim]Use 'Promote discovered athletes' to add them to your tracked roster.[/dim]")
            finally:
                conn.close()
        except ImportError:
            _print("[red]Explore scraper not available yet.[/red]")
        except Exception as exc:
            _print(f"[red]Error: {exc}[/red]")
    elif choice == "Promote discovered athletes":
        try:
            from ingestion.config import load_settings
            from ingestion.db import connect, init_db, list_explore_stubs, promote_explore_athletes
            settings = load_settings()
            init_db(settings.db_path)
            conn = connect(settings.db_path)
            try:
                stubs = list_explore_stubs(conn)
                if not stubs:
                    _print("[yellow]No discovered athletes waiting to be promoted.[/yellow]")
                else:
                    _print(f"[cyan]{len(stubs)} discovered athletes (not yet tracked):[/cyan]")
                    choices_list = [
                        f"athlete_{s['athlete_id']} (ID {s['athlete_id']}, via {s['first_seen_source']}, seen {s['last_seen_at'][:10]})"
                        for s in stubs
                    ]
                    selected = questionary.checkbox(
                        "Select athletes to promote to tracked roster:",
                        choices=choices_list,
                    ).ask()
                    if selected:
                        ids_to_promote = []
                        for sel in selected:
                            import re as _re
                            m = _re.search(r"ID (\d+)", sel)
                            if m:
                                ids_to_promote.append(int(m.group(1)))
                        promoted = promote_explore_athletes(conn, ids_to_promote)
                        _print(f"[green]Promoted {promoted} athlete(s) to tracked roster. They will be backfilled on next sync.[/green]")
                    else:
                        _print("[yellow]Nothing selected.[/yellow]")
            finally:
                conn.close()
        except Exception as exc:
            _print(f"[red]Error: {exc}[/red]")
    elif choice == "Backfill only (no feed sync)":
        steps = questionary.text("Max backfill steps (leave blank for unlimited):").ask()
        args = ["-m", "ingestion.main", "--backfill-only"]
        if steps and steps.strip():
            try:
                step_count = min(int(steps.strip()), 2 ** 30)
            except ValueError:
                step_count = 2 ** 30
        else:
            step_count = 2 ** 30
        args += ["--backfill-steps", str(step_count)]
        _run(args)


# ── Menu 2: Download Media ─────────────────────────────────────────────────


def _menu_media() -> None:
    choices = [
        "Download profile photos",
        "Download activity photos",
        "Download all (profiles + activities)",
        "← Back",
    ]
    choice = questionary.select("Download Media", choices=choices).ask()
    if choice is None or choice == "← Back":
        return

    mode_map = {
        "Download profile photos": "profiles",
        "Download activity photos": "activities",
        "Download all (profiles + activities)": "all",
    }
    mode = mode_map.get(choice)
    if mode:
        _run(["-m", "ingestion.photo_downloader", "--mode", mode])


# ── Menu 3: Analysis ──────────────────────────────────────────────────────


def _menu_analysis() -> None:
    choices = [
        "Route clusters",
        "Route overlaps",
        "Co-occurrence (proximity on same date)",
        "Athlete stats",
        "Run all analysis",
        "← Back",
    ]
    choice = questionary.select("Analysis (on-demand)", choices=choices).ask()
    if choice is None or choice == "← Back":
        return

    try:
        import ingestion.analysis as analysis
    except ImportError:
        _print("[red]Analysis module not available.[/red]")
        return

    from ingestion.config import load_settings
    from ingestion.db import connect, init_db
    import threading
    settings = load_settings()
    init_db(settings.db_path)
    conn = connect(settings.db_path)
    shutdown = threading.Event()
    try:
        if choice in ("Route clusters", "Run all analysis"):
            sport = questionary.text("Sport type (Run/Ride/etc):", default="Run").ask() or "Run"
            _print(f"[cyan]Computing route clusters for {sport}...[/cyan]")
            analysis.compute_route_clusters(conn, sport_type=sport, shutdown_event=shutdown)
            _print("[green]Route clusters computed.[/green]")

        if choice in ("Route overlaps", "Run all analysis"):
            _print("[cyan]Computing route overlaps...[/cyan]")
            analysis.compute_route_overlaps(conn, shutdown_event=shutdown)
            _print("[green]Route overlaps computed.[/green]")

        if choice in ("Co-occurrence (proximity on same date)", "Run all analysis"):
            _print("[cyan]Computing co-occurrence...[/cyan]")
            analysis.compute_co_occurrence(conn, shutdown_event=shutdown)
            _print("[green]Co-occurrence computed.[/green]")

        if choice in ("Athlete stats", "Run all analysis"):
            _print("[cyan]Computing athlete stats...[/cyan]")
            analysis.compute_athlete_stats(conn, shutdown_event=shutdown)
            _print("[green]Athlete stats computed.[/green]")
    except Exception as exc:
        _print(f"[red]Analysis error: {exc}[/red]")
    finally:
        conn.close()


# ── Menu 4: Frontend ──────────────────────────────────────────────────────


def _backend_pid() -> int | None:
    """Return PID of process on port 8000, or None if free."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$c = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -ErrorAction SilentlyContinue | "
         "Select-Object -First 1; if ($null -eq $c) { Write-Output 'NONE' } else { Write-Output $c.OwningProcess }"],
        capture_output=True, text=True,
    )
    val = result.stdout.strip()
    return None if val == "NONE" or not val.isdigit() else int(val)


def _menu_frontend() -> None:
    choices = [
        "Start local viewer (port 8000)",
        "Stop local viewer",
        "Viewer status",
        "Build frontend (npm run build)",
        "Open in browser",
        "← Back",
    ]
    choice = questionary.select("Viewer App", choices=choices).ask()
    if choice is None or choice == "← Back":
        return

    if choice == "Start local viewer (port 8000)":
        pid = _backend_pid()
        if pid is not None:
            _print(f"[yellow]Port 8000 already in use by PID {pid}. Stop the running viewer first.[/yellow]")
            return
        _print("[cyan]Starting FastAPI backend on http://127.0.0.1:8000 ...[/cyan]")
        _print("[dim]Close this window or press Ctrl+C to stop.[/dim]")
        subprocess.run(
            [_PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(_ROOT),
        )
    elif choice == "Stop local viewer":
        pid = _backend_pid()
        if pid is None:
            _print("[yellow]No viewer running on port 8000.[/yellow]")
            return
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
            capture_output=True,
        )
        _print(f"[green]Stopped viewer (PID {pid}).[/green]")
    elif choice == "Viewer status":
        pid = _backend_pid()
        if pid is None:
            _print("[yellow]No viewer running on port 8000.[/yellow]")
        else:
            _print(f"[green]Viewer running — PID {pid} — http://127.0.0.1:8000[/green]")
    elif choice == "Build frontend (npm run build)":
        frontend_dir = _ROOT / "frontend"
        if not frontend_dir.exists():
            _print("[red]frontend/ directory not found.[/red]")
            questionary.press_any_key_to_continue("Press any key to return to menu...").ask()
            return
        npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
        _print("[cyan]Running npm run build...[/cyan]")
        try:
            result = subprocess.run([npm_cmd, "run", "build"], cwd=str(frontend_dir))
            if result.returncode == 0:
                _print("[green]Build succeeded.[/green]")
            else:
                _print(f"[red]Build failed (exit code {result.returncode}).[/red]")
        except FileNotFoundError:
            _print("[red]npm not found — make sure Node.js is installed and in your PATH.[/red]")
        except KeyboardInterrupt:
            _print("[yellow]Build cancelled.[/yellow]")
        questionary.press_any_key_to_continue("Press any key to return to menu...").ask()
    elif choice == "Open in browser":
        webbrowser.open("http://127.0.0.1:8000")
        _print("[green]Opened http://127.0.0.1:8000 in your browser.[/green]")


# ── Menu 5: Database ──────────────────────────────────────────────────────


def _menu_database() -> None:
    choices = [
        "Show database stats",
        "Backup database",
        "Repair backfill state",
        "Reset backfill for one athlete",
        "Reset backfill for ALL athletes",
        "Check DB integrity",
        "← Back",
    ]
    choice = questionary.select("Database", choices=choices).ask()
    if choice is None or choice == "← Back":
        return

    from ingestion.config import load_settings
    from ingestion.db import connect, init_db, get_status_summary
    import shutil

    settings = load_settings()
    init_db(settings.db_path)

    if choice == "Show database stats":
        conn = connect(settings.db_path)
        try:
            summary = get_status_summary(conn)
            if console:
                t = Table(title="Database Stats")
                t.add_column("Metric", style="cyan")
                t.add_column("Value", style="green")
                for k, v in summary.items():
                    t.add_row(str(k), str(v))
                console.print(t)
            else:
                for k, v in summary.items():
                    print(f"{k}: {v}")
        finally:
            conn.close()

    elif choice == "Backup database":
        db_path = Path(settings.db_path)
        backup = db_path.with_suffix(".db.bak")
        shutil.copy2(db_path, backup)
        _print(f"[green]Backed up to {backup}[/green]")

    elif choice == "Repair backfill state":
        _run(["-m", "ingestion.main", "--check-health"])

    elif choice == "Reset backfill for one athlete":
        athlete_id_str = questionary.text("Athlete ID to reset:").ask()
        if not athlete_id_str or not athlete_id_str.strip().isdigit():
            _print("[red]Invalid athlete ID.[/red]")
            return
        _run(["-m", "ingestion.main", "--reset-backfill", "--athlete-id", athlete_id_str.strip()])

    elif choice == "Reset backfill for ALL athletes":
        confirm = questionary.confirm(
            "Reset backfill state to pending for ALL tracked athletes? This cannot be undone.",
            default=False,
        ).ask()
        if confirm:
            _run(["-m", "ingestion.main", "--reset-backfill"])
        else:
            _print("[yellow]Cancelled.[/yellow]")

    elif choice == "Check DB integrity":
        _run(["-m", "ingestion.main", "--check-db-integrity"])


# ── Menu 6: Authentication ────────────────────────────────────────────────


def _menu_auth() -> None:
    choices = [
        "Setup cookies (Playwright interactive login)",
        "Validate current session",
        "View session status",
        "← Back",
    ]
    choice = questionary.select("Authentication", choices=choices).ask()
    if choice is None or choice == "← Back":
        return

    from ingestion.config import load_settings
    from ingestion.session import StravaSession

    settings = load_settings()
    cookies_file = str(_ROOT / "cookies.txt")

    if choice == "Setup cookies (Playwright interactive login)":
        _print("[cyan]Opening browser for Strava login...[/cyan]")
        try:
            session = StravaSession.from_sources(
                settings,
                auth_mode="playwright",
                cookies_file=cookies_file,
            )
            session.persist_cookie()
            _print("[green]Cookies saved successfully.[/green]")
        except Exception as exc:
            _print(f"[red]Login failed: {exc}[/red]")

    elif choice == "Validate current session":
        try:
            session = StravaSession.from_sources(
                settings,
                auth_mode="cookiestxt",
                cookies_file=cookies_file,
            )
            athlete = session.validate()
            _print(f"[green]Session valid. Athlete: {athlete.get('firstname', '')} {athlete.get('lastname', '')} (ID: {athlete.get('id', '?')})[/green]")
        except Exception as exc:
            _print(f"[red]Session invalid: {exc}[/red]")

    elif choice == "View session status":
        from ingestion.db import connect, init_db
        init_db(settings.db_path)
        conn = connect(settings.db_path)
        try:
            row = conn.execute(
                "SELECT auth_mode, captured_at FROM session_state WHERE is_active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                _print(f"Auth mode: {row['auth_mode']}, Captured: {row['captured_at']}")
            else:
                _print("[yellow]No active session in DB.[/yellow]")
        finally:
            conn.close()


# ── Main menu ─────────────────────────────────────────────────────────────


def main() -> None:
    if not _RICH_AVAILABLE:
        print("WARNING: questionary/rich not installed. Run: pip install questionary rich")
        print("Falling back to ingestion.main CLI.")
        from ingestion.main import main as ingestion_main
        ingestion_main()
        return

    _print("[bold cyan]Strava Toolkit[/bold cyan]")

    while True:
        choice = questionary.select(
            "What would you like to do?",
            choices=[
                "1. Sync & Scrape",
                "2. Download Media",
                "3. Analysis (on-demand)",
                "4. Viewer App",
                "5. Database",
                "6. Authentication",
                "0. Exit",
            ],
        ).ask()

        if choice is None or choice.startswith("0."):
            _print("[dim]Goodbye.[/dim]")
            break
        elif choice.startswith("1."):
            _menu_sync()
        elif choice.startswith("2."):
            _menu_media()
        elif choice.startswith("3."):
            _menu_analysis()
        elif choice.startswith("4."):
            _menu_frontend()
        elif choice.startswith("5."):
            _menu_database()
        elif choice.startswith("6."):
            _menu_auth()
