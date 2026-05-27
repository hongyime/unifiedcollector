#!/usr/bin/env python3
"""
Mock Telegram client fixtures for testing.
Provides simulated TelegramClient behavior to avoid real API calls.
"""
from typing import Optional, List, Any, AsyncIterator
from unittest.mock import MagicMock, AsyncMock
from unittest.mock import patch


class MockTelegramClient:
    """
    Mock Telegram client that simulates Telethon client behavior.
    """
    
    def __init__(self, *args, **kwargs):
        self.is_connected = False
        self.is_user_authorized = True
        self.session_path = kwargs.get('session', 'test_session')
        self._mock_data = {}
        
    async def start(self, phone: str) -> None:
        """Mock start method"""
        self.is_connected = True
        
    async def disconnect(self) -> None:
        """Mock disconnect method"""
        self.is_connected = False
        
    async def is_user_authorized(self) -> bool:
        """Mock authorization check"""
        return True
        
    async def get_me(self) -> MagicMock:
        """Mock getting current user info"""
        me = MagicMock()
        me.id = 123456
        me.first_name = "Test"
        me.last_name = "User"
        me.username = "test_user"
        me.phone = "+1234567890"
        return me
        
    async def get_entity(self, entity: Any) -> MagicMock:
        """Mock getting entity info"""
        entity_obj = MagicMock()
        entity_obj.id = 123456
        entity_obj.name = "Test Entity"
        entity_obj.title = "Test Entity Title"
        return entity_obj
        
    async def iter_dialogs(self) -> AsyncIterator[MagicMock]:
        """Mock iterating over dialogs"""
        for i in range(1, 4):
            dialog = MagicMock()
            chat = MagicMock()
            chat.id = i * 1000
            chat.title = f"Test Chat {i}"
            chat.name = f"test_chat_{i}"
            dialog.entity = chat
            dialog.name = chat.title
            yield dialog
            
    async def get_messages(self, entity: Any, limit: int = 100) -> List[MagicMock]:
        """Mock getting messages"""
        messages = []
        for i in range(1, limit + 1):
            msg = MagicMock()
            msg.id = i
            msg.text = f"Test message {i}"
            msg.date = MagicMock()
            msg.sender_id = 123456
            msg.chat_id = entity.id if entity else 1000
            msg.media = None
            msg.photo = None
            msg.document = None
            messages.append(msg)
        return messages
        
    async def download_media(self, message: Any, file_path: Optional[str] = None) -> Optional[str]:
        """Mock downloading media"""
        return file_path or "test_download.jpg"
        
    async def download_profile_photo(
        self,
        entity: Any,
        file_path: Optional[str] = None
    ) -> Optional[str]:
        """Mock downloading profile photo"""
        return file_path or "test_profile_photo.jpg"
        
    async def join_channel(self, channel: Any) -> MagicMock:
        """Mock joining a channel"""
        joined = MagicMock()
        joined.chats = [MagicMock()]
        return joined
        
    async def leave_chat(self, chat: Any) -> None:
        """Mock leaving a chat"""
        pass
        
    async def send_message(
        self,
        entity: Any,
        message: str,
        **kwargs
    ) -> MagicMock:
        """Mock sending a message"""
        msg = MagicMock()
        msg.id = 999999
        msg.text = message
        msg.date = MagicMock()
        return msg
        
    async def send_file(
        self,
        entity: Any,
        file: Any,
        **kwargs
    ) -> MagicMock:
        """Mock sending a file"""
        msg = MagicMock()
        msg.id = 999998
        msg.file = MagicMock()
        return msg


def create_mock_client(*args, **kwargs) -> MockTelegramClient:
    """
    Create a mock Telegram client instance.
    
    Returns:
        MockTelegramClient instance
    """
    return MockTelegramClient(*args, **kwargs)


def patch_telegram_client():
    """
    Create a patch context manager for mocking TelegramClient.
    
    Usage:
        with patch_telegram_client():
            # Code that uses TelegramClient will use the mock
            client = TelegramClient('test.session', api_id, api_hash)
    
    Returns:
        Context manager for patching
    """
    def mock_factory(*args, **kwargs):
        return MockTelegramClient(*args, **kwargs)
    
    return patch('telethon.TelegramClient', side_effect=mock_factory)


def patch_connect_function():
    """
    Patch the connect function from toolkit.core.parallel_processor.
    
    Returns:
        Context manager for patching connect
    """
    def mock_connect_function(info, progress_callback=None):
        """
        Mock connect function that returns a mock client.
        
        Args:
            info: Account info dictionary
            progress_callback: Progress callback (ignored)
            
        Returns:
            MockTelegramClient instance
        """
        client = MockTelegramClient(
            sessions_path='test_sessions',
            session_name=f"test_{info['name']}"
        )
        client.account_info = info
        return client
    
    return patch('src.core.parallel_processor.connect', side_effect=mock_connect_function)


if __name__ == "__main__":
    # Test the mock client
    print("Testing Mock Telegram Client...")
    import asyncio
    
    async def test_client():
        client = create_mock_client()
        print(f"Created mock client: {client}")
        
        await client.start("+1234567890")
        print(f"Client started: {client.is_connected}")
        
        me = await client.get_me()
        print(f"Me: {me.first_name} {me.last_name} (@{me.username})")
        
        await client.disconnect()
        print(f"Client disconnected: {not client.is_connected}")
    
    asyncio.run(test_client())
    print("\n✅ Mock client tests passed!")
