import json
import sqlite3
import base64
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from db import InventoryError, initialize, move_stock, snapshot, create_product, set_product_status
from server import ShopHandler


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "isolated.sqlite3"
        initialize(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_and_purchase_restock(self):
        state = snapshot(self.db_path)
        self.assertEqual([item["id"] for item in state["products"]], ["apple", "banana"])
        self.assertEqual(state["products"][0]["stock"], 24)
        purchased = move_stock("purchase", "apple", 3, "buy-1", self.db_path)
        self.assertEqual(purchased["stock_after"], 21)
        restored = move_stock("restock", "apple", 5, "add-1", self.db_path)
        self.assertEqual(restored["stock_after"], 26)
        self.assertEqual(snapshot(self.db_path)["sales_cents"], 1770)
        initialize(self.db_path)
        self.assertEqual(snapshot(self.db_path)["products"][0]["stock"], 26)

    def test_idempotency_and_request_conflict(self):
        first = move_stock("purchase", "banana", 2, "same-request", self.db_path)
        again = move_stock("purchase", "banana", 2, "same-request", self.db_path)
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(snapshot(self.db_path)["products"][1]["stock"], 16)
        with self.assertRaises(InventoryError) as raised:
            move_stock("restock", "banana", 2, "same-request", self.db_path)
        self.assertEqual(raised.exception.status, 409)

    def test_concurrent_purchases_cannot_oversell(self):
        def buy(number):
            try:
                move_stock("purchase", "apple", 1, f"concurrent-{number}", self.db_path)
                return True
            except InventoryError as error:
                self.assertEqual(error.status, 409)
                return False

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(buy, range(40)))
        self.assertEqual(sum(results), 24)
        state = snapshot(self.db_path)
        self.assertEqual(state["products"][0]["stock"], 0)
        self.assertEqual(state["movement_count"], 24)

    def test_reject_invalid_quantity_and_stock_shortage(self):
        for quantity in (0, -1, 1.5, True, 10000):
            with self.assertRaises(InventoryError):
                move_stock("restock", "apple", quantity, "invalid", self.db_path)
        with self.assertRaises(InventoryError) as raised:
            move_stock("purchase", "banana", 19, "too-many", self.db_path)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(snapshot(self.db_path)["movement_count"], 0)

    def product_payload(self, key="new-product", **changes):
        payload = {"name": "上海青", "category": "蔬菜", "unit": "袋", "description": "当日鲜选",
                   "price_cents": 350, "stock": 8, "request_id": key}
        payload.update(changes)
        return payload

    def test_custom_listing_purchase_and_pagination(self):
        product = create_product(self.product_payload(), self.db_path)["product"]
        self.assertEqual(product["unit"], "袋")
        self.assertEqual(move_stock("purchase", product["id"], 2, "new-buy", self.db_path)["stock_after"], 6)
        for i in range(10):
            create_product(self.product_payload(f"listing-{i}", name=f"商品{i}", stock=0), self.db_path)
        pages = [snapshot(self.db_path, page=i) for i in range(1, 4)]
        self.assertEqual([len(state["products"]) for state in pages], [6, 6, 1])
        self.assertEqual(len({p["id"] for state in pages for p in state["products"]}), 13)
        self.assertTrue(all(state["product_count"] == 13 and state["stock_total"] == 48 for state in pages))
        self.assertEqual(snapshot(self.db_path, page=999)["page"], 3)

    def test_concurrent_listing_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: create_product(self.product_payload(), self.db_path), range(10)))
        self.assertEqual(len({r["product"]["id"] for r in results}), 1)
        self.assertEqual(sum(not r["duplicate"] for r in results), 1)
        self.assertEqual(snapshot(self.db_path)["movement_count"], 1)
        with self.assertRaises(InventoryError) as raised:
            create_product(self.product_payload(price_cents=399), self.db_path)
        self.assertEqual(raised.exception.status, 409)

    def test_invalid_listing_does_not_write(self):
        for changes in ({"name": " "}, {"category": "无效分类"}, {"price_cents": True},
                        {"price_cents": 0}, {"stock": -1}, {"stock": 1.5}, {"unit": ""},
                        {"image_data": "data:image/png;base64," + base64.b64encode(b"invalid").decode()}):
            with self.assertRaises(InventoryError):
                create_product(self.product_payload(**changes), self.db_path)
        self.assertEqual(snapshot(self.db_path)["product_count"], 2)

    def test_migrate_old_database_preserves_stock(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute("CREATE TABLE products (id TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT NULL)")
            connection.execute("INSERT INTO products VALUES ('apple', '苹果', 590, 7)")
            connection.commit()
        initialize(legacy)
        initialize(legacy)
        self.assertEqual(snapshot(legacy)["products"][0]["stock"], 7)
        self.assertEqual(snapshot(legacy)["products"][0]["unit"], "件")
        create_product(self.product_payload(), legacy)
        self.assertEqual(snapshot(legacy)["product_count"], 3)

    def test_delist_relist_preserves_inventory_and_history(self):
        move_stock("purchase", "apple", 2, "before-delist", self.db_path)
        first = set_product_status("apple", False, "delist-1", self.db_path)
        self.assertEqual(first["stock_after"], 22)
        replay = set_product_status("apple", False, "delist-1", self.db_path)
        self.assertTrue(replay["duplicate"])
        active = snapshot(self.db_path)
        self.assertEqual([p["id"] for p in active["products"]], ["banana"])
        self.assertEqual(active["stock_total"], 40)
        self.assertEqual(active["sales_cents"], 1180)
        self.assertEqual(active["movement_count"], 2)
        inactive = snapshot(self.db_path, status="inactive")
        self.assertEqual(inactive["products"][0]["stock"], 22)
        for i in range(3):
            with self.assertRaises(InventoryError) as raised:
                move_stock("purchase", "apple", 1, f"blocked-{i}", self.db_path)
            self.assertEqual(raised.exception.status, 409)
        # A successful purchase retry remains idempotent even after delisting.
        self.assertTrue(move_stock("purchase", "apple", 2, "before-delist", self.db_path)["duplicate"])
        move_stock("restock", "apple", 3, "inactive-stock", self.db_path)
        set_product_status("apple", True, "relist-1", self.db_path)
        self.assertEqual(snapshot(self.db_path)["products"][0]["stock"], 25)
        initialize(self.db_path)
        self.assertEqual(snapshot(self.db_path)["active_count"], 2)
        with self.assertRaises(InventoryError) as raised:
            set_product_status("apple", True, "delist-1", self.db_path)
        self.assertEqual(raised.exception.status, 409)

    def test_delist_last_page_and_empty_catalog(self):
        new_ids = [create_product(self.product_payload(f"page-product-{i}"), self.db_path)["product"]["id"] for i in range(5)]
        self.assertEqual(snapshot(self.db_path, page=2)["product_count"], 7)
        set_product_status(new_ids[-1], False, "last-page-down", self.db_path)
        self.assertEqual(snapshot(self.db_path, page=2)["page"], 1)
        for i, product_id in enumerate(["apple", "banana"] + new_ids[:-1]):
            set_product_status(product_id, False, f"down-{i}", self.db_path)
        empty = snapshot(self.db_path)
        self.assertEqual(empty["products"], [])
        self.assertEqual(empty["pages"], 1)
        self.assertEqual(snapshot(self.db_path, status="inactive")["pages"], 2)
        initialize(self.db_path)
        self.assertEqual(snapshot(self.db_path)["active_count"], 0)

    def test_concurrent_delist_and_purchase_are_serialized(self):
        def operation(i):
            if i == 0:
                set_product_status("apple", False, "concurrent-down", self.db_path)
                return 0
            try:
                move_stock("purchase", "apple", 1, f"race-buy-{i}", self.db_path)
                return 1
            except InventoryError as error:
                self.assertEqual(error.status, 409)
                return 0
        with ThreadPoolExecutor(max_workers=12) as pool:
            purchased = sum(pool.map(operation, range(12)))
        product = snapshot(self.db_path, status="inactive")["products"][0]
        self.assertEqual(product["stock"], 24 - purchased)
        self.assertEqual(product["active"], 0)
        with self.assertRaises(InventoryError):
            move_stock("purchase", "apple", 1, "race-after-down", self.db_path)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "http.sqlite3"
        initialize(self.db_path)
        self.env_patch = patch.dict("os.environ", {"SHOP_DB_BACKEND": "sqlite", "SHOP_DB_PATH": str(self.db_path)})
        self.env_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ShopHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.env_patch.stop()
        self.temp.cleanup()

    def test_http_purchase_and_static_page(self):
        with urlopen(self.base + "/") as response:
            self.assertIn("鲜邻", response.read().decode("utf-8"))
        body = json.dumps({"action": "purchase", "product_id": "apple", "quantity": 2,
                           "request_id": "http-buy"}).encode("utf-8")
        request = Request(self.base + "/api/movements", body,
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(json.load(response)["stock_after"], 22)
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.load(response)["duplicate"])
        with urlopen(self.base + "/api/state") as response:
            self.assertEqual(json.load(response)["products"][0]["stock"], 22)

    def test_http_invalid_input(self):
        body = json.dumps({"action": "purchase", "product_id": "apple", "quantity": 999,
                           "request_id": "shortage"}).encode("utf-8")
        request = Request(self.base + "/api/movements", body,
                          headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 409)

    def test_http_listing_image_pagination_and_replay(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jW1kAAAAASUVORK5CYII=")
        payload = {"name": "牛奶", "category": "乳品饮料", "price_cents": 680, "stock": 0,
                   "unit": "盒", "request_id": "image-product", "image_data": "data:image/png;base64," + base64.b64encode(png).decode()}
        request = Request(self.base + "/api/products", json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
            product = json.load(response)["product"]
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        with urlopen(self.base + product["image_url"]) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), png)
        for page in ("0", "abc"):
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.base + "/api/state?page=" + page)
            self.assertEqual(raised.exception.code, 400)
        with urlopen(self.base + "/api/state?page=99") as response:
            state = json.load(response)
            self.assertEqual(state["page"], 1)
            self.assertEqual(state["product_count"], 3)

    def test_http_product_status_and_default_image(self):
        request = Request(self.base + "/api/product-status", json.dumps({"product_id": "apple", "active": False,
            "request_id": "http-down"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.load(response)["duplicate"])
        with urlopen(self.base + "/api/state?status=inactive") as response:
            state = json.load(response)
            self.assertEqual(state["products"][0]["id"], "apple")
            self.assertEqual(state["inactive_count"], 1)
        with urlopen(self.base + "/product-placeholder.svg") as response:
            self.assertEqual(response.headers["Content-Type"], "image/svg+xml")
            self.assertIn(b"<svg", response.read())
        bad = Request(self.base + "/api/product-status", json.dumps({"product_id": "apple", "active": "false",
            "request_id": "http-bad-down"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(bad)
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
