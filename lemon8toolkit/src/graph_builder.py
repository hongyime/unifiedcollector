"""
Unified Lemon8 Toolkit - Network Graph Builder
Compute graph edges from user relationships
"""
import sqlite3
from typing import Dict, List, Any, Optional
from datetime import datetime

import config


class GraphBuilder:
    """Build network graph from user relationships"""
    
    def __init__(self):
        config.ensure_data_directory()
        self.conn = sqlite3.connect(config.LEMON8_DB_FILE, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        config.configure_db_connection(self.conn)
        self._init_table()
    
    def _init_table(self):
        """Initialize graph_edges table"""
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS graph_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_username TEXT NOT NULL,
                target_username TEXT NOT NULL,
                edge_type TEXT NOT NULL CHECK(edge_type IN ('follows','mentioned','co_tagged')),
                weight INTEGER DEFAULT 1,
                first_seen_ts TEXT DEFAULT (datetime('now')),
                UNIQUE(source_username, target_username, edge_type)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_graph_source 
            ON graph_edges(source_username)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_graph_target 
            ON graph_edges(target_username)
        ''')
        self.conn.commit()
    
    def add_edge(
        self,
        source_username: str,
        target_username: str,
        edge_type: str,
        weight: int = 1
    ):
        """
        Add or update an edge in the graph
        
        Args:
            source_username: Source user
            target_username: Target user
            edge_type: Type of edge (follows, mentioned, co_tagged)
            weight: Edge weight (default: 1)
        """
        source_username = source_username.lstrip('@').lower()
        target_username = target_username.lstrip('@').lower()
        
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO graph_edges (source_username, target_username, edge_type, weight)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_username, target_username, edge_type) 
            DO UPDATE SET weight = weight + ?
        ''', (source_username, target_username, edge_type, weight, weight))
        self.conn.commit()
    
    def build_graph_from_users(self, limit: Optional[int] = None) -> Dict[str, int]:
        """
        Build all graph edges from tracked users and tags.

        Populates three edge types:
        - follows:    A discovered B (A scraped B's profile or B appeared in A's feed)
        - mentioned:  A mentioned B in their profile (related_users from user scrape)
        - co_tagged:  A and B both appeared in the same tag/topic scrape
        """
        import json

        cursor = self.conn.cursor()

        query = 'SELECT username, metadata FROM users'
        if limit:
            query += f' LIMIT {limit}'
        cursor.execute(query)
        users = cursor.fetchall()

        edges_created = {'follows': 0, 'mentioned': 0, 'co_tagged': 0}

        for user in users:
            username = user['username']
            try:
                metadata = json.loads(user['metadata']) if user['metadata'] else {}
            except (json.JSONDecodeError, ValueError):
                continue

            # follows: A discovered B
            discovered_from = metadata.get('discovered_from')
            if discovered_from and discovered_from not in ('feed_scraping', 'seed_feed'):
                self.add_edge(discovered_from, username, 'follows', 1)
                edges_created['follows'] += 1

            # mentioned: A's profile listed B as a related user
            for related in metadata.get('related_users', []):
                related = related.lstrip('@').lower() if related else ''
                if related and related != username:
                    self.add_edge(username, related, 'mentioned', 1)
                    edges_created['mentioned'] += 1

        # co_tagged: users who appeared together in the same tag scrape
        edges_created['co_tagged'] += self._build_co_tagged_edges_from_tags()

        return edges_created

    def _build_co_tagged_edges_from_tags(self) -> int:
        """
        Create co_tagged edges between all user pairs that appeared in the same
        tag/topic scrape. Reads related_users lists from the tags table metadata.
        Returns the number of edges created/incremented.
        """
        import json
        from itertools import combinations

        cursor = self.conn.cursor()
        cursor.execute('SELECT tag_id, metadata FROM tags')

        count = 0
        for row in cursor.fetchall():
            try:
                metadata = json.loads(row['metadata']) if row['metadata'] else {}
            except (json.JSONDecodeError, ValueError):
                continue

            users = [u.lstrip('@').lower() for u in metadata.get('related_users', []) if u]
            users = list(dict.fromkeys(users))  # deduplicate, preserve order

            for a, b in combinations(users, 2):
                if a and b and a != b:
                    self.add_edge(a, b, 'co_tagged', 1)
                    self.add_edge(b, a, 'co_tagged', 1)
                    count += 2

        return count
    
    def get_user_connections(self, username: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get all connections for a user
        
        Args:
            username: Username to get connections for
            
        Returns:
            Dict with 'outgoing' and 'incoming' edge lists
        """
        username = username.lstrip('@').lower()
        cursor = self.conn.cursor()
        
        # Outgoing edges (user → others)
        cursor.execute('''
            SELECT * FROM graph_edges 
            WHERE source_username = ? 
            ORDER BY weight DESC
        ''', (username,))
        outgoing = [dict(row) for row in cursor.fetchall()]
        
        # Incoming edges (others → user)
        cursor.execute('''
            SELECT * FROM graph_edges 
            WHERE target_username = ? 
            ORDER BY weight DESC
        ''', (username,))
        incoming = [dict(row) for row in cursor.fetchall()]
        
        return {
            'outgoing': outgoing,
            'incoming': incoming
        }
    
    def get_graph_stats(self) -> Dict[str, Any]:
        """Get graph statistics"""
        cursor = self.conn.cursor()
        
        # Total edges
        cursor.execute('SELECT COUNT(*) as count FROM graph_edges')
        total_edges = cursor.fetchone()['count']
        
        # Edges by type
        cursor.execute('''
            SELECT edge_type, COUNT(*) as count 
            FROM graph_edges 
            GROUP BY edge_type
        ''')
        edges_by_type = {row['edge_type']: row['count'] for row in cursor.fetchall()}
        
        # Unique nodes
        cursor.execute('''
            SELECT COUNT(DISTINCT username) as count FROM (
                SELECT source_username as username FROM graph_edges
                UNION
                SELECT target_username as username FROM graph_edges
            )
        ''')
        unique_nodes = cursor.fetchone()['count']
        
        # Top connected users (by outgoing edges)
        cursor.execute('''
            SELECT source_username, COUNT(*) as connections 
            FROM graph_edges 
            GROUP BY source_username 
            ORDER BY connections DESC 
            LIMIT 10
        ''')
        top_sources = [dict(row) for row in cursor.fetchall()]
        
        # Top connected users (by incoming edges)
        cursor.execute('''
            SELECT target_username, COUNT(*) as connections 
            FROM graph_edges 
            GROUP BY target_username 
            ORDER BY connections DESC 
            LIMIT 10
        ''')
        top_targets = [dict(row) for row in cursor.fetchall()]
        
        return {
            'total_edges': total_edges,
            'edges_by_type': edges_by_type,
            'unique_nodes': unique_nodes,
            'top_sources': top_sources,
            'top_targets': top_targets
        }
    
    def export_graph_json(self, output_path: str) -> bool:
        """
        Export graph to JSON format (Cytoscape.js compatible)
        
        Args:
            output_path: Path to output JSON file
            
        Returns:
            True if successful, False otherwise
        """
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM graph_edges')
        edges = [dict(row) for row in cursor.fetchall()]
        
        # Build nodes set
        nodes = set()
        for edge in edges:
            nodes.add(edge['source_username'])
            nodes.add(edge['target_username'])
        
        # Format for Cytoscape.js
        graph_data = {
            'nodes': [{'data': {'id': node}} for node in nodes],
            'edges': [
                {
                    'data': {
                        'id': f"e{edge['id']}",
                        'source': edge['source_username'],
                        'target': edge['target_username'],
                        'type': edge['edge_type'],
                        'weight': edge['weight']
                    }
                }
                for edge in edges
            ]
        }
        
        try:
            import json
            from pathlib import Path
            output_file = Path(output_path)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(graph_data, f, indent=2, ensure_ascii=False)
            print(f"✅ Exported graph to {output_path}")
            return True
        except Exception as e:
            print(f"❌ Error exporting graph: {e}")
            return False
    
    def clear_graph(self):
        """Clear all graph edges"""
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM graph_edges')
        self.conn.commit()
        print("🗑️ Graph edges cleared")
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
