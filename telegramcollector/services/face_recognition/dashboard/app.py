"""
Face Recognition Dashboard — Streamlit app (Phase 6)

Panels:
  - Processing Control  (start/stop, cursor, rate, status)
  - Threshold Controls  (similarity / quality thresholds)
  - Dead Letter Queue   (DLQ viewer + retry)
  - Identity Gallery    (browse / search / sort identities)
  - Identity Detail     (merge / split / rename)
  - Face Search         (placeholder — requires processing service)
"""
import logging
import os

import psycopg2
import psycopg2.extras
import redis as redis_lib
import streamlit as st

from shared.config import get_dynamic_setting, set_dynamic_setting, get_hub_group_id
from shared.dashboard_styles import inject_global_styles, render_service_nav

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Live keys for the config panel
# ---------------------------------------------------------------------------

FACE_LIVE_KEYS = {
    "FACE_PROCESSING_ENABLED",
    "FACE_SIMILARITY_THRESHOLD",
    "FACE_MIN_QUALITY_THRESHOLD",
    "USER_INTEL_PROCESSING_ENABLED",
    "USER_INTEL_NETWORK_ENABLED",
}

# ---------------------------------------------------------------------------
# Page config — MUST be the first Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Face Recognition", layout="wide")
inject_global_styles()

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_connection():
    """Return a new psycopg2 connection."""
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "postgres"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "telegramcollector"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def db_fetch(sql: str, *args) -> list[dict]:
    """Execute sql and return all rows as dicts."""
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, args if args else None)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return rows


def db_fetchone(sql: str, *args) -> dict | None:
    """Execute sql and return one row as dict, or None."""
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, args if args else None)
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    return dict(row) if row else None


def db_execute(sql: str, *args) -> None:
    """Execute sql (INSERT/UPDATE/DELETE) and commit."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, args if args else None)
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Shared Redis client (cached across Streamlit reruns)
# ---------------------------------------------------------------------------

@st.cache_resource
def _get_redis_client() -> redis_lib.Redis | None:
    """Create and return a shared Redis client, or None if unavailable."""
    try:
        client = redis_lib.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=int(os.environ.get("REDIS_DB", "0")),
            password=os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        client.ping()
        return client
    except Exception as exc:
        logger.warning("Redis unavailable for dashboard: %s", exc)
        return None


def get_redis() -> redis_lib.Redis | None:
    return _get_redis_client()


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

def _init_session_state():
    if "page" not in st.session_state:
        st.session_state.page = "processing_control"
    if "selected_identity" not in st.session_state:
        st.session_state.selected_identity = None


def _sidebar_nav():
    st.sidebar.title("Face Recognition")
    if st.sidebar.button("🔄 Refresh", key="nav_refresh"):
        st.rerun()
    st.sidebar.divider()
    pages = {
        "processing_control": "⚙️ Processing Control",
        "thresholds": "🎚️ Threshold Controls",
        "dlq": "📋 Dead Letter Queue",
        "gallery": "👥 Identity Gallery",
        "detail": "🔍 Identity Detail",
        "face_search": "🖼️ Face Search",
        "management": "🔧 Management",
        "config": "⚙️ Config",
    }
    for key, label in pages.items():
        if st.sidebar.button(label, key=f"nav_{key}", use_container_width=True):
            st.session_state.page = key
            st.rerun()
    render_service_nav("Face Recognition")
    return st.session_state.page


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def render_processing_control():
    st.header("⚙️ Processing Control")

    # Read current enabled state from Redis (dynamic setting)
    enabled_raw = get_dynamic_setting(
        "FACE_PROCESSING_ENABLED",
        os.environ.get("FACE_PROCESSING_ENABLED", "true"),
    )
    if isinstance(enabled_raw, str):
        is_enabled = enabled_raw.lower() in ("true", "1", "yes")
    else:
        is_enabled = bool(enabled_raw)

    if is_enabled:
        st.markdown('<span style="color:green; font-size:1.2em;">● Running</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span style="color:red; font-size:1.2em;">● Paused</span>', unsafe_allow_html=True)

    button_label = "Stop Processing" if is_enabled else "Start Processing"
    if st.button(button_label):
        set_dynamic_setting("FACE_PROCESSING_ENABLED", str(not is_enabled))
        st.rerun()

    st.divider()

    try:
        row = db_fetchone(
            "SELECT last_message_id FROM collector.service_cursors WHERE service_name = 'face_recognition'"
        )
        cursor_pos = row["last_message_id"] if row else 0
    except Exception as exc:
        cursor_pos = None
        logger.warning("Failed to fetch cursor position: %s", exc)

    try:
        row = db_fetchone(
            "SELECT COUNT(*) AS cnt FROM face_recognition.processed_media "
            "WHERE processed_at >= NOW() - INTERVAL '60 seconds'"
        )
        rate_per_min = int(row["cnt"]) if row else 0
    except Exception as exc:
        rate_per_min = None
        logger.warning("Failed to fetch processing rate: %s", exc)

    col1, col2 = st.columns(2)
    col1.metric("Cursor Position", cursor_pos if cursor_pos is not None else "N/A")
    col2.metric("Processing Rate (msgs/min)", rate_per_min if rate_per_min is not None else "N/A")


def render_config_panel_page():
    from shared.config_manager import render_config_panel
    st.header("⚙️ Config")
    render_config_panel("face_recognition", FACE_LIVE_KEYS)


def render_threshold_controls():
    st.header("🎚️ Threshold Controls")
    from shared.config_manager import render_config_panel
    render_config_panel("face_recognition", FACE_LIVE_KEYS)


def render_dlq():
    import json
    import pandas as pd
    from collections import defaultdict

    st.header("📋 Dead Letter Queue")

    redis_client = get_redis()
    if redis_client is None:
        st.warning("Redis is unavailable — cannot display DLQ.")
        return

    DLQ_KEY = "face_recognition:dlq"
    RETRY_KEY = "face_recognition:dlq:retry"
    PERMANENT_RETRY_THRESHOLD = 5

    try:
        raw_items = redis_client.lrange(DLQ_KEY, 0, -1)
    except Exception as exc:
        st.error(f"Failed to read DLQ from Redis: {exc}")
        return

    items = []
    for raw in raw_items:
        try:
            items.append(json.loads(raw))
        except Exception:
            pass

    if not items:
        st.success("DLQ is empty — no failed messages.")
        return

    def is_permanent(item: dict) -> bool:
        return item.get("is_permanent", False) or item.get("retry_count", 0) >= PERMANENT_RETRY_THRESHOLD

    total = len(items)
    permanent_items = [i for i in items if is_permanent(i)]
    transient_items = [i for i in items if not is_permanent(i)]

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Pending", total)
    col2.metric("Transient (retryable)", len(transient_items))
    col3.metric("Permanent (failed)", len(permanent_items))

    st.divider()

    if transient_items:
        if st.button("🔄 Retry All Transient", type="primary"):
            try:
                pipe = redis_client.pipeline()
                for item in transient_items:
                    item_copy = dict(item)
                    item_copy["retry_count"] = 0
                    pipe.rpush(RETRY_KEY, json.dumps(item_copy))
                for item in transient_items:
                    pipe.lrem(DLQ_KEY, 1, json.dumps(item))
                pipe.execute()
                st.success(f"Re-queued {len(transient_items)} transient item(s) for retry.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed to re-queue transient items: {exc}")
    else:
        st.info("No transient items to retry.")

    st.divider()

    st.subheader("Breakdown by Error Type")
    grouped: dict[str, dict] = defaultdict(lambda: {"transient": 0, "permanent": 0, "total": 0})
    for item in items:
        error_type = item.get("error_type") or "unknown"
        grouped[error_type]["total"] += 1
        if is_permanent(item):
            grouped[error_type]["permanent"] += 1
        else:
            grouped[error_type]["transient"] += 1

    summary_rows = [
        {"error_type": et, "total": c["total"], "transient": c["transient"], "permanent": c["permanent"]}
        for et, c in sorted(grouped.items(), key=lambda x: -x[1]["total"])
    ]
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    with st.expander("View all DLQ items"):
        rows = [
            {
                "message_id": item.get("message_id", ""),
                "file_unique_id": item.get("file_unique_id", ""),
                "error_type": item.get("error_type", ""),
                "error_message": item.get("error_message", ""),
                "retry_count": item.get("retry_count", 0),
                "is_permanent": is_permanent(item),
                "created_at": item.get("created_at", ""),
            }
            for item in items
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_identity_gallery():
    st.header("👥 Identity Gallery")

    col_search, col_sort = st.columns([3, 1])
    with col_search:
        search_text = st.text_input("Search by label", placeholder="Type to filter…", key="gallery_search")
    with col_sort:
        sort_order = st.selectbox("Sort by", options=["face_count DESC", "updated_at DESC"], key="gallery_sort")

    order_clause = "face_count DESC" if sort_order == "face_count DESC" else "updated_at DESC"

    try:
        if search_text.strip():
            rows = db_fetch(
                f"SELECT id, label, face_count, message_count, updated_at "
                f"FROM face_recognition.telegram_topics WHERE label ILIKE %s ORDER BY {order_clause}",
                f"%{search_text.strip()}%",
            )
        else:
            rows = db_fetch(
                f"SELECT id, label, face_count, message_count, updated_at "
                f"FROM face_recognition.telegram_topics ORDER BY {order_clause}"
            )
    except Exception as exc:
        st.error(f"Failed to load identities: {exc}")
        return

    if not rows:
        st.info("No identities found." if not search_text else f"No identities matching '{search_text}'.")
        return

    st.caption(f"{len(rows)} identit{'y' if len(rows) == 1 else 'ies'} found")
    st.divider()

    for row in rows:
        col_info, col_btn = st.columns([5, 1])
        with col_info:
            updated = row["updated_at"].strftime("%Y-%m-%d %H:%M") if row["updated_at"] else "—"
            st.markdown(
                f"**{row['label']}** &nbsp; "
                f"<span style='color:grey'>faces: {row['face_count']} &nbsp;|&nbsp; "
                f"messages: {row['message_count']} &nbsp;|&nbsp; updated: {updated}</span>",
                unsafe_allow_html=True,
            )
        with col_btn:
            if st.button("View", key=f"gallery_row_{row['id']}"):
                st.session_state.selected_identity = row["id"]
                st.session_state.page = "detail"
                st.rerun()


def render_identity_detail():
    st.header("🔍 Identity Detail")

    if st.button("← Back to Gallery"):
        st.session_state.page = "gallery"
        st.rerun()

    if st.session_state.selected_identity is None:
        st.warning("No identity selected. Go to the Identity Gallery and click a row.")
        return

    identity_id: int = st.session_state.selected_identity

    try:
        identity = db_fetchone(
            "SELECT id, topic_id, label, face_count, message_count, updated_at "
            "FROM face_recognition.telegram_topics WHERE id = %s",
            identity_id,
        )
    except Exception as exc:
        st.error(f"Failed to load identity: {exc}")
        return

    if identity is None:
        st.error(f"Identity ID {identity_id} not found.")
        return

    col_label, col_link = st.columns([4, 1])
    with col_label:
        st.subheader(f"{identity['label']}  (ID: {identity_id})")
        st.caption(
            f"Faces: {identity['face_count']} | "
            f"Messages: {identity['message_count']} | "
            f"Updated: {identity['updated_at'].strftime('%Y-%m-%d %H:%M') if identity['updated_at'] else '—'}"
        )
    with col_link:
        telegram_topic_id = identity["topic_id"]
        hub_group_id = get_hub_group_id()
        if telegram_topic_id and hub_group_id:
            topic_url = f"https://t.me/c/{abs(hub_group_id)}/{telegram_topic_id}"
            st.link_button("📨 Open in Telegram", topic_url)
        else:
            st.caption("No Telegram topic yet")

    st.divider()

    tab_media, tab_merge, tab_split, tab_rename = st.tabs(["Media", "Merge", "Split", "Rename"])

    with tab_media:
        st.subheader("Uploaded Media")
        try:
            media_rows = db_fetch(
                "SELECT id, source_chat_id, source_message_id, topic_id, hub_message_id, created_at "
                "FROM face_recognition.uploaded_media WHERE topic_id = %s ORDER BY created_at DESC",
                identity_id,
            )
        except Exception as exc:
            st.error(f"Failed to load media: {exc}")
            media_rows = []

        if not media_rows:
            st.info("No uploaded media for this identity.")
        else:
            import pandas as pd
            df = pd.DataFrame([
                {
                    "id": r["id"],
                    "source_chat_id": r["source_chat_id"],
                    "source_message_id": r["source_message_id"],
                    "hub_message_id": r["hub_message_id"],
                    "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else "",
                }
                for r in media_rows
            ])
            st.dataframe(df, use_container_width=True, hide_index=True)

    with tab_merge:
        st.subheader("Merge into another Identity")
        st.caption("All embeddings and media for this identity will be moved to the target. This identity will be deleted.")
        st.info("Use the API to perform merge operations: `POST /api/corrections/merge`")

    with tab_split:
        st.subheader("Split selected embeddings into a new Identity")
        try:
            emb_rows = db_fetch(
                "SELECT id, quality_score, frame_index, source_message_id, detection_timestamp "
                "FROM face_recognition.face_embeddings WHERE topic_id = %s ORDER BY quality_score DESC",
                identity_id,
            )
        except Exception as exc:
            st.error(f"Failed to load embeddings: {exc}")
            emb_rows = []

        if not emb_rows:
            st.info("No embeddings found for this identity.")
        else:
            emb_options = {
                f"ID {r['id']} — quality {r['quality_score']:.3f} (msg {r['source_message_id']}, frame {r['frame_index']})": r["id"]
                for r in emb_rows
            }
            selected_labels = st.multiselect(
                "Select embeddings to split off",
                options=list(emb_options.keys()),
                key="split_embedding_select",
            )
            if st.button("Split", type="primary", key="split_btn"):
                st.info("Use the API to perform split operations: `POST /api/corrections/split`")

    with tab_rename:
        st.subheader("Rename this Identity")
        new_label = st.text_input("New label", value=identity["label"], key="rename_label_input")
        if st.button("Rename", type="primary", key="rename_btn"):
            new_label = new_label.strip()
            if not new_label:
                st.error("Label cannot be empty.")
            elif new_label == identity["label"]:
                st.info("Label is unchanged.")
            else:
                try:
                    db_execute(
                        "UPDATE face_recognition.telegram_topics SET label = %s WHERE id = %s",
                        new_label, identity_id,
                    )
                    st.success(f"Renamed to '{new_label}'.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Rename failed: {exc}")


def render_face_search():
    st.header("🖼️ Face Search")
    st.info(
        "Face search requires the face recognition processing service to be running. "
        "Use the API endpoint `POST /api/search` to search for matching identities."
    )


def render_management():
    st.header("🔧 Management")

    st.subheader("Reset Progress")
    st.caption("Truncates all face_recognition tables and resets the service cursor to 0.")
    st.warning("⚠️ This will delete all face embeddings, topics, and processed media records.")
    confirm_reset = st.text_input("Type 'RESET' to confirm", key="fr_confirm_reset")
    if st.button("Reset Progress", key="fr_btn_reset", type="primary"):
        if confirm_reset == "RESET":
            try:
                db_execute("TRUNCATE face_recognition.face_embeddings CASCADE")
                db_execute("TRUNCATE face_recognition.uploaded_media CASCADE")
                db_execute("TRUNCATE face_recognition.processed_media CASCADE")
                db_execute("TRUNCATE face_recognition.telegram_topics CASCADE")
                db_execute(
                    "UPDATE collector.service_cursors SET last_message_id=0, updated_at=NOW() "
                    "WHERE service_name='face_recognition'"
                )
                st.success("Face recognition progress reset.")
            except Exception as exc:
                st.error(f"Reset failed: {exc}")
        else:
            st.error("Confirmation text does not match.")

    st.divider()

    st.subheader("Delete Identity")
    st.caption("Delete a specific identity and all its embeddings.")

    try:
        topics = db_fetch(
            "SELECT id, label, face_count FROM face_recognition.telegram_topics ORDER BY label"
        )
    except Exception as exc:
        topics = []
        st.error(f"Failed to load identities: {exc}")

    if topics:
        topic_options = {f"{t['label']} (ID: {t['id']}, faces: {t['face_count']})": t["id"] for t in topics}
        selected_topic_label = st.selectbox("Select identity to delete", options=list(topic_options.keys()), key="fr_delete_topic")
        if st.button("Delete Identity", key="fr_btn_delete_topic"):
            topic_id = topic_options[selected_topic_label]
            try:
                db_execute("DELETE FROM face_recognition.telegram_topics WHERE id = %s", topic_id)
                st.success("Identity deleted.")
            except Exception as exc:
                st.error(f"Delete failed: {exc}")
    else:
        st.info("No identities found.")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    _init_session_state()
    page = _sidebar_nav()

    if page == "processing_control":
        render_processing_control()
    elif page == "thresholds":
        render_threshold_controls()
    elif page == "dlq":
        render_dlq()
    elif page == "gallery":
        render_identity_gallery()
    elif page == "detail":
        render_identity_detail()
    elif page == "face_search":
        render_face_search()
    elif page == "management":
        render_management()
    elif page == "config":
        render_config_panel_page()
    else:
        render_processing_control()


if __name__ == "__main__":
    main()
