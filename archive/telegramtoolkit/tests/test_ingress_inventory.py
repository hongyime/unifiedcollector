#!/usr/bin/env python3
from pathlib import Path

import pytest


def test_main_menu_lists_all_top_level_options():
    main_text = Path("main.py").read_text(encoding="utf-8")
    for option in range(0, 17):
        assert f'"{option}"' in main_text or f"{option}." in main_text


def test_cli_aliases_cover_all_expected_commands():
    main_text = Path("main.py").read_text(encoding="utf-8")
    expected_aliases = [
        "unified", "join", "leave", "media", "users", "links",
        "multi", "external", "profiles", "photos", "dashboard",
        "visualize", "export", "data", "accounts", "state",
        "backup", "resend", "pipeline",
    ]
    for alias in expected_aliases:
        assert alias in main_text


def test_interactive_ingress_modules_exist():
    expected_paths = [
        Path("main.py"),
        Path("src/managers/account_manager.py"),
        Path("src/managers/manage_download_state.py"),
        Path("src/managers/join_groups.py"),
        Path("src/managers/leave_groups.py"),
        Path("src/managers/send_photos.py"),
        Path("src/managers/backup.py"),
        Path("src/managers/resender.py"),
        Path("src/runners/multi_platform_links.py"),
        Path("src/managers/download_profile_photos.py"),
    ]
    for expected_path in expected_paths:
        assert expected_path.exists(), f"Missing ingress module: {expected_path}"
