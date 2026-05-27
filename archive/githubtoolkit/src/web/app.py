"""Flask web application for GitHub social graph visualization."""
from flask import Flask, render_template, jsonify, request
import aiosqlite
import asyncio
from pathlib import Path

from src.config import Config
from src.database import get_stats, search_users
from src.github_client import GitHubAPIClient
from src.pat_manager import PATManager

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

MAX_GRAPH_NODES = 500  # max nodes sent to browser per request


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/graph')
def api_graph():
    """Paginated graph endpoint. Params: limit, offset, min_followers, search."""
    try:
        limit = min(int(request.args.get('limit', MAX_GRAPH_NODES)), MAX_GRAPH_NODES)
        offset = int(request.args.get('offset', 0))
        min_followers = int(request.args.get('min_followers', 0))
        search = request.args.get('search', '').strip()

        async def get_data():
            async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
                # Build node filter
                conditions = ["spider_status='completed'"]
                params = []
                if min_followers > 0:
                    conditions.append("followers_count >= ?")
                    params.append(min_followers)
                if search:
                    conditions.append("(username LIKE ? OR display_name LIKE ?)")
                    params.extend([f"%{search}%", f"%{search}%"])

                where = "WHERE " + " AND ".join(conditions)
                params_full = params + [limit, offset]

                cursor = await db.execute(f"""
                    SELECT username, user_id, display_name, avatar_url,
                           followers_count, following_count, bio
                    FROM users {where}
                    ORDER BY followers_count DESC
                    LIMIT ? OFFSET ?
                """, params_full)

                nodes = []
                usernames = set()
                async for row in cursor:
                    nodes.append({
                        'id': row[0], 'user_id': row[1],
                        'name': row[2] or row[0], 'avatar': row[3],
                        'followers': row[4], 'following': row[5], 'bio': row[6]
                    })
                    usernames.add(row[0])

                # Only return edges between visible nodes
                edges = []
                if usernames:
                    placeholders = ','.join('?' * len(usernames))
                    un_list = list(usernames)
                    cursor2 = await db.execute(f"""
                        SELECT source_username, target_username, edge_type
                        FROM graph_edges
                        WHERE source_username IN ({placeholders})
                          AND target_username IN ({placeholders})
                    """, un_list + un_list)
                    async for row in cursor2:
                        edges.append({'source': row[0], 'target': row[1], 'type': row[2]})

                # Total count for pagination
                cursor3 = await db.execute(
                    f"SELECT COUNT(*) FROM users {where}", params)
                total = (await cursor3.fetchone())[0]

                return {'nodes': nodes, 'edges': edges,
                        'total': total, 'limit': limit, 'offset': offset}

        return jsonify(run_async(get_data()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users')
def api_users():
    """Paginated user list. Params: page, per_page, search, min_followers."""
    try:
        per_page = min(int(request.args.get('per_page', 50)), 200)
        page = max(int(request.args.get('page', 1)), 1)
        offset = (page - 1) * per_page
        search = request.args.get('search', '').strip()
        min_followers = int(request.args.get('min_followers', 0))

        async def get_data():
            async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
                conditions = []
                params = []
                if search:
                    conditions.append("(username LIKE ? OR display_name LIKE ? OR bio LIKE ?)")
                    params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
                if min_followers > 0:
                    conditions.append("followers_count >= ?")
                    params.append(min_followers)
                where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

                cursor = await db.execute(f"""
                    SELECT username, user_id, display_name, bio, email,
                           location, company, followers_count, following_count,
                           avatar_url, spider_status
                    FROM users {where}
                    ORDER BY followers_count DESC
                    LIMIT ? OFFSET ?
                """, params + [per_page, offset])

                users = [
                    {'username': r[0], 'user_id': r[1], 'display_name': r[2],
                     'bio': r[3], 'email': r[4], 'location': r[5], 'company': r[6],
                     'followers': r[7], 'following': r[8], 'avatar_url': r[9],
                     'spider_status': r[10]}
                    for r in await cursor.fetchall()
                ]

                c2 = await db.execute(f"SELECT COUNT(*) FROM users {where}", params)
                total = (await c2.fetchone())[0]

                return {'users': users, 'total': total, 'page': page,
                        'per_page': per_page, 'pages': (total + per_page - 1) // per_page}

        return jsonify(run_async(get_data()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/search')
def api_search():
    """Search users by bio/email/location/company. Params: q, email_domain, location, company."""
    try:
        q = request.args.get('q', '').strip()
        email_domain = request.args.get('email_domain', '').strip()
        location = request.args.get('location', '').strip()
        company = request.args.get('company', '').strip()
        limit = min(int(request.args.get('limit', 100)), 500)

        async def get_data():
            async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
                return await search_users(
                    db,
                    query=q or None,
                    email_domain=email_domain or None,
                    location=location or None,
                    company=company or None,
                    limit=limit
                )

        results = run_async(get_data())
        return jsonify({'results': results, 'count': len(results)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stats')
def api_stats():
    try:
        async def get_data():
            async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
                return await get_stats(db)
        return jsonify(run_async(get_data()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/user/<username>')
def api_user(username):
    try:
        async def get_data():
            async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
                cursor = await db.execute("""
                    SELECT username, user_id, display_name, bio, email, location,
                           company, blog_url, avatar_url, followers_count, following_count,
                           public_repos, account_type, spider_status
                    FROM users WHERE username=?
                """, (username,))
                row = await cursor.fetchone()
                if not row:
                    return None

                # Get repos
                rc = await db.execute(
                    "SELECT full_name, language, stars, description FROM repositories WHERE owner=? ORDER BY stars DESC LIMIT 10",
                    (username,))
                repos = [{'full_name': r[0], 'language': r[1], 'stars': r[2], 'description': r[3]}
                         for r in await rc.fetchall()]

                return {
                    'username': row[0], 'user_id': row[1], 'display_name': row[2],
                    'bio': row[3], 'email': row[4], 'location': row[5],
                    'company': row[6], 'blog_url': row[7], 'avatar_url': row[8],
                    'followers_count': row[9], 'following_count': row[10],
                    'public_repos': row[11], 'account_type': row[12],
                    'spider_status': row[13], 'repos': repos
                }

        data = run_async(get_data())
        if data is None:
            return jsonify({'error': 'User not found'}), 404
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/follow/<username>', methods=['POST'])
def api_follow(username):
    try:
        pat_manager = PATManager()
        pat = pat_manager.load_pat()
        if not pat:
            return jsonify({'error': 'PAT token not configured'}), 401

        async def follow():
            async with GitHubAPIClient(pat) as client:
                return await client.follow_user(username)

        success = run_async(follow())
        if success:
            return jsonify({'success': True, 'message': f'Followed {username}'})
        return jsonify({'error': 'Failed to follow user'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def run_server(host: str = None, port: int = None, debug: bool = False):
    host = host or Config.FLASK_HOST
    port = port or Config.FLASK_PORT
    print(f"🌐 Starting GitHub Toolkit web server...")
    print(f"   URL: http://{host}:{port}")
    print(f"   Press Ctrl+C to stop")
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    run_server()
