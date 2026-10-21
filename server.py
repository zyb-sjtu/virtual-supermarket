"""HTTP API and role-gated static frontend with an independently configured database."""

from __future__ import annotations

import csv
import io
import json
import os
import socket
from contextlib import closing, nullcontext
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import auth
import agent_confirmations
import agent_tools
from agent_registry import ToolContext, ToolError
from db import (InventoryError, initialize, move_stock, snapshot, create_product, product_image, set_product_status,
                create_order, cancel_order, get_order, list_orders, edit_product, adjust_inventory,
                import_products, inventory_history, audit_history, export_records, database_path, connect, CATEGORIES)
from runtime import DatabaseLease
from storage import backend, postgres_options, DATABASE_ERRORS


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
ASSETS = {
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/login.css": ("login.css", "text/css; charset=utf-8"),
    "/login.js": ("login.js", "text/javascript; charset=utf-8"),
    "/customer.css": ("customer.css", "text/css; charset=utf-8"),
    "/customer.js": ("customer.js", "text/javascript; charset=utf-8"),
    "/product-placeholder.svg": ("product-placeholder.svg", "image/svg+xml"),
    "/import-template.json": ("import-template.json", "application/json; charset=utf-8"),
}


class ShopHandler(BaseHTTPRequestHandler):
    server_version = "LocalShop/1.4"
    timeout = 15

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        try:
            query = parse_qs(urlsplit(self.path).query)
            session = self._session()

            if path in ("/login", "/login/"):
                self._page("login.html")
                return
            if path in ("/merchant", "/merchant/"):
                if session is None:
                    self._redirect("/login")
                elif session["role"] != auth.MERCHANT:
                    self._redirect("/customer")
                else:
                    self._page("index.html")
                return
            if path in ("/customer", "/customer/"):
                if session is None:
                    self._redirect("/login")
                elif session["role"] != auth.CUSTOMER:
                    self._redirect("/merchant")
                else:
                    self._page("customer.html")
                return
            if path in ("", "/"):
                if session is None:
                    self._redirect("/login")
                elif session["role"] == auth.MERCHANT:
                    self._redirect("/merchant")
                else:
                    self._redirect("/customer")
                return
            if path.startswith("/api/agent/"):
                self._handle_agent_get(path, parts)
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
            if path == "/api/session":
                self._json(200, {"authenticated": session is not None,
                                 "role": session["role"] if session else None})
                return
            if path == "/api/state":
                session = self._require_api((auth.MERCHANT, auth.CUSTOMER))
                if session is None:
                    return
                status = query.get("status", ["active"])[0]
                if session["role"] == auth.CUSTOMER:
                    status = "active"
                state = snapshot(page=int(query.get("page", ["1"])[0]), status=status,
                                 search=query.get("search", [""])[0], category=query.get("category", [""])[0])
                if session["role"] == auth.CUSTOMER:
                    state = {key: state[key] for key in
                             ("products", "page", "page_size", "status", "pages", "product_count")}
                self._json(HTTPStatus.OK, state)
                return
            if path == "/api/orders":
                session = self._require_api((auth.MERCHANT, auth.CUSTOMER))
                if session is None:
                    return
                buyer_key = session["buyer_key"] if session["role"] == auth.CUSTOMER else None
                self._json(200, list_orders(int(query.get("page", ["1"])[0]), query.get("status", [""])[0],
                                            buyer_key=buyer_key))
                return
            if len(parts) == 3 and parts[:2] == ["api", "orders"]:
                session = self._require_api((auth.MERCHANT, auth.CUSTOMER))
                if session is None:
                    return
                buyer_key = session["buyer_key"] if session["role"] == auth.CUSTOMER else None
                self._json(200, {"order": get_order(parts[2], buyer_key=buyer_key)})
                return
            if path == "/api/inventory":
                if self._require_api((auth.MERCHANT,)) is None:
                    return
                self._json(200, inventory_history(query.get("product_id", [""])[0], int(query.get("page", ["1"])[0])))
                return
            if path == "/api/audit":
                if self._require_api((auth.MERCHANT,)) is None:
                    return
                self._json(200, audit_history(int(query.get("page", ["1"])[0])))
                return
            if path == "/api/categories":
                if self._require_api((auth.MERCHANT, auth.CUSTOMER)) is None:
                    return
                self._json(200, {"categories": list(CATEGORIES)})
                return
            if path == "/api/export":
                if self._require_api((auth.MERCHANT,)) is None:
                    return
                kind = query.get("kind", ["products"])[0]
                headers, rows = export_records(kind)
                output = io.StringIO(newline="")
                writer = csv.writer(output)
                writer.writerow(headers)
                for row in rows:
                    writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value for value in row])
                self._bytes(200, output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", f'{kind}.csv')
                return
            if len(parts) == 4 and parts[:2] == ["api", "products"] and parts[3] == "image":
                if self._require_api((auth.MERCHANT, auth.CUSTOMER)) is None:
                    return
                data, mime = product_image(parts[2])
                self._bytes(HTTPStatus.OK, data, mime)
                return

            asset = ASSETS.get(path)
            if asset is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})
                return
            data = (STATIC / asset[0]).read_bytes()
            self._bytes(HTTPStatus.OK, data, asset[1])
        except ValueError:
            self._json(400, {"error": "页码必须是正整数"})
        except InventoryError as error:
            self._json(error.status, {"error": str(error)})
        except DATABASE_ERRORS:
            self._json(503, {"error": "数据库暂不可用，请稍后重试"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        if path.startswith("/api/agent/"):
            self._handle_agent_post(path, parts)
            return
        cancelling = len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "cancel"
        allowed = ("/api/session", "/api/logout", "/api/movements", "/api/products", "/api/product-status",
                   "/api/orders", "/api/inventory/adjust", "/api/products/import")
        if path not in allowed and not cancelling:
            self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        if path == "/api/logout":
            auth.delete_session(self._session_token())
            self._json(200, {"ok": True}, headers=[("Set-Cookie", self._clear_session_cookie())])
            return
        try:
            payload = self._payload()
            if path == "/api/session":
                role = payload.get("role")
                if role not in auth.ROLES:
                    raise InventoryError("身份无效")
                old_token = self._session_token()
                if old_token:
                    auth.delete_session(old_token)
                token, session = auth.create_session(role)
                redirect = "/merchant" if role == auth.MERCHANT else "/customer"
                self._json(200, {"role": role, "redirect": redirect},
                           headers=[("Set-Cookie", self._session_cookie(token))])
                return
            if path == "/api/movements":
                session = self._require_api((auth.MERCHANT, auth.CUSTOMER))
                if session is None:
                    return
                action = payload.get("action")
                if action == "purchase" and session["role"] != auth.CUSTOMER:
                    self._json(403, {"error": "当前身份不能购买"})
                    return
                if action == "restock" and session["role"] != auth.MERCHANT:
                    self._json(403, {"error": "当前身份不能补货"})
                    return
                result = move_stock(action, payload.get("product_id"), payload.get("quantity"),
                                    payload.get("request_id"),
                                    buyer_key=session["buyer_key"] if session["role"] == auth.CUSTOMER else None)
            elif path == "/api/products":
                if self._require_api((auth.MERCHANT,)) is None:
                    return
                result = create_product(payload)
            elif path == "/api/product-status":
                if self._require_api((auth.MERCHANT,)) is None:
                    return
                result = set_product_status(payload.get("product_id"), payload.get("active"), payload.get("request_id"))
            elif path == "/api/products/import":
                if self._require_api((auth.MERCHANT,)) is None:
                    return
                result = import_products(payload)
            elif path == "/api/inventory/adjust":
                if self._require_api((auth.MERCHANT,)) is None:
                    return
                result = adjust_inventory(payload)
            elif path == "/api/orders":
                session = self._require_api((auth.CUSTOMER,))
                if session is None:
                    return
                result = create_order(payload, buyer_key=session["buyer_key"])
            elif cancelling:
                session = self._require_api((auth.MERCHANT, auth.CUSTOMER))
                if session is None:
                    return
                result = cancel_order(parts[2], payload,
                                      buyer_key=session["buyer_key"] if session["role"] == auth.CUSTOMER else None)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
                return
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
        if self._require_api((auth.MERCHANT,)) is None:
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

    def _handle_agent_get(self, path: str, parts: list[str]) -> None:
        session = self._agent_session()
        if session is None:
            return
        context = self._agent_context(session)
        try:
            if path == "/api/agent/tools":
                self._agent_ok({"tools": agent_tools.registry.list_for(context)})
                return
            if len(parts) == 4 and parts[:3] == ["api", "agent", "confirmations"]:
                data = agent_confirmations.store.get(parts[3], context.role, context.buyer_key)
                self._agent_ok(data, confirmation=data)
                return
            self._agent_error("TOOL_NOT_FOUND", "接口不存在", 404)
        except agent_confirmations.ConfirmationError as error:
            self._agent_error(error.code, str(error), error.status)
        except ToolError as error:
            self._agent_error(error.code, str(error), error.status, details=error.details)
        except DATABASE_ERRORS:
            self._agent_error("DATABASE_UNAVAILABLE", "数据库暂不可用，请稍后重试", 503)

    def _handle_agent_post(self, path: str, parts: list[str]) -> None:
        session = self._agent_session()
        if session is None:
            return
        context = self._agent_context(session)
        if len(parts) == 4 and parts[:3] == ["api", "agent", "tools"]:
            tool = parts[3]
            request_id = None
            try:
                payload = self._payload()
                if set(payload) - {"arguments", "request_id", "confirmation_id"}:
                    self._agent_error("INVALID_ARGUMENT", "请求包含未知字段", 400, tool=tool)
                    return
                arguments = payload.get("arguments", {})
                if not isinstance(arguments, dict):
                    self._agent_error("INVALID_ARGUMENT", "arguments 必须是对象", 400, tool=tool)
                    return
                request_id = payload.get("request_id")
                confirmation_id = payload.get("confirmation_id")
                spec = agent_tools.registry.resolve(tool, context)
                result = agent_tools.registry.invoke(tool, arguments, context,
                                                     request_id=request_id, confirmation_id=confirmation_id)
                duplicate = bool(result.get("duplicate")) if isinstance(result, dict) else False
                confirmation = result.get("confirmation") if isinstance(result, dict) else None
                self._agent_ok(result, tool=tool, request_id=request_id, duplicate=duplicate,
                               requires_confirmation=spec.requires_confirmation, confirmation=confirmation)
            except ToolError as error:
                self._agent_error(error.code, str(error), error.status, tool=tool,
                                  request_id=request_id, details=error.details)
            except InventoryError as error:
                code = ("PAYLOAD_TOO_LARGE" if error.status == 413 else
                        "UNSUPPORTED_MEDIA_TYPE" if error.status == 415 else "INVALID_ARGUMENT")
                self._agent_error(code, str(error), error.status, tool=tool, request_id=request_id)
            except (ValueError, json.JSONDecodeError):
                self._agent_error("INVALID_ARGUMENT", "JSON 格式无效", 400, tool=tool, request_id=request_id)
            except DATABASE_ERRORS:
                self._agent_error("DATABASE_UNAVAILABLE", "数据库暂不可用，请稍后重试", 503,
                                  tool=tool, request_id=request_id)
            return
        if len(parts) == 5 and parts[:3] == ["api", "agent", "confirmations"] and parts[4] == "approve":
            try:
                data = agent_confirmations.store.approve(parts[3], context.role, context.buyer_key)
                self._agent_ok(data, confirmation=data)
            except agent_confirmations.ConfirmationError as error:
                self._agent_error(error.code, str(error), error.status)
            return
        self._agent_error("TOOL_NOT_FOUND", "接口不存在", 404)

    def _agent_session(self):
        session = self._session()
        if session is None:
            self._agent_error("UNAUTHENTICATED", "请先选择身份登录", 401)
            return None
        return session

    @staticmethod
    def _agent_context(session) -> ToolContext:
        return ToolContext(session["role"], session.get("buyer_key"))

    def _agent_ok(self, data, tool=None, request_id=None, duplicate=False,
                  requires_confirmation=False, confirmation=None) -> None:
        self._json(200, {
            "ok": True, "data": data, "error": None, "tool": tool, "request_id": request_id,
            "duplicate": duplicate, "requires_confirmation": requires_confirmation,
            "confirmation": confirmation,
        })

    def _agent_error(self, code: str, message: str, status: int, tool=None,
                     request_id=None, details=None) -> None:
        self._json(status, {
            "ok": False, "data": None,
            "error": {"code": code, "message": message, "status": status, "details": details or []},
            "tool": tool, "request_id": request_id, "duplicate": False,
            "requires_confirmation": False, "confirmation": None,
        })

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(auth.COOKIE_NAME)
        return morsel.value if morsel else ""

    def _session(self):
        return auth.get_session(self._session_token())

    def _require_api(self, roles):
        session = self._session()
        if session is None:
            self._json(401, {"error": "请先选择身份登录"})
            return None
        if session["role"] not in roles:
            self._json(403, {"error": "当前身份无权执行此操作"})
            return None
        return session

    @staticmethod
    def _session_cookie(token: str) -> str:
        return f"{auth.COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={auth.SESSION_TTL_SECONDS}"

    @staticmethod
    def _clear_session_cookie() -> str:
        return f"{auth.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _page(self, filename: str) -> None:
        data = (STATIC / filename).read_bytes()
        self._bytes(HTTPStatus.OK, data, "text/html; charset=utf-8")

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

    def _json(self, status: int, payload: dict, headers=None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._bytes(status, data, "application/json; charset=utf-8", headers=headers)

    def _bytes(self, status: int, data: bytes, mime: str, filename=None, headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or []):
            self.send_header(name, value)
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
