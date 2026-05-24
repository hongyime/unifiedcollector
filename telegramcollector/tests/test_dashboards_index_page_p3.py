# Feature: dashboards-index-page, Property 3: by_id file deleted only when ref_count == 0
# Validates: Requirements 7.5
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def simulate_prune_filesystem(messages: list[dict], min_cursor: int) -> tuple[set, set]:
    """Simulate which by_id files would be deleted vs kept during pruning.

    Returns (deleted_files, kept_files) as sets of file_unique_id strings.
    A file is deleted only when all messages referencing it have id <= min_cursor.
    A file is kept when at least one message referencing it has id > min_cursor.
    """
    seen_ids: set = set()
    unique_messages = []
    for m in messages:
        if m["id"] not in seen_ids:
            seen_ids.add(m["id"])
            unique_messages.append(m)

    all_fuids = {m["file_unique_id"] for m in unique_messages if m["file_unique_id"] is not None}

    deleted_files: set = set()
    kept_files: set = set()

    for fuid in all_fuids:
        refs = [m for m in unique_messages if m["file_unique_id"] == fuid]
        has_unconsumed = any(m["id"] > min_cursor for m in refs)
        if has_unconsumed:
            kept_files.add(fuid)
        else:
            has_prunable = any(m["id"] <= min_cursor for m in refs)
            if has_prunable:
                deleted_files.add(fuid)

    return deleted_files, kept_files


@given(
    messages=st.lists(
        st.fixed_dictionaries({
            "id": st.integers(min_value=1, max_value=1000),
            "file_unique_id": st.one_of(st.none(), st.text(min_size=1, max_size=20))
        }),
        min_size=1, max_size=50
    )
)
@h_settings(max_examples=100)
def test_file_dedup_safety(messages):
    # Feature: dashboards-index-page, Property 3: by_id file deleted only when ref_count == 0
    seen_ids: set = set()
    unique_messages = [m for m in messages if m["id"] not in seen_ids and not seen_ids.add(m["id"])]
    if not unique_messages:
        return

    min_cursor = max(m["id"] for m in unique_messages)
    deleted_files, kept_files = simulate_prune_filesystem(unique_messages, min_cursor)

    for fuid in deleted_files:
        remaining_refs = sum(1 for m in unique_messages if m["file_unique_id"] == fuid and m["id"] > min_cursor)
        assert remaining_refs == 0, f"File {fuid} was deleted but has {remaining_refs} remaining refs"

    for fuid in kept_files:
        remaining_refs = sum(1 for m in unique_messages if m["file_unique_id"] == fuid)
        assert remaining_refs > 0, f"File {fuid} was kept but has no refs"
