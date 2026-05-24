"""
Bulk Sender Dashboard — Streamlit UI.

Serves on port 8505. Provides job creation, live progress monitoring,
and pause/resume/cancel controls for bulk send jobs.
"""

import os

import psycopg2
import psycopg2.extras
import streamlit as st
from shared.config_manager import render_config_panel
from shared.dashboard_styles import inject_global_styles, render_service_nav

from services.bulk_sender.job_manager import JobManager

# Build DSN from env vars
dsn = (
    f"postgresql://{os.environ.get('DB_USER', 'postgres')}:{os.environ.get('DB_PASSWORD', '')}"
    f"@{os.environ.get('DB_HOST', 'postgres')}:{os.environ.get('DB_PORT', '5432')}"
    f"/{os.environ.get('DB_NAME', 'telegramcollector')}"
)


@st.cache_resource
def get_job_manager() -> JobManager:
    return JobManager(dsn=dsn)


jm = get_job_manager()

st.set_page_config(page_title="Bulk Sender", layout="wide")
inject_global_styles()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Bulk Sender")
    if st.button("🔄 Refresh"):
        st.rerun()
    render_service_nav("Bulk Sender")

st.title("Bulk Sender Dashboard")

# ---- Job Creation Form ----
st.header("Create New Job")

with st.form("create_job_form"):
    # 1. Account dropdown from collector.telegram_accounts WHERE status='active'
    # Query accounts directly via psycopg2 (not through JobManager)
    try:
        conn = psycopg2.connect(dsn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, phone_number"
                " FROM collector.telegram_accounts"
                " WHERE status='active'"
                " ORDER BY phone_number"
            )
            accounts = cur.fetchall()
        conn.close()
    except Exception as e:
        accounts = []
        st.error(f"Could not load accounts: {e}")

    account_options = {
        f"{a['phone_number']} (id={a['id']})": a["id"] for a in accounts
    }
    selected_account_label = st.selectbox(
        "Account",
        options=list(account_options.keys()) if account_options else ["No active accounts"],
    )

    # 2. Target chat identifier
    target_chat = st.text_input("Target Chat (numeric ID or @username)")

    # 3. Source type
    source_type = st.radio("Source Type", options=["folder", "collector_query"])

    # 4. Source-specific inputs
    source_path = None
    collector_query = None
    preview_count = None

    if source_type == "folder":
        source_path = st.text_input("Folder Path")
    else:
        st.subheader("Collector Query Filters")
        chat_id_str = st.text_input("Chat ID (optional)")
        date_from = st.date_input("Date From (optional)", value=None)
        date_to = st.date_input("Date To (optional)", value=None)
        message_type = st.selectbox(
            "Message Type", options=["photo", "video", "document"], index=0
        )
        sender_id_str = st.text_input("Sender ID (optional)")

        collector_query = {
            "chat_id": int(chat_id_str) if chat_id_str.strip() else None,
            "date_from": str(date_from) if date_from else None,
            "date_to": str(date_to) if date_to else None,
            "message_type": message_type or None,
            "sender_id": int(sender_id_str) if sender_id_str.strip() else None,
        }

        try:
            preview_count = jm.count_collector_query(collector_query)
            st.info(f"Preview: {preview_count} matching files")
        except Exception as e:
            st.warning(f"Could not get preview count: {e}")

    submitted = st.form_submit_button("Create Job")

    if submitted:
        # Validation
        errors = []
        if not account_options or selected_account_label == "No active accounts":
            errors.append("Please select an active account.")
        if not target_chat.strip():
            errors.append("Please enter a target chat identifier.")
        if source_type == "folder" and not source_path:
            errors.append("Please enter a folder path.")
        if source_type == "collector_query" and collector_query is None:
            errors.append("Please configure collector query filters.")

        if errors:
            for err in errors:
                st.error(err)
        else:
            try:
                account_id = account_options.get(selected_account_label)

                # Parse target_chat as int if numeric, else keep as string
                try:
                    target_chat_id = int(target_chat.strip())
                except ValueError:
                    target_chat_id = target_chat.strip()

                # Resolve file list to get total_files count
                job_dict = {
                    "source_type": source_type,
                    "source_path": source_path,
                    "collector_query": collector_query,
                }
                try:
                    file_list = jm.resolve_file_list(job_dict)
                    total_files = len(file_list)
                except Exception:
                    total_files = preview_count or 0

                job_id = jm.create_job(
                    account_id=account_id,
                    target_chat_id=target_chat_id,
                    source_type=source_type,
                    source_path=source_path,
                    collector_query=collector_query,
                    total_files=total_files,
                )
                st.success(f"Job #{job_id} created successfully!")
            except Exception as e:
                st.error(f"Failed to create job: {e}")

import time

st.header("Jobs")

# Query all jobs
try:
    conn = psycopg2.connect(dsn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, status, sent_count, total_files, source_type, 
                   source_path, created_at, updated_at, account_id, target_chat_id
            FROM bulk_sender.send_jobs
            ORDER BY created_at DESC
        """)
        jobs = cur.fetchall()
    conn.close()
except Exception as e:
    jobs = []
    st.error(f"Could not load jobs: {e}")

has_running = any(j["status"] == "running" for j in jobs)

for job in jobs:
    job_id = job["id"]
    status = job["status"]
    sent = job["sent_count"] or 0
    total = job["total_files"] or 0

    with st.expander(f"Job #{job_id} — {status.upper()} ({sent}/{total} files)", expanded=(status == "running")):
        col1, col2 = st.columns(2)
        with col1:
            st.write(f"**Status:** {status}")
            st.write(f"**Sent:** {sent} / {total}")
            if total > 0 and sent > 0 and status == "running":
                # Estimate time remaining based on send_delay
                remaining = total - sent
                est_seconds = remaining * 1.5  # default send_delay
                st.write(f"**Est. remaining:** {int(est_seconds)}s")
        with col2:
            st.write(f"**Account ID:** {job['account_id']}")
            st.write(f"**Target Chat:** {job['target_chat_id']}")

        # Progress bar
        if total > 0:
            st.progress(sent / total)

        # Action buttons
        if status == "pending":
            if st.button("▶ Start", key=f"start_{job_id}"):
                jm.set_status(job_id, "running")
                st.rerun()
        elif status == "running":
            col_pause, col_cancel = st.columns(2)
            with col_pause:
                if st.button("⏸ Pause", key=f"pause_{job_id}"):
                    jm.set_status(job_id, "paused")
                    st.rerun()
            with col_cancel:
                if st.button("✖ Cancel", key=f"cancel_{job_id}"):
                    jm.set_status(job_id, "cancelled")
                    st.rerun()
        elif status == "paused":
            col_resume, col_cancel = st.columns(2)
            with col_resume:
                if st.button("▶ Resume", key=f"resume_{job_id}"):
                    jm.set_status(job_id, "running")
                    st.rerun()
            with col_cancel:
                if st.button("✖ Cancel", key=f"cancel_{job_id}"):
                    jm.set_status(job_id, "cancelled")
                    st.rerun()

        # Scrollable error log panel
        try:
            conn = psycopg2.connect(dsn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT error_message, file_path, created_at
                    FROM bulk_sender.job_errors
                    WHERE job_id = %(job_id)s
                    ORDER BY created_at DESC
                    LIMIT 100
                """, {"job_id": job_id})
                errors = cur.fetchall()
            conn.close()
        except Exception:
            errors = []

        if errors:
            st.write(f"**Errors ({len(errors)}):**")
            error_text = "\n".join(
                f"[{e['created_at']}] {e.get('file_path', '')} — {e.get('error_message', '')}"
                for e in errors
            )
            st.text_area("Error Log", value=error_text, height=150, key=f"errors_{job_id}", disabled=True)

# Auto-refresh every 2 seconds while any job is running
if has_running:
    time.sleep(2)
    st.rerun()

# ── Config Section ────────────────────────────────────────────────────────
st.divider()
st.header("⚙️ Config")
render_config_panel("bulk_sender", set())

# ── Management Section ────────────────────────────────────────────────────
st.divider()
st.header("🔧 Management")

# Cancel All Jobs
st.subheader("Cancel All Jobs")
st.caption("Sets all pending and running jobs to 'cancelled'.")
if st.button("Cancel All Jobs", key="bs_cancel_all"):
    try:
        conn = psycopg2.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bulk_sender.send_jobs SET status='cancelled', updated_at=NOW() "
                "WHERE status IN ('pending', 'running', 'paused')"
            )
            count = cur.rowcount
        conn.commit()
        conn.close()
        st.success(f"Cancelled {count} job(s).")
        st.rerun()
    except Exception as e:
        st.error(f"Failed to cancel jobs: {e}")

st.divider()

# Reset Progress
st.subheader("Reset Progress")
st.caption("Truncates all bulk_sender tables.")
st.warning("⚠️ This will delete all jobs and sent items.")
bs_confirm_reset = st.text_input("Type 'RESET' to confirm", key="bs_confirm_reset")
if st.button("Reset Progress", key="bs_btn_reset", type="primary"):
    if bs_confirm_reset == "RESET":
        try:
            conn = psycopg2.connect(dsn)
            with conn.cursor() as cur:
                cur.execute("TRUNCATE bulk_sender.sent_items CASCADE")
                cur.execute("TRUNCATE bulk_sender.send_jobs CASCADE")
            conn.commit()
            conn.close()
            st.success("Bulk sender progress reset.")
            st.rerun()
        except Exception as e:
            st.error(f"Reset failed: {e}")
    else:
        st.error("Confirmation text does not match.")
