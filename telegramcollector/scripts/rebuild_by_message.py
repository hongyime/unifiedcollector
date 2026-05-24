"""One-shot: create all missing by_message hardlinks from existing by_id files.

Uses the DB to map file_unique_id → chat_id/message_id/ext, then creates
hardlinks instantly without needing the media download queue.

Run inside the collector container:
  python /tmp/rebuild_by_message.py
"""
import asyncio
import os
import sys

sys.path.insert(0, "/app")

from shared.config import settings


EXT_MAP = {"photo": "jpg", "video": "mp4", "circle_video": "mp4"}
BASE = settings.MEDIA_STORE_PATH  # /mnt/hdd/media


def by_id_path(file_unique_id: str, ext: str) -> str:
    return os.path.join(BASE, "by_id", f"{file_unique_id}.{ext.lstrip('.')}")


def by_msg_path(chat_id: int, message_id: int, ext: str) -> str:
    parent = os.path.join(BASE, "by_message", str(chat_id))
    os.makedirs(parent, exist_ok=True)
    return os.path.join(parent, f"{message_id}.{ext.lstrip('.')}")


async def main() -> None:
    import asyncpg

    dsn = (
        f"postgresql://{settings.DB_USER}:{settings.DB_PASSWORD}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)

    rows = await pool.fetch(
        """
        SELECT chat_id, message_id, file_unique_id, message_type
          FROM collector.raw_messages
         WHERE has_media = TRUE
           AND file_unique_id IS NOT NULL
           AND message_type IN ('photo', 'video', 'circle_video')
           AND media_path IS NOT NULL
        ORDER BY id ASC
        """
    )

    linked = 0
    skipped_no_by_id = 0
    already_exists = 0

    for row in rows:
        ext = EXT_MAP.get(row["message_type"], "bin")
        bid = by_id_path(str(row["file_unique_id"]), ext)
        bmsg = by_msg_path(row["chat_id"], row["message_id"], ext)

        if os.path.exists(bmsg):
            already_exists += 1
            continue

        if not os.path.exists(bid):
            skipped_no_by_id += 1
            continue

        try:
            os.link(bid, bmsg)
            linked += 1
        except Exception as e:
            print(f"hardlink failed {bid} → {bmsg}: {e}")

    print(
        f"Done. linked={linked} already_existed={already_exists} "
        f"no_by_id={skipped_no_by_id} total={len(rows)}"
    )
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
