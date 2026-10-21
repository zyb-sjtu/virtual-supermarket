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
from db import initialize
from server import ShopHandler
from tests.session_helpers import login


class PermissionTests(unittest.TestCase):
    def setUp(self):
        auth.reset_sessions()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "permissions.sqlite3"
        initialize(self.db)
        self.env = patch.dict("os.environ", {"SHOP_DB_BACKEND": "sqlite", "SHOP_DB_PATH": str(self.db)})
        self.env.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ShopHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.anonymous = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
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

    def body(self, result):
        return json.loads(result[2].decode("utf-8"))

    def test_anonymous_access_and_role_pages(self):
        status, _, _, _ = self.call(self.anonymous, "/api/state")
        self.assertEqual(status, 401)
        status, _, page, _ = self.call(self.anonymous, "/login")
        self.assertEqual(status, 200)
        self.assertIn("选择身份", page.decode("utf-8"))
        status, _, page, final_url = self.call(self.anonymous, "/merchant")
        self.assertEqual(status, 200)
        self.assertTrue(final_url.endswith("/login"))

        merchant, _ = login(self.base, "merchant")
        customer, _ = login(self.base, "customer")
        status, _, page, final_url = self.call(merchant, "/merchant")
        self.assertEqual(status, 200)
        self.assertTrue(final_url.endswith("/merchant"))
        self.assertIn("商品与库存", page.decode("utf-8"))
        status, _, _, final_url = self.call(merchant, "/customer")
        self.assertEqual(status, 200)
        self.assertTrue(final_url.endswith("/merchant"))
        status, _, page, final_url = self.call(customer, "/customer")
        self.assertEqual(status, 200)
        self.assertTrue(final_url.endswith("/customer"))
        self.assertIn("在售商品", page.decode("utf-8"))
        status, _, _, final_url = self.call(customer, "/merchant")
        self.assertEqual(status, 200)
        self.assertTrue(final_url.endswith("/customer"))

    def test_role_api_matrix(self):
        merchant, _ = login(self.base, "merchant")
        customer, _ = login(self.base, "customer")

        status, _, body, _ = self.call(customer, "/api/state")
        self.assertEqual(status, 200)
        state = self.body((status, {}, body, ""))
        self.assertIn("products", state)
        self.assertNotIn("sales_cents", state)
        self.assertNotIn("movements", state)
        status, _, body, _ = self.call(merchant, "/api/state")
        self.assertEqual(status, 200)
        state = self.body((status, {}, body, ""))
        self.assertIn("sales_cents", state)
        self.assertIn("movements", state)

        product = {"name": "权限测试商品", "price_cents": 100, "stock": 1, "request_id": "perm-product"}
        status, _, _, _ = self.call(customer, "/api/products", "POST", product)
        self.assertEqual(status, 403)
        status, _, _, _ = self.call(merchant, "/api/products", "POST", product)
        self.assertEqual(status, 201)

        purchase = {"action": "purchase", "product_id": "apple", "quantity": 1, "request_id": "perm-customer-buy"}
        status, _, _, _ = self.call(merchant, "/api/movements", "POST", purchase)
        self.assertEqual(status, 403)
        status, _, _, _ = self.call(customer, "/api/movements", "POST", purchase)
        self.assertEqual(status, 201)

        restock = {"action": "restock", "product_id": "apple", "quantity": 1, "request_id": "perm-restock"}
        status, _, _, _ = self.call(customer, "/api/movements", "POST", restock)
        self.assertEqual(status, 403)
        status, _, _, _ = self.call(merchant, "/api/movements", "POST", restock)
        self.assertEqual(status, 201)

        order = {"request_id": "perm-order", "items": [{"product_id": "apple", "quantity": 1}]}
        status, _, _, _ = self.call(merchant, "/api/orders", "POST", order)
        self.assertEqual(status, 403)
        status, _, _, _ = self.call(customer, "/api/orders", "POST", order)
        self.assertEqual(status, 201)

        for path in ("/api/inventory", "/api/audit", "/api/export?kind=products"):
            status, _, _, _ = self.call(customer, path)
            self.assertEqual(status, 403, path)
            status, _, _, _ = self.call(merchant, path)
            self.assertEqual(status, 200, path)

    def test_customer_order_scope_and_cancel(self):
        merchant, _ = login(self.base, "merchant")
        customer_a, _ = login(self.base, "customer")
        customer_b, _ = login(self.base, "customer")
        purchase = {"action": "purchase", "product_id": "apple", "quantity": 1, "request_id": "scope-buy"}
        status, _, body, _ = self.call(customer_a, "/api/movements", "POST", purchase)
        self.assertEqual(status, 201)
        order_id = self.body((status, {}, body, ""))["order_id"]

        status, _, body, _ = self.call(customer_a, "/api/orders")
        self.assertEqual(status, 200)
        self.assertEqual(self.body((status, {}, body, ""))["count"], 1)
        status, _, body, _ = self.call(customer_b, "/api/orders")
        self.assertEqual(status, 200)
        self.assertEqual(self.body((status, {}, body, ""))["count"], 0)
        status, _, _, _ = self.call(customer_b, f"/api/orders/{order_id}")
        self.assertEqual(status, 404)
        status, _, _, _ = self.call(customer_b, f"/api/orders/{order_id}/cancel", "POST",
                                     {"reason": "越权取消", "request_id": "scope-cancel-other"})
        self.assertEqual(status, 404)
        status, _, _, _ = self.call(customer_a, f"/api/orders/{order_id}")
        self.assertEqual(status, 200)
        status, _, _, _ = self.call(customer_a, f"/api/orders/{order_id}/cancel", "POST",
                                     {"reason": "本人取消", "request_id": "scope-cancel-own"})
        self.assertEqual(status, 201)
        status, _, body, _ = self.call(merchant, "/api/orders")
        self.assertEqual(status, 200)
        self.assertEqual(self.body((status, {}, body, ""))["count"], 1)

    def test_logout_and_invalid_role(self):
        customer, _ = login(self.base, "customer")
        status, _, _, _ = self.call(customer, "/api/logout", "POST")
        self.assertEqual(status, 200)
        status, _, _, _ = self.call(customer, "/api/state")
        self.assertEqual(status, 401)
        status, _, _, _ = self.call(self.anonymous, "/api/session", "POST", {"role": "admin"})
        self.assertEqual(status, 400)
        status, _, body, _ = self.call(self.anonymous, "/api/session")
        self.assertEqual(status, 200)
        self.assertFalse(self.body((status, {}, body, ""))["authenticated"])


if __name__ == "__main__":
    unittest.main()
