"""DB helper for the collector dashboard."""
import os
import psycopg2
from shared.config import Settings

settings = Settings()


def get_connection():
    """Return a psycopg2 connection."""
    return psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        dbname=settings.DB_NAME,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
    )


def check_postgres() -> bool:
    """Return True if Postgres is reachable, False on any exception."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return True
    except Exception:
        return False
