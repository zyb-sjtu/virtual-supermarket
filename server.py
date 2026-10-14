"""HTTP API and static frontend with an independently configured database."""

from __future__ import annotations

import json
import csv
import io
import os
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from db import (InventoryError, initialize, move_stock, snapshot, create_product, product_image, set_product_status,
                create_order, cancel_order, get_order, list_orders, edit_product, adjust_inventory,
                import_products, inventory_history, audit_history, export_records, database_path, connect, CATEGORIES)
from contextlib import closing
from runtime import DatabaseLease
from storage import backend, postgres_options, DATABASE_ERRORS
from contextlib import nullcontext


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/product-placeholder.svg": ("product-placeholder.svg", "image/svg+xml"),
    "/import-template.json": ("import-template.json", "application/json; charset=utf-8"),
}


class ShopHandler(BaseHTTPRequestHandler):
    server_version = "LocalShop/1.4"
    timeout = 15

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            query = parse_qs(urlsplit(self.path).query)
            page = int(query.get("page", ["1"])[0])
            status = query.get("status", [""])[0]
            if path == "/api/state":
                self._json(HTTPStatus.OK, snapshot(page=page, status=status or "active",
                    search=query.get("search", [""])[0], category=query.get("category", [""])[0]))
                return
            if path == "/api/orders":
                self._json(200, list_orders(page, status))
                return
            if len(path.strip("/").split("/")) == 3 and path.startswith("/api/orders/"):
                self._json(200, {"order": get_order(path.split("/")[3])})
                return
            if path == "/api/inventory":
                self._json(200, inventory_history(query.get("product_id", [""])[0], page))
                return
            if path == "/api/audit":
                self._json(200, audit_history(page))
                return
            if path == "/api/categories":
                self._json(200, {"categories": list(CATEGORIES)})
                return
            if path == "/api/health":
                with closing(connect()) as connection:
                    connection.execute("SELECT 1 FROM products LIMIT 1").fetchone()
                details = {"engine": backend()}
                if backend() == "postgresql":
                    options = postgres_options()
                    details.update(host=options["host"], port=options["port"], name=options["dbname"])
                self._json(200, {"status": "ok", "database": "ok", "version": "1.4", **details})
                return
            if path == "/api/export":
                kind = query.get("kind", ["products"])[0]
                headers, rows = export_records(kind)
                output = io.StringIO(newline="")
                writer = csv.writer(output)
                writer.writerow(headers)
                for row in rows:
                    writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value for value in row])
                self._bytes(200, output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", f'{kind}.csv')
                return
            segments = path.strip("/").split("/")
            if len(segments) == 4 and segments[:2] == ["api", "products"] and segments[3] == "image":
                data, mime = product_image(segments[2])
                self._bytes(HTTPStatus.OK, data, mime)
                return
        except ValueError:
            self._json(400, {"error": "页码必须是正整数"})
            return
        except InventoryError as error:
            self._json(error.status, {"error": str(error)})
            return
        except DATABASE_ERRORS:
            self._json(503, {"error": "数据库暂不可用，请稍后重试"})
            return
        asset = ASSETS.get(path)
        if asset is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})
            return
        data = (STATIC / asset[0]).read_bytes()
        self._bytes(HTTPStatus.OK, data, asset[1])

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        cancelling = len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "cancel"
        if path not in ("/api/movements", "/api/products", "/api/product-status", "/api/orders", "/api/inventory/adjust", "/api/products/import") and not cancelling:
            self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        try:
            payload = self._payload()
            if path == "/api/products":
                result = create_product(payload)
            elif path == "/api/product-status":
                result = set_product_status(payload.get("product_id"), payload.get("active"), payload.get("request_id"))
            elif path == "/api/orders":
                result = create_order(payload)
            elif path == "/api/inventory/adjust":
                result = adjust_inventory(payload)
            elif path == "/api/products/import":
                result = import_products(payload)
            elif cancelling:
                result = cancel_order(parts[2], payload)
            else:
                result = move_stock(payload.get("action"), payload.get("product_id"),
                                    payload.get("quantity"), payload.get("request_id"))
            self._json(HTTPStatus.OK if result["duplicate"] else HTTPStatus.CREATED, result)
        except (ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON 格式无效"})
        except InventoryError as error:
            self._json(error.status, {"error": str(error)})
        except DATABASE_ERRORS:
            self._json(503, {"error": "数据库暂不可用，请使用原请求编号重试"})

    def do_PATCH(self):
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "products"]:
            self._json(404, {"error": "接口不存在"})
            return
        try:
            result = edit_product(parts[2], self._payload())
            self._json(200, result)
        except ValueError:
            self._json(400, {"error": "JSON 格式无效"})
        except InventoryError as error:
            self._json(error.status, {"error": str(error)})
        except DATABASE_ERRORS:
            self._json(503, {"error": "数据库暂不可用，请使用原请求编号重试"})

    def _payload(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 1500000:
            raise InventoryError("请求内容大小无效", 413)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise InventoryError("请使用 JSON 格式", 415)
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise InventoryError("请求内容无效")
        return payload

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._bytes(status, data, "application/json; charset=utf-8")

    def _bytes(self, status: int, data: bytes, mime: str, filename=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)


class ShopServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    host = os.environ.get("SHOP_HOST", "127.0.0.1")
    port = int(os.environ.get("SHOP_PORT", "8000"))
    try:
        with DatabaseLease(database_path()) if backend() == "sqlite" else nullcontext():
            initialize()
            server = ShopServer((host, port), ShopHandler)
            print(f"超市管理系统已启动：http://{host}:{port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (RuntimeError, OSError) as error:
        raise SystemExit(f"启动失败：{error}")
    except DATABASE_ERRORS:
        raise SystemExit("启动失败：数据库连接或初始化失败，请检查数据库服务及连接配置")


if __name__ == "__main__":
    main()
