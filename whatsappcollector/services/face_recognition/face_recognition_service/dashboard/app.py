from __future__ import annotations

import sys
import os as _os
from typing import Any

from ..config import settings
from ..database import database
from ..matcher import identity_matcher
from ..processor import face_processor

sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..', '..', '..', '..'))
from shared.live_config import ConfigOverlay
from shared.dashboard_live_config import render_live_config_panel

_overlay = ConfigOverlay(settings, "face_recognition", settings.REDIS_URL)


async def get_identity_gallery(limit: int = 100, sort_by: str = "last_seen") -> list[dict[str, Any]]:
    rows = await database.list_identities(limit=limit, sort_by=sort_by)
    return [dict(row) for row in rows]


async def search_faces_from_image(image_path: str, limit: int = 5) -> list[dict[str, Any]]:
    embeddings = face_processor.process_media_file(image_path, "image/jpeg")
    if not embeddings:
        return []
    rows = await database.search_identities(embeddings[0].embedding, limit=limit)
    return [dict(row) for row in rows]


async def rename_identity(identity_id: str, label: str) -> None:
    await identity_matcher.rename_identity(identity_id, label)


async def merge_identities(source_identity_id: str, target_identity_id: str) -> None:
    await identity_matcher.merge_identities(source_identity_id, target_identity_id)


async def split_identity(identity_id: str, embedding_ids: list[int], new_label: str = "Unknown") -> str:
    return await identity_matcher.split_identity(identity_id, embedding_ids, new_label=new_label)


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Face Recognition", layout="wide")
    st.title("Face Recognition")
    st.caption(f"Models: {settings.FACE_MODELS_PATH}")

    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.caption("Manually refresh all dashboard data.")

    st.info("Run the service worker to populate identities. Use the controls below to manage identities.")

    gallery_col, controls_col = st.columns(2)
    with gallery_col:
        st.subheader("Identity gallery")
        st.write("Use the helper functions in this module for automated tests or integrations.")
    with controls_col:
        st.subheader("Identity Management")
        st.write("Use rename, merge, and split operations to manage detected identities.")

    st.divider()
    with st.expander("⚙️ Live Config", expanded=False):
        render_live_config_panel(_overlay, "face_recognition")


if __name__ == "__main__":
    main()
