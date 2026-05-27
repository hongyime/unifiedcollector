"""
Tracking State Manager
Utility to inspect and reset resumable tracking without deleting saved outputs.

This keeps option 14 focused on recovery/reset work while supporting both:
- checkpoint-only reset
- full re-check reset
"""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from src.core.console import configure_console_output
from src.core.dynamic_config import get_accounts
from src.core.resilience import atomic_json_write
from src.core.state_manager import get_state_manager


configure_console_output()


class _Subsystem:
    UNIFIED = "unified"
    MEDIA = "media"
    LINKS = "links"
    USERS = "users"
    PHOTO_SEND = "photo_send"
    PROFILE_PHOTOS = "profile_photos"
    ALL = "all"


class _ResetMode:
    CHECKPOINT = "checkpoint_only"
    FULL_RECHECK = "full_recheck"


class DownloadStateManager:
    """Interactive manager for tracking and reset controls."""

    RESET_MODES = {
        "1": (_ResetMode.CHECKPOINT, "Checkpoint Only"),
        "2": (_ResetMode.FULL_RECHECK, "Full Re-check"),
    }

    def __init__(self):
        self.data_dir = Path("data")
        self.data_dir.mkdir(exist_ok=True)
        self.state = get_state_manager()

    def _legacy_paths_for(self, subsystem_key: str, account_name: Optional[str] = None) -> List[Path]:
        paths: List[Path] = []

        if subsystem_key in {_Subsystem.UNIFIED, _Subsystem.ALL}:
            if account_name:
                paths.append(self.data_dir / f"{account_name}_download_state.json")
            else:
                paths.extend(sorted(self.data_dir.glob("*_download_state.json")))

        if subsystem_key in {_Subsystem.PHOTO_SEND, _Subsystem.ALL}:
            paths.extend([
                self.data_dir / "photo_send_progress.json",
                self.data_dir / "sent_photo_hashes.txt",
                self.data_dir / "sent_photo_hashes.json",
            ])

        if subsystem_key in {_Subsystem.PROFILE_PHOTOS, _Subsystem.ALL}:
            paths.append(self.data_dir / "downloaded_profile_photos.json")

        if subsystem_key in {_Subsystem.MEDIA, _Subsystem.ALL}:
            paths.extend([
                self.data_dir / "downloaded_hashes.txt",
                self.data_dir / "downloaded_hashes.json",
            ])

        seen = set()
        ordered: List[Path] = []
        for path in paths:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
        return ordered

    def _backup_legacy_files(self, paths: Iterable[Path]) -> List[Path]:
        backup_dir = self.data_dir / "tracking_reset_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        created: List[Path] = []
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for path in paths:
            if not path.exists() or not path.is_file():
                continue
            backup_path = backup_dir / f"{timestamp}_{path.name}"
            shutil.copy2(path, backup_path)
            created.append(backup_path)
        return created

    def _reset_legacy_download_state(self, account_name: Optional[str] = None, chat_id: Optional[str] = None):
        if account_name:
            state_files = [self.data_dir / f"{account_name}_download_state.json"]
        else:
            state_files = sorted(self.data_dir.glob("*_download_state.json"))

        for state_file in state_files:
            if not state_file.exists():
                continue
            if chat_id is None:
                atomic_json_write(str(state_file), {})
                continue

            try:
                with open(state_file, "r", encoding="utf-8") as handle:
                    payload = json.load(handle) or {}
            except Exception:
                payload = {}
            payload.pop(str(chat_id), None)
            atomic_json_write(str(state_file), payload)

    def _reset_sent_photo_hashes_legacy(self):
        atomic_json_write(str(self.data_dir / "sent_photo_hashes.json"), {"hashes": []})
        (self.data_dir / "sent_photo_hashes.txt").write_text("", encoding="utf-8")

    def _reset_profile_photo_legacy(self):
        atomic_json_write(str(self.data_dir / "downloaded_profile_photos.json"), [])

    def _reset_media_hashes_legacy(self):
        atomic_json_write(str(self.data_dir / "downloaded_hashes.json"), [])
        (self.data_dir / "downloaded_hashes.txt").write_text("", encoding="utf-8")

    def _reset_photo_send_legacy(self):
        atomic_json_write(str(self.data_dir / "photo_send_progress.json"), {})

    def _print_saved_data_notice(self, mode_label: str):
        print("\nℹ️  Tracking reset only. Saved files and exported data remain in place.")
        if "Full" in mode_label:
            print("ℹ️  Full re-check may revisit items and can append duplicates during future runs.")

    def _choose_reset_mode(self) -> Optional[tuple[str, str]]:
        print("\nReset Mode")
        print("1. Checkpoint Only")
        print("2. Full Re-check")
        choice = input("Choose reset mode (1-2, blank to cancel): ").strip()
        if not choice:
            print("↩️ Cancelled.")
            return None
        mode = self.RESET_MODES.get(choice)
        if not mode:
            print("❌ Invalid reset mode")
            return None
        self._print_saved_data_notice(mode[1])
        return mode

    def _confirm_reset(self, label: str, mode_label: str) -> bool:
        confirm = input(f"\n⚠️ Reset {label} using {mode_label}? (y/N): ").strip().lower()
        return confirm == "y"

    def _select_account(self) -> Optional[str]:
        accounts = get_accounts()
        if not accounts:
            print("❌ No configured accounts found.")
            return None
        print("\n📋 Available accounts:")
        for index, account in enumerate(accounts, 1):
            print(f"   {index}. {account['name']}")
        try:
            selection = int(input("\nSelect account number: ").strip()) - 1
        except ValueError:
            print("❌ Invalid input")
            return None
        if selection < 0 or selection >= len(accounts):
            print("❌ Invalid account number")
            return None
        return accounts[selection]["name"]

    def _run_reset(
        self,
        *,
        subsystem_key: str,
        label: str,
        reset_action: Callable[[str], None],
        account_name: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        mode = self._choose_reset_mode()
        if mode is None:
            return

        mode_key, mode_label = mode
        target_label = label
        if account_name:
            target_label = f"{target_label} for {account_name}"
        if chat_id:
            target_label = f"{target_label} / chat {chat_id}"
        if not self._confirm_reset(target_label, mode_label):
            print("↩️ Reset cancelled.")
            return

        self.state.flush_all_buffers()
        backup_paths = self._backup_legacy_files(self._legacy_paths_for(subsystem_key, account_name))
        reset_action(mode_key)
        if backup_paths:
            print(f"📦 Backed up {len(backup_paths)} tracking file(s) before reset.")
        print(f"✅ Reset complete: {target_label} ({mode_label})")

    def show_tracking_summary(self):
        """Show current tracking summary across modern and legacy stores."""
        summary = self.state.get_tracking_summary()
        print("\n📊 Tracking Summary")
        print("=" * 60)
        print(f"Unified scan checkpoints: {summary['scan_progress']}")
        print(f"Feature checkpoints:      {summary['feature_progress']}")
        print(f"Collected links tracked:  {summary['link_collection']}")
        print(f"Media hash trackers:      {summary['download_hashes']}")
        print(f"User failed lookups:      {summary['failed_lookups']}")
        print(f"Photo send progress:      {summary['photo_send_progress']}")
        print(f"Profile photo tracking:   {summary['profile_photo_tracking']}")

        legacy_files = [
            *sorted(self.data_dir.glob("*_download_state.json")),
            self.data_dir / "photo_send_progress.json",
            self.data_dir / "sent_photo_hashes.txt",
            self.data_dir / "downloaded_profile_photos.json",
            self.data_dir / "downloaded_hashes.txt",
        ]
        print("\nLegacy tracking sidecars:")
        for path in legacy_files:
            status = "present" if path.exists() else "missing"
            print(f"  - {path.name}: {status}")

    def reset_scan_tracking(self, account_name: Optional[str] = None, chat_id: Optional[str] = None):
        def action(_mode_key: str):
            self.state.reset_scan_progress(account_name=account_name, chat_id=chat_id)
            self.state.reset_feature_progress_scope(account_name=account_name, chat_id=chat_id)
            self._reset_legacy_download_state(account_name=account_name, chat_id=chat_id)

        self._run_reset(
            subsystem_key=_Subsystem.UNIFIED,
            label="unified scan tracking",
            reset_action=action,
            account_name=account_name,
            chat_id=chat_id,
        )

    def reset_media_tracking(self, account_name: Optional[str] = None, chat_id: Optional[str] = None):
        def action(mode_key: str):
            self.state.reset_feature_progress_scope(
                account_name=account_name,
                chat_id=chat_id,
                feature_name="media",
            )
            if mode_key == _ResetMode.FULL_RECHECK:
                self.state.reset_download_hashes()
                self._reset_media_hashes_legacy()

        self._run_reset(
            subsystem_key=_Subsystem.MEDIA,
            label="media tracking",
            reset_action=action,
            account_name=account_name,
            chat_id=chat_id,
        )

    def reset_link_tracking(self, account_name: Optional[str] = None, chat_id: Optional[str] = None):
        def action(mode_key: str):
            self.state.reset_feature_progress_scope(
                account_name=account_name,
                chat_id=chat_id,
                feature_name="links",
            )
            if mode_key == _ResetMode.FULL_RECHECK:
                self.state.reset_link_collection(platform="telegram", account_name=account_name)

        self._run_reset(
            subsystem_key=_Subsystem.LINKS,
            label="link tracking",
            reset_action=action,
            account_name=account_name,
            chat_id=chat_id,
        )

    def reset_user_tracking(self, account_name: Optional[str] = None, chat_id: Optional[str] = None):
        def action(mode_key: str):
            self.state.reset_feature_progress_scope(
                account_name=account_name,
                chat_id=chat_id,
                feature_name="users",
            )
            if mode_key == _ResetMode.FULL_RECHECK:
                self.state.reset_failed_lookups()

        self._run_reset(
            subsystem_key=_Subsystem.USERS,
            label="user-analysis tracking",
            reset_action=action,
            account_name=account_name,
            chat_id=chat_id,
        )

    def reset_photo_send_tracking(self, account_name: Optional[str] = None, chat_id: Optional[str] = None):
        def action(mode_key: str):
            self.state.reset_photo_send_progress(account_name=account_name, chat_id=chat_id)
            self._reset_photo_send_legacy()
            if mode_key == _ResetMode.FULL_RECHECK:
                self._reset_sent_photo_hashes_legacy()

        self._run_reset(
            subsystem_key=_Subsystem.PHOTO_SEND,
            label="photo-send tracking",
            reset_action=action,
            account_name=account_name,
            chat_id=chat_id,
        )

    def reset_profile_photo_tracking(self, account_name: Optional[str] = None, chat_id: Optional[str] = None):
        def action(_mode_key: str):
            self.state.reset_profile_photo_tracking()
            self._reset_profile_photo_legacy()

        self._run_reset(
            subsystem_key=_Subsystem.PROFILE_PHOTOS,
            label="profile-photo tracking",
            reset_action=action,
            account_name=account_name,
        )

    def reset_all_tracking(self):
        def action(mode_key: str):
            self.state.reset_scan_progress()
            self.state.reset_feature_progress_scope()
            self.state.reset_photo_send_progress()
            self._reset_legacy_download_state()
            self._reset_photo_send_legacy()
            if mode_key == _ResetMode.FULL_RECHECK:
                self.state.reset_download_hashes()
                self.state.reset_failed_lookups()
                self.state.reset_profile_photo_tracking()
                self.state.reset_link_collection()
                self._reset_sent_photo_hashes_legacy()
                self._reset_profile_photo_legacy()
                self._reset_media_hashes_legacy()

        self._run_reset(
            subsystem_key=_Subsystem.ALL,
            label="all tracking",
            reset_action=action,
        )

    def _handle_scoped_reset(self, reset_method: Callable[..., None], allow_chat: bool = True):
        print("\nScope")
        print("1. Reset everything for this subsystem")
        print("2. Reset one account")
        if allow_chat:
            print("3. Reset one account/chat")
        scope_choice = input("Choose scope: ").strip()

        if scope_choice == "1":
            reset_method()
            return
        if scope_choice == "2":
            account_name = self._select_account()
            if account_name:
                reset_method(account_name=account_name)
            return
        if scope_choice == "3" and allow_chat:
            account_name = self._select_account()
            if not account_name:
                return
            chat_id = input(f"Enter chat ID to reset for {account_name}: ").strip().lstrip("@")
            if not chat_id:
                print("❌ Chat ID is required")
                return
            reset_method(account_name=account_name, chat_id=chat_id)
            return

        print("❌ Invalid scope")

    # Backward-compatible wrappers used by older tests and callers.
    def show_all_progress(self):
        self.show_tracking_summary()

    def reset_account_progress(self, account_name):
        self.reset_scan_tracking(account_name=account_name)

    def reset_chat_progress(self, account_name, chat_id):
        self.reset_scan_tracking(account_name=account_name, chat_id=chat_id)

    def show_menu(self):
        """Show interactive menu"""
        while True:
            print("\n" + "=" * 60)
            print("📊 Tracking / Reset State")
            print("=" * 60)
            print("1️⃣  Show Tracking Summary")
            print("2️⃣  Reset Unified Scan Progress")
            print("3️⃣  Reset Media Tracking")
            print("4️⃣  Reset Link Tracking")
            print("5️⃣  Reset User-Analysis Tracking")
            print("6️⃣  Reset Photo-Send Tracking")
            print("7️⃣  Reset Profile-Photo Tracking")
            print("8️⃣  Reset All Tracking")
            print("9️⃣  List Available Accounts")
            print("0️⃣  Exit")
            print("=" * 60)

            choice = input("\n🔧 Enter your choice: ").strip()

            if choice == "1":
                self.show_tracking_summary()
            elif choice == "2":
                self._handle_scoped_reset(self.reset_scan_tracking, allow_chat=True)
            elif choice == "3":
                self._handle_scoped_reset(self.reset_media_tracking, allow_chat=True)
            elif choice == "4":
                self._handle_scoped_reset(self.reset_link_tracking, allow_chat=True)
            elif choice == "5":
                self._handle_scoped_reset(self.reset_user_tracking, allow_chat=True)
            elif choice == "6":
                self._handle_scoped_reset(self.reset_photo_send_tracking, allow_chat=True)
            elif choice == "7":
                self._handle_scoped_reset(self.reset_profile_photo_tracking, allow_chat=False)
            elif choice == "8":
                self.reset_all_tracking()
            elif choice == "9":
                accounts = get_accounts()
                print("\n📋 Configured Accounts:")
                for index, account in enumerate(accounts, 1):
                    print(f"   {index}. {account['name']} ({account['phone']})")
            elif choice == "0":
                print("\n👋 Goodbye!")
                break
            else:
                print("❌ Invalid choice. Please try again.")


if __name__ == "__main__":
    manager = DownloadStateManager()
    manager.show_menu()
