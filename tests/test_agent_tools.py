import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, build_opener, HTTPCookieProcessor

import auth
import agent_confirmations
import agent_tools
from agent_registry import ToolContext, ToolError
from db import connect, create_product, initialize, snapshot
from server import ShopHandler
from tests.session_helpers import login


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        auth.reset_sessions()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "agent.sqlite3"
        self.env = patch.dict("os.environ", {"SHOP_DB_BACKEND": "sqlite", "SHOP_DB_PATH": str(self.db)})
        self.env.start()
        initialize(self.db)
        agent_confirmations.store.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ShopHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.merchant, _ = login(self.base, "merchant")
        self.customer, _ = login(self.base, "customer")
        self.customer_b, _ = login(self.base, "customer")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        agent_confirmations.store.reset()
        self.env.stop()
        self.temp.cleanup()
        auth.reset_sessions()

    def call(self, opener, path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with opener.open(request) as response:
                return response.status, response.headers, response.read(), response.geturl()
        except Exception as error:
            if hasattr(error, "code") and hasattr(error, "read"):
                return error.code, error.headers, error.read(), error.geturl()
            raise

    def tool(self, opener, name, arguments=None, request_id=None, confirmation_id=None):
        payload = {"arguments": arguments or {}}
        if request_id is not None:
            payload["request_id"] = request_id
        if confirmation_id is not None:
            payload["confirmation_id"] = confirmation_id
        return self.call(opener, f"/api/agent/tools/{name}", "POST", payload)

    def body(self, result):
        return json.loads(result[2].decode("utf-8"))

    def test_anonymous_and_role_filtered_tool_lists(self):
        anonymous = build_opener(HTTPCookieProcessor(CookieJar()))
        status, _, body, _ = self.call(anonymous, "/api/agent/tools")
        self.assertEqual(status, 401)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "UNAUTHENTICATED")

        status, _, body, _ = self.call(self.customer, "/api/agent/tools")
        self.assertEqual(status, 200)
        result = self.body((status, {}, body, ""))
        names = {tool["name"] for tool in result["data"]["tools"]}
        self.assertEqual(names, {
            "customer.list_categories", "customer.search_products", "customer.get_product",
            "customer.get_my_orders", "customer.get_my_order",
            "customer.prepare_order", "customer.commit_order",
            "customer.prepare_buy_now", "customer.commit_buy_now",
            "customer.prepare_order_cancel", "customer.commit_order_cancel",
        })

        status, _, body, _ = self.call(self.merchant, "/api/agent/tools")
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in self.body((status, {}, body, ""))["data"]["tools"]}
        self.assertEqual(names, {
            "merchant.list_categories", "merchant.list_products", "merchant.get_product",
            "merchant.list_orders", "merchant.get_order", "merchant.get_inventory_history",
            "merchant.get_audit_events", "merchant.get_sales_summary",
        })
        self.assertFalse(any("image" in name or "price" in name for name in names))

    def test_read_tools_and_role_errors(self):
        status, headers, body, _ = self.tool(self.customer, "customer.search_products",
                                              {"query": "苹果", "page": 1})
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Set-Cookie"))
        result = self.body((status, {}, body, ""))
        self.assertTrue(result["data"]["products"])
        product = result["data"]["products"][0]
        self.assertIn("stock", product)
        self.assertNotIn("sales_cents", result["data"])
        self.assertNotIn("buyer_key", json.dumps(result))

        status, _, body, _ = self.tool(self.merchant, "merchant.get_sales_summary")
        self.assertEqual(status, 200)
        summary = self.body((status, {}, body, ""))["data"]
        self.assertIn("sales_cents", summary)
        self.assertIn("stock_total", summary)

        status, _, body, _ = self.tool(self.customer, "merchant.list_orders")
        self.assertEqual(status, 403)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "FORBIDDEN")

        status, _, body, _ = self.tool(self.customer, "customer.no_such_tool")
        self.assertEqual(status, 404)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "TOOL_NOT_FOUND")

        status, _, body, _ = self.tool(self.customer, "customer.search_products", {"page": 0})
        self.assertEqual(status, 400)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "INVALID_ARGUMENT")

        status, _, body, _ = self.tool(self.customer, "customer.search_products", {"role": "merchant"})
        self.assertEqual(status, 400)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "INVALID_ARGUMENT")

        before = snapshot(self.db)["products"]
        self.tool(self.merchant, "merchant.get_inventory_history", {"product_id": "apple"})
        after = snapshot(self.db)["products"]
        self.assertEqual(before, after)

    def test_customer_order_scope(self):
        status, _, body, _ = self.call(self.customer, "/api/orders", "POST",
                                       {"request_id": "scope-order", "items": [{"product_id": "apple", "quantity": 1}]})
        self.assertEqual(status, 201)
        order_id = self.body((status, {}, body, ""))["order"]["id"]

        status, _, body, _ = self.tool(self.customer, "customer.get_my_orders")
        self.assertEqual(status, 200)
        self.assertEqual(self.body((status, {}, body, ""))["data"]["count"], 1)
        status, _, body, _ = self.tool(self.customer, "customer.get_my_order", {"order_id": order_id})
        self.assertEqual(status, 200)
        self.assertNotIn("buyer_key", json.dumps(self.body((status, {}, body, ""))))

        status, _, body, _ = self.tool(self.customer_b, "customer.get_my_orders")
        self.assertEqual(status, 200)
        self.assertEqual(self.body((status, {}, body, ""))["data"]["count"], 0)
        status, _, body, _ = self.tool(self.customer_b, "customer.get_my_order", {"order_id": order_id})
        self.assertEqual(status, 404)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "NOT_FOUND")

        status, _, body, _ = self.tool(self.merchant, "merchant.list_orders")
        self.assertEqual(status, 200)
        self.assertEqual(self.body((status, {}, body, ""))["data"]["count"], 1)

    def test_prepare_approve_commit_and_replay(self):
        items = [{"product_id": "apple", "quantity": 2}]
        status, _, body, _ = self.tool(self.customer, "customer.prepare_order", {"items": items})
        self.assertEqual(status, 200)
        prepared = self.body((status, {}, body, ""))
        self.assertEqual(prepared["data"]["preview"]["total_cents"], 1180)
        confirmation_id = prepared["data"]["confirmation"]["id"]
        self.assertEqual(prepared["data"]["confirmation"]["state"], "pending")

        status, _, body, _ = self.call(self.customer_b, f"/api/agent/confirmations/{confirmation_id}")
        self.assertEqual(status, 404)
        status, _, body, _ = self.call(self.customer_b, f"/api/agent/confirmations/{confirmation_id}/approve", "POST")
        self.assertEqual(status, 404)

        status, _, body, _ = self.tool(self.customer, "customer.commit_order", {"items": items},
                                       request_id="agent-order-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_NOT_APPROVED")

        status, _, body, _ = self.call(self.customer, f"/api/agent/confirmations/{confirmation_id}/approve", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(self.body((status, {}, body, ""))["data"]["state"], "approved")

        status, _, body, _ = self.tool(self.customer, "customer.commit_order", {"items": items},
                                       request_id="agent-order-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 200)
        committed = self.body((status, {}, body, ""))
        self.assertEqual(committed["data"]["order"]["total_cents"], 1180)
        self.assertTrue(committed["requires_confirmation"])
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 22)

        status, _, body, _ = self.tool(self.customer, "customer.commit_order", {"items": items},
                                       request_id="agent-order-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 200)
        self.assertTrue(self.body((status, {}, body, ""))["duplicate"])

        status, _, body, _ = self.tool(self.customer, "customer.commit_order", {"items": items},
                                       request_id="agent-order-2", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_USED")

    def test_confirmation_mismatch_and_stale_price(self):
        items = [{"product_id": "apple", "quantity": 1}]
        status, _, body, _ = self.tool(self.customer, "customer.prepare_order", {"items": items})
        confirmation_id = self.body((status, {}, body, ""))["data"]["confirmation"]["id"]
        self.call(self.customer, f"/api/agent/confirmations/{confirmation_id}/approve", "POST")

        status, _, body, _ = self.tool(self.customer, "customer.commit_order",
                                       {"items": [{"product_id": "apple", "quantity": 2}]},
                                       request_id="mismatch", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_MISMATCH")

        status, _, body, _ = self.tool(self.customer, "customer.prepare_order", {"items": items})
        confirmation_id = self.body((status, {}, body, ""))["data"]["confirmation"]["id"]
        self.call(self.customer, f"/api/agent/confirmations/{confirmation_id}/approve", "POST")

        status, _, body, _ = self.call(self.merchant, "/api/state?status=active")
        apple = next(product for product in self.body((status, {}, body, ""))["products"] if product["id"] == "apple")
        status, _, body, _ = self.call(self.merchant, "/api/products/apple", "PATCH",
                                       {"price_cents": 700, "version": apple["version"],
                                        "request_id": "agent-price-change"})
        self.assertEqual(status, 200)

        status, _, body, _ = self.tool(self.customer, "customer.commit_order", {"items": items},
                                       request_id="stale-order", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_STALE")

    def seed_catalog(self):
        products = [
            ("低价菜", 100, 5), ("缺货菜", 200, 0), ("中价菜", 300, 1),
            ("中高价菜", 350, 1), ("高价菜", 550, 7), ("顶价菜", 900, 2),
        ]
        for index, (name, price, stock) in enumerate(products):
            create_product({
                "name": name, "category": "蔬菜", "unit": "件", "zone": "测试区",
                "price_cents": price, "stock": stock, "sku": f"TEST-{index}",
                "request_id": f"catalog-{index}",
            }, self.db)

    def test_product_detail_role_visibility(self):
        status, _, body, _ = self.call(self.customer, "/api/products/apple")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["product"]["id"], "apple")

        status, _, body, _ = self.call(self.customer, "/api/products/missing")
        self.assertEqual(status, 404)

        status, _, body, _ = self.call(self.merchant, "/api/product-status", "POST",
                                       {"product_id": "apple", "active": False, "request_id": "delist-apple"})
        self.assertEqual(status, 201)

        status, _, body, _ = self.call(self.customer, "/api/products/apple")
        self.assertEqual(status, 404)
        status, _, body, _ = self.call(self.merchant, "/api/products/apple")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["product"]["active"])

        status, _, body, _ = self.tool(self.customer, "customer.get_product", {"product_id": "apple"})
        self.assertEqual(status, 404)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "NOT_FOUND")
        status, _, body, _ = self.tool(self.merchant, "merchant.get_product", {"product_id": "apple"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["data"]["product"]["active"])

    def test_catalog_filters_sort_and_page_size(self):
        self.seed_catalog()
        status, _, body, _ = self.tool(self.customer, "customer.search_products",
                                       {"min_price_cents": 500, "max_price_cents": 600, "sort": "price_desc"})
        self.assertEqual(status, 200)
        result = json.loads(body)["data"]
        self.assertEqual(result["product_count"], 2)
        self.assertEqual([item["price_cents"] for item in result["products"]], [590, 550])
        self.assertEqual(result["filters"]["sort"], "price_desc")

        status, _, body, _ = self.call(self.customer,
                                       "/api/state?min_price_cents=500&max_price_cents=600&sort=price_desc")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["product_count"], 2)
        self.assertEqual(state["filters"]["min_price_cents"], 500)

        status, _, body, _ = self.tool(self.customer, "customer.search_products", {"in_stock": False})
        self.assertEqual(status, 200)
        out_of_stock = json.loads(body)["data"]
        self.assertEqual(out_of_stock["product_count"], 1)
        self.assertEqual(out_of_stock["products"][0]["stock"], 0)

        status, _, body, _ = self.tool(self.customer, "customer.search_products",
                                       {"in_stock": True, "sort": "price_asc"})
        self.assertEqual(status, 200)
        page = json.loads(body)["data"]
        self.assertEqual(page["page_size"], 6)
        self.assertEqual(len(page["products"]), 6)
        self.assertEqual(page["pages"], 2)
        prices = [item["price_cents"] for item in page["products"]]
        self.assertEqual(prices, sorted(prices))

        status, _, body, _ = self.tool(self.customer, "customer.search_products",
                                       {"min_price_cents": 600, "max_price_cents": 500})
        self.assertEqual(status, 400)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "INVALID_ARGUMENT")
        status, _, body, _ = self.tool(self.customer, "customer.search_products", {"sort": "bogus"})
        self.assertEqual(status, 400)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "INVALID_ARGUMENT")

    def test_buy_now_prepare_approve_commit_and_replay(self):
        arguments = {"product_id": "apple", "quantity": 2}
        status, _, body, _ = self.tool(self.customer, "customer.prepare_buy_now", arguments)
        self.assertEqual(status, 200)
        prepared = json.loads(body)["data"]
        self.assertEqual(prepared["preview"]["total_cents"], 1180)
        confirmation_id = prepared["confirmation"]["id"]

        status, _, body, _ = self.tool(self.customer, "customer.commit_buy_now", arguments,
                                       request_id="buy-now-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_NOT_APPROVED")

        status, _, body, _ = self.call(self.customer,
                                       f"/api/agent/confirmations/{confirmation_id}/approve", "POST")
        self.assertEqual(status, 200)

        status, _, body, _ = self.tool(self.customer, "customer.commit_buy_now", arguments,
                                       request_id="buy-now-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 200)
        committed = json.loads(body)
        self.assertEqual(committed["data"]["order"]["total_cents"], 1180)
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 22)

        status, _, body, _ = self.tool(self.customer, "customer.commit_buy_now", arguments,
                                       request_id="buy-now-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["duplicate"])

        status, _, body, _ = self.tool(self.customer, "customer.commit_buy_now", arguments,
                                       request_id="buy-now-2", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_USED")

        status, _, body, _ = self.tool(self.customer, "customer.prepare_buy_now",
                                       {"product_id": "apple", "quantity": 1})
        confirmation_id = json.loads(body)["data"]["confirmation"]["id"]
        self.call(self.customer, f"/api/agent/confirmations/{confirmation_id}/approve", "POST")
        status, _, body, _ = self.call(self.merchant, "/api/state?status=active")
        apple = next(product for product in json.loads(body)["products"] if product["id"] == "apple")
        status, _, body, _ = self.call(self.merchant, "/api/products/apple", "PATCH",
                                       {"price_cents": 700, "version": apple["version"],
                                        "request_id": "buy-now-price-change"})
        self.assertEqual(status, 200)
        status, _, body, _ = self.tool(self.customer, "customer.commit_buy_now",
                                       {"product_id": "apple", "quantity": 1},
                                       request_id="buy-now-stale", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_STALE")
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 22)
        status, _, body, _ = self.call(self.customer, f"/api/agent/confirmations/{confirmation_id}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["state"], "approved")

    def test_order_cancel_flow_foreign_order_404_and_replay(self):
        status, _, body, _ = self.call(self.customer, "/api/orders", "POST",
                                       {"request_id": "cancel-order-1",
                                        "items": [{"product_id": "apple", "quantity": 2}]})
        self.assertEqual(status, 201)
        order_id = json.loads(body)["order"]["id"]
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 22)

        status, _, body, _ = self.tool(self.customer_b, "customer.prepare_order_cancel",
                                       {"order_id": order_id, "reason": "非本人订单"})
        self.assertEqual(status, 404)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "NOT_FOUND")

        arguments = {"order_id": order_id, "reason": "顾客取消"}
        status, _, body, _ = self.tool(self.customer, "customer.prepare_order_cancel", arguments)
        self.assertEqual(status, 200)
        prepared = json.loads(body)["data"]
        self.assertTrue(prepared["preview"]["cancellable"])
        confirmation_id = prepared["confirmation"]["id"]

        status, _, body, _ = self.tool(self.customer, "customer.commit_order_cancel", arguments,
                                       request_id="cancel-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_NOT_APPROVED")

        self.call(self.customer, f"/api/agent/confirmations/{confirmation_id}/approve", "POST")
        status, _, body, _ = self.tool(self.customer, "customer.commit_order_cancel", arguments,
                                       request_id="cancel-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 200)
        result = json.loads(body)["data"]
        self.assertTrue(result["changed"])
        self.assertEqual(result["order"]["status"], "cancelled")
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 24)

        status, _, body, _ = self.tool(self.customer, "customer.commit_order_cancel", arguments,
                                       request_id="cancel-1", confirmation_id=confirmation_id)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 24)

        status, _, body, _ = self.tool(self.customer, "customer.commit_order_cancel", arguments,
                                       request_id="cancel-2", confirmation_id=confirmation_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.body((status, {}, body, ""))["error"]["code"], "CONFIRMATION_USED")

        status, _, body, _ = self.tool(self.customer, "customer.prepare_order_cancel",
                                       {"order_id": order_id, "reason": "再次取消"})
        again_id = json.loads(body)["data"]["confirmation"]["id"]
        self.assertFalse(json.loads(body)["data"]["preview"]["cancellable"])
        self.call(self.customer, f"/api/agent/confirmations/{again_id}/approve", "POST")
        status, _, body, _ = self.tool(self.customer, "customer.commit_order_cancel",
                                       {"order_id": order_id, "reason": "再次取消"},
                                       request_id="cancel-3", confirmation_id=again_id)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["data"]["changed"])
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 24)

    def test_confirmation_persistence_expiry_and_release(self):
        store = agent_confirmations.ConfirmationStore(self.db)
        arguments = {"product_id": "apple", "quantity": 1}
        token = store.create("customer.commit_buy_now", "customer", "buyer-x",
                             arguments, {"items": [], "total_cents": 590})
        store2 = agent_confirmations.ConfirmationStore(self.db)
        self.assertEqual(store2.get(token["id"], "customer", "buyer-x")["state"], "pending")
        store2.approve(token["id"], "customer", "buyer-x")
        claimed = store2.validate_for_commit(token["id"], "customer", "buyer-x",
                                             "customer.commit_buy_now", arguments, "persist-1")
        self.assertEqual(claimed["state"], "consumed")
        replay = store2.validate_for_commit(token["id"], "customer", "buyer-x",
                                            "customer.commit_buy_now", arguments, "persist-1")
        self.assertEqual(replay["state"], "consumed")
        self.assertEqual(agent_confirmations.ConfirmationStore(self.db).get(
            token["id"], "customer", "buyer-x")["state"], "consumed")
        store2.release(token["id"], "customer", "buyer-x", "persist-1")
        self.assertEqual(store2.get(token["id"], "customer", "buyer-x")["state"], "approved")
        with self.assertRaises(agent_confirmations.ConfirmationError) as raised:
            store2.get(token["id"], "customer", "other-buyer")
        self.assertEqual(raised.exception.code, "CONFIRMATION_NOT_FOUND")

        short = store2.create("customer.commit_buy_now", "customer", "buyer-x",
                              arguments, {"items": []}, ttl_seconds=0.01)
        time.sleep(0.02)
        self.assertEqual(store2.get(short["id"], "customer", "buyer-x")["state"], "expired")
        with self.assertRaises(agent_confirmations.ConfirmationError) as raised:
            store2.approve(short["id"], "customer", "buyer-x")
        self.assertEqual(raised.exception.code, "CONFIRMATION_EXPIRED")

    def test_concurrent_commit_claims_once(self):
        context = ToolContext("customer", "concurrent-buyer")
        arguments = {"product_id": "apple", "quantity": 1}
        prepared = agent_tools.registry.invoke("customer.prepare_buy_now", arguments, context)
        confirmation_id = prepared["confirmation"]["id"]
        agent_confirmations.store.approve(confirmation_id, "customer", "concurrent-buyer")

        def commit(index):
            try:
                result = agent_tools.registry.invoke(
                    "customer.commit_buy_now", arguments, context,
                    request_id=f"concurrent-{index}", confirmation_id=confirmation_id)
                return "ok", result["order"]["id"]
            except ToolError as error:
                return error.code, None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, range(2)))
        self.assertEqual(sorted(code for code, _ in results), ["CONFIRMATION_USED", "ok"])
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 23)
        with closing(connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
