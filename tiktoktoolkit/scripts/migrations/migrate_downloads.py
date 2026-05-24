#!/usr/bin/env python3
r"""
Migration script: Reorganize old TikTok downloads to new flat structure.

Old structure:
  Z:\media\tiktok\username_<username>\<files>

New structure:
  downloads\username_<username>\<username>_YYYY-MM-DD_videoid.ext

Usage:
  python migrate_downloads.py --source Z:\media\tiktok --dest downloads
  python migrate_downloads.py --source Z:\media\tiktok --dest downloads --dry-run
  python migrate_downloads.py --source Z:\media\tiktok --dest downloads --copy (don't move)
"""

import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple
import click


def extract_username_from_folder(folder_name: str) -> Optional[str]:
    """Extract username from folder name like 'username_alice'."""
    if folder_name.startswith('username_'):
        return folder_name[len('username_'):]
    return None


def extract_video_id(filename: str) -> Optional[str]:
    """Extract video ID from filename.
    
    Handles formats like:
    - 7538090007253306631.mp4 (pure ID)
    - 7398933192687750407 some caption [7322116319288625154].mp3 (caption with bracket ID)
    - 7260749033810038022_01 caption [hash].jpg (with suffix)
    - username.jpg or username_YYYYMMDD_HHMMSS.jpg (profile pics - skip)
    """
    # Profile pic files (no numeric ID): skip them
    if filename.endswith('.jpg') or filename.endswith('.jpeg'):
        # Check if it starts with a video ID
        if not re.match(r'^(\d{6,})', filename):
            return None
    
    # First, try to extract from brackets at the end [ID]
    bracket_match = re.search(r'\[([a-f0-9]{32}|[\d]+)\]', filename)
    if bracket_match:
        bracket_id = bracket_match.group(1)
        # Prefer numeric IDs over hashes
        if bracket_id.isdigit():
            return bracket_id
    
    # Otherwise, extract leading numeric ID
    id_match = re.match(r'^(\d{6,})', filename)
    if id_match:
        return id_match.group(1)
    
    return None


def get_posting_date(filepath: Path) -> str:
    """Get posting date from file modification time, formatted as YYYY-MM-DD."""
    try:
        mtime = filepath.stat().st_mtime
        return datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
    except Exception:
        return datetime.now().strftime('%Y-%m-%d')


def safe_filename(name: str, max_len: int = 120) -> str:
    """Sanitize filename."""
    sanitized = re.sub(r'[^A-Za-z0-9_.-]+', '_', name)[:max_len]
    return sanitized


def build_new_filename(video_id: str, ext: str) -> str:
    """Build new filename: videoid.ext (flat layout)"""
    return f"{video_id}.{ext}"


def migrate_file(src_file: Path, dest_dir: Path, username: str, dry_run: bool = False, 
                 use_copy: bool = False) -> Tuple[bool, str]:
    """Migrate a single file. Returns (success, message)."""
    
    # Extract video ID (returns None for profile pics without video IDs)
    video_id = extract_video_id(src_file.name)
    if not video_id:
        # Handle profile pics that don't have extractable video IDs
        if src_file.suffix.lower() in ['.jpg', '.jpeg', '.png', '.webp']:
            # Use download timestamp format for profile pics
            download_time = datetime.fromtimestamp(src_file.stat().st_mtime).strftime('%Y%m%d_%H%M%S')
            new_name = f"{safe_filename(username)}_profile_{download_time}{src_file.suffix.lower()}"
            dest_file = dest_dir / new_name
            
            if dry_run:
                return True, f"[DRY] {src_file.name} -> {new_name} [profile]"
            
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if use_copy:
                    import shutil
                    shutil.copy2(src_file, dest_file)
                    return True, f"COPY: {src_file.name} -> {new_name} [profile]"
                else:
                    src_file.replace(dest_file)
                    return True, f"MOVE: {src_file.name} -> {new_name} [profile]"
            except Exception as e:
                return False, f"ERROR: {src_file.name} - {e}"
        
        return False, f"SKIP: No video ID found in: {src_file.name}"
    
    # Get file extension
    ext = src_file.suffix.lstrip('.')
    
    # Build new filename (flat layout: video_id.ext)
    new_name = build_new_filename(video_id, ext)
    dest_file = dest_dir / new_name
    
    # Handle duplicates
    counter = 1
    base_name = dest_file.stem
    while dest_file.exists():
        dest_file = dest_dir / f"{base_name}_{counter}.{ext}"
        counter += 1
    
    if dry_run:
        return True, f"[DRY] {src_file.name} -> {dest_file.name}"
    
    try:
        # Ensure destination directory exists
        dest_dir.mkdir(parents=True, exist_ok=True)
        
        if use_copy:
            import shutil
            shutil.copy2(src_file, dest_file)
            return True, f"COPY: {src_file.name} -> {dest_file.name}"
        else:
            src_file.replace(dest_file)
            return True, f"MOVE: {src_file.name} -> {dest_file.name}"
    except Exception as e:
        return False, f"ERROR: {src_file.name} - {e}"


@click.command()
@click.option('--source', required=True, type=click.Path(exists=True), help='Source directory (e.g., Z:\\media\\tiktok)')
@click.option('--dest', default='downloads', type=click.Path(), help='Destination directory (default: downloads)')
@click.option('--dry-run', is_flag=True, help='Show what would be done without actually doing it')
@click.option('--copy', is_flag=True, help='Copy files instead of moving (preserve originals)')
@click.option('--stats', is_flag=True, help='Show statistics only, no migration')
def migrate_downloads(source: str, dest: str, dry_run: bool, copy: bool, stats: bool):
    """Migrate old TikTok downloads to the new flat structure."""
    
    source_path = Path(source).resolve()
    dest_path = Path(dest).resolve()
    
    click.echo(f"\n{'=' * 70}")
    click.echo(f"TikTok Downloads Migration Tool")
    click.echo(f"{'=' * 70}")
    click.echo(f"Source: {source_path}")
    click.echo(f"Dest:   {dest_path}")
    click.echo(f"Mode:   {'DRY-RUN' if dry_run else ('COPY' if copy else 'MOVE')}")
    click.echo()
    
    # Scan source directories
    username_dirs = [d for d in source_path.iterdir() if d.is_dir() and d.name.startswith('username_')]
    
    if not username_dirs:
        click.echo("ERROR: No 'username_*' directories found in source.")
        return
    
    total_files = 0
    total_success = 0
    total_errors = 0
    username_summary = {}
    
    for user_dir in sorted(username_dirs):
        username = extract_username_from_folder(user_dir.name)
        if not username:
            continue
        
        # Get all files recursively
        files = list(user_dir.rglob('*'))
        media_files = [f for f in files if f.is_file() and f.suffix.lower() 
                       in ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.jpg', '.jpeg', '.png', '.webp', '.mp3']]
        
        if not media_files:
            continue
        
        dest_user_dir = dest_path / f"username_{username}"
        
        click.echo(f"[{username}] Files: {len(media_files)}")
        
        if stats:
            click.echo()
            continue
        
        user_success = 0
        user_errors = 0
        
        for media_file in media_files:
            success, msg = migrate_file(media_file, dest_user_dir, username, dry_run, copy)
            
            if success:
                user_success += 1
            else:
                user_errors += 1
                click.echo(f"   {msg}", err=True)
        
        click.echo(f"   Result: {user_success} {'migrated' if not dry_run else 'to migrate'}, {user_errors} errors\n")
        
        username_summary[username] = (user_success, user_errors)
        total_files += len(media_files)
        total_success += user_success
        total_errors += user_errors
    
    # Summary
    click.echo(f"\n{'=' * 70}")
    if stats:
        click.echo(f"STATISTICS")
        click.echo(f"  Total usernames: {len(username_dirs)}")
        click.echo(f"  Total files to migrate: {total_files}")
        click.echo(f"  Users with files: {len(username_summary)}")
    else:
        click.echo(f"[OK] MIGRATION COMPLETE" if not dry_run else "[OK] DRY-RUN COMPLETE")
        click.echo(f"  Total files processed: {total_files}")
        click.echo(f"  Successful: {total_success}")
        click.echo(f"  Errors: {total_errors}")
        if not dry_run and not copy:
            click.echo(f"\nFiles moved to: {dest_path}")
        elif not dry_run and copy:
            click.echo(f"\nFiles copied to: {dest_path}")
            click.echo(f"   (Originals preserved in: {source_path})")
    
    click.echo(f"{'=' * 70}\n")
    
    # Show recommendation
    if not dry_run and not stats:
        if total_errors == 0 and total_success > 0:
            click.echo("[SUCCESS] Migration complete!")
            click.echo("\nNext steps:")
            click.echo("  1. Test the new downloads structure")
            click.echo("  2. Run: python main.py utils import-existing --root downloads")
            click.echo("     (to register all files with the tracker)")
            click.echo("  3. Keep old folder until you confirm everything works")


if __name__ == '__main__':
    migrate_downloads()
