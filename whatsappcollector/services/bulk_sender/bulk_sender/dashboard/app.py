from __future__ import annotations

import asyncio

import streamlit as st

from bulk_sender.database import database


st.set_page_config(page_title="Bulk Sender", layout="wide")


async def _render_async() -> None:
    database.pool = None
    await database.connect()
    stats = await database.summary_stats()
    st.title("Bulk Sender")
    c1, c2, c3 = st.columns(3)
    c1.metric("Pending jobs", stats["pending"])
    c2.metric("Running jobs", stats["running"])
    c3.metric("Sent items", stats["sent"])
    st.warning("External mode requires operator_confirmed=TRUE and enforces hard daily cap of 150.")

    st.subheader("Create Send Job")
    with st.form("create_send_job", clear_on_submit=False):
        session_name = st.text_input("Session name", value="session_1")
        mode = st.selectbox("Mode", options=["internal", "external"], index=0)
        source_path = st.text_input("Source path", value="/data/media")
        requested_by = st.text_input("Requested by", value="operator")
        operator_confirmed = st.checkbox("Operator confirmed (required for external mode)", value=False)
        targets_raw = st.text_area(
            "Target chats (one per line or comma-separated; required for external mode)",
            value="",
            height=120,
        )
        submitted = st.form_submit_button("Queue Job")

    if submitted:
        targets = [
            token.strip()
            for token in targets_raw.replace("\n", ",").split(",")
            if token.strip()
        ]

        if not session_name.strip():
            st.error("session_name is required")
        elif not source_path.strip():
            st.error("source_path is required")
        elif mode == "external" and not targets:
            st.error("At least one external target chat is required")
        elif mode == "external" and not operator_confirmed:
            st.error("operator_confirmed must be checked for external jobs")
        else:
            try:
                job_id = await database.create_send_job(
                    session_name=session_name.strip(),
                    mode=mode,
                    source_path=source_path.strip(),
                    target_chat_jids=targets,
                    operator_confirmed=operator_confirmed,
                    requested_by=requested_by.strip() or "operator",
                )
                st.success(f"Queued job #{job_id}")
            except Exception as exc:
                st.error(f"Failed to queue job: {exc}")

    st.subheader("Recent Jobs")
    recent = await database.list_recent_jobs(limit=25)
    st.dataframe([dict(row) for row in recent], use_container_width=True)

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
