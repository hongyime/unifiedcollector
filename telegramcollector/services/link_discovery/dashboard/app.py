"""
Streamlit dashboard for the Link Discovery Service.
Run on port 8504:
    streamlit run services/link_discovery/dashboard/app.py --server.port 8504
"""

import os
import psycopg2
import psycopg2.extras
import streamlit as st
from shared.config_manager import render_config_panel
from shared.dashboard_styles import inject_global_styles, render_service_nav

# ---------------------------------------------------------------------------
# Live keys for the config panel
# ---------------------------------------------------------------------------

LD_LIVE_KEYS = {"LINK_DISCOVERY_PROCESSING_ENABLED"}

# ---------------------------------------------------------------------------
# Language helpers for queue rules multiselect
# ---------------------------------------------------------------------------

ISO_LANGUAGES: list[tuple[str, str]] = [
    ("en", "English"), ("zh", "Chinese"), ("ar", "Arabic"),
    ("ru", "Russian"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("pt", "Portuguese"), ("hi", "Hindi"),
    ("ja", "Japanese"), ("ko", "Korean"), ("tr", "Turkish"),
    ("id", "Indonesian"), ("vi", "Vietnamese"), ("th", "Thai"),
    ("fa", "Persian"), ("uk", "Ukrainian"), ("pl", "Polish"),
    ("nl", "Dutch"), ("it", "Italian"),
]

LANGUAGE_OPTIONS: list[str] = [f"{code} — {name}" for code, name in ISO_LANGUAGES]


def _lang_options_to_codes(selected: list[str]) -> list[str] | None:
    """Convert 'en — English' display strings to bare codes. Returns None if empty."""
    codes = [s.split(" — ")[0] for s in selected]
    return codes if codes else None


def _codes_to_lang_options(codes: list[str] | None) -> list[str]:
    """Convert stored codes back to display strings for multiselect pre-selection."""
    if not codes:
        return []
    code_set = set(codes)
    return [opt for opt in LANGUAGE_OPTIONS if opt.split(" — ")[0] in code_set]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PARAMS = {
    "host": os.environ.get("DB_HOST", os.environ.get("POSTGRES_HOST", "postgres")),
    "port": int(os.environ.get("DB_PORT", os.environ.get("POSTGRES_PORT", "5432"))),
    "dbname": os.environ.get("DB_NAME", os.environ.get("POSTGRES_DB", "telegramcollector")),
    "user": os.environ.get("DB_USER", os.environ.get("POSTGRES_USER", "postgres")),
    "password": os.environ.get("DB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")),
}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@st.cache_resource
def get_connection():
    conn = psycopg2.connect(**DB_PARAMS)
    conn.autocommit = True
    return conn


def query(sql: str, params=None) -> list[dict]:
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params=None) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, params)


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Link Discovery", layout="wide")
inject_global_styles()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Link Discovery")
    if st.button("🔄 Refresh"):
        st.rerun()
    render_service_nav("Link Discovery")

st.title("Link Discovery Dashboard")

tab_stats, tab_links, tab_rules, tab_management, tab_config = st.tabs(["Statistics", "Discovered Links", "Queue Rules", "Management", "⚙️ Config"])

# ---------------------------------------------------------------------------
# Tab 1 — Statistics Panel (Requirements 11.1, 11.2, 11.3)
# ---------------------------------------------------------------------------

with tab_stats:
    st.header("Statistics")
    try:
        stats = query(
            """
            SELECT
                COUNT(*)                                                    AS total,
                COUNT(*) FILTER (WHERE status = 'new')                     AS new_count,
                COUNT(*) FILTER (WHERE status = 'queued')                  AS queued_count,
                COUNT(*) FILTER (WHERE status = 'joined')                  AS joined_count,
                COUNT(*) FILTER (WHERE status = 'skipped')                 AS skipped_count,
                COUNT(*) FILTER (WHERE status = 'invalid')                 AS invalid_count
              FROM link_discovery.discovered_links
            """
        )
        s = stats[0] if stats else {}

        cursor_rows = query(
            "SELECT last_message_id FROM collector.service_cursors WHERE service_name = 'link_discovery'"
        )
        cursor_val = cursor_rows[0]["last_message_id"] if cursor_rows else 0

        col1, col2 = st.columns(2)
        col1.metric("Total", s.get("total", 0))
        col2.metric("New", s.get("new_count", 0))
        col1, col2 = st.columns(2)
        col1.metric("Queued", s.get("queued_count", 0))
        col2.metric("Joined", s.get("joined_count", 0))
        col1, col2 = st.columns(2)
        col1.metric("Skipped", s.get("skipped_count", 0))
        col2.metric("Invalid", s.get("invalid_count", 0))
        col1, _ = st.columns(2)
        col1.metric("Cursor", cursor_val)

    except Exception as e:
        st.error(f"Error loading statistics: {e}")

# ---------------------------------------------------------------------------
# Tab 2 — Discovered Links Table (Requirements 10.1–10.4, 8.1–8.4)
# ---------------------------------------------------------------------------

with tab_links:
    st.header("Discovered Links")

    # Filter controls
    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    with col_f1:
        status_filter = st.selectbox("Status", ["(all)", "new", "queued", "joined", "skipped", "invalid"])
    with col_f2:
        language_filter = st.text_input("Language (ISO 639-1)", "")
    with col_f3:
        link_type_filter = st.selectbox("Link Type", ["(all)", "group", "channel", "unknown", "bot", "user"])
    with col_f4:
        keyword_filter = st.text_input("Keyword search (title/link)", "")

    # Build query
    conditions = []
    params = []
    if status_filter != "(all)":
        conditions.append(f"status = %s")
        params.append(status_filter)
    if language_filter.strip():
        conditions.append("language = %s")
        params.append(language_filter.strip())
    if link_type_filter != "(all)":
        conditions.append("link_type = %s")
        params.append(link_type_filter)
    if keyword_filter.strip():
        conditions.append("(chat_title ILIKE %s OR link ILIKE %s)")
        params.extend([f"%{keyword_filter.strip()}%", f"%{keyword_filter.strip()}%"])

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        rows = query(
            f"""
            SELECT link, link_type, status, chat_title, language, member_count,
                   raw_message_id, discovered_at
              FROM link_discovery.discovered_links
            {where_clause}
             ORDER BY discovered_at DESC
             LIMIT 500
            """,
            params or None,
        )

        count_rows = query(
            f"SELECT COUNT(*) AS cnt FROM link_discovery.discovered_links {where_clause}",
            params or None,
        )
        total_count = count_rows[0]["cnt"] if count_rows else 0
        st.caption(f"Showing up to 500 of {total_count} matching links")

        if rows:
            # Multi-select for manual queue action
            selected_links = st.multiselect(
                "Select links to queue manually",
                options=[r["link"] for r in rows],
            )

            st.dataframe(rows, use_container_width=True)

            # Manual queue action (Requirements 8.1–8.4)
            if selected_links:
                st.subheader("Manual Queue Action")
                account_rows = query(
                    "SELECT id, phone_number FROM collector.telegram_accounts WHERE status = 'active' ORDER BY id ASC"
                )
                account_options = {f"{r['id']} — {r['phone_number']}": r["id"] for r in account_rows}
                selected_account_label = st.selectbox(
                    "Select account (required)",
                    options=["(none)"] + list(account_options.keys()),
                )

                if st.button("Queue selected links"):
                    if selected_account_label == "(none)":
                        st.error("You must select an account before queuing links.")
                    else:
                        account_id = account_options[selected_account_label]
                        errors = []
                        for link in selected_links:
                            try:
                                execute(
                                    """
                                    INSERT INTO collector.group_join_queue
                                        (link, account_id, status, source, language_filter, added_at)
                                    VALUES (%s, %s, 'pending', 'link_discovery', TRUE, NOW())
                                    ON CONFLICT DO NOTHING;
                                    """,
                                    (link, account_id),
                                )
                                execute(
                                    "UPDATE link_discovery.discovered_links SET status = 'queued' WHERE link = %s",
                                    (link,),
                                )
                            except Exception as e:
                                errors.append(f"{link}: {e}")
                        if errors:
                            st.error("Some links failed: " + "; ".join(errors))
                        else:
                            st.success(f"Queued {len(selected_links)} link(s) with account {account_id}.")
                            st.rerun()
        else:
            st.info("No links match the current filters.")

    except Exception as e:
        st.error(f"Error loading links: {e}")

# ---------------------------------------------------------------------------
# Tab 3 — Queue Rules CRUD (Requirements 9.1–9.6)
# ---------------------------------------------------------------------------

with tab_rules:
    st.header("Queue Rules")

    try:
        rules = query(
            """
            SELECT id, name, is_active, auto_queue,
                   language_whitelist, language_blacklist,
                   keyword_whitelist, keyword_blacklist,
                   min_member_count, max_member_count, created_at
              FROM link_discovery.queue_rules
             ORDER BY id ASC
            """
        )

        if rules:
            st.dataframe(rules, use_container_width=True)

            # Toggle is_active
            st.subheader("Toggle Rule Active State")
            rule_ids = [r["id"] for r in rules]
            toggle_id = st.selectbox("Rule ID to toggle", options=rule_ids, key="toggle_id")
            if st.button("Toggle is_active"):
                execute(
                    "UPDATE link_discovery.queue_rules SET is_active = NOT is_active WHERE id = %s",
                    (toggle_id,),
                )
                st.success(f"Toggled rule {toggle_id}.")
                st.rerun()

            # Delete rule
            st.subheader("Delete Rule")
            delete_id = st.selectbox("Rule ID to delete", options=rule_ids, key="delete_id")
            if st.button("Delete rule (confirm)"):
                execute("DELETE FROM link_discovery.queue_rules WHERE id = %s", (delete_id,))
                st.success(f"Deleted rule {delete_id}.")
                st.rerun()

            # Edit rule
            st.subheader("Edit Rule")
            edit_id = st.selectbox("Rule ID to edit", options=rule_ids, key="edit_id")
            edit_rule = next((r for r in rules if r["id"] == edit_id), None)
            if edit_rule:
                with st.form("edit_rule_form"):
                    edit_name = st.text_input("Name", value=edit_rule["name"])
                    edit_auto_queue = st.checkbox("auto_queue", value=edit_rule["auto_queue"])
                    edit_lang_wl_sel = st.multiselect(
                        "language_whitelist",
                        options=LANGUAGE_OPTIONS,
                        default=_codes_to_lang_options(edit_rule.get("language_whitelist")),
                        help="Select languages to whitelist",
                    )
                    edit_lang_bl_sel = st.multiselect(
                        "language_blacklist",
                        options=LANGUAGE_OPTIONS,
                        default=_codes_to_lang_options(edit_rule.get("language_blacklist")),
                        help="Select languages to blacklist",
                    )
                    _existing_kw_wl = edit_rule.get("keyword_whitelist") or []
                    edit_kw_wl_sel = st.multiselect(
                        "keyword_whitelist",
                        options=_existing_kw_wl,
                        default=_existing_kw_wl,
                        help="Type keywords and press Enter to add",
                    )
                    _existing_kw_bl = edit_rule.get("keyword_blacklist") or []
                    edit_kw_bl_sel = st.multiselect(
                        "keyword_blacklist",
                        options=_existing_kw_bl,
                        default=_existing_kw_bl,
                        help="Type keywords and press Enter to add",
                    )
                    edit_min_mc = st.number_input("min_member_count (0 = no limit)", value=edit_rule.get("min_member_count") or 0, min_value=0)
                    edit_max_mc = st.number_input("max_member_count (0 = no limit)", value=edit_rule.get("max_member_count") or 0, min_value=0)
                    submitted = st.form_submit_button("Update rule")
                    if submitted:
                        if not edit_name.strip():
                            st.error("Rule name cannot be empty.")
                        else:
                            execute(
                                """
                                UPDATE link_discovery.queue_rules
                                   SET name = %s, auto_queue = %s,
                                       language_whitelist = %s, language_blacklist = %s,
                                       keyword_whitelist = %s, keyword_blacklist = %s,
                                       min_member_count = %s, max_member_count = %s
                                 WHERE id = %s
                                """,
                                (
                                    edit_name.strip(),
                                    edit_auto_queue,
                                    _lang_options_to_codes(edit_lang_wl_sel),
                                    _lang_options_to_codes(edit_lang_bl_sel),
                                    edit_kw_wl_sel or None,
                                    edit_kw_bl_sel or None,
                                    edit_min_mc if edit_min_mc > 0 else None,
                                    edit_max_mc if edit_max_mc > 0 else None,
                                    edit_id,
                                ),
                            )
                            st.success(f"Updated rule {edit_id}.")
                            st.rerun()
        else:
            st.info("No queue rules defined yet.")

    except Exception as e:
        st.error(f"Error loading rules: {e}")

    # Create new rule
    st.subheader("Create New Rule")
    with st.form("create_rule_form"):
        new_name = st.text_input("Name (required)")
        new_auto_queue = st.checkbox("auto_queue", value=True)
        new_lang_wl_sel = st.multiselect(
            "language_whitelist",
            options=LANGUAGE_OPTIONS,
            default=[],
            help="Select languages to whitelist (only links in these languages will be queued)",
        )
        new_lang_bl_sel = st.multiselect(
            "language_blacklist",
            options=LANGUAGE_OPTIONS,
            default=[],
            help="Select languages to blacklist (links in these languages will be skipped)",
        )
        new_kw_wl_sel = st.multiselect(
            "keyword_whitelist",
            options=[],
            default=[],
            help="Type keywords and press Enter to add. Only links containing these keywords will be queued.",
        )
        new_kw_bl_sel = st.multiselect(
            "keyword_blacklist",
            options=[],
            default=[],
            help="Type keywords and press Enter to add. Links containing these keywords will be skipped.",
        )
        new_min_mc = st.number_input("min_member_count (0 = no limit)", value=0, min_value=0)
        new_max_mc = st.number_input("max_member_count (0 = no limit)", value=0, min_value=0)
        create_submitted = st.form_submit_button("Create rule")
        if create_submitted:
            if not new_name.strip():
                st.error("Rule name cannot be empty.")
            else:
                try:
                    execute(
                        """
                        INSERT INTO link_discovery.queue_rules
                            (name, is_active, auto_queue,
                             language_whitelist, language_blacklist,
                             keyword_whitelist, keyword_blacklist,
                             min_member_count, max_member_count, created_at)
                        VALUES (%s, TRUE, %s, %s, %s, %s, %s, %s, %s, NOW())
                        """,
                        (
                            new_name.strip(),
                            new_auto_queue,
                            _lang_options_to_codes(new_lang_wl_sel),
                            _lang_options_to_codes(new_lang_bl_sel),
                            new_kw_wl_sel or None,
                            new_kw_bl_sel or None,
                            new_min_mc if new_min_mc > 0 else None,
                            new_max_mc if new_max_mc > 0 else None,
                        ),
                    )
                    st.success(f"Created rule '{new_name.strip()}'.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error creating rule: {e}")

# ---------------------------------------------------------------------------
# Tab 4 — Management
# ---------------------------------------------------------------------------

with tab_management:
    st.header("Management")

    st.subheader("Reset Progress")
    st.caption("Truncates all link_discovery tables and resets the service cursor to 0.")
    st.warning("⚠️ This will delete all discovered links and queue rules.")
    ld_confirm_reset = st.text_input("Type 'RESET' to confirm", key="ld_confirm_reset")
    if st.button("Reset Progress", key="ld_btn_reset", type="primary"):
        if ld_confirm_reset == "RESET":
            try:
                execute("TRUNCATE link_discovery.discovered_links CASCADE")
                execute("TRUNCATE link_discovery.queue_rules CASCADE")
                execute(
                    "UPDATE collector.service_cursors SET last_message_id=0, updated_at=NOW() "
                    "WHERE service_name='link_discovery'"
                )
                st.success("Link discovery progress reset.")
            except Exception as exc:
                st.error(f"Reset failed: {exc}")
        else:
            st.error("Confirmation text does not match.")

# ---------------------------------------------------------------------------
# Tab 5 — Config Panel
# ---------------------------------------------------------------------------

with tab_config:
    st.header("⚙️ Config")
    render_config_panel("link_discovery", LD_LIVE_KEYS)
