from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.core.collection_action_queue import (
    derive_collection_actions,
    resolve_stale_actions_from_direct_health,
    sync_collection_action_queue,
)


def test_derive_collection_actions_maps_auth_blocker_and_yield_gap():
    source_matrix = {
        "sources": [
            {
                "source": "instagram",
                "status": "live",
                "blocker": {
                    "severity": "error",
                    "kind": "auth_wall",
                    "summary": "manual login required",
                },
                "current_hour": {"stored": 0},
                "last_complete_hour": {"stored": 0},
            },
            {
                "source": "x",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none"},
                "current_hour": {"stored": 1},
                "last_complete_hour": {"stored": 2},
            },
        ],
        "browser_extension": {
            "issues": [
                {
                    "kind": "browser_maintenance_stalled",
                    "severity": "error",
                    "message": "maintenance pass is stalled",
                }
            ],
            "maintenance": {"state": "running"},
        },
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)
    by_type = {(item["source"], item["action_type"]): item for item in actions}

    assert by_type[("instagram", "manual_auth_needed")]["priority"] == 1
    assert by_type[("instagram", "manual_auth_needed")]["reason"] == "manual login required"
    assert by_type[("x", "target_starved")]["reason"] == "below 5/hour media-output floor"
    assert by_type[("browser_extension", "repair_browser")]["priority"] == 1


def test_derive_collection_actions_maps_whatsapp_pairing_to_manual_auth():
    source_matrix = {
        "sources": [
            {
                "source": "whatsapp",
                "status": "unpaired",
                "blocker": {
                    "severity": "warning",
                    "kind": "whatsapp_pairing",
                    "summary": "WhatsApp bridge is waiting for QR pairing.",
                    "next_action": "Open the bridge QR and pair the second device.",
                },
                "current_hour": {"messages": 0},
                "last_complete_hour": {"messages": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [
        ("whatsapp", "manual_auth_needed")
    ]
    assert actions[0]["priority"] == 1


def test_derive_collection_actions_ignores_sources_above_yield_floor():
    source_matrix = {
        "sources": [
            {
                "source": "facebook",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none"},
                "current_hour": {"media_items": 7},
                "last_complete_hour": {"media_items": 9},
            },
        ],
        "browser_extension": {"issues": []},
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_skips_yield_floor_when_last_complete_stats_missing():
    source_matrix = {
        "sources": [
            {
                "source": "instagram",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "current_hour": {
                    "records": 0,
                    "media_items": 0,
                    "rate_limits": 0,
                    "access_errors": 0,
                },
                "last_complete_hour": {},
            },
        ],
        "browser_extension": {"issues": []},
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_enforces_website_by_default(monkeypatch):
    monkeypatch.delenv("COLLECTION_ACTION_YIELD_SOURCES", raising=False)
    source_matrix = {
        "sources": [
            {
                "source": "website",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "current_hour": {"records": 0, "media_items": 0},
                "last_complete_hour": {"records": 0, "media_items": 0},
            },
            {
                "source": "github",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "current_hour": {"records": 0, "media_items": 0},
                "last_complete_hour": {"records": 9, "media_items": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [("website", "target_starved")]


def test_derive_collection_actions_uses_24h_grace_for_slow_website_crawl(monkeypatch):
    monkeypatch.delenv("COLLECTION_ACTION_YIELD_SOURCES", raising=False)
    monkeypatch.delenv("COLLECTION_ACTION_SLOW_YIELD_SOURCES", raising=False)
    source_matrix = {
        "sources": [
            {
                "source": "website",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "current_hour": {"records": 0, "media_items": 0},
                "last_complete_hour": {"records": 0, "media_items": 0},
                "last_24h": {"records": 493, "media_items": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_keeps_website_gap_when_24h_is_empty(monkeypatch):
    monkeypatch.delenv("COLLECTION_ACTION_YIELD_SOURCES", raising=False)
    monkeypatch.delenv("COLLECTION_ACTION_SLOW_YIELD_SOURCES", raising=False)
    source_matrix = {
        "sources": [
            {
                "source": "website",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "current_hour": {"records": 0, "media_items": 0},
                "last_complete_hour": {"records": 0, "media_items": 0},
                "last_24h": {"records": 0, "media_items": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [("website", "target_starved")]


def test_derive_collection_actions_can_enforce_extra_sources_by_env(monkeypatch):
    monkeypatch.setenv("COLLECTION_ACTION_YIELD_SOURCES", "github,website")
    source_matrix = {
        "sources": [
            {
                "source": "github",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "current_hour": {"records": 0, "media_items": 0},
                "last_complete_hour": {"records": 0, "media_items": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [("github", "target_starved")]


def test_derive_collection_actions_honors_yield_source_override(monkeypatch):
    monkeypatch.setenv("COLLECTION_ACTION_YIELD_SOURCES", "x")
    source_matrix = {
        "sources": [
            {
                "source": "website",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "current_hour": {"records": 0, "media_items": 0},
                "last_complete_hour": {"records": 0, "media_items": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_ignores_quiet_beeper_subsource_rows():
    source_matrix = {
        "sources": [
            {
                "source": "beeper_signal",
                "status": "stale",
                "collection_mode": "messaging bridge",
                "blocker": {
                    "kind": "quiet_beeper_subsource",
                    "severity": "ok",
                    "summary": "Beeper / Signal via Beeper",
                    "next_action": "No action unless you expected new messages in this Beeper network.",
                },
                "current_hour": {"messages": 0},
                "last_complete_hour": {"messages": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_ignores_stats_unavailable_fallback_rows():
    source_matrix = {
        "sources": [
            {
                "source": "x",
                "status": "unknown",
                "blocker": {
                    "kind": "stats_unavailable",
                    "severity": "ok",
                    "summary": "source matrix build timed out before a cache was available",
                    "next_action": "Wait for the background source-matrix refresh.",
                },
                "current_hour": {"records": 0, "media_items": 0, "stats_unavailable": True},
                "last_complete_hour": {"records": 0, "media_items": 0, "stats_unavailable": True},
            }
        ]
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_ignores_source_liveness_timeout_skeleton_rows():
    source_matrix = {
        "sources": [
            {
                "source": "website",
                "status": "unknown",
                "blocker": {
                    "kind": "source_liveness_timeout",
                    "severity": "warning",
                    "summary": "source liveness query timed out; showing known source skeleton until DB load drops",
                    "next_action": "Wait for source-matrix refresh after DB load drops.",
                },
                "detail": "source liveness query timed out; showing known source skeleton until DB load drops",
                "current_hour": {"records": 0, "media_items": 0, "stats_unavailable": True},
                "last_complete_hour": {"records": 0, "media_items": 0, "stats_unavailable": True},
            }
        ],
        "browser_extension": {"issues": []},
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_does_not_claim_yield_gap_when_window_stats_unavailable():
    source_matrix = {
        "sources": [
            {
                "source": "facebook",
                "status": "live",
                "blocker": {"kind": "none", "severity": "ok", "next_action": "No operator action."},
                "current_hour": {"records": 0, "media_items": 0, "media_stats_unavailable": True},
                "last_complete_hour": {"records": 0, "media_items": 0},
            }
        ]
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_maps_yield_gap_with_recent_rate_pressure_to_blocked():
    source_matrix = {
        "sources": [
            {
                "source": "tiktok",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "current_hour": {"media_items": 0, "rate_limits": 0},
                "last_complete_hour": {"media_items": 0, "rate_limits": 2},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [("tiktok", "source_blocked")]
    assert actions[0]["reason"] == "recent rate-limit or access pressure"


def test_derive_collection_actions_ignores_last_complete_pressure_after_rate_limit_expires():
    source_matrix = {
        "sources": [
            {
                "source": "tiktok",
                "status": "live",
                "blocker": {"severity": "ok", "kind": "none", "next_action": "No operator action."},
                "rate_limit": {"active_now": False, "active_until": "2026-08-21T21:14:28+00:00"},
                "current_hour": {"media_items": 0, "rate_limits": 0},
                "last_complete_hour": {"media_items": 0, "rate_limits": 2},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [("tiktok", "target_starved")]
    assert actions[0]["reason"] == "below 5/hour media-output floor"


def test_derive_collection_actions_ignores_live_warning_page_shell_with_recent_output():
    source_matrix = {
        "sources": [
            {
                "source": "x",
                "status": "live",
                "blocker": {
                    "kind": "browser_page_error",
                    "severity": "warning",
                    "summary": "x tab showed try_again_empty_state",
                },
                "current_hour": {"records": 5, "media_items": 0},
                "last_complete_hour": {"records": 0, "media_items": 0},
                "last_24h": {"records": 1, "media_items": 0, "liveness_floor": True},
            },
        ],
        "browser_extension": {"issues": []},
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_converts_expired_cooldown_to_media_floor_action():
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    source_matrix = {
        "sources": [
            {
                "source": "lemon8",
                "status": "live",
                "blocker": {
                    "kind": "cooldown",
                    "severity": "warning",
                    "summary": f"Recent HTTP pressure is cooling down until {expired}.",
                },
                "rate_limit": {"active_now": True, "active_until": expired},
                "current_hour": {"records": 8, "media_items": 0},
                "last_complete_hour": {"records": 9, "media_items": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [("lemon8", "target_starved")]
    assert actions[0]["reason"] == "below 5/hour media-output floor"


def test_derive_collection_actions_keeps_active_cooldown_blocker():
    active_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    source_matrix = {
        "sources": [
            {
                "source": "lemon8",
                "status": "live",
                "blocker": {
                    "kind": "cooldown",
                    "severity": "warning",
                    "summary": f"Recent HTTP pressure is cooling down until {active_until}.",
                },
                "rate_limit": {"active_now": True, "active_until": active_until},
                "current_hour": {"records": 8, "media_items": 0},
                "last_complete_hour": {"records": 9, "media_items": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [("lemon8", "source_blocked")]


def test_derive_collection_actions_suppresses_stale_browser_capture_when_extension_content_is_fresh():
    source_matrix = {
        "sources": [
            {
                "source": "lemon8",
                "status": "live",
                "blocker": {
                    "kind": "browser_capture_stalled",
                    "severity": "warning",
                    "summary": "browser content progress is 11720s old (> 3600s)",
                },
                "current_hour": {"records": 0, "media_items": 0},
                "last_complete_hour": {"records": 0, "media_items": 0},
            },
        ],
        "browser_extension": {
            "ingest": [
                {
                    "platform": "lemon8",
                    "endpoint": "media",
                    "age_seconds": 25,
                    "observed_count": 100,
                    "stored_count": 21,
                    "fresh_after_seconds": 600,
                },
            ],
            "issues": [],
        },
    }

    assert derive_collection_actions(source_matrix, min_useful_per_hour=5) == []


def test_derive_collection_actions_surfaces_degraded_browser_maintenance_without_issue_rows():
    source_matrix = {
        "sources": [],
        "browser_extension": {
            "maintenance": {
                "state": "degraded",
                "detail": "browser extension tabs unhealthy after reload/profile restart",
            },
            "issues": [],
        },
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [
        ("browser_extension", "repair_browser")
    ]
    assert actions[0]["priority"] == 2
    assert actions[0]["reason"] == "browser extension tabs unhealthy after reload/profile restart"


def test_derive_collection_actions_suppresses_stale_degraded_maintenance_when_ingest_is_fresh():
    source_matrix = {
        "sources": [],
        "browser_extension": {
            "maintenance": {
                "state": "degraded",
                "detail": "browser extension tabs unhealthy after reload/profile restart",
            },
            "ingest_health": {
                "active": True,
                "content_active": True,
                "last_content_age_seconds": 42,
                "fresh_after_seconds": 600,
            },
            "issues": [],
        },
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert actions == []


def test_derive_collection_actions_keeps_cdp_unavailable_even_when_ingest_was_recent():
    source_matrix = {
        "sources": [],
        "browser_extension": {
            "maintenance": {
                "state": "cdp_unavailable",
                "detail": "Chrome CDP is unreachable",
            },
            "ingest_health": {
                "active": True,
                "content_active": True,
                "last_content_age_seconds": 42,
                "fresh_after_seconds": 600,
            },
            "issues": [],
        },
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert [(item["source"], item["action_type"]) for item in actions] == [
        ("browser_extension", "repair_browser")
    ]


class _FakeConn:
    def __init__(self):
        self.executes = []
        self.resolved = 0

    async def execute(self, query, *args):
        self.executes.append((query, args))
        return "OK"

    async def fetchval(self, query, *args):
        self.executes.append((query, args))
        return self.resolved

    async def fetch(self, query, *args):
        self.executes.append((query, args))
        return [
            {
                "source": "x",
                "action_type": "target_starved",
                "scope_key": "scope",
                "status": "open",
                "priority": 5,
                "reason": "below 5/hour useful-output floor",
                "evidence": {"threshold": 5},
                "first_seen_at": None,
                "last_seen_at": None,
                "resolved_at": None,
            }
        ]


def test_sync_collection_action_queue_upserts_and_resolves_stale_generated_actions():
    conn = _FakeConn()
    source_matrix = {
        "sources": [
            {
                "source": "x",
                "status": "live",
                "current_hour": {"stored": 0},
                "last_complete_hour": {"stored": 0},
            },
        ],
        "browser_extension": {"issues": []},
    }

    result = asyncio.run(sync_collection_action_queue(conn, source_matrix))

    assert result["derived"] == 1
    assert result["open"] == 1
    assert any("CREATE TABLE IF NOT EXISTS collection_action_queue" in query for query, _ in conn.executes)
    upsert = next(args for query, args in conn.executes if "INSERT INTO collection_action_queue" in query)
    assert upsert[0] == "x"
    assert upsert[1] == "target_starved"
    assert upsert[3] == 5
    assert any("NOT EXISTS" in query and "generated_by" in query for query, _ in conn.executes)


def test_derive_collection_actions_suppresses_target_starved_when_rolling_output_passes():
    source_matrix = {
        "sources": [
            {
                "source": "x",
                "status": "live",
                "current_hour": {"media_items": 0, "records": 0, "rate_limits": 0, "access_errors": 0},
                "last_complete_hour": {"media_items": 0, "records": 0, "rate_limits": 0, "access_errors": 0},
                "stored_rolling_60m": 26,
                "observed_rolling_60m": 56,
            },
            {
                "source": "facebook",
                "status": "live",
                "current_hour": {"media_items": 0, "records": 0, "rate_limits": 0, "access_errors": 0},
                "last_complete_hour": {"media_items": 0, "records": 0, "rate_limits": 0, "access_errors": 0},
                "rolling_60m": {"media_items": 18},
            },
        ],
        "browser_extension": {"issues": []},
    }

    actions = derive_collection_actions(source_matrix, min_useful_per_hour=5)

    assert actions == []


def test_direct_health_cleanup_resolves_stale_browser_timeout_and_capture_actions():
    conn = _FakeConn()
    browser_extension = {
        "maintenance": {
            "state": "degraded",
            "detail": "maintenance loop sleeping after nonzero pass",
            "running_stalled": False,
        }
    }

    resolved = asyncio.run(
        resolve_stale_actions_from_direct_health(conn, browser_extension=browser_extension)
    )

    assert resolved == 0
    queries = "\n".join(query for query, _ in conn.executes)
    assert "reason ILIKE 'maintenance pass timed out%'" in queries
    assert "source_health returned to running after stale browser-capture action" in queries
    assert "sh.updated_at > q.last_seen_at" in queries

def test_covered_warning_notes_reports_fresh_content_coverage():
    """Suppressed stalled-warnings must surface as operator-visible coverage notes."""
    source_matrix = {
        "sources": [
            {
                "source": "facebook",
                "status": "live",
                "blocker": {"kind": "browser_capture_stalled", "severity": "warning"},
            },
            {
                "source": "x",
                "status": "live",
                "blocker": {"kind": "none", "severity": "ok"},
            },
        ],
        "browser_extension": {
            "issues": [],
            "ingest": [
                {
                    "platform": "facebook",
                    "endpoint": "posts",
                    "age_seconds": 60,
                    "fresh_after_seconds": 600,
                    "stored": 7,
                    "observed": 9,
                }
            ],
        },
    }

    notes = _caq_module().covered_warning_notes(source_matrix)

    assert [n["source"] for n in notes] == ["facebook"]
    assert notes[0]["kind"] == "browser_capture_stalled"
    assert "fresh" in notes[0]["coverage"]


def test_covered_warning_notes_empty_when_nothing_covered():
    source_matrix = {
        "sources": [
            {
                "source": "facebook",
                "status": "degraded",
                "blocker": {"kind": "browser_capture_stalled", "severity": "warning"},
            }
        ],
        "browser_extension": {"issues": []},
    }

    assert _caq_module().covered_warning_notes(source_matrix) == []


def _caq_module():
    from src.core import collection_action_queue as mod

    return mod
