from __future__ import annotations

import sys
import os as _os
from pathlib import Path

import streamlit as st

from media_archival.config import settings
from media_archival.database import database

sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..', '..', '..'))
from shared.live_config import ConfigOverlay
from shared.dashboard_live_config import render_live_config_panel

_overlay = ConfigOverlay(settings, "media_archival", settings.REDIS_URL)


st.set_page_config(page_title="Media Archival Dashboard", layout="wide")


async def _render_async() -> None:
    database.pool = None
    await database.connect()
    st.title("Media Archival")
    st.caption("Phase D operational dashboard")

    cursor = await database.get_media_cursor()
    backlog_rows = await database.get_pending_media_messages(cursor, 200)
    expiring_rows = await database.list_expiring_media(settings.MEDIA_REDOWNLOAD_LOOKAHEAD_HOURS)

    c1, c2, c3 = st.columns(3)
    c1.metric("Download queue depth", len(backlog_rows))
    storage_root = Path(settings.MEDIA_STORAGE_PATH)
    total_bytes = 0
    if storage_root.exists():
        for file in storage_root.rglob("*"):
            if file.is_file() and not file.is_symlink():
                total_bytes += file.stat().st_size
    c2.metric("Storage used", f"{total_bytes / 1024 / 1024:.2f} MB")
    c3.metric("Expiring media", len(expiring_rows))

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Backlog preview")
        st.dataframe([dict(r) for r in backlog_rows], use_container_width=True)
    with col2:
        st.subheader("Recent expiring media")
        st.dataframe([dict(r) for r in expiring_rows], use_container_width=True)

    st.subheader("Cleanup history")
    st.info("Cleanup history is recorded in service logs and Prometheus counters.")

    st.divider()
    with st.expander("⚙️ Live Config", expanded=False):
        render_live_config_panel(_overlay, "media_archival")

    await database.close()


def main() -> None:
    import asyncio

    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.caption("Manually refresh all dashboard data.")

    asyncio.run(_render_async())


if __name__ == "__main__":
    main()
