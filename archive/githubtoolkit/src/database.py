"""Database operations for GitHub Toolkit."""
import aiosqlite
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

from src.config import Config


# Database schema
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

-- GitHub users
CREATE TABLE IF NOT EXISTS users (
    username            TEXT PRIMARY KEY,
    user_id             INTEGER UNIQUE,
    display_name        TEXT,
    bio                 TEXT,
    email               TEXT,
    location            TEXT,
    company             TEXT,
    blog_url            TEXT,
    avatar_url          TEXT,
    avatar_md5          TEXT,
    followers_count     INTEGER DEFAULT 0,
    following_count     INTEGER DEFAULT 0,
    public_repos        INTEGER DEFAULT 0,
    is_private          INTEGER DEFAULT 0,
    account_type        TEXT DEFAULT 'User',
    status              TEXT DEFAULT 'active',
    spider_status       TEXT DEFAULT 'pending',
    hop_count           INTEGER DEFAULT 0,
    first_seen_ts       TEXT DEFAULT (datetime('now')),
    last_scraped_ts     TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id);
CREATE INDEX IF NOT EXISTS idx_users_spider_status ON users(spider_status);
CREATE INDEX IF NOT EXISTS idx_users_followers ON users(followers_count DESC);

-- Social graph edges (follows relationships)
CREATE TABLE IF NOT EXISTS graph_edges (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_username     TEXT NOT NULL,
    target_username     TEXT NOT NULL,
    edge_type           TEXT DEFAULT 'follows',
    discovered_ts       TEXT DEFAULT (datetime('now')),
    UNIQUE(source_username, target_username),
    FOREIGN KEY (source_username) REFERENCES users(username) ON DELETE RESTRICT,
    FOREIGN KEY (target_username) REFERENCES users(username) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON graph_edges(source_username);
CREATE INDEX IF NOT EXISTS idx_edges_target ON graph_edges(target_username);

-- Profile photo history (avatar change tracking)
CREATE TABLE IF NOT EXISTS profile_photo_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT NOT NULL,
    user_id             INTEGER,
    avatar_url          TEXT NOT NULL,
    avatar_md5          TEXT NOT NULL,
    avatar_phash        TEXT,
    avatar_blob         BLOB,
    file_path           TEXT,
    detected_at         TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (username) REFERENCES users(username) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_photo_history_username ON profile_photo_history(username, detected_at DESC);

-- Avatar downloads (sequential ID range downloads)
CREATE TABLE IF NOT EXISTS avatar_downloads (
    user_id             INTEGER PRIMARY KEY,
    md5_hash            TEXT NOT NULL,
    file_path           TEXT,
    downloaded_at       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_avatar_hash ON avatar_downloads(md5_hash);

-- Download progress (for sequential range downloads)
CREATE TABLE IF NOT EXISTS download_progress (
    key                 TEXT PRIMARY KEY,
    value               TEXT
);

-- API quota tracking
CREATE TABLE IF NOT EXISTS api_quota_usage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT NOT NULL,
    operation           TEXT NOT NULL,
    requests_used       INTEGER DEFAULT 0,
    recorded_at         TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quota_date ON api_quota_usage(date);

-- Repositories
CREATE TABLE IF NOT EXISTS repositories (
    id                  INTEGER PRIMARY KEY,
    owner               TEXT NOT NULL,
    name                TEXT NOT NULL,
    full_name           TEXT UNIQUE NOT NULL,
    description         TEXT,
    language            TEXT,
    stars               INTEGER DEFAULT 0,
    forks               INTEGER DEFAULT 0,
    is_fork             INTEGER DEFAULT 0,
    created_at          TEXT,
    updated_at          TEXT,
    first_seen_ts       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_repos_owner ON repositories(owner);
CREATE INDEX IF NOT EXISTS idx_repos_language ON repositories(language);
CREATE INDEX IF NOT EXISTS idx_repos_stars ON repositories(stars DESC);

-- Indexes for text search on users
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_location ON users(location);
CREATE INDEX IF NOT EXISTS idx_users_company ON users(company);

-- FTS5 full-text search on user profile fields
CREATE VIRTUAL TABLE IF NOT EXISTS users_fts USING fts5(
    username,
    display_name,
    bio,
    email,
    location,
    company,
    content='users',
    content_rowid='rowid'
);
"""


async def init_database(db_path: Path = Config.DB_PATH):
    """Initialize database schema."""
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()


async def rebuild_fts(db: aiosqlite.Connection):
    """Rebuild FTS5 index from users table."""
    await db.execute("INSERT INTO users_fts(users_fts) VALUES('rebuild')")
    await db.commit()


async def reset_in_progress_spiders(db_path: Path = Config.DB_PATH):
    """Reset spider_status from 'in_progress' to 'pending' on startup (crash recovery)."""
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.execute("UPDATE users SET spider_status='pending' WHERE spider_status='in_progress'")
        await db.commit()


# User operations
async def upsert_user(db: aiosqlite.Connection, user_data: Dict[str, Any]) -> bool:
    """Insert or update user record.
    
    Args:
        db: Database connection
        user_data: User data from GitHub API
        
    Returns:
        True if successful
    """
    try:
        await db.execute("""
            INSERT INTO users (
                username, user_id, display_name, bio, email, location, company, blog_url,
                avatar_url, followers_count, following_count, public_repos, account_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                display_name=excluded.display_name,
                bio=excluded.bio,
                email=COALESCE(excluded.email, users.email),
                location=excluded.location,
                company=excluded.company,
                blog_url=excluded.blog_url,
                avatar_url=excluded.avatar_url,
                followers_count=excluded.followers_count,
                following_count=excluded.following_count,
                public_repos=excluded.public_repos,
                account_type=excluded.account_type,
                last_scraped_ts=datetime('now')
        """, (
            user_data.get('login'),
            user_data.get('id'),
            user_data.get('name'),
            user_data.get('bio'),
            user_data.get('email'),
            user_data.get('location'),
            user_data.get('company'),
            user_data.get('blog'),
            user_data.get('avatar_url'),
            user_data.get('followers', 0),
            user_data.get('following', 0),
            user_data.get('public_repos', 0),
            user_data.get('type', 'User')
        ))
        return True
    except Exception as e:
        print(f"❌ Failed to upsert user: {e}")
        return False


async def add_user_if_not_exists(db: aiosqlite.Connection, username: str, hop_count: int = 0) -> bool:
    """Add user to database if not already exists.
    
    Args:
        db: Database connection
        username: GitHub username
        hop_count: Spider hop count from seed user
        
    Returns:
        True if user was added (new), False if already exists
    """
    try:
        cursor = await db.execute("SELECT username FROM users WHERE username=?", (username,))
        exists = await cursor.fetchone()
        
        if exists:
            return False
        
        await db.execute("""
            INSERT OR IGNORE INTO users (username, spider_status, hop_count)
            VALUES (?, 'pending', ?)
        """, (username, hop_count))
        return True
    except Exception as e:
        print(f"❌ Failed to add user: {e}")
        return False


async def get_pending_spider_users(db: aiosqlite.Connection, limit: int = 100) -> List[str]:
    """Get list of users pending spider.
    
    Args:
        db: Database connection
        limit: Maximum number of users to return
        
    Returns:
        List of usernames
    """
    cursor = await db.execute("""
        SELECT username FROM users
        WHERE spider_status='pending'
        ORDER BY hop_count ASC, first_seen_ts ASC
        LIMIT ?
    """, (limit,))
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def update_spider_status(db: aiosqlite.Connection, username: str, status: str):
    """Update spider status for a user.
    
    Args:
        db: Database connection
        username: GitHub username
        status: New status ('pending', 'in_progress', 'completed')
    """
    await db.execute("""
        UPDATE users SET spider_status=?, last_scraped_ts=datetime('now')
        WHERE username=?
    """, (status, username))


# Graph edge operations
async def add_edge(db: aiosqlite.Connection, source: str, target: str, edge_type: str = 'follows'):
    """Add graph edge (relationship).
    
    Args:
        db: Database connection
        source: Source username
        target: Target username
        edge_type: Type of relationship (default: 'follows')
    """
    try:
        await db.execute("""
            INSERT OR IGNORE INTO graph_edges (source_username, target_username, edge_type)
            VALUES (?, ?, ?)
        """, (source, target, edge_type))
    except Exception as e:
        print(f"❌ Failed to add edge: {e}")


async def get_graph_data(db: aiosqlite.Connection) -> Dict[str, Any]:
    """Get complete graph data for visualization.
    
    Args:
        db: Database connection
        
    Returns:
        Dict with nodes and edges
    """
    # Get nodes
    cursor = await db.execute("""
        SELECT username, user_id, display_name, avatar_url, followers_count, following_count, bio
        FROM users
        WHERE spider_status='completed'
    """)
    nodes = []
    async for row in cursor:
        nodes.append({
            'id': row[0],
            'user_id': row[1],
            'name': row[2] or row[0],
            'avatar': row[3],
            'followers': row[4],
            'following': row[5],
            'bio': row[6]
        })
    
    # Get edges
    cursor = await db.execute("""
        SELECT source_username, target_username, edge_type
        FROM graph_edges
    """)
    edges = []
    async for row in cursor:
        edges.append({
            'source': row[0],
            'target': row[1],
            'type': row[2]
        })
    
    return {'nodes': nodes, 'edges': edges}


# Statistics
async def get_stats(db: aiosqlite.Connection) -> Dict[str, Any]:
    """Get database statistics.
    
    Args:
        db: Database connection
        
    Returns:
        Dict with statistics
    """
    stats = {}
    
    # Total users
    cursor = await db.execute("SELECT COUNT(*) FROM users")
    stats['total_users'] = (await cursor.fetchone())[0]
    
    # Users by spider status
    cursor = await db.execute("SELECT spider_status, COUNT(*) FROM users GROUP BY spider_status")
    stats['spider_status'] = {row[0]: row[1] for row in await cursor.fetchall()}
    
    # Total edges
    cursor = await db.execute("SELECT COUNT(*) FROM graph_edges")
    stats['total_edges'] = (await cursor.fetchone())[0]
    
    # Total avatars downloaded
    cursor = await db.execute("SELECT COUNT(*) FROM avatar_downloads")
    stats['avatars_downloaded'] = (await cursor.fetchone())[0]
    
    # Top users by followers
    cursor = await db.execute("""
        SELECT username, followers_count FROM users
        WHERE followers_count > 0
        ORDER BY followers_count DESC
        LIMIT 10
    """)
    stats['top_users'] = [{'username': row[0], 'followers': row[1]} for row in await cursor.fetchall()]
    
    return stats


# Avatar download tracking
async def save_avatar_download(db: aiosqlite.Connection, user_id: int, md5_hash: str, file_path: str):
    """Save avatar download record.
    
    Args:
        db: Database connection
        user_id: GitHub user ID
        md5_hash: MD5 hash of avatar
        file_path: Path to saved file
    """
    await db.execute("""
        INSERT OR REPLACE INTO avatar_downloads (user_id, md5_hash, file_path)
        VALUES (?, ?, ?)
    """, (user_id, md5_hash, file_path))


async def get_downloaded_hashes(db: aiosqlite.Connection) -> set:
    """Get set of all downloaded avatar hashes."""
    cursor = await db.execute("SELECT md5_hash FROM avatar_downloads")
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def search_users(db: aiosqlite.Connection, query: str = None, email_domain: str = None,
                       location: str = None, company: str = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Search users by bio/email/location/company using FTS5 or column filters."""
    conditions = []
    params = []

    if query:
        # FTS5 search across bio, email, username, display_name, location, company
        cursor = await db.execute("""
            SELECT u.username, u.user_id, u.display_name, u.bio, u.email,
                   u.location, u.company, u.followers_count, u.following_count, u.avatar_url
            FROM users u
            JOIN users_fts f ON u.rowid = f.rowid
            WHERE users_fts MATCH ?
            ORDER BY u.followers_count DESC
            LIMIT ?
        """, (query, limit))
    else:
        if email_domain:
            conditions.append("email LIKE ?")
            params.append(f"%@{email_domain}")
        if location:
            conditions.append("location LIKE ?")
            params.append(f"%{location}%")
        if company:
            conditions.append("company LIKE ?")
            params.append(f"%{company}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        cursor = await db.execute(f"""
            SELECT username, user_id, display_name, bio, email,
                   location, company, followers_count, following_count, avatar_url
            FROM users {where}
            ORDER BY followers_count DESC
            LIMIT ?
        """, params)

    rows = await cursor.fetchall()
    return [
        {
            'username': r[0], 'user_id': r[1], 'display_name': r[2],
            'bio': r[3], 'email': r[4], 'location': r[5], 'company': r[6],
            'followers': r[7], 'following': r[8], 'avatar_url': r[9]
        }
        for r in rows
    ]


async def upsert_repository(db: aiosqlite.Connection, repo_data: Dict[str, Any]) -> bool:
    """Insert or update repository record."""
    try:
        await db.execute("""
            INSERT INTO repositories (id, owner, name, full_name, description, language,
                                      stars, forks, is_fork, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(full_name) DO UPDATE SET
                description=excluded.description,
                language=excluded.language,
                stars=excluded.stars,
                forks=excluded.forks,
                updated_at=excluded.updated_at
        """, (
            repo_data.get('id'),
            repo_data.get('owner', {}).get('login') if isinstance(repo_data.get('owner'), dict) else repo_data.get('owner'),
            repo_data.get('name'),
            repo_data.get('full_name'),
            repo_data.get('description'),
            repo_data.get('language'),
            repo_data.get('stargazers_count', 0),
            repo_data.get('forks_count', 0),
            1 if repo_data.get('fork') else 0,
            repo_data.get('created_at'),
            repo_data.get('updated_at'),
        ))
        return True
    except Exception as e:
        print(f"❌ Failed to upsert repo: {e}")
        return False


async def add_contribution_edge(db: aiosqlite.Connection, source: str, target: str,
                                 edge_type: str, repo_full_name: str = None):
    """Add a contribution-based graph edge."""
    try:
        await db.execute("""
            INSERT OR IGNORE INTO graph_edges (source_username, target_username, edge_type)
            VALUES (?, ?, ?)
        """, (source, target, edge_type))
    except Exception as e:
        print(f"❌ Failed to add contribution edge: {e}")
