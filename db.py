"""Supermarket business rules, backed by PostgreSQL or an explicit SQLite path."""

from __future__ import annotations

import os
import base64
import binascii
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
from storage import backend, PostgresConnection, is_postgres, begin_read, begin_write, insert_id, product_order


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "shop.sqlite3"
PRODUCTS = (
    ("apple", "苹果", 590, 24),
    ("banana", "香蕉", 490, 18),
)
CATEGORIES = ("水果", "蔬菜", "肉禽水产", "乳品饮料", "粮油食品", "日用百货", "其他")
PRODUCT_COLUMNS = "id, sku, name, price_cents, stock, category, unit, description, zone, version, active, CASE WHEN image IS NOT NULL THEN 1 ELSE 0 END AS has_image"
MAX_STOCK = 1000000000
PAGE_SIZE = 6
SORTS = ("default", "price_asc", "price_desc", "stock_desc", "name_asc")


class InventoryError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def database_path() -> Path:
    return Path(os.environ.get("SHOP_DB_PATH", str(DEFAULT_DB_PATH))).expanduser().resolve()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    if backend(db_path) == "postgresql":
        return PostgresConnection()
    path = db_path or database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def initialize(db_path: Path | None = None) -> None:
    if backend(db_path) == "postgresql":
        from postgres_schema import initialize as initialize_postgres
        initialize_postgres()
        return
    with closing(connect(db_path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
                    stock INTEGER NOT NULL CHECK (stock >= 0)
                );
                CREATE TABLE IF NOT EXISTS movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL CHECK (action IN ('purchase', 'restock')),
                    product_id TEXT NOT NULL REFERENCES products(id),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
                    stock_after INTEGER NOT NULL CHECK (stock_after >= 0),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE INDEX IF NOT EXISTS movements_created_idx ON movements(id DESC);
                """
            )
        begin_write(connection)
        try:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(products)")}
            for name, declaration in (
                ("category", "TEXT NOT NULL DEFAULT '水果'"),
                ("unit", "TEXT NOT NULL DEFAULT '件'"),
                ("description", "TEXT NOT NULL DEFAULT ''"),
                ("image", "BLOB"),
                ("image_type", "TEXT"),
                ("active", "INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE products ADD COLUMN {name} {declaration}")
            movement_columns = {row["name"] for row in connection.execute("PRAGMA table_info(movements)")}
            if "reason" not in movement_columns:
                connection.execute("ALTER TABLE movements ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            connection.execute("""CREATE TABLE IF NOT EXISTS product_creations (
                request_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                product_id TEXT NOT NULL UNIQUE REFERENCES products(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS product_status_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                product_id TEXT NOT NULL REFERENCES products(id),
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                changed INTEGER NOT NULL CHECK (changed IN (0, 1)),
                stock_after INTEGER NOT NULL CHECK (stock_after >= 0),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )""")
            connection.executemany(
                "INSERT OR IGNORE INTO products (id, name, price_cents, stock) VALUES (?, ?, ?, ?)",
                PRODUCTS,
            )
            _migrate_business(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _product_result(row) -> dict:
    product = dict(row)
    product["image_url"] = f"/api/products/{product['id']}/image" if product.pop("has_image") else None
    return product


def _price_filter(value, label: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000000:
        raise InventoryError(f"{label}必须是 0 到 100000000 的整数分")
    return value


def snapshot(db_path: Path | None = None, page: int = 1, status: str = "active", search: str = "", category: str = "",
             min_price_cents=None, max_price_cents=None, in_stock=None, sort: str = "default") -> dict:
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 1000000:
        raise InventoryError("页码必须是正整数")
    if status not in ("active", "inactive"):
        raise InventoryError("商品状态无效")
    active = int(status == "active")
    search = _text(search, "搜索词", 100, required=False)
    if category and category not in CATEGORIES:
        raise InventoryError("商品分类无效")
    min_price_cents = _price_filter(min_price_cents, "最低价格")
    max_price_cents = _price_filter(max_price_cents, "最高价格")
    if min_price_cents is not None and max_price_cents is not None and min_price_cents > max_price_cents:
        raise InventoryError("价格区间无效")
    if in_stock is not None and not isinstance(in_stock, bool):
        raise InventoryError("库存筛选无效")
    sort = sort or "default"
    if sort not in SORTS:
        raise InventoryError("排序方式无效")
    pattern = '%' + search.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
    conditions = ["active = ?", "(? = '' OR category = ?)",
                  "(? = '' OR lower(name || ' ' || sku) LIKE lower(?) ESCAPE '!')"]
    args = [active, category, category, search, pattern]
    if min_price_cents is not None:
        conditions.append("price_cents >= ?")
        args.append(min_price_cents)
    if max_price_cents is not None:
        conditions.append("price_cents <= ?")
        args.append(max_price_cents)
    if in_stock is not None:
        conditions.append("stock > 0" if in_stock else "stock = 0")
    filters = " AND ".join(conditions)
    with closing(connect(db_path)) as connection:
        begin_read(connection)
        summary = connection.execute("""SELECT COUNT(*) AS total_count, COALESCE(SUM(stock), 0) AS stock_total,
            COALESCE(SUM(active), 0) AS active_count FROM products""").fetchone()
        count = connection.execute(f"SELECT COUNT(*) FROM products WHERE {filters}", args).fetchone()[0]
        pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        order = product_order(connection)
        ordering = {
            "default": order,
            "price_asc": f"price_cents ASC, {order} ASC",
            "price_desc": f"price_cents DESC, {order} ASC",
            "stock_desc": f"stock DESC, {order} ASC",
            "name_asc": f"name ASC, {order} ASC",
        }[sort]
        products = [_product_result(row) for row in connection.execute(
            f"SELECT {PRODUCT_COLUMNS} FROM products WHERE {filters} ORDER BY {ordering} LIMIT {PAGE_SIZE} OFFSET ?",
            args + [(page - 1) * PAGE_SIZE]
        )]
        movements = [dict(row) for row in connection.execute(
            """SELECT m.id AS id, m.request_id, m.action, m.product_id, m.product_name,
                      m.quantity, m.unit_price_cents, m.stock_after, m.created_at AS created_at, m.reason, m.unit,
                      CASE WHEN m.action = 'purchase' THEN -m.quantity ELSE m.quantity END AS delta
               FROM movements m JOIN products p ON p.id = m.product_id
               UNION ALL
               SELECT s.id, s.request_id, CASE WHEN s.active = 1 THEN 'relist' ELSE 'delist' END,
                      s.product_id, p.name, 0, p.price_cents, s.stock_after, s.created_at, '', p.unit, 0
               FROM product_status_changes s JOIN products p ON p.id = s.product_id WHERE s.changed = 1
               UNION ALL
               SELECT e.id, e.request_id, e.kind, e.product_id, e.product_name, abs(e.delta),
                      e.unit_price_cents, e.stock_after, e.created_at, e.reason, e.unit, e.delta
               FROM inventory_entries e WHERE e.kind IN ('cancel', 'adjust')
               ORDER BY created_at DESC, id DESC LIMIT 12"""
        )]
        totals = connection.execute(
            """SELECT COUNT(*) AS movement_count,
                      COALESCE(SUM(CASE WHEN action = 'purchase' THEN quantity * unit_price_cents ELSE 0 END), 0) AS sales_cents
               FROM movements"""
        ).fetchone()
        status_count = connection.execute("SELECT COUNT(*) FROM product_status_changes WHERE changed = 1").fetchone()[0]
        extra_count = connection.execute("SELECT COUNT(*) FROM inventory_entries WHERE kind IN ('cancel', 'adjust')").fetchone()[0]
        refunded = connection.execute("SELECT COALESCE(SUM(total_cents), 0) FROM orders WHERE status = 'cancelled'").fetchone()[0]
        return {"products": products, "movements": movements, "page": page, "page_size": PAGE_SIZE, "status": status,
                "pages": pages, "product_count": count, "stock_total": summary["stock_total"],
                "active_count": summary["active_count"], "inactive_count": summary["total_count"] - summary["active_count"],
                "movement_count": totals["movement_count"] + status_count + extra_count,
                "sales_cents": totals["sales_cents"] - refunded, "gross_sales_cents": totals["sales_cents"],
                "refunded_cents": refunded, "engine": backend(db_path),
                "filters": {"min_price_cents": min_price_cents, "max_price_cents": max_price_cents,
                            "in_stock": in_stock, "sort": sort}}


def create_product(payload: dict, db_path: Path | None = None) -> dict:
    name = _text(payload.get("name"), "商品名称", 40)
    category = _text(payload.get("category", "其他"), "分类", 20)
    if category not in CATEGORIES:
        raise InventoryError("请选择有效的商品分类")
    unit = _text(payload.get("unit", "件"), "计量单位", 8)
    description = _text(payload.get("description", ""), "商品简介", 100, required=False)
    zone = _text(payload.get("zone", "未分区"), "货架分区", 40)
    sku = _sku(payload.get("sku", ""))
    price = payload.get("price_cents")
    stock = payload.get("stock", 0)
    if isinstance(price, bool) or not isinstance(price, int) or not 1 <= price <= 100000000:
        raise InventoryError("单价必须在 0.01 到 1000000.00 元之间，最多两位小数")
    if isinstance(stock, bool) or not isinstance(stock, int) or not 0 <= stock <= 9999:
        raise InventoryError("初始库存必须是 0 到 9999 的整数")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise InventoryError("请求编号无效")
    image, image_type = _decode_image(payload.get("image_data"))
    fingerprint = hashlib.sha256(json.dumps(
        [name, category, unit, description, zone, sku, price, stock,
         hashlib.sha256(image).hexdigest() if image else None], ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    with closing(connect(db_path)) as connection:
        begin_write(connection)
        try:
            previous = connection.execute("SELECT * FROM product_creations WHERE request_id = ?", (request_id,)).fetchone()
            if previous:
                legacy_fingerprint = hashlib.sha256(json.dumps(
                    [name, category, unit, description, price, stock, hashlib.sha256(image).hexdigest() if image else None],
                    ensure_ascii=False).encode("utf-8")).hexdigest()
                allowed_fingerprints = {fingerprint}
                if zone == "未分区" and not sku:
                    allowed_fingerprints.add(legacy_fingerprint)
                if previous["fingerprint"] not in allowed_fingerprints:
                    raise InventoryError("请求编号已用于其他上架操作", 409)
                product_id = previous["product_id"]
            else:
                product_id = uuid.uuid4().hex
                actual_sku = sku or "SKU-" + product_id[:12].upper()
                if connection.execute("SELECT 1 FROM products WHERE sku = ?", (actual_sku,)).fetchone():
                    raise InventoryError("商品 SKU 已存在", 409)
                connection.execute("""INSERT INTO products
                    (id, name, price_cents, stock, category, unit, description, image, image_type, sku, zone)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (product_id, name, price, stock, category, unit, description, image, image_type, actual_sku, zone))
                connection.execute("INSERT INTO product_creations (request_id, fingerprint, product_id) VALUES (?, ?, ?)",
                                   (request_id, fingerprint, product_id))
                if stock:
                    _insert_movement(connection, product_id, "restock", stock, f"listing:{product_id}", "initial_stock")
                else:
                    _ledger(connection, product_id, 0, "opening", "新品初始库存", request_id)
                _audit(connection, "product_create", product_id, request_id, None,
                       {"sku": actual_sku, "name": name, "price_cents": price, "stock": stock})
            product = _product_result(connection.execute(f"SELECT {PRODUCT_COLUMNS} FROM products WHERE id = ?", (product_id,)).fetchone())
            order = product_order(connection)
            position = connection.execute(f"SELECT COUNT(*) FROM products WHERE active = ? AND {order} <= (SELECT {order} FROM products WHERE id = ?)", (product["active"], product_id)).fetchone()[0]
            connection.commit()
            return {"product": product, "page": (position + 5) // 6, "duplicate": bool(previous)}
        except Exception:
            connection.rollback()
            raise


def set_product_status(product_id: str, active: bool, request_id: str, db_path: Path | None = None) -> dict:
    if not isinstance(product_id, str) or not product_id:
        raise InventoryError("商品不存在", 404)
    if not isinstance(active, bool):
        raise InventoryError("上架状态必须为布尔值")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise InventoryError("请求编号无效")
    with closing(connect(db_path)) as connection:
        begin_write(connection)
        try:
            previous = connection.execute("SELECT * FROM product_status_changes WHERE request_id = ?", (request_id,)).fetchone()
            if previous:
                if (previous["product_id"], previous["active"]) != (product_id, int(active)):
                    raise InventoryError("请求编号已用于其他上下架操作", 409)
                result = dict(previous)
            else:
                product = connection.execute("SELECT active, stock FROM products WHERE id = ?", (product_id,)).fetchone()
                if product is None:
                    raise InventoryError("商品不存在", 404)
                changed = product["active"] != int(active)
                connection.execute("UPDATE products SET active = ?, version = version + ? WHERE id = ?", (int(active), int(changed), product_id))
                status_id = insert_id(connection, """INSERT INTO product_status_changes
                    (request_id, product_id, active, changed, stock_after) VALUES (?, ?, ?, ?, ?)""",
                    (request_id, product_id, int(active), int(changed), product["stock"]))
                result = dict(connection.execute("SELECT * FROM product_status_changes WHERE id = ?", (status_id,)).fetchone())
                if changed:
                    _audit(connection, "product_status", product_id, request_id, {"active": product["active"]}, {"active": int(active)})
            connection.commit()
            result["duplicate"] = bool(previous)
            return result
        except Exception:
            connection.rollback()
            raise


def _text(value, label: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise InventoryError(f"{label}格式无效")
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise InventoryError(f"{label}须填写 1 到 {limit} 个字符" if required else f"{label}最多 {limit} 个字符")
    return value


def _decode_image(value):
    if value is None or value == "":
        return None, None
    if not isinstance(value, str) or len(value) > 1400000:
        raise InventoryError("图片不得超过 1 MB")
    prefix, separator, encoded = value.partition(",")
    allowed = {"data:image/png;base64": "image/png", "data:image/jpeg;base64": "image/jpeg", "data:image/webp;base64": "image/webp"}
    if not separator or prefix not in allowed:
        raise InventoryError("图片仅支持 JPG、PNG 和 WebP")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise InventoryError("图片内容无效")
    matches = {"image/png": blob.startswith(b"\x89PNG\r\n\x1a\n"),
               "image/jpeg": blob.startswith(b"\xff\xd8\xff"),
               "image/webp": blob.startswith(b"RIFF") and blob[8:12] == b"WEBP"}
    mime = allowed[prefix]
    if not blob or len(blob) > 1024 * 1024 or not matches[mime]:
        raise InventoryError("图片内容无效或超过 1 MB")
    return blob, mime


def product_image(product_id: str, db_path: Path | None = None):
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT image, image_type FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None or row["image"] is None:
            raise InventoryError("商品图片不存在", 404)
        return row["image"], row["image_type"]


def product_by_id(product_id: str, db_path: Path | None = None) -> dict:
    with closing(connect(db_path)) as connection:
        begin_read(connection)
        row = connection.execute(f"SELECT {PRODUCT_COLUMNS} FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            raise InventoryError("商品不存在", 404)
        return _product_result(row)


def get_product(product_id: str, db_path: Path | None = None, active_only: bool = False) -> dict:
    if not isinstance(product_id, str) or not product_id:
        raise InventoryError("商品不存在", 404)
    product = product_by_id(product_id, db_path)
    if active_only and not product["active"]:
        raise InventoryError("商品不存在", 404)
    return product


def move_stock(action: str, product_id: str, quantity: int, request_id: str,
               db_path: Path | None = None, buyer_key: str | None = None) -> dict:
    if action not in ("purchase", "restock"):
        raise InventoryError("操作类型无效")
    if not isinstance(product_id, str) or not product_id:
        raise InventoryError("商品不存在", 404)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 9999:
        raise InventoryError("数量必须是 1 到 9999 的整数")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise InventoryError("请求编号无效")

    with closing(connect(db_path)) as connection:
        begin_write(connection)
        try:
            existing = connection.execute(
                "SELECT * FROM movements WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing:
                if (existing["action"], existing["product_id"], existing["quantity"]) != (action, product_id, quantity):
                    raise InventoryError("请求编号已用于其他操作", 409)
                connection.commit()
                return _movement_result(existing, duplicate=True)

            product = connection.execute(
                "SELECT price_cents, active FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            if product is None:
                raise InventoryError("商品不存在", 404)
            if action == "purchase" and not product["active"]:
                raise InventoryError("商品已下架，无法购买，请刷新商品列表", 409)

            if action == "purchase":
                order, duplicate = _create_order(connection, [{"product_id": product_id, "quantity": quantity}], request_id,
                                                 movement_request=request_id, buyer_key=buyer_key)
                movement = connection.execute("SELECT * FROM movements WHERE order_id = ? ORDER BY id LIMIT 1", (order["id"],)).fetchone()
                connection.commit()
                return _movement_result(movement, duplicate)
            else:
                _change_stock(connection, product_id, quantity)
            movement = _insert_movement(connection, product_id, action, quantity, request_id, "商品补货")
            _audit(connection, "restock", product_id, request_id, None, {"quantity": quantity, "stock_after": movement["stock_after"]})
            connection.commit()
            return _movement_result(movement)
        except Exception:
            connection.rollback()
            raise


def _movement_result(row: sqlite3.Row, duplicate: bool = False) -> dict:
    return {
        "id": row["id"], "request_id": row["request_id"], "action": row["action"],
        "product_id": row["product_id"], "quantity": row["quantity"],
        "unit_price_cents": row["unit_price_cents"], "stock_after": row["stock_after"],
        "created_at": row["created_at"], "duplicate": duplicate,
        "order_id": row["order_id"],
    }


def _migrate_business(connection):
    """One transactional migration, including historical orders and opening balances."""
    columns = {r["name"] for r in connection.execute("PRAGMA table_info(products)")}
    for name, declaration in (("sku", "TEXT"), ("zone", "TEXT NOT NULL DEFAULT '未分区'"),
                              ("version", "INTEGER NOT NULL DEFAULT 1")):
        if name not in columns:
            connection.execute(f"ALTER TABLE products ADD COLUMN {name} {declaration}")
    for row in connection.execute("SELECT id FROM products WHERE sku IS NULL").fetchall():
        sku = "SKU-" + (row["id"].upper() if row["id"] in ("apple", "banana") else uuid.uuid4().hex[:12].upper())
        connection.execute("UPDATE products SET sku = ? WHERE id = ?", (sku, row["id"]))
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS product_sku_unique ON products(sku)")
    columns = {r["name"] for r in connection.execute("PRAGMA table_info(movements)")}
    for name, declaration in (("order_id", "TEXT"), ("product_name", "TEXT NOT NULL DEFAULT ''"),
                              ("unit", "TEXT NOT NULL DEFAULT '件'"), ("sku", "TEXT NOT NULL DEFAULT ''")):
        if name not in columns:
            connection.execute(f"ALTER TABLE movements ADD COLUMN {name} {declaration}")
    statements = (
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)",
        """CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('completed', 'cancelled')),
            total_cents INTEGER NOT NULL CHECK (total_cents >= 0), buyer_key TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            cancelled_at TEXT, cancellation_reason TEXT NOT NULL DEFAULT '')""",
        """CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL REFERENCES orders(id),
            product_id TEXT NOT NULL REFERENCES products(id), product_name TEXT NOT NULL, sku TEXT NOT NULL,
            unit TEXT NOT NULL, quantity INTEGER NOT NULL CHECK (quantity > 0),
            unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0), stock_after INTEGER NOT NULL,
            UNIQUE(order_id, product_id))""",
        """CREATE TABLE IF NOT EXISTS inventory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
            product_id TEXT NOT NULL REFERENCES products(id), product_name TEXT NOT NULL, sku TEXT NOT NULL, unit TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('opening','listing','purchase','restock','adjust','cancel')),
            delta INTEGER NOT NULL, stock_after INTEGER NOT NULL CHECK (stock_after >= 0),
            unit_price_cents INTEGER NOT NULL, reason TEXT NOT NULL, request_id TEXT NOT NULL,
            order_id TEXT REFERENCES orders(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, entity_id TEXT NOT NULL,
            request_id TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE IF NOT EXISTS api_requests (
            scope TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL,
            PRIMARY KEY(scope, request_id))""",
        """CREATE TABLE IF NOT EXISTS agent_confirmations (
            id TEXT PRIMARY KEY, tool TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('customer', 'merchant')), buyer_key TEXT,
            arguments_fingerprint TEXT NOT NULL, preview_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'consumed', 'expired')),
            created_at REAL NOT NULL, expires_at REAL NOT NULL,
            approved_at REAL, consumed_at REAL, consumed_request_id TEXT)""",
        "CREATE INDEX IF NOT EXISTS inventory_product_idx ON inventory_entries(product_id, id)",
        "CREATE INDEX IF NOT EXISTS order_created_idx ON orders(created_at DESC, id)",
        "CREATE INDEX IF NOT EXISTS items_product_idx ON order_items(product_id)",
        "CREATE INDEX IF NOT EXISTS agent_confirmations_expiry_idx ON agent_confirmations(state, expires_at)",
    )
    for statement in statements:
        connection.execute(statement)
    order_columns = {r["name"] for r in connection.execute("PRAGMA table_info(orders)")}
    if "buyer_key" not in order_columns:
        connection.execute("ALTER TABLE orders ADD COLUMN buyer_key TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS orders_buyer_idx ON orders(buyer_key, created_at DESC, id)")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")
    if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 2").fetchone():
        return
    # Existing prices were already captured. Older product names/units can only
    # be backfilled from their metadata at migration time.
    connection.execute("""UPDATE movements SET product_name = (SELECT name FROM products WHERE id = product_id),
        unit = (SELECT unit FROM products WHERE id = product_id), sku = (SELECT sku FROM products WHERE id = product_id)""")
    for row in connection.execute("SELECT * FROM movements WHERE action = 'purchase' ORDER BY id").fetchall():
        order_id = f"legacy-{row['id']}"
        items = [{"product_id": row["product_id"], "quantity": row["quantity"]}]
        connection.execute("INSERT INTO orders(id, request_id, fingerprint, status, total_cents, created_at) VALUES (?, ?, ?, 'completed', ?, ?)",
                           (order_id, row["request_id"], _fingerprint(items), row["quantity"] * row["unit_price_cents"], row["created_at"]))
        connection.execute("""INSERT INTO order_items(order_id, product_id, product_name, sku, unit, quantity, unit_price_cents, stock_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (order_id, row["product_id"], row["product_name"], row["sku"], row["unit"],
            row["quantity"], row["unit_price_cents"], row["stock_after"]))
        connection.execute("UPDATE movements SET order_id = ? WHERE id = ?", (order_id, row["id"]))
    for product in connection.execute("SELECT * FROM products ORDER BY rowid").fetchall():
        history = connection.execute("SELECT * FROM movements WHERE product_id = ? ORDER BY id", (product["id"],)).fetchall()
        opening = product["stock"] - sum((-r["quantity"] if r["action"] == "purchase" else r["quantity"]) for r in history)
        if opening < 0:
            raise InventoryError("历史库存与台账不一致，请先检查备份", 409)
        _ledger(connection, product["id"], opening, "opening", "历史期初库存迁移", "migration-v2",
                event_key="opening:" + product["id"], stock_after=opening)
        for row in history:
            delta = -row["quantity"] if row["action"] == "purchase" else row["quantity"]
            _ledger(connection, product["id"], delta, "listing" if row["reason"] == "initial_stock" else row["action"],
                    row["reason"] or "历史库存记录迁移", row["request_id"], event_key=f"movement:{row['id']}",
                    stock_after=row["stock_after"], order_id=row["order_id"], price=row["unit_price_cents"], created_at=row["created_at"])
    connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")


def _sku(value):
    if not isinstance(value, str):
        raise InventoryError("SKU 格式无效")
    value = value.strip().upper()
    if value and not re.fullmatch(r"[A-Z0-9_-]{1,32}", value):
        raise InventoryError("SKU 须为 1 到 32 位字母、数字、下划线或短横线")
    return value


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _request_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise InventoryError("请求编号无效")
    return value


def _audit(connection, event, entity_id, request_id, before, after):
    connection.execute("INSERT INTO audit_events(event, entity_id, request_id, before_json, after_json) VALUES (?, ?, ?, ?, ?)",
        (event, entity_id, request_id, json.dumps(before, ensure_ascii=False) if before is not None else None,
         json.dumps(after, ensure_ascii=False)))


def _memo(connection, scope, request_id, fingerprint):
    row = connection.execute("SELECT * FROM api_requests WHERE scope = ? AND request_id = ?", (scope, request_id)).fetchone()
    if row:
        if row["fingerprint"] != fingerprint:
            raise InventoryError("请求编号已用于不同内容", 409)
        result = json.loads(row["result_json"])
        result["duplicate"] = True
        return result
    return None


def _remember(connection, scope, request_id, fingerprint, result):
    connection.execute("INSERT INTO api_requests VALUES (?, ?, ?, ?)",
                       (scope, request_id, fingerprint, json.dumps(result, ensure_ascii=False)))


def _change_stock(connection, product_id, delta):
    cursor = connection.execute("UPDATE products SET stock = stock + ? WHERE id = ? AND stock + ? BETWEEN 0 AND ?",
                                (delta, product_id, delta, MAX_STOCK))
    if cursor.rowcount != 1:
        raise InventoryError("库存不足或库存数量超出允许范围", 409)


def _ledger(connection, product_id, delta, kind, reason, request_id, event_key=None, stock_after=None, order_id=None, price=None, created_at=None):
    product = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    entry_id = insert_id(connection, """INSERT INTO inventory_entries(event_key, product_id, product_name, sku, unit, kind,
        delta, stock_after, unit_price_cents, reason, request_id, order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_key or uuid.uuid4().hex, product_id, product["name"], product["sku"], product["unit"], kind, delta,
         product["stock"] if stock_after is None else stock_after, product["price_cents"] if price is None else price,
         reason, request_id, order_id))
    if created_at:
        connection.execute("UPDATE inventory_entries SET created_at = ? WHERE id = ?", (created_at, entry_id))
    return entry_id


def _insert_movement(connection, product_id, action, quantity, request_id, reason, order_id=None):
    product = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    movement_id = insert_id(connection, """INSERT INTO movements(request_id, action, product_id, quantity, unit_price_cents,
        stock_after, reason, product_name, unit, sku, order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (request_id, action, product_id, quantity, product["price_cents"], product["stock"], reason,
         product["name"], product["unit"], product["sku"], order_id))
    _ledger(connection, product_id, -quantity if action == "purchase" else quantity,
            "listing" if reason == "initial_stock" else action, reason, request_id,
            event_key=f"movement:{movement_id}", order_id=order_id)
    return connection.execute("SELECT * FROM movements WHERE id = ?", (movement_id,)).fetchone()


def _order_result(connection, order_id, buyer_key=None):
    row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None or (buyer_key is not None and row["buyer_key"] != buyer_key):
        raise InventoryError("订单不存在", 404)
    result = dict(row)
    result.pop("fingerprint")
    result.pop("buyer_key", None)
    result["items"] = [dict(r) for r in connection.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,))]
    return result


def _order_items(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise InventoryError("订单须包含 1 到 50 种商品")
    items = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("product_id"), str) or not item["product_id"]:
            raise InventoryError("订单商品格式无效")
        quantity = item.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 9999:
            raise InventoryError("商品数量须为 1 到 9999 的整数")
        if item["product_id"] in seen:
            raise InventoryError("订单中同一商品只能出现一次")
        seen.add(item["product_id"])
        items.append({"product_id": item["product_id"], "quantity": quantity})
    return sorted(items, key=lambda item: item["product_id"])


def _create_order(connection, items, request_id, movement_request=None, buyer_key=None, expected_prices=None):
    fingerprint = _fingerprint(items)
    previous = connection.execute("SELECT * FROM orders WHERE request_id = ?", (request_id,)).fetchone()
    if previous:
        if previous["fingerprint"] != fingerprint:
            raise InventoryError("请求编号已用于其他订单", 409)
        if buyer_key is not None and previous["buyer_key"] != buyer_key:
            raise InventoryError("请求编号已用于其他订单", 409)
        return _order_result(connection, previous["id"], buyer_key), True
    products = []
    for item in items:
        product = connection.execute("SELECT * FROM products WHERE id = ?", (item["product_id"],)).fetchone()
        if product is None:
            raise InventoryError("订单商品不存在", 404)
        if not product["active"]:
            raise InventoryError(f"{product['name']}已下架，无法购买", 409)
        if product["stock"] < item["quantity"]:
            raise InventoryError(f"{product['name']}库存不足", 409)
        products.append(product)
    if expected_prices is not None:
        for product, item in zip(products, items):
            if expected_prices.get(product["id"]) != product["price_cents"]:
                raise InventoryError("商品价格已变化，请重新确认", 409)
    order_id = uuid.uuid4().hex
    total = sum(p["price_cents"] * item["quantity"] for p, item in zip(products, items))
    connection.execute("INSERT INTO orders(id, request_id, fingerprint, status, total_cents, buyer_key) VALUES (?, ?, ?, 'completed', ?, ?)",
                       (order_id, request_id, fingerprint, total, buyer_key))
    for i, (product, item) in enumerate(zip(products, items)):
        _change_stock(connection, product["id"], -item["quantity"])
        movement = _insert_movement(connection, product["id"], "purchase", item["quantity"],
                                    movement_request or f"order:{order_id}:{i}", "订单购买", order_id)
        connection.execute("""INSERT INTO order_items(order_id, product_id, product_name, sku, unit, quantity, unit_price_cents, stock_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (order_id, product["id"], product["name"], product["sku"], product["unit"],
            item["quantity"], product["price_cents"], movement["stock_after"]))
    _audit(connection, "order_create", order_id, request_id, None, {"total_cents": total, "items": items})
    return _order_result(connection, order_id), False


def create_order(payload, db_path=None, buyer_key=None, expected_prices=None):
    items = _order_items(payload.get("items"))
    request_id = _request_id(payload.get("request_id"))
    with closing(connect(db_path)) as connection:
        begin_write(connection)
        try:
            order, duplicate = _create_order(connection, items, request_id, buyer_key=buyer_key,
                                             expected_prices=expected_prices)
            connection.commit()
            return {"order": order, "duplicate": duplicate}
        except Exception:
            connection.rollback()
            raise


def cancel_order(order_id, payload, db_path=None, buyer_key=None):
    reason = _text(payload.get("reason"), "取消原因", 200)
    request_id = _request_id(payload.get("request_id"))
    fingerprint = _fingerprint([order_id, reason])
    with closing(connect(db_path)) as connection:
        begin_write(connection)
        try:
            order = _order_result(connection, order_id, buyer_key)
            previous = _memo(connection, "cancel", request_id, fingerprint)
            if previous:
                connection.commit()
                return previous
            changed = order["status"] == "completed"
            if changed:
                for item in order["items"]:
                    _change_stock(connection, item["product_id"], item["quantity"])
                    _ledger(connection, item["product_id"], item["quantity"], "cancel", reason, request_id,
                            order_id=order_id, price=item["unit_price_cents"])
                cancelled_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                connection.execute("""UPDATE orders SET status = 'cancelled', cancellation_reason = ?,
                    cancelled_at = ? WHERE id = ?""", (reason, cancelled_at, order_id))
                _audit(connection, "order_cancel", order_id, request_id, {"status": "completed"}, {"reason": reason, "total_cents": order["total_cents"]})
            result = {"order": _order_result(connection, order_id, buyer_key), "changed": changed, "duplicate": False}
            _remember(connection, "cancel", request_id, fingerprint, result)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def get_order(order_id, db_path=None, buyer_key=None):
    with closing(connect(db_path)) as connection:
        return _order_result(connection, order_id, buyer_key)


def _page(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000000:
        raise InventoryError("页码必须是正整数")
    return value


def list_orders(page=1, status="", db_path=None, buyer_key=None):
    _page(page)
    if status not in ("", "completed", "cancelled"):
        raise InventoryError("订单状态无效")
    where = "(? = '' OR status = ?)" + (" AND buyer_key = ?" if buyer_key is not None else "")
    args = (status, status) + ((buyer_key,) if buyer_key is not None else ())
    with closing(connect(db_path)) as connection:
        begin_read(connection)
        count = connection.execute(f"SELECT COUNT(*) FROM orders WHERE {where}", args).fetchone()[0]
        pages = max(1, (count + 9) // 10)
        page = min(page, pages)
        ids = connection.execute(f"SELECT id FROM orders WHERE {where} ORDER BY created_at DESC, id DESC LIMIT 10 OFFSET ?",
                                 args + ((page - 1) * 10,)).fetchall()
        return {"orders": [_order_result(connection, r["id"]) for r in ids], "count": count, "page": page, "pages": pages, "page_size": 10}


def edit_product(product_id, payload, db_path=None):
    request_id = _request_id(payload.get("request_id"))
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise InventoryError("请提供商品版本号")
    allowed = {"name", "price_cents", "category", "description", "zone", "image_data", "version", "request_id"}
    if set(payload) - allowed:
        raise InventoryError("SKU、单位、库存和上架状态须通过对应操作管理")
    updates = {}
    for field, limit, required in (("name", 40, True), ("description", 100, False), ("zone", 40, True)):
        if field in payload:
            updates[field] = _text(payload[field], field, limit, required)
    if "price_cents" in payload:
        value = payload["price_cents"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100000000:
            raise InventoryError("商品单价格式或范围无效")
        updates["price_cents"] = value
    if "category" in payload:
        if payload["category"] not in CATEGORIES:
            raise InventoryError("商品分类无效")
        updates["category"] = payload["category"]
    image = _decode_image(payload["image_data"]) if "image_data" in payload else None
    if not updates and image is None:
        raise InventoryError("没有提供可编辑字段")
    fingerprint = _fingerprint([product_id, payload])
    with closing(connect(db_path)) as connection:
        begin_write(connection)
        try:
            previous = _memo(connection, "edit", request_id, fingerprint)
            if previous:
                connection.commit()
                return previous
            before = connection.execute(f"SELECT {PRODUCT_COLUMNS} FROM products WHERE id = ?", (product_id,)).fetchone()
            if before is None:
                raise InventoryError("商品不存在", 404)
            if before["version"] != version:
                raise InventoryError("商品已被其他操作修改，请刷新后重新编辑", 409)
            if image is not None:
                updates["image"], updates["image_type"] = image
            assignments = ", ".join(f"{field} = ?" for field in updates)
            connection.execute(f"UPDATE products SET {assignments}, version = version + 1 WHERE id = ?", tuple(updates.values()) + (product_id,))
            after = _product_result(connection.execute(f"SELECT {PRODUCT_COLUMNS} FROM products WHERE id = ?", (product_id,)).fetchone())
            _audit(connection, "product_edit", product_id, request_id, _product_result(before), after)
            result = {"product": after, "duplicate": False}
            _remember(connection, "edit", request_id, fingerprint, result)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def adjust_inventory(payload, db_path=None):
    product_id = _text(payload.get("product_id"), "商品编号", 100)
    reason = _text(payload.get("reason"), "调整原因", 200)
    request_id = _request_id(payload.get("request_id"))
    delta = payload.get("delta")
    if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0 or abs(delta) > 9999:
        raise InventoryError("调整数量须为 -9999 到 9999 的非零整数")
    fingerprint = _fingerprint([product_id, delta, reason])
    with closing(connect(db_path)) as connection:
        begin_write(connection)
        try:
            previous = _memo(connection, "adjust", request_id, fingerprint)
            if previous:
                connection.commit()
                return previous
            before = connection.execute("SELECT stock FROM products WHERE id = ?", (product_id,)).fetchone()
            if before is None:
                raise InventoryError("商品不存在", 404)
            _change_stock(connection, product_id, delta)
            entry_id = _ledger(connection, product_id, delta, "adjust", reason, request_id)
            after = before["stock"] + delta
            _audit(connection, "inventory_adjust", product_id, request_id, {"stock": before["stock"]}, {"stock": after, "reason": reason})
            result = {"id": entry_id, "product_id": product_id, "delta": delta, "stock_after": after, "duplicate": False}
            _remember(connection, "adjust", request_id, fingerprint, result)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def import_products(payload, db_path=None):
    request_id = _request_id(payload.get("request_id"))
    values = payload.get("products")
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise InventoryError("每次导入须包含 1 到 100 个商品")
    normalized = []
    for value in values:
        if not isinstance(value, dict) or value.get("image_data"):
            raise InventoryError("批量导入仅接受商品字段，图片请在上架后单独编辑")
        sku = _sku(value.get("sku", ""))
        if not sku:
            raise InventoryError("批量导入的每个商品都须提供 SKU")
        category = value.get("category", "其他")
        if category not in CATEGORIES:
            raise InventoryError("商品分类无效")
        price, stock = value.get("price_cents"), value.get("stock", 0)
        if isinstance(price, bool) or not isinstance(price, int) or not 1 <= price <= 100000000:
            raise InventoryError("导入商品的单价无效")
        if isinstance(stock, bool) or not isinstance(stock, int) or not 0 <= stock <= 9999:
            raise InventoryError("导入商品的库存无效")
        normalized.append({"sku": sku, "name": _text(value.get("name"), "商品名称", 40), "category": category,
            "unit": _text(value.get("unit", "件"), "单位", 8), "zone": _text(value.get("zone", "未分区"), "分区", 40),
            "description": _text(value.get("description", ""), "简介", 100, False), "price_cents": price, "stock": stock})
    fingerprint = _fingerprint(normalized)
    if len({value["sku"] for value in normalized}) != len(normalized):
        raise InventoryError("导入文件内包含重复 SKU", 409)
    with closing(connect(db_path)) as connection:
        begin_write(connection)
        try:
            previous = _memo(connection, "import", request_id, fingerprint)
            if previous:
                connection.commit()
                return previous
            ids = []
            for value in normalized:
                if connection.execute("SELECT 1 FROM products WHERE sku = ?", (value["sku"],)).fetchone():
                    raise InventoryError(f"SKU {value['sku']} 已存在，整批导入已回滚", 409)
                product_id = uuid.uuid4().hex
                connection.execute("""INSERT INTO products(id, sku, name, category, unit, zone, description, price_cents, stock)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (product_id, value["sku"], value["name"], value["category"], value["unit"],
                    value["zone"], value["description"], value["price_cents"], value["stock"]))
                if value["stock"]:
                    _insert_movement(connection, product_id, "restock", value["stock"], f"listing:{product_id}", "initial_stock")
                else:
                    _ledger(connection, product_id, 0, "opening", "导入初始库存", request_id)
                _audit(connection, "product_import", product_id, request_id, None, value)
                ids.append(product_id)
            result = {"product_ids": ids, "count": len(ids), "duplicate": False}
            _remember(connection, "import", request_id, fingerprint, result)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def inventory_history(product_id="", page=1, db_path=None):
    _page(page)
    with closing(connect(db_path)) as connection:
        begin_read(connection)
        if product_id and not connection.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone():
            raise InventoryError("商品不存在", 404)
        count = connection.execute("SELECT COUNT(*) FROM inventory_entries WHERE ? = '' OR product_id = ?", (product_id, product_id)).fetchone()[0]
        pages = max(1, (count + 19) // 20)
        page = min(page, pages)
        rows = [dict(r) for r in connection.execute("SELECT * FROM inventory_entries WHERE ? = '' OR product_id = ? ORDER BY id DESC LIMIT 20 OFFSET ?",
                                                  (product_id, product_id, (page - 1) * 20))]
        return {"entries": rows, "count": count, "page": page, "pages": pages, "page_size": 20}


def audit_history(page=1, db_path=None):
    _page(page)
    with closing(connect(db_path)) as connection:
        begin_read(connection)
        count = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        pages = max(1, (count + 19) // 20)
        page = min(page, pages)
        rows = [dict(r) for r in connection.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 20 OFFSET ?", ((page - 1) * 20,))]
        for row in rows:
            row["before"] = json.loads(row.pop("before_json")) if row["before_json"] is not None else row.pop("before_json")
            row["after"] = json.loads(row.pop("after_json"))
        return {"events": rows, "count": count, "page": page, "pages": pages, "page_size": 20}


def integrity_report(db_path=None, connection=None):
    if connection is not None:
        return _integrity_report(connection)
    with closing(connect(db_path)) as connection:
        begin_read(connection)
        return _integrity_report(connection)


def _integrity_report(connection):
    if is_postgres(connection):
        # PostgreSQL validates constraints on each committed write. This is
        # a business consistency check, not an offline disk integrity scan.
        physical = ["ok"]
        foreign_keys = [list(row.values()) for row in connection.execute("""SELECT conname FROM pg_constraint
            WHERE connamespace = 'public'::regnamespace AND contype IN ('f','c') AND NOT convalidated""")]
    else:
        physical = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    discrepancies = [dict(r) for r in connection.execute("""SELECT p.id, p.sku, p.stock,
        COALESCE(SUM(e.delta), 0) AS ledger_stock FROM products p LEFT JOIN inventory_entries e ON e.product_id = p.id
        GROUP BY p.id HAVING p.stock != COALESCE(SUM(e.delta), 0)""")]
    order_errors = [dict(r) for r in connection.execute("""SELECT o.id, o.total_cents, COALESCE(SUM(i.quantity * i.unit_price_cents),0) AS item_total
        FROM orders o LEFT JOIN order_items i ON i.order_id = o.id GROUP BY o.id
        HAVING o.total_cents != COALESCE(SUM(i.quantity * i.unit_price_cents),0)""")]
    chain_errors = [dict(r) for r in connection.execute("""WITH sequence AS (
        SELECT id, product_id, delta, stock_after, COALESCE(LAG(stock_after) OVER (PARTITION BY product_id ORDER BY id),0) AS previous_stock
        FROM inventory_entries) SELECT * FROM sequence WHERE stock_after != previous_stock + delta""")]
    refund_errors = [dict(r) for r in connection.execute("""SELECT i.order_id, i.product_id, i.quantity, o.status,
        COALESCE((SELECT SUM(e.delta) FROM inventory_entries e WHERE e.order_id=i.order_id AND e.product_id=i.product_id AND e.kind='cancel'),0) AS restored
        FROM order_items i JOIN orders o ON o.id=i.order_id
        WHERE COALESCE((SELECT SUM(e.delta) FROM inventory_entries e WHERE e.order_id=i.order_id AND e.product_id=i.product_id AND e.kind='cancel'),0)
              != CASE WHEN o.status='cancelled' THEN i.quantity ELSE 0 END""")]
    return {"ok": physical == ["ok"] and not foreign_keys and not discrepancies and not order_errors and not chain_errors and not refund_errors,
            "engine": "postgresql" if is_postgres(connection) else "sqlite",
            "storage_check": "committed constraints" if is_postgres(connection) else "SQLite integrity_check",
            "foreign_keys": foreign_keys, "inventory_mismatches": discrepancies,
            "ledger_chain_mismatches": chain_errors, "order_mismatches": order_errors, "refund_mismatches": refund_errors}


def export_records(kind, db_path=None):
    queries = {
        "products": "SELECT id, sku, name, category, zone, unit, price_cents, stock, active, version, description FROM products",
        "orders": "SELECT id, status, total_cents, created_at, cancelled_at, cancellation_reason FROM orders ORDER BY created_at, id",
        "inventory": "SELECT id, product_id, product_name, sku, unit, kind, delta, stock_after, reason, order_id, created_at FROM inventory_entries ORDER BY id",
        "audit": "SELECT * FROM audit_events ORDER BY id",
    }
    if kind not in queries:
        raise InventoryError("导出类型无效")
    with closing(connect(db_path)) as connection:
        query = queries[kind] + f" ORDER BY {product_order(connection)}" if kind == "products" else queries[kind]
        cursor = connection.execute(query)
        return [column[0] for column in cursor.description], [list(row.values()) if is_postgres(connection) else list(row) for row in cursor]
