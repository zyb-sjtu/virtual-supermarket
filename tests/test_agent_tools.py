import json
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, build_opener, HTTPCookieProcessor

import auth
import agent_confirmations
from db import initialize, snapshot
from server import ShopHandler
from tests.session_helpers import login


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        auth.reset_sessions()
        agent_confirmations.store.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "agent.sqlite3"
        initialize(self.db)
        self.env = patch.dict("os.environ", {"SHOP_DB_BACKEND": "sqlite", "SHOP_DB_PATH": str(self.db)})
        self.env.start()
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
        self.env.stop()
        self.temp.cleanup()
        auth.reset_sessions()
        agent_confirmations.store.reset()

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
            "customer.list_categories", "customer.search_products",
            "customer.get_my_orders", "customer.get_my_order",
            "customer.prepare_order", "customer.commit_order",
        })

        status, _, body, _ = self.call(self.merchant, "/api/agent/tools")
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in self.body((status, {}, body, ""))["data"]["tools"]}
        self.assertEqual(names, {
            "merchant.list_categories", "merchant.list_products", "merchant.list_orders",
            "merchant.get_order", "merchant.get_inventory_history",
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


if __name__ == "__main__":
    unittest.main()
