"""Collector Dashboard — Streamlit entry point (port 8501)."""
import os
import time
import streamlit as st
from services.collector.dashboard.db import check_postgres, get_connection
from services.collector.dashboard.redis_client import get_redis
from shared.config import Settings
from shared.config_manager import render_config_panel
from shared.config_store import config_store
from shared.dashboard_styles import inject_global_styles, render_service_nav
from services.collector.dashboard.pruning import (
    compute_min_cursor,
    fetch_prune_candidates,
    estimate_disk_recovery,
    execute_prune,
)

COLLECTOR_LIVE_KEYS = {"STORY_SCAN_ENABLED"}
FACE_LIVE_KEYS = {
    "FACE_PROCESSING_ENABLED",
    "FACE_SIMILARITY_THRESHOLD",
    "FACE_MIN_QUALITY_THRESHOLD",
}
USER_INTEL_LIVE_KEYS = {"USER_INTEL_PROCESSING_ENABLED", "USER_INTEL_NETWORK_ENABLED"}
LINK_DISCOVERY_LIVE_KEYS = {"LINK_DISCOVERY_PROCESSING_ENABLED"}

settings = Settings()


def get_status_for_action(action: str) -> str:
    """Map a backfill control action to the corresponding DB status value."""
    return {
        "pause": "paused",
        "resume": "running",
        "cancel": "failed",
    }[action]

st.set_page_config(page_title="Collector Dashboard", layout="wide")
inject_global_styles()

# ── Auto-refresh every 30 seconds ────────────────────────────────────────────
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = time.time()
if time.time() - st.session_state.last_refresh > 30:
    st.session_state.last_refresh = time.time()
    st.rerun()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Collector")
    if st.button("🔄 Refresh"):
        st.session_state.last_refresh = time.time()
        st.rerun()
    st.caption("Auto-refreshes every 30s")
    render_service_nav("Collector")

# ── Postgres health check (runs once per page load) ──────────────────────────
postgres_ok = check_postgres()
if not postgres_ok:
    st.error("Postgres unreachable")

# ── Tabs ─────────────────────────────────────────────────────────────────────
tabs = st.tabs(["Health", "Accounts", "Backfill Jobs", "Group Join Queue", "Chats", "DLQ", "Pruning", "Management", "⚙️ Config"])

# ── Tab 1: Health ─────────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader("Worker Status")

    redis_client = get_redis()

    # Worker status table
    if redis_client is None:
        st.warning("Redis unavailable — worker status unknown")
        accounts = []
    else:
        # Fetch accounts from Postgres
        accounts = []
        if postgres_ok:
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, phone_number FROM collector.telegram_accounts ORDER BY phone_number"
                )
                accounts = cur.fetchall()
                cur.close()
                conn.close()
            except Exception:
                accounts = []

        rows = []
        for _account_id, phone_number in accounts:
            try:
                hash_data = redis_client.hgetall(f"worker:status:{phone_number}")
            except Exception:
                hash_data = {}

            connected = hash_data.get(b"connected", hash_data.get("connected", b"")).decode(
                "utf-8", errors="ignore"
            ) if hash_data else ""
            last_message_at = hash_data.get(b"last_message_at", hash_data.get("last_message_at", b"")).decode(
                "utf-8", errors="ignore"
            ) if hash_data else ""
            clock_drift = hash_data.get(b"clock_drift", hash_data.get("clock_drift", b"")).decode(
                "utf-8", errors="ignore"
            ) if hash_data else ""

            rows.append({
                "Phone": phone_number,
                "Connected": connected,
                "Last Message At": last_message_at,
            })

            # Clock drift warning
            if clock_drift in ("True", "true", "1"):
                st.warning(f"⚠️ Clock drift detected for {phone_number}")

        if rows:
            st.dataframe(rows, use_container_width=True)
        else:
            st.info("No accounts found.")

    # ── Queue metrics ─────────────────────────────────────────────────────────
    st.subheader("Queue Metrics")

    try:
        media_queue_depth = redis_client.llen("collector:media_queue") if redis_client else "unknown"
    except Exception:
        media_queue_depth = "unknown"

    try:
        dlq_count = redis_client.llen("collector:dlq") if redis_client else "unknown"
    except Exception:
        dlq_count = "unknown"

    col1, col2 = st.columns(2)
    col1.metric("Media Queue Depth", media_queue_depth)
    col2.metric("DLQ Count", dlq_count)

    # ── Postgres reachability ─────────────────────────────────────────────────
    st.subheader("Postgres")
    if postgres_ok:
        st.success("✅ Postgres reachable")
    else:
        st.error("❌ Postgres unreachable")

# ── Tab 2: Accounts ───────────────────────────────────────────────────────────
KNOWN_SERVICES = ["collector", "face_recognition", "user_intelligence", "link_discovery", "bulk_sender"]

with tabs[1]:
    # ── Parse bot usernames from BOT_TOKENS env var ───────────────────────────
    bot_tokens_raw = os.environ.get("BOT_TOKENS", "")
    bot_names = []
    for entry in bot_tokens_raw.split(";"):
        parts = entry.strip().split(":")
        if len(parts) >= 2:
            bot_names.append(parts[0].strip())

    # ── Add Account via Telegram Bot ──────────────────────────────────────────
    st.subheader("Add Account via Telegram Bot")
    if bot_names:
        for bot_name in bot_names:
            st.markdown(
                f"Message [@{bot_name}](https://t.me/{bot_name}) with `/startcollector` to register a new account."
            )
    else:
        st.info("No BOT_TOKENS configured. Set the BOT_TOKENS environment variable (format: `BotName:token;BotName2:token2`) to enable bot-based account registration.")

    st.divider()

    # ── Account list ──────────────────────────────────────────────────────────
    st.subheader("All Accounts")

    if not postgres_ok:
        st.warning("Postgres unreachable — cannot load accounts")
    else:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, phone_number, display_name, status, last_active "
                "FROM collector.telegram_accounts ORDER BY phone_number"
            )
            account_rows = cur.fetchall()
            cur.close()
            conn.close()

            if account_rows:
                for account_id, phone_number, display_name, status, last_active in account_rows:
                    col_info, col_status, col_action = st.columns([3, 2, 2])
                    with col_info:
                        st.markdown(f"**{phone_number}**")
                        if display_name:
                            st.caption(display_name)
                    with col_status:
                        badge = "🟢 active" if status == "active" else "🔴 inactive"
                        st.write(badge)
                        if last_active:
                            st.caption(f"Last active: {last_active}")
                    with col_action:
                        if status == "active":
                            if st.button("Logout", key=f"logout_acctab_{account_id}"):
                                try:
                                    conn2 = get_connection()
                                    cur2 = conn2.cursor()
                                    cur2.execute(
                                        "UPDATE collector.telegram_accounts SET status='inactive' WHERE id=%s",
                                        (account_id,),
                                    )
                                    conn2.commit()
                                    cur2.close()
                                    conn2.close()
                                    st.success(f"Account {phone_number} set to inactive.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed to logout: {e}")
                    st.divider()
            else:
                st.info("No accounts found.")
        except Exception as e:
            st.warning(f"Error loading accounts: {e}")

# ── Tab 3: Backfill Jobs ──────────────────────────────────────────────────────
with tabs[2]:
    if not postgres_ok:
        st.warning("Postgres unreachable — cannot load backfill jobs")
    else:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT bj.id, ta.phone_number, bj.chat_id, bj.status, bj.messages_done, bj.estimated_total "
                "FROM collector.backfill_jobs bj "
                "JOIN collector.telegram_accounts ta ON ta.id = bj.account_id "
                "ORDER BY bj.created_at DESC"
            )
            job_rows = cur.fetchall()
            cur.close()
            conn.close()

            if job_rows:
                header_cols = st.columns([1, 2, 2, 2, 2, 3])
                for col, label in zip(header_cols, ["ID", "Account", "Chat ID", "Status", "Progress", "Actions"]):
                    col.markdown(f"**{label}**")

                for job_id, phone_number, chat_id, status, messages_done, estimated_total in job_rows:
                    progress = f"{messages_done or 0} / {estimated_total or '?'}"
                    row_cols = st.columns([1, 2, 2, 2, 2, 3])
                    row_cols[0].write(job_id)
                    row_cols[1].write(phone_number)
                    row_cols[2].write(chat_id)
                    row_cols[3].write(status)
                    row_cols[4].write(progress)

                    with row_cols[5]:
                        btn_cols = st.columns(3)
                        for btn_col, action in zip(btn_cols, ["pause", "resume", "cancel"]):
                            if btn_col.button(action.capitalize(), key=f"{action}##{job_id}"):
                                try:
                                    new_status = get_status_for_action(action)
                                    conn2 = get_connection()
                                    cur2 = conn2.cursor()
                                    cur2.execute(
                                        "UPDATE collector.backfill_jobs SET status=%s, updated_at=NOW() WHERE id=%s",
                                        (new_status, job_id),
                                    )
                                    conn2.commit()
                                    cur2.close()
                                    conn2.close()
                                    st.success(f"Job {job_id} {action}d")
                                except Exception as e:
                                    st.error(f"Failed to {action} job {job_id}: {e}")
            else:
                st.info("No backfill jobs found.")
        except Exception:
            st.warning("Postgres unreachable — cannot load backfill jobs")

    st.subheader("Create Backfill Job")
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, phone_number FROM collector.telegram_accounts WHERE status='active' ORDER BY phone_number"
        )
        active_accounts = cur.fetchall()
        cur.close()
        conn.close()
    except Exception:
        active_accounts = []

    with st.form("create_backfill"):
        chat_id_input = st.number_input("Chat ID", min_value=0, step=1, value=0)
        account_options = {phone: acc_id for acc_id, phone in active_accounts}
        account_label = st.selectbox("Account", options=list(account_options.keys()) if account_options else [None])
        account_id_input = account_options.get(account_label) if account_label else None
        submitted = st.form_submit_button("Create")

    if submitted:
        if not chat_id_input or not account_id_input:
            st.error("Both chat and account are required")
        else:
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO collector.backfill_jobs (account_id, chat_id, status, created_at, updated_at) "
                    "VALUES (%s, %s, 'pending', NOW(), NOW())",
                    (account_id_input, chat_id_input),
                )
                conn.commit()
                cur.close()
                conn.close()
                st.success("Backfill job created")
            except Exception as e:
                st.error(f"Failed to create backfill job: {e}")

# ── Tab 4: Group Join Queue ───────────────────────────────────────────────────
with tabs[3]:
    if not postgres_ok:
        st.warning("Postgres unreachable — cannot load group join queue")
    else:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, link, source, language_filter, added_at "
                "FROM collector.group_join_queue WHERE status='pending' ORDER BY added_at"
            )
            pending_rows = cur.fetchall()

            cur.execute(
                "SELECT id, phone_number FROM collector.telegram_accounts WHERE status='active'"
            )
            active_accounts = cur.fetchall()
            cur.close()
            conn.close()

            account_options = {phone: acc_id for acc_id, phone in active_accounts}
            account_labels = list(account_options.keys())

            # ── Bulk approve control ──────────────────────────────────────────
            bulk_account_label = st.selectbox(
                "Bulk approve account",
                options=[None] + account_labels,
                key="bulk_account",
            )
            if st.button("Approve All"):
                if not bulk_account_label:
                    st.error("Select an account before bulk approving")
                else:
                    bulk_account_id = account_options[bulk_account_label]
                    errors = []
                    for row in pending_rows:
                        row_id = row[0]
                        try:
                            conn2 = get_connection()
                            cur2 = conn2.cursor()
                            cur2.execute(
                                "UPDATE collector.group_join_queue SET status='approved', account_id=%s WHERE id=%s",
                                (bulk_account_id, row_id),
                            )
                            conn2.commit()
                            cur2.close()
                            conn2.close()
                        except Exception as e:
                            errors.append(str(e))
                    if errors:
                        st.error(f"Some rows failed: {errors}")
                    else:
                        st.success("All pending rows approved")

            st.divider()

            # ── Per-row rendering ─────────────────────────────────────────────
            if pending_rows:
                for row_id, link, source, language_filter, added_at in pending_rows:
                    with st.container():
                        cols = st.columns([3, 2, 2, 2, 2, 1, 1])
                        cols[0].write(link)
                        cols[1].write(source or "")
                        cols[2].write(language_filter or "")
                        cols[3].write(str(added_at) if added_at else "")

                        selected_account = cols[4].selectbox(
                            "Account",
                            options=[None] + account_labels,
                            key=f"account_{row_id}",
                            label_visibility="collapsed",
                        )

                        if cols[5].button("Approve", key=f"approve_{row_id}"):
                            if not selected_account:
                                st.error(f"Select an account before approving row {row_id}")
                            else:
                                try:
                                    account_id_val = account_options[selected_account]
                                    conn2 = get_connection()
                                    cur2 = conn2.cursor()
                                    cur2.execute(
                                        "UPDATE collector.group_join_queue SET status='approved', account_id=%s WHERE id=%s",
                                        (account_id_val, row_id),
                                    )
                                    conn2.commit()
                                    cur2.close()
                                    conn2.close()
                                    st.success(f"Row {row_id} approved")
                                except Exception as e:
                                    st.error(f"Failed to approve row {row_id}: {e}")

                        if cols[6].button("Skip", key=f"skip_{row_id}"):
                            try:
                                conn2 = get_connection()
                                cur2 = conn2.cursor()
                                cur2.execute(
                                    "UPDATE collector.group_join_queue SET status='skipped' WHERE id=%s",
                                    (row_id,),
                                )
                                conn2.commit()
                                cur2.close()
                                conn2.close()
                                st.success(f"Row {row_id} skipped")
                            except Exception as e:
                                st.error(f"Failed to skip row {row_id}: {e}")
            else:
                st.info("No pending group join requests.")

        except Exception:
            st.warning("Postgres unreachable — cannot load group join queue")

# ── Tab 5: Chats ──────────────────────────────────────────────────────────────
with tabs[4]:
    if not postgres_ok:
        st.warning("Postgres unreachable — cannot load chats")
    else:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT c.id, c.title, c.type, c.member_count, c.is_admin, "
                "MAX(rm.collected_at) AS last_collected "
                "FROM collector.chats c "
                "LEFT JOIN collector.raw_messages rm ON rm.chat_id = c.id "
                "GROUP BY c.id "
                "ORDER BY last_collected DESC NULLS LAST"
            )
            chat_rows = cur.fetchall()
            cur.close()
            conn.close()

            if chat_rows:
                st.dataframe(
                    [
                        {
                            "ID": row[0],
                            "Title": row[1],
                            "Type": row[2],
                            "Members": row[3],
                            "Is Admin": row[4],
                            "Last Collected": str(row[5]) if row[5] else "",
                        }
                        for row in chat_rows
                    ],
                    use_container_width=True,
                )
            else:
                st.info("No chats found.")

            selected_chat_id = st.selectbox(
                "Select a chat to view scan checkpoints",
                options=[None] + [row[0] for row in chat_rows],
                format_func=lambda x: "— select —" if x is None else str(x),
            )

            if selected_chat_id is not None:
                try:
                    conn2 = get_connection()
                    cur2 = conn2.cursor()
                    cur2.execute(
                        "SELECT sc.account_id, ta.phone_number, sc.last_processed_message_id, "
                        "sc.last_seen_message_id, sc.is_complete, sc.last_updated "
                        "FROM collector.scan_checkpoints sc "
                        "JOIN collector.telegram_accounts ta ON ta.id = sc.account_id "
                        "WHERE sc.chat_id = %s",
                        (selected_chat_id,),
                    )
                    checkpoint_rows = cur2.fetchall()
                    cur2.close()
                    conn2.close()

                    if checkpoint_rows:
                        st.dataframe(
                            [
                                {
                                    "Account ID": row[0],
                                    "Phone": row[1],
                                    "Last Processed": row[2],
                                    "Last Seen": row[3],
                                    "Complete": row[4],
                                    "Last Updated": str(row[5]) if row[5] else "",
                                }
                                for row in checkpoint_rows
                            ],
                            use_container_width=True,
                        )
                    else:
                        st.info("No scan checkpoints found for this chat.")
                except Exception:
                    st.warning("Postgres unreachable — cannot load chats")

        except Exception:
            st.warning("Postgres unreachable — cannot load chats")

# ── Tab 6: DLQ ───────────────────────────────────────────────────────────────
with tabs[5]:
    st.subheader("Dead Letter Queue")

    dlq_redis = get_redis()

    if dlq_redis is None:
        st.warning("Redis unavailable — DLQ data unavailable")
    else:
        # ── Counts ───────────────────────────────────────────────────────────
        dlq_categories = ["pending", "transient", "permanent", "resource"]
        counts = {}
        for cat in dlq_categories:
            try:
                counts[cat] = dlq_redis.llen(f"collector:dlq:{cat}")
            except Exception:
                counts[cat] = "unknown"

        # Summary table
        st.dataframe(
            [{"Category": cat, "Count": counts[cat]} for cat in dlq_categories],
            use_container_width=True,
        )

        # Metrics row
        metric_cols = st.columns(4)
        for col, cat in zip(metric_cols, dlq_categories):
            col.metric(cat.capitalize(), counts[cat])

        st.divider()

        # ── Retry All Transient ───────────────────────────────────────────────
        st.subheader("Retry All Transient")
        st.warning("This will re-enqueue all transient DLQ items.")
        if st.button("Confirm Retry", key="confirm_retry"):
            try:
                moved = 0
                while True:
                    item = dlq_redis.rpoplpush("collector:dlq:transient", "collector:media_queue")
                    if item is None:
                        break
                    moved += 1
                st.success(f"Transient items re-enqueued ({moved} items)")
            except Exception as e:
                st.error(f"Redis error during retry: {e}")

        st.divider()

        # ── Clear Permanent Failures ──────────────────────────────────────────
        st.subheader("Clear Permanent Failures")
        st.warning("This will permanently delete all permanent DLQ items.")
        if st.button("Confirm Clear", key="confirm_clear"):
            try:
                dlq_redis.delete("collector:dlq:permanent")
                st.success("Permanent failures cleared")
            except Exception as e:
                st.error(f"Redis error during clear: {e}")

# ── Tab 7: Pruning ────────────────────────────────────────────────────────────
with tabs[6]:
    st.subheader("Media Pruning")

    if not postgres_ok:
        st.warning("Postgres unreachable — cannot compute pruning candidates")
    else:
        try:
            conn = get_connection()
            min_cursor = compute_min_cursor(conn)
            conn.close()

            if min_cursor is None:
                st.info("No active services with cursors found. Nothing to prune.")
                prunable_count = 0
                candidates = []
                disk_recovery = 0
            else:
                conn = get_connection()
                candidates = fetch_prune_candidates(conn, min_cursor)
                conn.close()
                prunable_count = len(candidates)
                disk_recovery = estimate_disk_recovery(candidates, settings.MEDIA_STORE_PATH)

            # Display metrics
            col1, col2, col3 = st.columns(3)
            col1.metric("Prunable Messages", prunable_count)
            col2.metric("Estimated Disk Recovery", f"{disk_recovery / (1024**2):.1f} MB" if disk_recovery else "0 MB")

            # Confirm Prune button — disabled when prunable_count == 0
            if st.button("Confirm Prune", disabled=(prunable_count == 0)):
                st.warning(f"⚠️ This will permanently delete {prunable_count} messages and their media files.")
                if st.button("Yes, prune now", key="confirm_prune_final"):
                    try:
                        conn = get_connection()
                        result = execute_prune(conn, candidates, settings.MEDIA_STORE_PATH)
                        conn.close()
                        st.success(
                            f"Pruning complete: {result.db_rows_deleted} rows deleted, "
                            f"{result.symlinks_deleted} symlinks removed, "
                            f"{result.files_deleted} files deleted, "
                            f"{result.files_skipped} files kept (still referenced)."
                        )
                        if result.errors:
                            st.warning(f"Errors encountered: {len(result.errors)}")
                            for err in result.errors[:10]:
                                st.text(err)
                    except Exception as e:
                        st.error(f"Pruning failed: {e}")
        except Exception as e:
            st.warning(f"Error computing pruning candidates: {e}")

# ── Tab 8: Management ─────────────────────────────────────────────────────
with tabs[7]:
    st.subheader("Account Management")

    if not postgres_ok:
        st.warning("Postgres unreachable — cannot perform management operations")
    else:
        # ── Logout Account ────────────────────────────────────────────────
        st.markdown("#### Logout Account")
        st.caption("Sets account status to 'inactive' and deletes session files from all service directories.")
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, phone_number FROM collector.telegram_accounts WHERE status='active' ORDER BY phone_number"
            )
            active_accs = cur.fetchall()
            cur.close()
            conn.close()
        except Exception:
            active_accs = []

        if active_accs:
            logout_options = {phone: acc_id for acc_id, phone in active_accs}
            logout_phone = st.selectbox("Select account to logout", options=list(logout_options.keys()), key="logout_account")
            if st.button("Logout Account", key="btn_logout"):
                try:
                    conn = get_connection()
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE collector.telegram_accounts SET status='inactive' WHERE phone_number=%s",
                        (logout_phone,)
                    )
                    conn.commit()
                    cur.close()
                    conn.close()
                    # Delete session files
                    stem = logout_phone.lstrip("+")
                    for svc in KNOWN_SERVICES:
                        for ext in (".session", ".session-journal"):
                            path = f"{settings.SESSIONS_BASE_PATH}/{svc}/{stem}{ext}"
                            import os as _os
                            try:
                                if _os.path.exists(path):
                                    _os.remove(path)
                            except Exception:
                                pass
                    st.success(f"Account {logout_phone} logged out and session files removed.")
                except Exception as e:
                    st.error(f"Failed to logout account: {e}")
        else:
            st.info("No active accounts.")

        st.divider()

        # ── Reset Service Cursor ──────────────────────────────────────────
        st.markdown("#### Reset Service Cursor")
        st.caption("Resets a service cursor to 0, allowing full replay of messages.")
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT service_name FROM collector.service_cursors ORDER BY service_name")
            cursor_services = [row[0] for row in cur.fetchall()]
            cur.close()
            conn.close()
        except Exception:
            cursor_services = []

        if cursor_services:
            reset_service = st.selectbox("Select service", options=cursor_services, key="reset_cursor_service")
            col_r1, col_r2 = st.columns(2)
            with col_r1:
                if st.button("Reset This Cursor", key="btn_reset_cursor"):
                    try:
                        conn = get_connection()
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE collector.service_cursors SET last_message_id=0, updated_at=NOW() WHERE service_name=%s",
                            (reset_service,)
                        )
                        conn.commit()
                        cur.close()
                        conn.close()
                        st.success(f"Cursor for '{reset_service}' reset to 0.")
                    except Exception as e:
                        st.error(f"Failed to reset cursor: {e}")
            with col_r2:
                if st.button("Reset ALL Cursors", key="btn_reset_all_cursors"):
                    try:
                        conn = get_connection()
                        cur = conn.cursor()
                        cur.execute("UPDATE collector.service_cursors SET last_message_id=0, updated_at=NOW()")
                        conn.commit()
                        cur.close()
                        conn.close()
                        st.success("All service cursors reset to 0.")
                    except Exception as e:
                        st.error(f"Failed to reset cursors: {e}")

        st.divider()

        # ── Delete All Data ───────────────────────────────────────────────
        st.markdown("#### Delete All Data")
        st.warning("⚠️ DANGER ZONE: This will truncate ALL collector tables. This cannot be undone.")
        confirm_delete = st.text_input("Type 'DELETE ALL DATA' to confirm", key="confirm_delete_all")
        if st.button("Delete All Data", key="btn_delete_all", type="primary"):
            if confirm_delete == "DELETE ALL DATA":
                try:
                    conn = get_connection()
                    cur = conn.cursor()
                    tables = [
                        "collector.raw_messages", "collector.message_edits",
                        "collector.message_deletions", "collector.message_reactions",
                        "collector.user_sightings", "collector.user_profile_photos",
                        "collector.chat_members", "collector.stories",
                        "collector.backfill_jobs", "collector.scan_checkpoints",
                        "collector.admin_log_events", "collector.polls",
                        "collector.poll_votes", "collector.group_join_queue",
                        "collector.users", "collector.chats",
                    ]
                    for table in tables:
                        cur.execute(f"TRUNCATE {table} CASCADE")
                    cur.execute("UPDATE collector.service_cursors SET last_message_id=0, updated_at=NOW()")
                    conn.commit()
                    cur.close()
                    conn.close()
                    st.success("All collector data deleted.")
                except Exception as e:
                    st.error(f"Failed to delete data: {e}")
            else:
                st.error("Confirmation text does not match. No data was deleted.")

# ── Tab 9: Config ─────────────────────────────────────────────────────────
with tabs[8]:
    st.subheader("⚙️ Config")
    first_run = config_store.is_first_run()
    if first_run is True:
        st.info(
            "First-run setup detected: no configuration revision exists yet. "
            "Save platform + service settings below to create the initial revision."
        )
    elif first_run is None:
        st.warning(
            "Unable to verify configuration revision state right now. "
            "Using local fallback values where necessary."
        )
    else:
        st.caption("Config store active. Revisions are being tracked.")

    st.caption("Unified operations configuration. Live keys apply immediately; others auto-apply after service restart.")

    st.markdown("#### Platform & Secrets")
    render_config_panel("platform", set())

    st.divider()
    st.markdown("#### Collector")
    render_config_panel("collector", COLLECTOR_LIVE_KEYS)

    st.divider()
    st.markdown("#### Face Recognition")
    render_config_panel("face_recognition", FACE_LIVE_KEYS)

    st.divider()
    st.markdown("#### User Intelligence")
    render_config_panel("user_intelligence", USER_INTEL_LIVE_KEYS)

    st.divider()
    st.markdown("#### Link Discovery")
    render_config_panel("link_discovery", LINK_DISCOVERY_LIVE_KEYS)

    st.divider()
    st.markdown("#### Bulk Sender")
    render_config_panel("bulk_sender", set())

    st.divider()
    st.markdown("#### Shared Runtime")
    render_config_panel("shared", set())
