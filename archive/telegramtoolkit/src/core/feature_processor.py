#!/usr/bin/env python3
"""
Feature Processor Interface for Telegram Toolkit
Defines the contract for all feature processors in the unified orchestrator.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class FeatureProcessor(ABC):
    """
    Abstract base class for all feature processors.
    Each feature (user analysis, link collection, media download, etc.)
    must implement this interface to work with the MessageOrchestrator.
    """
    
    name: str = "unnamed_processor"
    feature_key: str = "unnamed_feature"
    
    @abstractmethod
    async def process_message(self, event: Dict[str, Any]) -> None:
        """
        Process a message event from the orchestrator.
        
        Args:
            event: Dictionary containing:
                - message: The Telegram message object
                - entity: The chat/channel entity
                - group_id: String ID of the group
                - group_name: Display name of the group
                - account_name: Name of the account scanning
                - client: TelegramClient instance
                - timestamp: When this message was processed
        """
        pass
    
    @abstractmethod
    async def on_scan_start(self, context: Dict[str, Any]) -> None:
        """
        Called when scanning starts for a group.
        
        Args:
            context: Contains group info, account info, and client
        """
        pass
    
    @abstractmethod
    async def on_scan_complete(self, context: Dict[str, Any]) -> None:
        """
        Called when scanning completes for a group.
        
        Args:
            context: Contains group info, account info, and statistics
        """
        pass
    
    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the processor (called once at startup).
        """
        pass
    
    @abstractmethod
    async def shutdown(self) -> None:
        """
        Clean shutdown (called once at exit).
        Save any pending data, close connections, etc.
        """
        pass

    async def discover_scan_targets(
        self,
        client,
        account: Dict[str, Any],
        group_ids: Optional[List[str]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Optional targeted-mode hook for processor-specific scan target discovery.

        Return a list of dictionaries with at least:
        - entity
        - group_id
        - group_name

        Returning `None` tells the orchestrator to use its default dialog
        discovery behavior.
        """
        return None
    
    def get_progress_key(self, account_name: str, group_id: str) -> str:
        """Generate a unique progress tracking key"""
        return f"{account_name}_{self.name}_{group_id}"
