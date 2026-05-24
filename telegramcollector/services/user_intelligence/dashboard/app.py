"""
Streamlit dashboard for the User Intelligence Service.
Run on port 8503:
    streamlit run services/user_intelligence/dashboard/app.py --server.port 8503
"""

import csv
import io
import os

import psycopg2
import psycopg2.extras
import streamlit as st
from shared.config_manager import render_config_panel
from shared.dashboard_styles import inject_global_styles, render_service_nav

# ---------------------------------------------------------------------------
# Live keys for the config panel
# ---------------------------------------------------------------------------

UI_LIVE_KEYS = {"USER_INTEL_PROCESSING_ENABLED", "USER_INTEL_NETWORK_ENABLED"}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NETWORK_ENABLED = os.environ.get("USER_INTEL_NETWORK_ENABLED", "true").lower() not in (
    "false", "0", "no",
)

DB_PARAMS = {
    "host": os.environ.get("DB_HOST", os.environ.get("POSTGRES_HOST", "postgres")),
    "port": int(os.environ.get("DB_PORT", os.environ.get("POSTGRES_PORT", "5432"))),
    "dbname": os.environ.get("DB_NAME", os.environ.get("POSTGRES_DB", "telegramcollector")),
    "user": os.environ.get("DB_USER", os.environ.get("POSTGRES_USER", "postgres")),
    "password": os.environ.get("DB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")),
}

# ---------------------------------------------------------------------------
# DB connection (cached for the lifetime of the Streamlit session)
# ---------------------------------------------------------------------------


@st.cache_resource
def get_connection():
    """Return a persistent psycopg2 connection, cached across reruns."""
    conn = psycopg2.connect(**DB_PARAMS)
    conn.autocommit = True
    return conn


def query(sql: str, params=None) -> list[dict]:
    """Execute *sql* and return rows as a list of dicts."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

st.set_page_config(page_title="User Intelligence", layout="wide")
inject_global_styles()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("User Intelligence")
    if st.button("🔄 Refresh"):
        st.rerun()
    render_service_nav("User Intelligence")

st.title("User Intelligence Dashboard")

# ---------------------------------------------------------------------------
# Section 1 — Operational statistics (Requirement 12)
# ---------------------------------------------------------------------------

st.header("Operational Statistics")

try:
    stats_rows = query(
        """
        SELECT
            (SELECT COUNT(DISTINCT user_id)
               FROM user_intelligence.user_chat_memberships)                AS total_users,
            (SELECT COUNT(*)
               FROM user_intelligence.user_history
              WHERE changed_at >= CURRENT_DATE)                             AS today_changes,
            (SELECT COALESCE(last_message_id, 0)
               FROM collector.service_cursors
              WHERE service_name = 'user_intelligence')                     AS cursor_value
        """
    )
    stats = stats_rows[0] if stats_rows else {}

    col1, col2 = st.columns(2)
    col1.metric("Distinct Users Tracked", stats.get("total_users", 0))
    col2.metric("Change Events Today", stats.get("today_changes", 0))
    col1, col2 = st.columns(2)
    col1.metric("Current Cursor", stats.get("cursor_value", 0))

    st.subheader("Top 10 Most Active Users")
    top_users = query(
        """
        SELECT user_id, SUM(message_count) AS total_messages
          FROM user_intelligence.user_chat_memberships
         GROUP BY user_id
         ORDER BY total_messages DESC
         LIMIT 10
        """
    )
    if top_users:
        st.dataframe(top_users, use_container_width=True)
    else:
        st.info("No membership data yet.")

except Exception as exc:
    st.error(f"Could not load statistics: {exc}")

st.divider()

# ---------------------------------------------------------------------------
# Section 2 — User search (Requirement 8.1)
# ---------------------------------------------------------------------------

st.header("User Search")

search_term = st.text_input(
    "Search by user_id, username, first_name, last_name, or phone",
    placeholder="e.g. alice  or  123456789",
)

selected_user_id: int | None = None

if search_term:
    try:
        # Try numeric match on user_id first, then text fields
        try:
            uid_int = int(search_term)
            numeric_clause = "OR u.id = %(uid)s"
        except ValueError:
            uid_int = None
            numeric_clause = ""

        search_sql = f"""
            SELECT id, username, first_name, last_name, phone
              FROM collector.users u
             WHERE u.username    ILIKE %(term)s
                OR u.first_name  ILIKE %(term)s
                OR u.last_name   ILIKE %(term)s
                OR u.phone       ILIKE %(term)s
                {numeric_clause}
             ORDER BY id
             LIMIT 50
        """
        params = {"term": f"%{search_term}%", "uid": uid_int}
        results = query(search_sql, params)

        if results:
            options = {
                f"{r['id']} — {r.get('username') or ''} {r.get('first_name') or ''} {r.get('last_name') or ''}".strip(): r["id"]
                for r in results
            }
            chosen_label = st.selectbox("Select a user", list(options.keys()))
            selected_user_id = options[chosen_label]
        else:
            st.info("No users found matching that search term.")
    except Exception as exc:
        st.error(f"Search error: {exc}")

st.divider()

# ---------------------------------------------------------------------------
# Sections 3–5 are only shown when a user is selected
# ---------------------------------------------------------------------------

if selected_user_id is not None:

    # -----------------------------------------------------------------------
    # Section 3 — Profile timeline (Requirements 8.2, 8.3)
    # -----------------------------------------------------------------------

    st.header(f"Profile Timeline — user {selected_user_id}")

    try:
        history = query(
            """
            SELECT field_name, old_value, new_value, changed_at
              FROM user_intelligence.user_history
             WHERE user_id = %s
             ORDER BY changed_at ASC
            """,
            (selected_user_id,),
        )
        if history:
            st.dataframe(history, use_container_width=True)
        else:
            st.info("No profile changes have been detected for this user.")
    except Exception as exc:
        st.error(f"Could not load profile timeline: {exc}")

    st.divider()

    # -----------------------------------------------------------------------
    # Section 4 — Group membership panel (Requirements 9.1, 9.2, 9.3)
    # -----------------------------------------------------------------------

    st.header(f"Group Memberships — user {selected_user_id}")

    try:
        memberships = query(
            """
            SELECT m.chat_id,
                   COALESCE(c.title, m.chat_id::text) AS chat_title,
                   m.first_seen,
                   m.last_seen,
                   m.message_count
              FROM user_intelligence.user_chat_memberships m
              LEFT JOIN collector.chats c ON c.id = m.chat_id
             WHERE m.user_id = %s
             ORDER BY m.last_seen DESC
            """,
            (selected_user_id,),
        )
        if memberships:
            st.dataframe(memberships, use_container_width=True)
        else:
            st.info("No group memberships found for this user.")
    except Exception as exc:
        st.error(f"Could not load memberships: {exc}")

    st.divider()

    # -----------------------------------------------------------------------
    # Section 5 — Network graph visualisation (Requirements 10.1, 10.2, 10.3)
    # -----------------------------------------------------------------------

    st.header(f"Network Graph — user {selected_user_id}")

    if not NETWORK_ENABLED:
        st.warning(
            "Network graph building is disabled (`USER_INTEL_NETWORK_ENABLED=False`). "
            "Graph data may be incomplete or unavailable."
        )
    else:
        try:
            import networkx as nx  # type: ignore

            # -- Bipartite user-group graph (Requirement 10.1) ---------------
            st.subheader("User-Group Bipartite Graph")
            bip_rows = query(
                """
                SELECT m.user_id, m.chat_id,
                       COALESCE(c.title, m.chat_id::text) AS chat_title
                  FROM user_intelligence.user_chat_memberships m
                  LEFT JOIN collector.chats c ON c.id = m.chat_id
                 WHERE m.user_id = %s
                """,
                (selected_user_id,),
            )

            if bip_rows:
                B = nx.Graph()
                user_node = f"user:{selected_user_id}"
                B.add_node(user_node, bipartite=0)
                for row in bip_rows:
                    chat_node = f"chat:{row['chat_id']} ({row['chat_title']})"
                    B.add_node(chat_node, bipartite=1)
                    B.add_edge(user_node, chat_node)

                _render_graph(B, title="User-Group Bipartite Graph")
            else:
                st.info("No membership data to visualise.")

            # -- User-user co-membership graph (Requirement 10.2) ------------
            st.subheader("User-User Co-membership Graph")
            conn_rows = query(
                """
                SELECT user_id_a, user_id_b, shared_chat_count
                  FROM user_intelligence.user_connections
                 WHERE user_id_a = %s OR user_id_b = %s
                """,
                (selected_user_id, selected_user_id),
            )

            if conn_rows:
                G = nx.Graph()
                for row in conn_rows:
                    G.add_edge(
                        row["user_id_a"],
                        row["user_id_b"],
                        weight=row["shared_chat_count"],
                    )
                _render_graph(G, title="User-User Co-membership Graph", weighted=True)
            else:
                st.info("No co-membership connections found for this user.")

        except ImportError:
            # Fallback: text-based adjacency list
            st.info("networkx is not installed — showing text-based adjacency list instead.")
            _render_text_graph(selected_user_id)
        except Exception as exc:
            st.error(f"Could not render network graph: {exc}")

    st.divider()

# ---------------------------------------------------------------------------
# Section 6 — CSV exports (Requirements 11.1–11.4)
# ---------------------------------------------------------------------------

st.header("CSV Exports")


def _build_csv(sql: str, params=None) -> str:
    """
    Stream query results into an in-memory CSV string without loading the full
    result set into a Python list first — rows are written one at a time.
    """
    conn = get_connection()
    buf = io.StringIO()
    writer = None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        for row in cur:
            d = dict(row)
            if writer is None:
                writer = csv.DictWriter(buf, fieldnames=list(d.keys()))
                writer.writeheader()
            writer.writerow(d)
    return buf.getvalue()


col_a, col_b, col_c = st.columns(3)

with col_a:
    if st.button("Generate Users.csv"):
        try:
            csv_data = _build_csv("SELECT * FROM collector.users ORDER BY id")
            st.download_button(
                label="Download Users.csv",
                data=csv_data,
                file_name="Users.csv",
                mime="text/csv",
            )
        except Exception as exc:
            st.error(f"Export failed: {exc}")

with col_b:
    if st.button("Generate Memberships.csv"):
        try:
            csv_data = _build_csv(
                """
                SELECT user_id, chat_id, first_seen, last_seen, message_count
                  FROM user_intelligence.user_chat_memberships
                 ORDER BY user_id, chat_id
                """
            )
            st.download_button(
                label="Download Memberships.csv",
                data=csv_data,
                file_name="Memberships.csv",
                mime="text/csv",
            )
        except Exception as exc:
            st.error(f"Export failed: {exc}")

with col_c:
    if st.button("Generate user_changes.csv"):
        try:
            csv_data = _build_csv(
                """
                SELECT user_id, field_name, old_value, new_value, changed_at
                  FROM user_intelligence.user_history
                 ORDER BY changed_at ASC
                """
            )
            st.download_button(
                label="Download user_changes.csv",
                data=csv_data,
                file_name="user_changes.csv",
                mime="text/csv",
            )
        except Exception as exc:
            st.error(f"Export failed: {exc}")


# ---------------------------------------------------------------------------
# Helper functions (defined after the main layout so they can be referenced
# above via forward reference — Python resolves them at call time)
# ---------------------------------------------------------------------------


def _render_graph(G, title: str = "", weighted: bool = False) -> None:
    """Render a networkx graph using matplotlib, or fall back to pyvis."""
    try:
        import matplotlib.pyplot as plt  # type: ignore

        fig, ax = plt.subplots(figsize=(8, 5))
        pos = _layout(G)
        nx = __import__("networkx")
        nx.draw_networkx(G, pos=pos, ax=ax, with_labels=True, node_size=500, font_size=8)
        if weighted:
            edge_labels = nx.get_edge_attributes(G, "weight")
            nx.draw_networkx_edge_labels(G, pos=pos, edge_labels=edge_labels, ax=ax)
        ax.set_title(title)
        ax.axis("off")
        st.pyplot(fig)
        plt.close(fig)
    except ImportError:
        # pyvis fallback
        try:
            from pyvis.network import Network  # type: ignore

            net = Network(height="400px", width="100%")
            net.from_nx(G)
            html = net.generate_html()
            st.components.v1.html(html, height=420)
        except ImportError:
            # Plain text adjacency list
            st.text(f"{title}\n" + "\n".join(f"  {u} -- {v}" for u, v in G.edges()))


def _layout(G):
    """Return a layout dict; bipartite layout if applicable, else spring."""
    import networkx as nx  # type: ignore

    try:
        top = {n for n, d in G.nodes(data=True) if d.get("bipartite") == 0}
        if top:
            return nx.bipartite_layout(G, top)
    except Exception:
        pass
    return nx.spring_layout(G, seed=42)


def _render_text_graph(user_id: int) -> None:
    """Fallback: show adjacency list when networkx is unavailable."""
    try:
        rows = query(
            """
            SELECT user_id_a, user_id_b, shared_chat_count
              FROM user_intelligence.user_connections
             WHERE user_id_a = %s OR user_id_b = %s
            """,
            (user_id, user_id),
        )
        if rows:
            lines = [f"user:{r['user_id_a']} -- user:{r['user_id_b']}  (shared chats: {r['shared_chat_count']})" for r in rows]
            st.text("\n".join(lines))
        else:
            st.info("No connections found.")
    except Exception as exc:
        st.error(f"Could not load connections: {exc}")

# ---------------------------------------------------------------------------
# Management Section
# ---------------------------------------------------------------------------

st.divider()
st.header("⚙️ Config")
render_config_panel("user_intelligence", UI_LIVE_KEYS)

st.divider()
st.header("🔧 Management")

st.subheader("Reset Progress")
st.caption("Truncates all user_intelligence tables and resets the service cursor to 0.")
st.warning("⚠️ This will delete all user history, memberships, and connection data.")
ui_confirm_reset = st.text_input("Type 'RESET' to confirm", key="ui_confirm_reset")
if st.button("Reset Progress", key="ui_btn_reset", type="primary"):
    if ui_confirm_reset == "RESET":
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("TRUNCATE user_intelligence.user_history CASCADE")
                cur.execute("TRUNCATE user_intelligence.user_chat_memberships CASCADE")
                cur.execute("TRUNCATE user_intelligence.user_connections CASCADE")
                cur.execute(
                    "UPDATE collector.service_cursors SET last_message_id=0, updated_at=NOW() "
                    "WHERE service_name='user_intelligence'"
                )
            st.success("User intelligence progress reset.")
        except Exception as exc:
            st.error(f"Reset failed: {exc}")
    else:
        st.error("Confirmation text does not match.")
