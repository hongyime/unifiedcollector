from __future__ import annotations

import asyncio

import streamlit as st

from user_intelligence.database import database


st.set_page_config(page_title="User Intelligence", layout="wide")


async def _render_async() -> None:
    database.pool = None
    await database.connect()
    stats = await database.summary_stats()

    st.title("User Intelligence")
    c1, c2, c3 = st.columns(3)
    c1.metric("Tracked users", stats["users"])
    c2.metric("Changes today", stats["changes_today"])
    c3.metric("Connections", stats["connections"])

    left_col, right_col = st.columns(2)

    with left_col:
        st.subheader("User History Timeline")
        search_query = st.text_input("Search user by JID, name, or phone number")
        selected_jid: str | None = None
        if search_query:
            results = await database.search_users(search_query)
            if not results:
                st.warning("No users found matching query.")
            else:
                user_options = {
                    f"{r['push_name'] or r['display_name'] or 'Unknown'} ({r['jid']}) — {r['phone_number'] or 'no phone'}": r['jid']
                    for r in results
                }
                selected_user_label = st.selectbox(
                    "Select user to view history", options=list(user_options.keys())
                )
                selected_jid = user_options[selected_user_label]

    with right_col:
        st.subheader("Timeline")
        if not search_query or selected_jid is None:
            st.info("Select a user on the left to view their history timeline.")
        else:
            timeline = await database.get_user_history_timeline(selected_jid)
            if not timeline:
                st.info("No history recorded for this user yet.")
            else:
                for event in timeline:
                    with st.expander(
                        f"{event['occurred_at']} — {event['event_type'].replace('_', ' ').title()}",
                        expanded=False,
                    ):
                        if event['event_type'] == 'profile_change':
                            st.write(f"Field: **{event['field_name']}**")
                            st.write(f"From: `{event['old_value']}`")
                            st.write(f"To: `{event['new_value']}`")
                        else:
                            st.write(f"Seen in chat: `{event['target']}`")

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
