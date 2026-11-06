import pytest

from src.core.profile_posts import get_profile_posts


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "lemon8_profiles"

    async def fetchrow(self, *_args):
        return {"id": "profile-id", "username": "creator"}

    async def fetch(self, _sql, *args):
        assert args == ("profile-id", 30)
        return [{"platform_post_id": "post", "title": "Title", "like_count": 2}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_lemon8_profile_posts_builds_canonical_permalink():
    payload = await get_profile_posts(_Pool(), source="lemon8", username="creator", limit=30)

    assert payload["profile"]["username"] == "creator"
    assert payload["posts"][0]["post_url"] == "https://www.lemon8-app.com/creator/post/post"


@pytest.mark.asyncio
async def test_tiktok_profile_posts_builds_canonical_permalink():
    class _TikTokAcquire(_Acquire):
        async def fetchrow(self, *_args):
            return {"id": "profile-id", "username": "creator"}

        async def fetch(self, _sql, *args):
            assert args == ("profile-id", 30)
            return [{"platform_post_id": "post", "media_source_url": None}]

    class _TikTokPool:
        def acquire(self):
            return _TikTokAcquire()

    payload = await get_profile_posts(_TikTokPool(), source="tiktok", username="creator", limit=30)

    assert payload["posts"][0]["post_url"] == "https://www.tiktok.com/@creator/video/post"


@pytest.mark.asyncio
async def test_threads_profile_posts_builds_canonical_permalink():
    class _ThreadsAcquire(_Acquire):
        async def fetchrow(self, *_args):
            return {"username": "creator"}

        async def fetch(self, _sql, *args):
            assert args == ("creator", 30)
            return [{"platform_post_id": "post"}]

    class _ThreadsPool:
        def acquire(self):
            return _ThreadsAcquire()

    payload = await get_profile_posts(_ThreadsPool(), source="threads", username="creator", limit=30)

    assert payload["posts"][0]["post_url"] == "https://www.threads.net/@creator/post/post"
