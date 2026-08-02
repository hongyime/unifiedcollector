import inspect

from src.scheduler import Scheduler


def _graph_edge_builder_source() -> str:
    return inspect.getsource(Scheduler._build_graph_edges)


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
