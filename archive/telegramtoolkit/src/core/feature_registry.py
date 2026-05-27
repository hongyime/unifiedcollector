#!/usr/bin/env python3
"""
Central registry for processor-backed features used by the unified orchestrator.

This keeps targeted feature runs and unified runs wired to the same processor
definitions so changes to a processor implementation automatically propagate to
both execution modes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional

from src.core.feature_processor import FeatureProcessor
from src.managers.processors.link_collector_processor import LinkCollectorProcessor
from src.managers.processors.media_downloader_processor import MediaDownloaderProcessor
from src.managers.processors.user_analyzer_processor import UserAnalyzerProcessor


ProcessorFactory = Callable[..., FeatureProcessor]


@dataclass(frozen=True)
class ProcessorFeatureDefinition:
    """Definition for a processor-backed feature."""

    key: str
    display_name: str
    description: str
    processor_factory: ProcessorFactory
    runtime_option_keys: tuple[str, ...] = ()
    include_in_unified: bool = True

    def build_processor(
        self,
        runtime_options: Optional[Mapping[str, object]] = None,
    ) -> FeatureProcessor:
        """Instantiate the processor using only supported runtime options."""
        options = dict(runtime_options or {})
        supported_options = {
            key: value
            for key, value in options.items()
            if key in self.runtime_option_keys
        }
        return self.processor_factory(**supported_options)


PROCESSOR_FEATURES: Dict[str, ProcessorFeatureDefinition] = {
    "links": ProcessorFeatureDefinition(
        key="links",
        display_name="Collect Telegram Links",
        description="Extract Telegram links from message text.",
        processor_factory=LinkCollectorProcessor,
    ),
    "users": ProcessorFeatureDefinition(
        key="users",
        display_name="Analyze Users",
        description="Extract user profiles and memberships from message history.",
        processor_factory=UserAnalyzerProcessor,
    ),
    "media": ProcessorFeatureDefinition(
        key="media",
        display_name="Download Media",
        description="Download media files from message history.",
        processor_factory=MediaDownloaderProcessor,
        runtime_option_keys=("save_path",),
    ),
}


def get_processor_feature_definition(feature_key: str) -> ProcessorFeatureDefinition:
    """Return the feature definition for a processor-backed feature."""
    try:
        return PROCESSOR_FEATURES[feature_key]
    except KeyError as exc:
        available = ", ".join(sorted(PROCESSOR_FEATURES))
        raise KeyError(f"Unknown processor feature '{feature_key}'. Available: {available}") from exc


def list_processor_feature_definitions(
    *,
    include_in_unified_only: bool = False,
) -> List[ProcessorFeatureDefinition]:
    """List registered processor-backed features in stable order."""
    definitions = list(PROCESSOR_FEATURES.values())
    if include_in_unified_only:
        definitions = [definition for definition in definitions if definition.include_in_unified]
    return definitions


def build_processors(
    feature_keys: Optional[Iterable[str]] = None,
    *,
    runtime_options_by_key: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> List[FeatureProcessor]:
    """Build processor instances for the requested feature keys."""
    options_lookup = runtime_options_by_key or {}

    if feature_keys is None:
        definitions = list_processor_feature_definitions(include_in_unified_only=True)
    else:
        definitions = [
            get_processor_feature_definition(feature_key)
            for feature_key in feature_keys
        ]

    return [
        definition.build_processor(options_lookup.get(definition.key))
        for definition in definitions
    ]
