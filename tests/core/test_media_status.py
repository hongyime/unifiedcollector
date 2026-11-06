import pytest
from pathlib import Path

from src.core.media_status import list_realtime_media_deliveries, list_recent_media, resolve_private_media_path


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetch(self, sql, *args):
        assert "media_items" in sql
        assert args == ("instagram", 30)
        return [{"source": "instagram", "filename": "post.jpg", "content_type": "image", "file_size": 5}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_recent_media_uses_bounded_source_query():
    rows = await list_recent_media(_Pool(), source="instagram", limit=30)

    assert rows[0]["filename"] == "post.jpg"


def test_private_media_path_stays_under_the_readonly_media_root(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    item = media_root / "item.jpg"
    item.write_bytes(b"x")

    assert resolve_private_media_path(str(item), roots=(media_root,)) == item
    with pytest.raises(ValueError, match="outside"):
        resolve_private_media_path(str(tmp_path / "outside.jpg"), roots=(media_root,))


@pytest.mark.asyncio
async def test_realtime_delivery_listing_is_bounded_and_source_scoped():
    class _DeliveryAcquire(_Acquire):
        async def fetchval(self, *_args):
            return "realtime_media_deliveries"

        async def fetch(self, sql, *args):
            assert "realtime_media_deliveries WHERE source=$1" in sql
            assert args == ("whatsapp", 500)
            return [{"source": "whatsapp", "status": "skipped"}]

    class _DeliveryPool:
        def acquire(self):
            return _DeliveryAcquire()

    payload = await list_realtime_media_deliveries(_DeliveryPool(), source="WhatsApp", limit=999)

    assert payload == {"available": True, "items": [{"source": "whatsapp", "status": "skipped"}]}
