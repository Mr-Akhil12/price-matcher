"""
Database utilities for Product Price Matcher v2.
Uses Turso (libsql) if TURSO_AUTH_TOKEN is set, else local SQLite.
Table names are prefixed with 'pm_' to coexist with other apps in the same DB.
"""
import sqlite3
import os
from contextlib import contextmanager
from urllib.parse import urlparse
from typing import Dict, List, Any, Optional

USE_TURSO = bool(os.environ.get('TURSO_AUTH_TOKEN'))

if USE_TURSO:
    import httpx
    from urllib.parse import urlparse

    TURSO_DB_URL = os.environ['TURSO_DB_URL']
    TURSO_AUTH_TOKEN = os.environ['TURSO_AUTH_TOKEN']
    _parsed = urlparse(TURSO_DB_URL)
    _http_url = f'https://{_parsed.netloc}/v2/pipeline'
    _headers = {'Authorization': f'Bearer {TURSO_AUTH_TOKEN}'}

    class Row:
        """Dict-like row that also supports positional indexing (row[0], row['col']).
        Values are coerced to proper Python types. Implements Mapping so dict(row) works."""
        def __init__(self, data: dict):
            self._data = {}
            self._vals = []
            for k, v in data.items():
                if isinstance(v, dict):
                    t = v.get('type', 'text')
                    rv = v.get('value')
                    if t == 'integer': rv = int(rv) if rv is not None else None
                    elif t == 'real': rv = float(rv) if rv is not None else None
                    elif t == 'null': rv = None
                    v = rv
                self._data[k] = v
                self._vals.append(v)

        def __getitem__(self, key):
            if isinstance(key, int):
                return self._vals[key]
            return self._data[key]

        def __getattr__(self, key):
            return self._data.get(key)

        def keys(self):
            return self._data.keys()

        def __len__(self):
            return len(self._vals)

        def __iter__(self):
            return iter(self._vals)

        def __repr__(self):
            return f'<Row {self._data}>'

    class TursoResult:
        """Wraps a libsql HTTP /pipeline response as a dict-accessible result."""
        def __init__(self, resp_data):
            self._data = resp_data
            results = resp_data.get('results', [])
            self._result = results[0] if results else {}
            rows_data = self._result.get('response', {}).get('result', {}).get('rows', [])
            self._rows = []
            cols = self._result.get('response', {}).get('result', {}).get('cols', [])
            for row in rows_data:
                d = {}
                for i, col in enumerate(cols):
                    v = row[i]
                    if isinstance(v, dict):
                        t = v.get('type', 'text')
                        rv = v.get('value')
                        if t == 'integer': rv = int(rv) if rv is not None else None
                        elif t == 'real': rv = float(rv) if rv is not None else None
                        elif t == 'null': rv = None
                        d[col['name']] = rv
                    else:
                        d[col['name']] = v
                self._rows.append(Row(d))

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

        @property
        def columns(self):
            return [c['name'] for c in self._result.get('response', {}).get('result', {}).get('cols', [])]

    @contextmanager
    def get_db_connection():
        """Context manager using Turso HTTP API (no WebSocket required)."""
        with httpx.Client(base_url=f'https://{_parsed.netloc}', headers=_headers, timeout=30.0) as client:
            baton = [None]  # mutable holder for transaction baton

            def execute(sql, params=None):
                params = list(params) if params else []
                def _arg(p):
                    if p is None: return {'type': 'null', 'value': None}
                    if isinstance(p, bool): return {'type': 'integer', 'value': '1' if p else '0'}
                    if isinstance(p, int): return {'type': 'integer', 'value': str(p)}
                    if isinstance(p, float): return {'type': 'real', 'value': str(p)}
                    return {'type': 'text', 'value': str(p)}
                payload = {
                    'requests': [{
                        'type': 'execute',
                        'stmt': {'sql': sql, 'args': [_arg(p) for p in params]}
                    }]
                }
                if baton[0]:
                    payload['baton'] = baton[0]
                resp = client.post('/v2/pipeline', json=payload)
                resp.raise_for_status()
                data = resp.json()
                baton[0] = data.get('baton')
                return TursoResult(data)

            yield _TursoConn(execute)
            baton[0] = None

    class _TursoConn:
        """Connection-like object returned by get_db_connection.
        execute() stores the result; fetchall()/fetchone() return it.
        cursor() returns self so both conn.execute() and cursor.execute() work."""
        def __init__(self, execute_fn):
            self._execute = execute_fn
            self._last_result = None

        def execute(self, sql, params=None):
            self._last_result = self._execute(sql, params)
            return self

        def cursor(self):
            return self

        def fetchall(self):
            return self._last_result.fetchall() if self._last_result else []

        def fetchone(self):
            return self._last_result.fetchone() if self._last_result else None

        def commit(self):
            pass  # autocommit

        def close(self):
            pass

else:
    DB_NAME = 'product_matcher'

    def get_db_path():
        data_dir = os.environ.get('DATA_DIR', os.getcwd())
        return os.path.join(data_dir, f'{DB_NAME}.db')

    @contextmanager
    def get_db_connection():
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA INIT
# ─────────────────────────────────────────────────────────────────────────────

_T = 'pm_'  # table name prefix

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {_T}products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    collection TEXT,
    base_price REAL NOT NULL DEFAULT 0.0,
    weight REAL,
    cost_of_goods REAL,
    brand TEXT,
    track_inventory INTEGER DEFAULT 0,
    inventory INTEGER,
    done INTEGER DEFAULT 0,
    visible TEXT DEFAULT 'true',
    product_image_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

CREATE TABLE IF NOT EXISTS {_T}variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    sku TEXT NOT NULL UNIQUE,
    surcharge REAL DEFAULT 0.0,
    weight REAL,
    visible TEXT DEFAULT 'true',
    discountable INTEGER DEFAULT 1,
    track_inventory INTEGER DEFAULT 0,
    inventory INTEGER,
    excluded_from_export INTEGER DEFAULT 0,
    option_1_name TEXT,
    option_1_value TEXT,
    option_2_name TEXT,
    option_2_value TEXT,
    option_3_name TEXT,
    option_3_value TEXT,
    option_4_name TEXT,
    option_4_value TEXT,
    option_5_name TEXT,
    option_5_value TEXT,
    option_6_name TEXT,
    option_6_value TEXT,
    done INTEGER DEFAULT 0,
    match_status TEXT DEFAULT 'unmatched',
    matched_rrp_id INTEGER,
    trust_score REAL DEFAULT 0.0,
    ribbon_status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES {_T}products(id)
)

CREATE TABLE IF NOT EXISTS {_T}reference_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    rrp REAL NOT NULL,
    source_file TEXT,
    rrp_description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

CREATE TABLE IF NOT EXISTS {_T}price_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id INTEGER NOT NULL,
    site_name TEXT NOT NULL,
    external_price REAL,
    external_url TEXT,
    external_product_name TEXT,
    availability TEXT,
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (variant_id) REFERENCES {_T}variants(id)
)

CREATE INDEX IF NOT EXISTS idx_pm_variants_sku ON {_T}variants(sku)
CREATE INDEX IF NOT EXISTS idx_pm_variants_product_id ON {_T}variants(product_id)
CREATE INDEX IF NOT EXISTS idx_pm_reference_prices_sku ON {_T}reference_prices(sku)
CREATE INDEX IF NOT EXISTS idx_pm_variants_match_status ON {_T}variants(match_status)
"""

def init_database():
    import re
    with get_db_connection() as conn:
        for stmt in re.split(r'\n(?=CREATE )', SCHEMA_SQL.strip()):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
    print("[db] Database initialised (Turso)" if USE_TURSO else "[db] Database initialised (SQLite)")

def clear_database():
    """Clear all data"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pm_price_comparisons")
        cursor.execute("DELETE FROM pm_variants")
        cursor.execute("DELETE FROM pm_products")
        cursor.execute("DELETE FROM pm_reference_prices")
        conn.commit()

# ─────────────────────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────────────────────

def get_database_stats() -> Dict[str, int]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        return {
            'products': cursor.execute("SELECT COUNT(*) FROM pm_products").fetchone()[0],
            'variants': cursor.execute("SELECT COUNT(*) FROM pm_variants").fetchone()[0],
            'reference_prices': cursor.execute("SELECT COUNT(*) FROM pm_reference_prices").fetchone()[0],
        }

def get_matching_stats() -> Dict[str, Any]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        total = cursor.execute("SELECT COUNT(*) FROM pm_variants").fetchone()[0]
        matched = cursor.execute("SELECT COUNT(*) FROM pm_variants WHERE match_status IN ('guaranteed','user_approved')").fetchone()[0]
        believed = cursor.execute("SELECT COUNT(*) FROM pm_variants WHERE match_status = 'believed'").fetchone()[0]
        unmatched = cursor.execute("SELECT COUNT(*) FROM pm_variants WHERE match_status = 'unmatched'").fetchone()[0]
        total_rrp = cursor.execute("SELECT COUNT(*) FROM pm_reference_prices").fetchone()[0]
        matched_rrp = cursor.execute("SELECT COUNT(*) FROM pm_reference_prices WHERE id IN (SELECT DISTINCT matched_rrp_id FROM pm_variants WHERE matched_rrp_id IS NOT NULL)").fetchone()[0]
        return {
            'total_variants': total,
            'guaranteed': matched,
            'believed': believed,
            'unmatched': unmatched,
            'total_reference_prices': total_rrp,
            'matched_reference_prices': matched_rrp,
            'match_rate': round(matched / total * 100, 1) if total > 0 else 0,
            'reference_match_rate': round(matched_rrp / total_rrp * 100, 1) if total_rrp > 0 else 0,
        }

# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTS
# ─────────────────────────────────────────────────────────────────────────────

def get_all_products() -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.*,
                   COUNT(v.id) as variant_count,
                   SUM(CASE WHEN v.match_status IN ('guaranteed','user_approved') THEN 1 ELSE 0 END) as matched_variants,
                   SUM(CASE WHEN v.match_status = 'believed' THEN 1 ELSE 0 END) as believed_variants,
                   SUM(CASE WHEN v.done = 1 THEN 1 ELSE 0 END) as done_variants
            FROM pm_products p
            LEFT JOIN pm_variants v ON p.id = v.product_id
            GROUP BY p.id
            ORDER BY p.name
        """)
        return [dict(row) for row in cursor.fetchall()]

def get_product_by_id(product_id: int) -> Optional[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pm_products WHERE id = ?", (product_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_variants_for_product(product_id: int) -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT v.*, rp.rrp as reference_price, rp.id as rrp_id
            FROM pm_variants v
            LEFT JOIN pm_reference_prices rp ON v.matched_rrp_id = rp.id
            WHERE v.product_id = ?
            ORDER BY v.sku
        """, (product_id,))
        return [dict(row) for row in cursor.fetchall()]

def update_variant_surcharge(variant_id: int, surcharge: float) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pm_variants SET surcharge = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (surcharge, variant_id)
        )
        conn.commit()
        return cursor.rowcount > 0

def update_variant_ribbon(variant_id: int, ribbon_status: str) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pm_variants SET ribbon_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ribbon_status, variant_id)
        )
        conn.commit()
        return cursor.rowcount > 0

def update_variant_match_status(variant_id: int, match_status: str, matched_rrp_id: Optional[int] = None, trust_score: float = 0.0) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE pm_variants
            SET match_status = ?, matched_rrp_id = ?, trust_score = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (match_status, matched_rrp_id, trust_score, variant_id))
        conn.commit()
        return cursor.rowcount > 0

def mark_variant_done(variant_id: int, done: bool) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pm_variants SET done = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (1 if done else 0, variant_id)
        )
        conn.commit()
        return cursor.rowcount > 0

def mark_all_variants_done(product_id: int, done: bool) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pm_variants SET done = ?, updated_at = CURRENT_TIMESTAMP WHERE product_id = ?",
            (1 if done else 0, product_id)
        )
        conn.commit()
        return cursor.rowcount

# ─────────────────────────────────────────────────────────────────────────────
# REFERENCE PRICES
# ─────────────────────────────────────────────────────────────────────────────

def get_all_reference_prices() -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pm_reference_prices ORDER BY sku")
        return [dict(row) for row in cursor.fetchall()]

def get_unmatched_reference_prices() -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT rp.* FROM pm_reference_prices rp
            LEFT JOIN pm_variants v ON v.matched_rrp_id = rp.id
            WHERE v.id IS NULL
            ORDER BY rp.sku
        """)
        return [dict(row) for row in cursor.fetchall()]

def search_variants(query: str, limit: int = 100) -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        q = f"%{query}%"
        cursor.execute("""
            SELECT v.*, p.name as product_name, p.base_price
            FROM pm_variants v
            JOIN pm_products p ON v.product_id = p.id
            WHERE v.sku LIKE ? OR p.name LIKE ?
            ORDER BY p.name
            LIMIT ?
        """, (q, q, limit))
        return [dict(row) for row in cursor.fetchall()]

# ─────────────────────────────────────────────────────────────────────────────
# BULK OPERATIONS
# ─────────────────────────────────────────────────────────────────────────────

def bulk_apply_surcharges(product_id: int) -> Dict[str, int]:
    """Apply RRP-based surcharges to all matched variants of a product"""
    updated = 0
    errors = 0
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT v.id, v.surcharge, rp.rrp, p.base_price
            FROM pm_variants v
            JOIN pm_products p ON v.product_id = p.id
            LEFT JOIN pm_reference_prices rp ON v.matched_rrp_id = rp.id
            WHERE v.product_id = ? AND rp.rrp IS NOT NULL
        """, (product_id,))
        for row in cursor.fetchall():
            required = row['rrp'] - row['base_price']
            cursor.execute(
                "UPDATE pm_variants SET surcharge = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (required, row['id'])
            )
            updated += 1
        conn.commit()
    return {'updated': updated, 'errors': errors}

def bulk_apply_all_surcharges() -> Dict[str, int]:
    """Apply RRP-based surcharges to ALL matched variants"""
    updated = 0
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT v.id, rp.rrp, p.base_price
            FROM pm_variants v
            JOIN pm_products p ON v.product_id = p.id
            JOIN pm_reference_prices rp ON v.matched_rrp_id = rp.id
            WHERE rp.rrp IS NOT NULL
        """)
        for row in cursor.fetchall():
            required = row['rrp'] - row['base_price']
            cursor.execute(
                "UPDATE pm_variants SET surcharge = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (required, row['id'])
            )
            updated += 1
        conn.commit()
    return {'updated': updated}

def get_all_collections() -> List[str]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT collection FROM pm_products
            WHERE collection IS NOT NULL AND collection != ''
            ORDER BY collection
        """)
        return [r['collection'] for r in cursor.fetchall()]

def get_product_cards(
    search: str = "",
    match_filter: str = "all",
    collection_filter: str = "all",
    ribbon_filter: str = "all",
    page: int = 1,
    per_page: int = 30
) -> Dict[str, Any]:
    """
    Get products with their variants for card visualization.
    Supports filtering by search, match status, collection, and ribbon.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()

        base_query = """
            FROM pm_products p
            LEFT JOIN pm_variants v ON p.id = v.product_id
            LEFT JOIN pm_reference_prices rp ON v.matched_rrp_id = rp.id
            WHERE 1=1
        """
        params = []

        if search:
            base_query += " AND (p.name LIKE ? OR v.sku LIKE ? OR p.description LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

        if match_filter != "all":
            base_query += " AND v.match_status = ?"
            params.append(match_filter)

        if collection_filter != "all":
            base_query += " AND p.collection = ?"
            params.append(collection_filter)

        if ribbon_filter != "all":
            base_query += " AND v.ribbon_status = ?"
            params.append(ribbon_filter)

        # Count total distinct products
        count_sql = f"SELECT COUNT(DISTINCT p.id) as cnt {base_query}"
        total = cursor.execute(count_sql, params).fetchone()['cnt']

        # Get products with variant aggregates
        offset = (page - 1) * per_page
        data_sql = f"""
            SELECT p.id, p.name, p.handle_id, p.base_price, p.collection, p.product_image_url,
                   COUNT(v.id) as total_variants,
                   SUM(CASE WHEN v.match_status IN ('guaranteed','user_approved') THEN 1 ELSE 0 END) as matched_variants,
                   SUM(CASE WHEN v.match_status = 'believed' THEN 1 ELSE 0 END) as believed_variants,
                   SUM(CASE WHEN v.match_status = 'unmatched' THEN 1 ELSE 0 END) as unmatched_variants,
                   SUM(CASE WHEN v.done = 1 THEN 1 ELSE 0 END) as done_variants,
                   MIN(v.ribbon_status) as primary_ribbon
            {base_query}
            GROUP BY p.id
            ORDER BY p.name
            LIMIT ? OFFSET ?
        """
        params.extend([per_page, offset])
        products = [dict(row) for row in cursor.execute(data_sql, params).fetchall()]

        return {
            'products': products,
            'total': total,
            'pages': (total + per_page - 1) // per_page,
            'page': page,
            'per_page': per_page,
        }

def get_product_card_detail(product_id: int) -> Optional[Dict]:
    """Get full product detail with all variants for a card"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        product = cursor.execute("SELECT * FROM pm_products WHERE id = ?", (product_id,)).fetchone()
        if not product:
            return None
        variants = get_variants_for_product(product_id)
        return {
            'product': dict(product),
            'variants': variants,
        }

# ─────────────────────────────────────────────────────────────────────────────
# EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def get_export_data() -> List[Dict]:
    """Get all product+variant data for export"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.handle_id, p.name, p.description, p.product_image_url,
                   p.collection, p.brand, p.base_price, p.visible as product_visible,
                   v.sku, v.surcharge, v.visible as variant_visible,
                   v.option_1_name, v.option_1_value,
                   v.option_2_name, v.option_2_value,
                   v.option_3_name, v.option_3_value,
                   v.ribbon_status, v.done
            FROM pm_products p
            LEFT JOIN pm_variants v ON p.id = v.product_id
            ORDER BY p.id, v.id
        """)
        return [dict(row) for row in cursor.fetchall()]
