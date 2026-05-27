from __future__ import annotations

import asyncio

import streamlit as st

from link_discovery.database import database


st.set_page_config(page_title="Link Discovery", layout="wide")


async def _render_async() -> None:
    database.pool = None
    await database.connect()
    stats = await database.summary_stats()
    st.title("Link Discovery")

    # Metrics row — 3 columns
    c1, c2, c3 = st.columns(3)
    c1.metric("Discovered links", stats["discovered"])
    c2.metric("Pending join queue", stats["queued"])
    c3.metric("Unassigned joins", stats["unassigned"])

    st.subheader("Join Queue Panel")
    pending = await database.list_pending_joins()
    active_sessions = await database.list_active_sessions()

    # Fetch rules for per-join session filtering
    async with database._pool().acquire() as _conn:
        rules = await database.list_active_rules(_conn)

    # Unassigned banner
    if stats["unassigned"] > 0:
        st.warning(f"{stats['unassigned']} join(s) have no session assigned and require attention.")

    # Bulk-assign control
    bulk_col1, bulk_col2 = st.columns([3, 1])
    with bulk_col1:
        bulk_session = st.selectbox(
            "Bulk assign session",
            options=active_sessions,
            key="bulk_session",
            disabled=not active_sessions,
        )
    with bulk_col2:
        st.write("")
        if st.button("Assign All", disabled=not active_sessions or not bulk_session):
            updated = await database.bulk_assign_session(bulk_session)
            st.success(f"Assigned {updated} join(s) to {bulk_session}")
            st.rerun()
    if not active_sessions:
        st.caption("No active sessions available. Start a session to enable bulk assignment.")

    if not pending:
        st.info("No pending joins in the queue.")
    else:
        # Collect misconfigured rules (deduplicated)
        warned_rules: set[int] = set()
        for rule in rules:
            if (
                rule.preferred_session is not None
                and rule.session_allowlist
                and rule.preferred_session not in rule.session_allowlist
                and rule.id not in warned_rules
            ):
                st.warning(
                    f"Rule '{rule.name}' misconfiguration: preferred_session '{rule.preferred_session}' "
                    f"is not in its session_allowlist {rule.session_allowlist}."
                )
                warned_rules.add(rule.id)

        for join in pending:
            # Determine matched rule by source name
            matched_rule = next((r for r in rules if r.name == join["source"]), None)

            # Filter session options by allowlist if applicable
            if matched_rule and matched_rule.session_allowlist:
                session_options = [s for s in active_sessions if s in matched_rule.session_allowlist]
            else:
                session_options = active_sessions

            is_unassigned = join["session_name"] is None
            link_display = f"⚠️ {join['link']}" if is_unassigned else join["link"]

            with st.container(border=True):
                col1, col2, col3 = st.columns([4, 2, 2])
                with col1:
                    st.text(f"Link: {link_display}")
                    st.caption(f"Source: {join['source']} | Added: {join['added_at']}")
                with col2:
                    current_session = join["session_name"]
                    default_index = session_options.index(current_session) if current_session in session_options else 0
                    selected_session = st.selectbox(
                        "Assign Session",
                        options=session_options,
                        index=default_index,
                        key=f"session_{join['id']}",
                        disabled=not session_options,
                    )
                with col3:
                    if st.button(
                        "Approve Join",
                        key=f"approve_{join['id']}",
                        disabled=not selected_session,
                    ):
                        await database.update_join_status(join["id"], session_name=selected_session, status="approved")
                        st.success(f"Approved join for {selected_session}")
                        st.rerun()

    await database.close()


def main() -> None:
    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.caption("Manually refresh all dashboard data.")
    asyncio.run(_render_async())


if __name__ == "__main__":
    main()
