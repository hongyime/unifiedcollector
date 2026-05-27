#!/usr/bin/env python3
import unittest
from pathlib import Path

import main
from src.core.message_orchestrator import MessageOrchestrator
from src.core.feature_registry import (
    build_processors,
    list_processor_feature_definitions,
)


class FeatureRegistryTests(unittest.TestCase):
    def _build_test_orchestrator(self, progress_by_feature):
        orchestrator = MessageOrchestrator.__new__(MessageOrchestrator)
        orchestrator.processors = []
        orchestrator.stats = {'processors_active': 0}

        class FakeState:
            def get_feature_progress_all(self, account_name, group_id):
                return dict(progress_by_feature)

        orchestrator.state = FakeState()
        return orchestrator

    def test_default_unified_processors_cover_registered_features(self):
        processors = build_processors()
        registered_names = [
            definition.build_processor().name
            for definition in list_processor_feature_definitions(include_in_unified_only=True)
        ]

        self.assertEqual(len(processors), len(registered_names))
        self.assertEqual(
            [processor.name for processor in processors],
            registered_names,
        )

    def test_media_processor_accepts_runtime_options(self):
        media_processor = build_processors(
            ["media"],
            runtime_options_by_key={"media": {"save_path": "custom_downloads"}},
        )[0]

        self.assertEqual(media_processor.name, "media_downloader")
        self.assertEqual(media_processor.save_path, Path("custom_downloads"))

    def test_toolkit_builds_targeted_orchestrator_from_registry(self):
        toolkit = main.TelegramToolkit.__new__(main.TelegramToolkit)
        orchestrator = toolkit.build_unified_orchestrator(["links", "users"])

        self.assertEqual(
            [processor.name for processor in orchestrator.processors],
            ["link_collector", "user_analyzer"],
        )

    def test_toolkit_builds_full_unified_orchestrator_from_registry(self):
        toolkit = main.TelegramToolkit.__new__(main.TelegramToolkit)
        orchestrator = toolkit.build_unified_orchestrator(
            runtime_options_by_key={"media": {"save_path": "downloads"}}
        )

        self.assertEqual(
            [processor.name for processor in orchestrator.processors],
            ["link_collector", "user_analyzer", "media_downloader"],
        )

    def test_registered_processors_expose_matching_feature_keys(self):
        for definition in list_processor_feature_definitions():
            processor = definition.build_processor()
            self.assertEqual(processor.feature_key, definition.key)

    def test_unified_start_message_id_uses_all_registered_processor_keys(self):
        orchestrator = self._build_test_orchestrator({
            "links": 120,
            "users": 80,
            "media": 100,
            "polls": 40,
        })

        class FakeProcessor:
            def __init__(self, name, feature_key):
                self.name = name
                self.feature_key = feature_key

        orchestrator.processors = [
            FakeProcessor("link_collector", "links"),
            FakeProcessor("user_analyzer", "users"),
            FakeProcessor("media_downloader", "media"),
            FakeProcessor("poll_processor", "polls"),
        ]

        self.assertEqual(
            orchestrator.get_unified_start_message_id("account1", "chat123"),
            40,
        )

    def test_unified_progress_snapshot_defaults_missing_registered_features_to_zero(self):
        orchestrator = self._build_test_orchestrator({
            "links": 120,
            "users": 80,
        })

        class FakeProcessor:
            def __init__(self, name, feature_key):
                self.name = name
                self.feature_key = feature_key

        orchestrator.processors = [
            FakeProcessor("link_collector", "links"),
            FakeProcessor("user_analyzer", "users"),
            FakeProcessor("future_processor", "future_feature"),
        ]

        self.assertEqual(
            orchestrator.get_unified_progress_snapshot("account1", "chat123"),
            {
                "links": 120,
                "users": 80,
                "future_feature": 0,
            },
        )
        self.assertEqual(
            orchestrator.get_unified_start_message_id("account1", "chat123"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
