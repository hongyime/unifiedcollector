"""Source-level assertions on the WhatsApp/Telegram graph-edge builder SQL.

Historically these assertions ran against ``Scheduler._build_graph_edges``.
LOGIC-005 (``docs/plans/scheduler-refactor.md`` step 11) extracted that method
to ``src.scheduler.handlers.graph_edges.BuildGraphEdgesHandler.run``; the
assertions still target the SQL text, only the inspection target moved.
"""
import inspect

from src.scheduler.handlers.graph_edges import BuildGraphEdgesHandler


def _graph_edge_builder_source() -> str:
    return inspect.getsource(BuildGraphEdgesHandler.run)


def test_graph_edge_builder_caps_large_whatsapp_groups():
    source = _graph_edge_builder_source()

    assert "GRAPH_EDGES_MAX_GROUP_SENDERS" in source
    assert "eligible_groups AS" in source
    assert "HAVING COUNT(*) BETWEEN 2 AND $1" in source
    assert "JOIN eligible_groups eg ON eg.chat_id = gm1.chat_id" in source


def test_graph_edge_builder_adds_bounded_telegram_co_groups():
    source = _graph_edge_builder_source()

    assert "GRAPH_EDGES_MAX_TELEGRAM_GROUP_MEMBERS" in source
    assert "telegram_chat_members tm" in source
    assert "tc.type = 'group'" in source
    assert "tc.members_count <= $1" in source
    assert "'telegram'" in source
    assert "'co_group'" in source


def test_graph_edge_builder_does_not_rewrite_unchanged_edges():
    source = _graph_edge_builder_source()

    assert "graph_edges.weight IS DISTINCT FROM EXCLUDED.weight" in source
    assert "graph_edges.last_seen_at < EXCLUDED.last_seen_at" in source
    assert "graph_edges.last_seen_at < NOW() - interval '1 hour'" in source
