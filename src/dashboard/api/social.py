"""Social registry routes for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 15
(cluster 4). Covers:

* ``/social/follow-edges/stats``
* ``/social/stats``
* ``/social/network``
* ``/social/users``
* ``/social/scrape-config``
* ``/social`` (HTML page)
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.core.recovery_profiles import record_clean_recovery_pass

logger = logging.getLogger(__name__)


_BROWSER_NORMAL_CYCLE_REST_MS = {
    "instagram": (180_000, 360_000),
    "facebook": (120_000, 240_000),
    "threads": (120_000, 240_000),
    "x": (120_000, 240_000),
    "tiktok": (180_000, 300_000),
    "lemon8": (60_000, 150_000),
    "strava": (90_000, 180_000),
    "pinterest": (240_000, 420_000),
    "snapchat": (300_000, 480_000),
    "airbnb": (300_000, 480_000),
}


def _lookup(name: str):
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


router = APIRouter()


@router.get("/social/follow-edges/stats")
async def follow_edges_stats(_user: dict = Depends(require_role("viewer"))):
    """Per-account follow graph (from follow_edges): how many followers/following
    were captured for EACH of your owned accounts, distinctly. Populated by the
    extension self-seed (per account, as you rotate logins) + the headless path."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT platform, owner_account,
                       count(*) FILTER (WHERE direction = 'follower')  AS followers,
                       count(*) FILTER (WHERE direction = 'following') AS following,
                       max(last_seen) AS last_seen
                FROM follow_edges
                GROUP BY platform, owner_account
                ORDER BY platform, owner_account
                """
            )
        except Exception:
            rows = []
    return [dict(r) for r in rows]


@router.get("/social/stats")
async def social_stats(_user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT platform, count(*) AS users, count(*) FILTER (WHERE profile_photo_url IS NOT NULL) AS with_photo FROM social_users GROUP BY platform ORDER BY 2 DESC"
        )
        content = {}
        for label, q in (
            ("instagram_posts", "SELECT count(*) FROM instagram_posts"),
            ("instagram_comments", "SELECT count(*) FROM instagram_comments"),
            ("threads_posts", "SELECT count(*) FROM threads_posts"),
            ("facebook_posts", "SELECT count(*) FROM facebook_posts"),
        ):
            try:
                content[label] = await conn.fetchval(q)
            except Exception:
                content[label] = None
    return {"users": [dict(r) for r in users], "content": content}


@router.get("/social/network")
async def social_network(_user: dict = Depends(require_role("viewer"))):
    """Your REAL captured network per platform, from social_users contexts.

    The Targets table is a tiny manual seed list; the actual follow graph lives
    here (e.g. tens of thousands of instagram users, thousands you follow). This
    surfaces following/followers so the Targets page can show your real reach
    instead of implying "2 strava targets" is your whole network.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT platform,
                   count(*)                                          AS total,
                   count(*) FILTER (WHERE 'follow'    = ANY(contexts)) AS following,
                   count(*) FILTER (WHERE 'follower'  = ANY(contexts)) AS followers
            FROM social_users
            GROUP BY platform
            ORDER BY total DESC
            """
        )
    return [dict(r) for r in rows]


@router.get("/social/users")
async def social_users_list(platform: str | None = None, q: str | None = None,
                            limit: int = 50, offset: int = 0,
                            _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 200))
    clauses, args = [], []
    if platform:
        args.append(platform); clauses.append(f"platform = ${len(args)}")
    if q:
        args.append(f"%{q}%"); clauses.append(f"(username ILIKE ${len(args)} OR display_name ILIKE ${len(args)})")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.extend([limit, offset])
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT platform, uid, username, display_name, profile_photo_url, times_seen, contexts, last_seen "
            f"FROM social_users {where} ORDER BY times_seen DESC, last_seen DESC LIMIT ${len(args)-1} OFFSET ${len(args)}",
            *args,
        )
    return [dict(r) for r in rows]


@router.get("/social/scrape-config")
async def social_scrape_config(_user: dict = Depends(require_role("viewer"))):
    """Per-platform browser pacing state for the extension.

    The activity rotator writes collection_schedules.enabled for the browser
    group; the extension polls this to skip scraping paused platforms so the
    shared CDP Chrome never drives more than the rotation width at once. Reads
    fail OPEN (all enabled) so a config error never silently halts collection.
    """
    browser_platforms = [
        "x", "instagram", "facebook", "threads", "tiktok", "lemon8",
        "strava", "pinterest", "snapchat", "airbnb",
    ]
    enabled_map = {p: True for p in browser_platforms}
    profiles = {
        p: {
            "mode": "near_live_polling",
            "volume_profile": "normal",
            "recovery_profiles": [],
            # Tab eligibility is deliberately based on an explicit health or
            # control result, never on the age of a last check.
            "tab_eligible": True,
            "tab_block_reason": None,
            "tab_retry_at": None,
            "health_status": None,
            "last_successful_check_at": None,
            "health_checked_at": None,
            "cycle_rest_min_ms": _BROWSER_NORMAL_CYCLE_REST_MS[p][0],
            "cycle_rest_max_ms": _BROWSER_NORMAL_CYCLE_REST_MS[p][1],
            "cooldown_rest_ms": min(_BROWSER_NORMAL_CYCLE_REST_MS[p][1], 300_000),
        }
        for p in browser_platforms
    }
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT source, enabled FROM collection_schedules WHERE source = ANY($1::text[])",
                browser_platforms,
            )

        async def optional_rows(query: str):
            async with pool.acquire() as optional_conn:
                return await optional_conn.fetch(query, browser_platforms)

        for r in rows:
            enabled_map[str(r["source"])] = bool(r["enabled"])
        # Schedule disablement is the browser's explicit manual/rotation stop.
        # It must win over every automatic profile below.
        for source, enabled in enabled_map.items():
            if not enabled:
                profiles[source].update(
                    mode="manually_paused",
                    volume_profile="manual",
                    tab_eligible=False,
                    tab_block_reason="manually_paused",
                )

        # The newer tables are optional during a rolling migration. Their
        # absence must retain the historical fail-open scrape-config behavior.
        try:
            controls = await optional_rows(
                """SELECT source_name, state, enabled, blocked_until
                   FROM source_controls WHERE source_name = ANY($1::text[])"""
            )
            now = datetime.now(timezone.utc)
            for control in controls:
                source = str(control.get("source_name") or "").lower()
                if source not in profiles or profiles[source]["mode"] == "manually_paused":
                    continue
                if control.get("state") == "PAUSED" or not control.get("enabled", True):
                    profiles[source].update(
                        mode="manually_paused",
                        volume_profile="manual",
                        tab_eligible=False,
                        tab_block_reason="manually_paused",
                    )
                    enabled_map[source] = False
                elif control.get("state") in {"RATE_LIMITED", "AUTH_FAILED"}:
                    blocked_until = control.get("blocked_until")
                    profiles[source].update(
                        tab_eligible=False,
                        tab_block_reason=str(control["state"]).lower(),
                        tab_retry_at=_iso(blocked_until),
                    )
                    if blocked_until and blocked_until > now:
                        profiles[source].update(mode="cooldown", volume_profile="cooldown")
        except Exception:
            pass
        try:
            health_rows = await optional_rows(
                """SELECT source, status, last_success_at, last_checked_at, blocker
                   FROM source_health WHERE source = ANY($1::text[])"""
            )
            for health in health_rows:
                source = str(health.get("source") or "").lower()
                if source not in profiles:
                    continue
                profile = profiles[source]
                profile.update(
                    health_status=health.get("status"),
                    last_successful_check_at=_iso(health.get("last_success_at")),
                    health_checked_at=_iso(health.get("last_checked_at")),
                )
                # Manual pauses remain the operator's decision regardless of
                # a later health write.
                if profile["mode"] == "manually_paused":
                    continue
                if health.get("status") in {"degraded", "dead", "auth_paused"}:
                    profile.update(
                        tab_eligible=False,
                        tab_block_reason=health.get("blocker") or health["status"],
                    )
        except Exception:
            pass
        try:
            recoveries = await optional_rows(
                """SELECT source_name, account_identity, restricted_until,
                          restricted_until <= now() AS exit_required
                   FROM source_recovery_profiles
                   WHERE state='restricted'
                     AND source_name = ANY($1::text[])""",
            )
            for recovery in recoveries:
                source = str(recovery.get("source_name") or "").lower()
                if source not in profiles or profiles[source]["mode"] == "manually_paused":
                    continue
                account = str(recovery.get("account_identity") or "").strip()
                profiles[source]["recovery_profiles"].append({
                    "account": account,
                    "exit_required": bool(recovery.get("exit_required")),
                })
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001 - fail open: never block scraping on config read error
        return {
            "enabled": browser_platforms,
            "disabled": [],
            "profiles": profiles,
            "fail_open": True,
            "error": exc.__class__.__name__,
        }
    return {
        "enabled": sorted([p for p, on in enabled_map.items() if on]),
        "disabled": sorted([p for p, on in enabled_map.items() if not on]),
        "profiles": profiles,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/social/recovery-clean-pass")
async def social_recovery_clean_pass(
    source: str = Body(...),
    account: str | None = Body(None),
    _user: dict = Depends(require_role("viewer")),
):
    """Accept only a browser's already-bounded Instagram exit observation."""
    if source.strip().lower() != "instagram":
        raise HTTPException(status_code=400, detail="recovery clean pass is only supported for instagram")
    profile = await record_clean_recovery_pass(await _get_pool(), "instagram", account=account)
    if profile is None:
        raise HTTPException(status_code=404, detail="no matching recovery profile")
    return {
        "state": profile.get("state"),
        "clean_passes": profile.get("clean"),
        "normal": profile.get("state") == "normal",
    }


_SOCIAL_HTML = """<!doctype html><html><head><meta charset=utf-8><title>Social registry</title>
<style>body{font:13px system-ui;background:#0f1117;color:#e6e8ee;margin:0;padding:16px}
h1{font-size:16px}.stats{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 16px}
.chip{background:#171a23;border:1px solid #2a2f3e;border-radius:8px;padding:8px 12px}
.chip b{color:#818cf8}input,select{background:#1e2230;color:#e6e8ee;border:1px solid #2a2f3e;border-radius:6px;padding:6px 8px;font:inherit}
table{border-collapse:collapse;width:100%;margin-top:12px}td,th{border-bottom:1px solid #2a2f3e;padding:6px 8px;text-align:left}
img{width:32px;height:32px;border-radius:50%;object-fit:cover;background:#222}.muted{color:#8b91a3}</style></head>
<body><h1>🧑‍🤝‍🧑 Social registry</h1><div class=stats id=stats></div>
<div><select id=platform><option value="">all platforms</option></select>
<input id=q placeholder="search username / name" oninput="load()"></div>
<table><thead><tr><th></th><th>platform</th><th>username</th><th>name</th><th>seen</th><th>contexts</th></tr></thead><tbody id=rows></tbody></table>
<script>
function esc(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function authHeaders(){try{const t=localStorage.getItem('auth_token');return t?{Authorization:'Bearer '+t}:{};}catch(e){return {};}}
async function stats(){const s=await (await fetch('/social/stats',{headers:authHeaders()})).json();
document.getElementById('stats').innerHTML=s.users.map(u=>`<div class=chip><b>${esc(u.platform)}</b> ${esc(String(u.users))} users · ${esc(String(u.with_photo))} 📷</div>`).join('')+
Object.entries(s.content).map(([k,v])=>`<div class=chip>${esc(k)}: <b>${esc(String(v??'—'))}</b></div>`).join('');
const sel=document.getElementById('platform');s.users.forEach(u=>{const o=document.createElement('option');o.value=u.platform;o.textContent=u.platform;sel.appendChild(o)});}
async function load(){const p=document.getElementById('platform').value,q=document.getElementById('q').value;
const r=await (await fetch(`/social/users?limit=100&platform=${encodeURIComponent(p)}&q=${encodeURIComponent(q)}`,{headers:authHeaders()})).json();
document.getElementById('rows').innerHTML=r.map(u=>`<tr><td>${u.profile_photo_url?'<img src="'+esc(u.profile_photo_url)+'">':''}</td>
<td>${esc(u.platform)}</td><td>${u.username?esc(u.username):'<span class=muted>—</span>'}</td><td>${esc(u.display_name||'')}</td>
<td>${esc(String(u.times_seen))}</td><td class=muted>${esc((u.contexts||[]).join(', '))}</td></tr>`).join('');}
document.getElementById('platform').onchange=load;stats().then(load);
</script></body></html>"""


@router.get("/social", response_class=HTMLResponse)
async def social_page(_user: dict = Depends(require_role("viewer"))):
    return HTMLResponse(_SOCIAL_HTML)
