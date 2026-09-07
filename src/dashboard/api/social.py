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

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role

logger = logging.getLogger(__name__)


def _lookup(name: str):
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


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
async def social_stats():
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
                            limit: int = 50, offset: int = 0):
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
async def social_scrape_config():
    """Per-platform scrape enable state for the browser extension (rotator control).

    The activity rotator writes collection_schedules.enabled for the browser
    group; the extension polls this to skip scraping paused platforms so the
    shared CDP Chrome never drives more than the rotation width at once. Reads
    fail OPEN (all enabled) so a config error never silently halts collection.
    """
    browser_platforms = ["x", "instagram", "facebook", "threads", "tiktok", "lemon8", "strava"]
    enabled_map = {p: True for p in browser_platforms}
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT source, enabled FROM collection_schedules WHERE source = ANY($1::text[])",
                browser_platforms,
            )
        for r in rows:
            enabled_map[str(r["source"])] = bool(r["enabled"])
    except Exception as exc:  # noqa: BLE001 - fail open: never block scraping on config read error
        return {
            "enabled": browser_platforms,
            "disabled": [],
            "fail_open": True,
            "error": exc.__class__.__name__,
        }
    return {
        "enabled": sorted([p for p, on in enabled_map.items() if on]),
        "disabled": sorted([p for p, on in enabled_map.items() if not on]),
        "checked_at": datetime.now(timezone.utc).isoformat(),
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
async function stats(){const s=await (await fetch('/social/stats')).json();
document.getElementById('stats').innerHTML=s.users.map(u=>`<div class=chip><b>${u.platform}</b> ${u.users} users · ${u.with_photo} 📷</div>`).join('')+
Object.entries(s.content).map(([k,v])=>`<div class=chip>${k}: <b>${v??'—'}</b></div>`).join('');
const sel=document.getElementById('platform');s.users.forEach(u=>{const o=document.createElement('option');o.value=u.platform;o.textContent=u.platform;sel.appendChild(o)});}
async function load(){const p=document.getElementById('platform').value,q=document.getElementById('q').value;
const r=await (await fetch(`/social/users?limit=100&platform=${encodeURIComponent(p)}&q=${encodeURIComponent(q)}`)).json();
document.getElementById('rows').innerHTML=r.map(u=>`<tr><td>${u.profile_photo_url?`<img src="${u.profile_photo_url}">`:''}</td>
<td>${u.platform}</td><td>${u.username||'<span class=muted>—</span>'}</td><td>${u.display_name||''}</td>
<td>${u.times_seen}</td><td class=muted>${(u.contexts||[]).join(', ')}</td></tr>`).join('');}
document.getElementById('platform').onchange=load;stats().then(load);
</script></body></html>"""


@router.get("/social", response_class=HTMLResponse)
async def social_page():
    return HTMLResponse(_SOCIAL_HTML)
