import pytest
import numpy as np
import uuid
from unittest.mock import patch, MagicMock
import importlib.util

if importlib.util.find_spec('identity_matcher') is None:
    pytest.skip('legacy identity_matcher module not available in current architecture', allow_module_level=True)

class FakeRow:
    def __init__(self, _id, distance):
        self.identity_id = _id
        self.id = _id
        self.distance = distance

class FakeResult:
    def __init__(self, row):
        self.row = row
    def fetchone(self):
        return self.row

class FakeEntity:
    def __init__(self):
        self.centroid = np.zeros(128).tolist()
        self.occurrence_count = 1

class FakeSession:
    def __init__(self, row=None, entity=None):
        self.fake_row = row
        self.fake_entity = entity
        self.add_called = False
        self.commit_called = False

    async def execute(self, *args, **kwargs):
        return FakeResult(self.fake_row)

    async def get(self, *args, **kwargs):
        return self.fake_entity

    def add(self, *args, **kwargs):
        self.add_called = True

    async def commit(self, *args, **kwargs):
        self.commit_called = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

@pytest.mark.asyncio
async def test_identity_matcher_match_existing():
    from identity_matcher import identity_matcher
    
    fake_row = FakeRow(_id=uuid.uuid4(), distance=0.1)
    fake_session = FakeSession(row=fake_row, entity=FakeEntity())
    
    with patch('identity_matcher.AsyncSessionLocal', return_value=fake_session), \
         patch('identity_matcher.insert') as mock_insert:
        identity_id, is_new = await identity_matcher.match(
            embedding=np.zeros(128),
            sender_jid='user1',
            sender_lid='lid1',
            media_id=str(uuid.uuid4())
        )
        
        assert identity_id == fake_row.id
        assert is_new is False
        assert fake_session.commit_called

@pytest.mark.asyncio
async def test_identity_matcher_match_new():
    from identity_matcher import identity_matcher
    
    fake_session = FakeSession(row=None)
    
    with patch('identity_matcher.AsyncSessionLocal', return_value=fake_session), \
         patch('identity_matcher.insert') as mock_insert:
        identity_id, is_new = await identity_matcher.match(
            embedding=np.zeros(128),
            sender_jid='user1',
            sender_lid='lid1',
            media_id=str(uuid.uuid4())
        )
        
        assert is_new is True
        assert identity_id is not None
        assert fake_session.add_called
        assert fake_session.commit_called
