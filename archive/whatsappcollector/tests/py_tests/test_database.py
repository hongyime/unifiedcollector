import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import importlib.util
import importlib

if importlib.util.find_spec('database') is None:
    pytest.skip('legacy database module not available in current architecture', allow_module_level=True)

database_module = importlib.import_module('database')
if not hasattr(database_module, 'AsyncSessionLocal'):
    pytest.skip('legacy database module API not present in current architecture', allow_module_level=True)

@pytest.mark.asyncio
async def test_database_health_check():
    with patch('database.AsyncSessionLocal') as mock_session_maker:
        mock_session = AsyncMock()
        mock_session_maker.return_value.__aenter__.return_value = mock_session
        
        # Mock result of execute
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1
        mock_session.execute.return_value = mock_result
        
        from database import database
        result = await database.health_check()
        assert result is True

@pytest.mark.asyncio
async def test_database_upsert_message():
    with patch('database.AsyncSessionLocal') as mock_session_maker:
        mock_session = AsyncMock()
        mock_session_maker.return_value.__aenter__.return_value = mock_session
        
        from database import database
        await database.upsert_message({
            'message_id': 'msg1',
            'chat_jid': 'chat1',
            'sender_jid': 'user1',
            'sender_lid': 'lid1',
            'timestamp': 1600000000,
            'message_type': 'text'
        })
        
        mock_session.execute.assert_called()
        mock_session.commit.assert_awaited()
