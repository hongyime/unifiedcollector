#!/usr/bin/env python3
"""
Media Downloader Processor for Unified Message Orchestrator
Downloads media files from messages.
"""
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from src.core.account_health import AccountFailureError, AccountHealthPolicy, is_account_error
from src.core.feature_processor import FeatureProcessor
from src.core.base_feature import BaseFeature
from src.core.media_policy import classify_document_media as classify_supported_document_media
from src.core.scan_targets import discover_scan_targets
from src.core.state_manager import get_state_manager
from src.core.progress_logger import log_info, log_success, log_error, log_warning


class MediaDownloaderProcessor(FeatureProcessor, BaseFeature):
    """
    Processor that downloads media from messages.
    Downloads only supported media types:
    photos, images, videos, and round video notes.
    """
    
    name = "media_downloader"
    feature_key = "media"
    
    def __init__(self, save_path: str = "downloads"):
        BaseFeature.__init__(self, name=self.name)
        self.state = get_state_manager()
        self.save_path = Path(save_path)
        self.stats = {
            'media_downloaded': 0,
            'duplicates_skipped': 0,
            'unsupported_skipped': 0,
            'errors': 0
        }
        self.downloaded_hashes = set()
        self._clients_map: Dict[str, Any] = {}
        self._account_health: Optional[AccountHealthPolicy] = None
        self._current_account_name: Optional[str] = None

        # Create save directory
        self.save_path.mkdir(parents=True, exist_ok=True)
    
    async def initialize(self) -> None:
        """Initialize the processor"""
        log_info(f"📥 [{self.name}] Initializing media downloader...")
        log_info(f"📂 [{self.name}] Save path: {self.save_path}")
        
        # Load existing hashes for deduplication
        self.downloaded_hashes = set(self.state.get_all_hashes())
        log_info(f"📚 [{self.name}] Loaded {len(self.downloaded_hashes)} existing hashes")
    
    async def shutdown(self) -> None:
        """Clean shutdown"""
        log_info(f"💾 [{self.name}] Shutting down... Media downloaded: {self.stats['media_downloaded']}")

    async def discover_scan_targets(
        self,
        client,
        account: Dict[str, Any],
        group_ids: Optional[list[str]] = None,
    ) -> Optional[list[Dict[str, Any]]]:
        """Include private chats and linked discussion groups to preserve media parity."""
        return await discover_scan_targets(
            client,
            group_ids=group_ids,
            include_private_chats=True,
            prefer_linked_discussions=True,
        )
    
    async def on_scan_start(self, context: Dict[str, Any]) -> None:
        """Called when scanning starts for a group"""
        group_name = context['group_name']
        self._clients_map = context.get('clients_map') or {}
        self._account_health = context.get('account_health')
        self._current_account_name = context.get('account_name')
        log_info(f"🎬 [{self.name}] Starting media download for: {group_name}")
    
    async def on_scan_complete(self, context: Dict[str, Any]) -> None:
        """Called when scanning completes for a group"""
        group_name = context['group_name']
        log_success(f"✅ [{self.name}] {group_name}: Downloaded {self.stats['media_downloaded']} media files")
    
    async def process_message(self, event: Dict[str, Any]) -> None:
        """Process a message event to download media"""
        message = event['message']
        group_name = event['group_name']
        group_id = event['group_id']
        account_name = event['account_name']
        client = event['client']
        
        # Capture shared resources into locals so concurrent account tasks cannot
        # clobber them between the set and the first await inside the download path.
        clients_map = event.get('clients_map') or self._clients_map
        account_health = event.get('account_health') or self._account_health
        self._clients_map = clients_map
        self._account_health = account_health

        # Check if message has media
        if not hasattr(message, 'media') or not message.media:
            # Save progress even if no media
            self.state.save_feature_progress(
                account_name,
                group_id,
                'media',
                message.id,
                self.stats['media_downloaded']
            )
            return

        # Handle different media types — pass account_name explicitly to avoid shared-state race
        if isinstance(message.media, MessageMediaPhoto):
            await self.download_photo(client, message, group_name, account_name, clients_map, account_health)
        elif isinstance(message.media, MessageMediaDocument):
            await self.download_supported_document(client, message, group_name, account_name, clients_map, account_health)
        
        # Save feature progress after processing message
        self.state.save_feature_progress(
            account_name, 
            group_id, 
            'media',
            message.id,
            self.stats['media_downloaded']
        )
    
    async def _download_with_fallback(
        self,
        client,
        message,
        filepath: Path,
        account_name: Optional[str] = None,
        clients_map: Optional[Dict[str, Any]] = None,
        account_health: Optional[AccountHealthPolicy] = None,
    ) -> bool:
        """Download media with cross-account fallback on FloodWait.

        account_name, clients_map, and account_health are passed explicitly so
        concurrent account tasks cannot clobber shared instance state between
        the first await (client.download_media) and the FloodWait handler.
        """
        from telethon.errors import FloodWaitError
        # Fall back to instance attributes only when caller doesn't supply values
        _account_name = account_name or self._current_account_name
        _clients_map = clients_map if clients_map is not None else self._clients_map
        _account_health = account_health or self._account_health

        try:
            await client.download_media(message, file=str(filepath))
            return True
        except FloodWaitError as e:
            if _account_health and _account_name:
                _account_health.record_flood_wait(_account_name, e.seconds)

            # Try alternate accounts
            if _clients_map and _account_health:
                candidates = list(_clients_map.keys())
                best = _account_health.get_best_account(candidates, exclude=_account_name)
                if best:
                    alt_client = _clients_map[best]
                    try:
                        await alt_client.download_media(message, file=str(filepath))
                        log_info(f"🔄 [{self.name}] Media fallback succeeded via {best}")
                        return True
                    except FloodWaitError as alt_e:
                        _account_health.record_flood_wait(best, alt_e.seconds)
                    except Exception:
                        pass

            # All fallbacks exhausted — wait on original
            import asyncio
            log_warning(f"⏳ [{self.name}] Rate limit: Waiting {e.seconds}s...")
            await asyncio.sleep(e.seconds)
            await client.download_media(message, file=str(filepath))
            return True

    async def download_photo(
        self,
        client,
        message,
        group_name: str,
        account_name: Optional[str] = None,
        clients_map: Optional[Dict[str, Any]] = None,
        account_health: Optional[AccountHealthPolicy] = None,
    ) -> None:
        """Download a photo from a message."""
        try:
            filename = self.generate_filename(message, 'photo', 'jpg', group_name)
            filepath = self.save_path / self.get_safe_group_folder(group_name) / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)

            if filepath.exists():
                file_hash = self.calculate_hash(filepath)
                if file_hash in self.downloaded_hashes:
                    self.stats['duplicates_skipped'] += 1
                    return

            await self._download_with_fallback(client, message, filepath, account_name, clients_map, account_health)

            file_hash = self.calculate_hash(filepath)
            self.state.save_hash(file_hash, str(filepath), filepath.stat().st_size)
            self.downloaded_hashes.add(file_hash)

            self.stats['media_downloaded'] += 1
            log_success(f"📸 [{self.name}] Downloaded: {filename}")

        except Exception as e:
            if is_account_error(e):
                raise AccountFailureError(self.name, e, phase=f"{self.name}:download_photo") from e
            self.stats['errors'] += 1
            log_error(f"❌ [{self.name}] Error downloading photo: {e}")
    
    async def download_supported_document(
        self,
        client,
        message,
        group_name: str,
        account_name: Optional[str] = None,
        clients_map: Optional[Dict[str, Any]] = None,
        account_health: Optional[AccountHealthPolicy] = None,
    ) -> None:
        """Download only supported document-backed media types."""
        try:
            doc = message.media.document
            if not doc:
                return

            media_kind = self.classify_document_media(doc)
            if media_kind is None:
                self.stats['unsupported_skipped'] += 1
                return

            media_type, ext = media_kind
            filename = self.generate_filename(message, media_type, ext, group_name)
            filepath = self.save_path / self.get_safe_group_folder(group_name) / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)

            if filepath.exists():
                file_hash = self.calculate_hash(filepath)
                if file_hash in self.downloaded_hashes:
                    self.stats['duplicates_skipped'] += 1
                    return

            await self._download_with_fallback(client, message, filepath, account_name, clients_map, account_health)
            file_hash = self.calculate_hash(filepath)
            self.state.save_hash(file_hash, str(filepath), filepath.stat().st_size)
            self.downloaded_hashes.add(file_hash)
            self.stats['media_downloaded'] += 1
            log_success(f"🎬 [{self.name}] Downloaded: {filename}")
        except Exception as e:
            if is_account_error(e):
                raise AccountFailureError(self.name, e, phase=f"{self.name}:download_document") from e
            self.stats['errors'] += 1
            log_error(f"❌ [{self.name}] Error downloading document: {e}")

    def classify_document_media(self, doc: Any) -> Optional[Tuple[str, str]]:
        """Return supported media type/extension for a document, or None if unsupported."""
        return classify_supported_document_media(doc)
    
    def generate_filename(self, message, media_type: str, extension: str, group_name: str) -> str:
        """Generate a unique filename for media"""
        # Format: [type]_msg[msgid]_[timestamp].[ext]
        timestamp = message.date.strftime('%Y%m%d_%H%M%S') if message.date else 'unknown'
        return f"{media_type}_msg{message.id}_{timestamp}.{extension}"

    def get_safe_group_folder(self, group_name: str) -> str:
        """Normalize group names so they are valid folder names across platforms."""
        safe_name = "".join(c for c in group_name if c.isalnum() or c in (" ", "-", "_")).strip()
        return safe_name[:80] or "unknown_group"
    
    def calculate_hash(self, filepath: Path) -> str:
        """Calculate SHA256 hash of a file"""
        hasher = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
