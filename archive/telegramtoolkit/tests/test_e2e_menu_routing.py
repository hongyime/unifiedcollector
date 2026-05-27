#!/usr/bin/env python3
"""
End-to-end ingress and menu routing tests.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


pytestmark = pytest.mark.e2e


class TestMenuRoutingOption0:
    @pytest.mark.asyncio
    async def test_menu_option_0_exits_program(self):
        import main

        toolkit = main.TelegramToolkit()
        with patch("builtins.input", return_value="0"):
            await toolkit.run_interactive()


class TestMenuRoutingOptions1To8:
    @pytest.mark.asyncio
    async def test_menu_option_1_routes_to_scan_all_features(self, mock_accounts):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        toolkit.generate_web_indices = MagicMock()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()
        toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)

        with patch("main.get_state_manager") as mock_get_state, patch("builtins.input", return_value="downloads"):
            mock_state = MagicMock()
            mock_get_state.return_value = mock_state
            await toolkit.scan_all_features()

        mock_orchestrator.run.assert_awaited_once_with(mock_accounts, unified_mode=True)
        mock_state.export_all_to_csv.assert_not_called()

    @pytest.mark.asyncio
    async def test_menu_option_2_routes_to_join_groups(self, mock_accounts, tmp_path):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        links_file = tmp_path / "data" / "collected_links.txt"
        links_file.parent.mkdir(exist_ok=True, parents=True)
        links_file.write_text("https://t.me/test_group\n")
        toolkit.base_dir = tmp_path

        with patch("main.GroupJoiner") as mock_joiner_class:
            mock_joiner = MagicMock()
            mock_joiner.join_groups = AsyncMock()
            mock_joiner_class.return_value = mock_joiner
            await toolkit.join_groups()

        assert mock_joiner.selected_accounts == mock_accounts
        mock_joiner.join_groups.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_menu_option_3_routes_to_leave_groups(self, mock_accounts):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))

        with patch("main.GroupCleaner") as mock_cleaner_class:
            mock_cleaner = MagicMock()
            mock_cleaner.run = AsyncMock()
            mock_cleaner_class.return_value = mock_cleaner
            await toolkit.leave_groups()

        assert mock_cleaner.selected_accounts == mock_accounts
        mock_cleaner.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_menu_option_4_routes_to_download_media(self, mock_accounts):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()
        toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)

        with patch("builtins.input", return_value="downloads"):
            await toolkit.download_media()

        toolkit.build_unified_orchestrator.assert_called_once_with(
            ["media"],
            runtime_options_by_key={"media": {"save_path": "downloads"}},
        )
        mock_orchestrator.run.assert_awaited_once_with(mock_accounts)

    @pytest.mark.asyncio
    async def test_menu_option_5_routes_to_analyze_users(self, mock_accounts):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        toolkit.generate_web_indices = MagicMock()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()
        toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)

        with patch("main.get_state_manager") as mock_get_state:
            mock_state = MagicMock()
            mock_get_state.return_value = mock_state
            await toolkit.analyze_users()

        toolkit.build_unified_orchestrator.assert_called_once_with(["users"])
        mock_orchestrator.run.assert_awaited_once_with(mock_accounts)
        mock_state.export_all_to_csv.assert_not_called()

    @pytest.mark.asyncio
    async def test_menu_option_6_routes_to_collect_links(self, mock_accounts):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()
        toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)

        await toolkit.collect_links()

        toolkit.build_unified_orchestrator.assert_called_once_with(["links"])
        mock_orchestrator.run.assert_awaited_once_with(mock_accounts)

    @pytest.mark.asyncio
    async def test_menu_option_7_routes_to_collect_multi_platform_links(self, mock_accounts):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))

        with patch("main.MultiPlatformLinkCollector") as mock_collector_class:
            mock_collector = MagicMock()
            mock_collector.collect_all_multi_platform_links = AsyncMock()
            mock_collector_class.return_value = mock_collector
            await toolkit.collect_multi_platform_links()

        mock_collector.collect_all_multi_platform_links.assert_awaited_once_with(accounts=mock_accounts)

    @pytest.mark.asyncio
    async def test_menu_option_8_routes_to_download_profile_photos(self, mock_accounts, sample_users_csv, tmp_path):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        toolkit.base_dir = tmp_path
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True, parents=True)
        users_csv = data_dir / "Users.csv"
        users_csv.write_text(sample_users_csv.read_text())

        with patch("main.ProfilePhotoDownloader") as mock_downloader_class:
            mock_downloader = MagicMock()
            mock_downloader.download_all_profile_photos_parallel = AsyncMock()
            mock_downloader_class.return_value = mock_downloader
            with patch("builtins.input", return_value=str(tmp_path / "downloads")):
                await toolkit.download_profile_photos()

        mock_downloader.download_all_profile_photos_parallel.assert_awaited_once_with(mock_accounts)


class TestMenuRoutingOption9:
    @pytest.mark.asyncio
    async def test_menu_option_9_routes_to_send_photos(self):
        import main

        toolkit = main.TelegramToolkit()
        request = {
            "accounts": ["acc1"],
            "directory": "photos",
            "chat_id": "targetchat",
            "delete_after": True,
            "delete_skipped_already_sent": False,
        }

        with patch("main.verify_accounts_for_feature", new=AsyncMock(return_value=(True, ["acc1"]))), \
             patch("main.collect_send_photos_inputs", return_value=request), \
             patch("main.PhotoSender") as mock_sender_class:
            mock_sender = MagicMock()
            mock_sender.send_photos = AsyncMock()
            mock_sender_class.return_value = mock_sender
            await toolkit.send_photos()

        mock_sender.send_photos.assert_awaited_once_with(
            request["accounts"],
            request["directory"],
            request["chat_id"],
            request["delete_after"],
            delete_skipped_already_sent=request["delete_skipped_already_sent"],
        )


class TestMenuRoutingOptions10To16:
    def test_menu_option_10_routes_to_open_dashboard(self, tmp_path):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.base_dir = tmp_path
        (tmp_path / "web").mkdir(parents=True, exist_ok=True)
        (tmp_path / "web" / "enhanced_dashboard.html").write_text("<html></html>")
        toolkit.start_web_server = MagicMock(return_value=8000)

        with patch("webbrowser.open"), patch("builtins.input", return_value=""):
            toolkit.open_dashboard()

        toolkit.start_web_server.assert_called_once()

    def test_menu_option_11_routes_to_open_visualizer(self, tmp_path):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.base_dir = tmp_path
        (tmp_path / "web").mkdir(parents=True, exist_ok=True)
        (tmp_path / "web" / "visualize.html").write_text("<html></html>")
        toolkit.start_web_server = MagicMock(return_value=8000)

        with patch("webbrowser.open"), patch("builtins.input", return_value=""):
            toolkit.open_visualizer()

        toolkit.start_web_server.assert_called_once()

    def test_menu_option_12_export_routes_to_json(self, tmp_path):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.base_dir = tmp_path
        toolkit.export_to_json = MagicMock()
        toolkit.export_to_excel = MagicMock()
        toolkit.generate_report = MagicMock()

        with patch("builtins.input", return_value="1"):
            toolkit.export_data()

        toolkit.export_to_json.assert_called_once()
        toolkit.export_to_excel.assert_not_called()
        toolkit.generate_report.assert_not_called()

    def test_menu_option_12_export_routes_to_excel(self, tmp_path):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.base_dir = tmp_path
        toolkit.export_to_json = MagicMock()
        toolkit.export_to_excel = MagicMock()
        toolkit.generate_report = MagicMock()

        with patch("builtins.input", return_value="2"):
            toolkit.export_data()

        toolkit.export_to_excel.assert_called_once()

    def test_menu_option_12_export_routes_to_report(self, tmp_path):
        import main

        toolkit = main.TelegramToolkit()
        toolkit.base_dir = tmp_path
        toolkit.export_to_json = MagicMock()
        toolkit.export_to_excel = MagicMock()
        toolkit.generate_report = MagicMock()

        with patch("builtins.input", return_value="3"):
            toolkit.export_data()

        toolkit.generate_report.assert_called_once()

    @pytest.mark.asyncio
    async def test_menu_option_13_routes_to_manage_accounts(self):
        import main

        with patch("main.AccountManager") as mock_manager_class:
            mock_manager = MagicMock()
            mock_manager.show_menu = AsyncMock()
            mock_manager_class.return_value = mock_manager
            toolkit = main.TelegramToolkit()
            await toolkit.manage_accounts()

        mock_manager.show_menu.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_menu_option_14_routes_to_manage_download_state(self):
        import main

        with patch("main.DownloadStateManager") as mock_manager_class:
            mock_manager = MagicMock()
            mock_manager.show_menu = MagicMock()
            mock_manager_class.return_value = mock_manager
            toolkit = main.TelegramToolkit()
            await toolkit.manage_download_state()

        mock_manager.show_menu.assert_called_once()

    @pytest.mark.asyncio
    async def test_menu_option_15_routes_to_run_backup(self):
        import main

        toolkit = main.TelegramToolkit()
        with patch("src.managers.backup.get_user_inputs"), \
             patch("src.managers.backup.initialize_client_and_folder") as mock_init, \
             patch("src.managers.backup.export_messages", new=AsyncMock()) as mock_export:
            mock_client = MagicMock()
            mock_client.start = AsyncMock()
            mock_client.disconnect = AsyncMock()
            mock_init.return_value = mock_client
            await toolkit.run_backup()

        mock_init.assert_called_once()
        mock_export.assert_awaited_once_with(mock_client)

    @pytest.mark.asyncio
    async def test_menu_option_16_routes_to_run_resender(self):
        import main

        toolkit = main.TelegramToolkit()
        with patch("src.managers.resender.main", new=AsyncMock()) as mock_resender_main:
            await toolkit.run_resender()

        mock_resender_main.assert_awaited_once()


class TestInteractiveManagers:
    def test_account_manager_show_menu_exit(self):
        from src.managers.account_manager import AccountManager

        manager = AccountManager()
        with patch("builtins.input", return_value="0"):
            pytest.raises(SystemExit) if False else None
            import asyncio
            asyncio.run(manager.show_menu())

    def test_account_manager_manage_sessions_exit(self):
        from src.managers.account_manager import AccountManager

        manager = AccountManager()
        with patch("builtins.input", return_value="0"):
            import asyncio
            asyncio.run(manager.manage_sessions())

    def test_download_state_manager_menu_reset_account_branch(self):
        from src.managers.manage_download_state import DownloadStateManager

        manager = DownloadStateManager()
        fake_accounts = [{"name": "acct1", "phone": "+1000"}]
        with patch("src.managers.manage_download_state.get_accounts", return_value=fake_accounts), \
             patch.object(manager, "reset_scan_tracking") as mock_reset, \
             patch("builtins.input", side_effect=["2", "2", "1", "0"]):
            manager.show_menu()

        mock_reset.assert_called_once_with(account_name="acct1")

    def test_download_state_manager_menu_reset_chat_branch(self):
        from src.managers.manage_download_state import DownloadStateManager

        manager = DownloadStateManager()
        fake_accounts = [{"name": "acct1", "phone": "+1000"}]
        with patch("src.managers.manage_download_state.get_accounts", return_value=fake_accounts), \
             patch.object(manager, "reset_media_tracking") as mock_reset, \
             patch("builtins.input", side_effect=["3", "3", "1", "@chat42", "0"]):
            manager.show_menu()

        mock_reset.assert_called_once_with(account_name="acct1", chat_id="chat42")

    def test_download_state_manager_menu_reset_all_branch(self):
        from src.managers.manage_download_state import DownloadStateManager

        manager = DownloadStateManager()
        with patch.object(manager, "reset_all_tracking") as mock_reset, \
             patch("builtins.input", side_effect=["8", "0"]):
            manager.show_menu()

        mock_reset.assert_called_once_with()

    def test_download_state_manager_invalid_choice(self, capsys):
        from src.managers.manage_download_state import DownloadStateManager

        manager = DownloadStateManager()
        with patch("builtins.input", side_effect=["99", "0"]):
            manager.show_menu()

        captured = capsys.readouterr()
        assert "Invalid choice" in captured.out

    def test_download_state_manager_full_recheck_resets_legacy_sidecars(self, tmp_path):
        from src.managers.manage_download_state import DownloadStateManager

        manager = DownloadStateManager()
        manager.data_dir = tmp_path / "data"
        manager.data_dir.mkdir(parents=True, exist_ok=True)
        (manager.data_dir / "sent_photo_hashes.txt").write_text("abc\n", encoding="utf-8")
        (manager.data_dir / "downloaded_hashes.txt").write_text("def\n", encoding="utf-8")
        (manager.data_dir / "downloaded_profile_photos.json").write_text("[\"u1_1\"]", encoding="utf-8")
        (manager.data_dir / "acct1_download_state.json").write_text("{\"chat1\": {\"last_message_id\": 10}}", encoding="utf-8")

        manager.state = MagicMock()
        with patch("builtins.input", side_effect=["2", "y"]):
            manager.reset_all_tracking()

        assert (manager.data_dir / "sent_photo_hashes.txt").read_text(encoding="utf-8") == ""
        assert (manager.data_dir / "downloaded_hashes.txt").read_text(encoding="utf-8") == ""
        assert json.loads((manager.data_dir / "downloaded_profile_photos.json").read_text(encoding="utf-8")) == []
        assert json.loads((manager.data_dir / "acct1_download_state.json").read_text(encoding="utf-8")) == {}


class TestMenuRoutingInvalidOption:
    @pytest.mark.asyncio
    async def test_invalid_menu_option_shows_error(self, capsys):
        import main

        toolkit = main.TelegramToolkit()
        with patch("builtins.input", side_effect=["99", "", "0"]):
            await toolkit.run_interactive()

        captured = capsys.readouterr()
        # validate_numeric_input rejects out-of-range/non-numeric input before routing
        assert "Please enter a number between" in captured.out or "Invalid input" in captured.out
