"""One-shot script: re-enqueue all media messages so MediaStore recreates by_message links.

For files already in by_id: MediaStore dedup check fires, creates hardlink, updates media_path.
For missing files: MediaStore downloads them fresh.

Run from inside the collector container:
  python scripts/rebuild_media_links.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")

from shared.config import settings


async def main() -> None:
    import asyncpg
    import redis.asyncio as aioredis

    dsn = (
        f"postgresql://{settings.DB_USER}:{settings.DB_PASSWORD}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)

    password_part = f":{settings.REDIS_PASSWORD}@" if settings.REDIS_PASSWORD else ""
    redis_url = f"redis://{password_part}{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}"
    r = await aioredis.from_url(redis_url, decode_responses=False)

    ext_map = {"photo": "jpg", "video": "mp4", "circle_video": "mp4"}

    rows = await pool.fetch(
        """
        SELECT chat_id, message_id, file_unique_id, message_type
          FROM collector.raw_messages
         WHERE has_media = TRUE
           AND file_unique_id IS NOT NULL
           AND message_type IN ('photo', 'video', 'circle_video')
        ORDER BY id ASC
        """
    )

    print(f"Re-enqueueing {len(rows)} media tasks...")
    batch = []
    for row in rows:
        task = {
            "task_type": "media_download",
            "chat_id": row["chat_id"],
            "message_id": row["message_id"],
            "file_unique_id": str(row["file_unique_id"]),
            "ext": ext_map.get(row["message_type"], "bin"),
            "file_size": 0,
            "_retry_count": 0,
        }
        batch.append(json.dumps(task).encode())

        if len(batch) >= 500:
            await r.lpush("collector:media_download_queue", *batch)
            batch.clear()

    if batch:
        await r.lpush("collector:media_download_queue", *batch)

    print(f"Done. Queue depth: {await r.llen('collector:media_download_queue')}")
    await pool.close()
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
