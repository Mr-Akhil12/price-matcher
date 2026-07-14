"""
Turso + libsql-client adapter for Product Price Matcher v2.

Uses the existing akhils-budget database with prefixed table names
to avoid conflicts. Falls back to local SQLite if Turso is unavailable.
"""
import os
import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Any, Optional

# ─── CONFIG ────────────────────────────────────────────────────────────────────

USE_TURSO = bool(os.environ.get('TURSO_AUTH_TOKEN'))
DB_NAME = 'product_matcher'

def _turso_conn():
    """Create a Turso connection. Lazily imported to avoid import errors."""
    from libsql_client import create_client_sync
    return create_client_sync(
        url=os.environ['TURSO_DATABASE_URL'],
        auth_token=os.environ['TURSO_AUTH_TOKEN'],
    )

def _sqlite_path():
    """Local SQLite path — DATA_DIR or current working directory."""
    data_dir = os.environ.get('DATA_DIR', os.getcwd())
    return os.path.join(data_dir, f'{DB_NAME}.db')

# ─── CONNECTION CONTEXT MANAGER ───────────────────────────────────────────────

@contextmanager
def get_db_connection():
    """
    Unified connection context manager.
    Uses Turso if TURSO_AUTH_TOKEN is set, otherwise local SQLite.
    """
    if USE_TURSO:
        conn = _turso_conn()
        conn.row_factory = _dict_row  # applies to Turso rows
        try:
            yield TursoWrapper(conn)
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(_sqlite_path())
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

class TursoWrapper:
    """
    Thin adapter so Turso ResultSet rows behave like sqlite3.Row dicts.
    Supports: row[key], len(row), iter(row), dict(row).
    """
    def __init__(self, conn):
        self._conn = conn
        self._rows = []
        self._columns = []

    def execute(self, sql: str, params=None):
        params = params or ()
        result = self._conn.execute(sql, params)
        self._rows = [dict(row) for row in result]
        self._columns = list(self._rows[0].keys()) if self._rows else []
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self):
        return len(self._rows)

    def commit(self):
        pass  # Turso auto-commits for DML

def _dict_row(cursor, row):
    """sqlite3 row factory — converts to dict."""
    return dict(zip([d[0] for d in cursor.description], row))
