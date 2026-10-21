"""Explicit PostgreSQL schema; SQLite migrations remain in db.py."""
from contextlib import closing
from storage import PostgresConnection, begin_write

TIMESTAMP = "TEXT NOT NULL DEFAULT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')"

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS products (
        id TEXT PRIMARY KEY, sort_order BIGSERIAL UNIQUE NOT NULL,
        sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        price_cents BIGINT NOT NULL CHECK (price_cents >= 0),
        stock BIGINT NOT NULL CHECK (stock BETWEEN 0 AND 1000000000),
        category TEXT NOT NULL DEFAULT '水果', unit TEXT NOT NULL DEFAULT '件',
        description TEXT NOT NULL DEFAULT '', zone TEXT NOT NULL DEFAULT '未分区',
        image BYTEA, image_type TEXT, active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
        version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1))""",
    """CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('completed','cancelled')),
        total_cents BIGINT NOT NULL CHECK (total_cents >= 0), buyer_key TEXT,
        cancelled_at TEXT, cancellation_reason TEXT NOT NULL DEFAULT '', created_at """ + TIMESTAMP + ")",
    """CREATE TABLE IF NOT EXISTS movements (
        id BIGSERIAL PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
        action TEXT NOT NULL CHECK (action IN ('purchase','restock')),
        product_id TEXT NOT NULL REFERENCES products(id), quantity BIGINT NOT NULL CHECK (quantity > 0),
        unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents >= 0),
        stock_after BIGINT NOT NULL CHECK (stock_after >= 0), reason TEXT NOT NULL DEFAULT '',
        order_id TEXT REFERENCES orders(id), product_name TEXT NOT NULL DEFAULT '',
        unit TEXT NOT NULL DEFAULT '件', sku TEXT NOT NULL DEFAULT '', created_at """ + TIMESTAMP + ")",
    """CREATE TABLE IF NOT EXISTS order_items (
        id BIGSERIAL PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
        product_id TEXT NOT NULL REFERENCES products(id), product_name TEXT NOT NULL, sku TEXT NOT NULL,
        unit TEXT NOT NULL, quantity BIGINT NOT NULL CHECK (quantity > 0),
        unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents >= 0), stock_after BIGINT NOT NULL,
        UNIQUE(order_id, product_id))""",
    """CREATE TABLE IF NOT EXISTS inventory_entries (
        id BIGSERIAL PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, product_id TEXT NOT NULL REFERENCES products(id),
        product_name TEXT NOT NULL, sku TEXT NOT NULL, unit TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('opening','listing','purchase','restock','adjust','cancel')),
        delta BIGINT NOT NULL, stock_after BIGINT NOT NULL CHECK (stock_after >= 0),
        unit_price_cents BIGINT NOT NULL, reason TEXT NOT NULL, request_id TEXT NOT NULL,
        order_id TEXT REFERENCES orders(id), created_at """ + TIMESTAMP + ")",
    """CREATE TABLE IF NOT EXISTS audit_events (
        id BIGSERIAL PRIMARY KEY, event TEXT NOT NULL, entity_id TEXT NOT NULL,
        request_id TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL, created_at """ + TIMESTAMP + ")",
    """CREATE TABLE IF NOT EXISTS api_requests (
        scope TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL,
        PRIMARY KEY(scope, request_id))""",
    """CREATE TABLE IF NOT EXISTS product_creations (
        request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        product_id TEXT NOT NULL UNIQUE REFERENCES products(id), created_at """ + TIMESTAMP + ")",
    """CREATE TABLE IF NOT EXISTS product_status_changes (
        id BIGSERIAL PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, product_id TEXT NOT NULL REFERENCES products(id),
        active INTEGER NOT NULL CHECK (active IN (0,1)), changed INTEGER NOT NULL CHECK (changed IN (0,1)),
        stock_after BIGINT NOT NULL CHECK (stock_after >= 0), created_at """ + TIMESTAMP + ")",
    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)",
    "CREATE INDEX IF NOT EXISTS inventory_product_idx ON inventory_entries(product_id,id)",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_key TEXT",
    "CREATE INDEX IF NOT EXISTS order_created_idx ON orders(created_at DESC,id)",
    "CREATE INDEX IF NOT EXISTS orders_buyer_idx ON orders(buyer_key, created_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS items_product_idx ON order_items(product_id)",
    "CREATE INDEX IF NOT EXISTS movements_created_idx ON movements(id DESC)",
]


def initialize(seed=True):
    with closing(PostgresConnection()) as connection:
        begin_write(connection)
        try:
            for statement in STATEMENTS:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (2), (3) ON CONFLICT DO NOTHING")
            if seed:
                from db import _ledger, PRODUCTS
                for product_id, name, price, stock in PRODUCTS:
                    cursor = connection.execute("""INSERT INTO products(id,sku,name,price_cents,stock)
                        VALUES (?,?,?,?,?) ON CONFLICT (id) DO NOTHING RETURNING id""",
                        (product_id, "SKU-" + product_id.upper(), name, price, stock))
                    if cursor.fetchone():
                        _ledger(connection, product_id, stock, "opening", "初始演示库存", "seed-v3", event_key="opening:" + product_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
