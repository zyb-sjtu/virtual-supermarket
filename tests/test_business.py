import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from db import (InventoryError, initialize, snapshot, create_product, move_stock, edit_product, import_products,
                create_order, cancel_order, list_orders, inventory_history, audit_history, integrity_report, adjust_inventory)
from maintenance import backup_database, restore_database
from runtime import DatabaseLease
from server import ShopHandler
from tests.session_helpers import login


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "shop.sqlite3"
        initialize(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def stock(self, product_id="apple"):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute("SELECT stock FROM products WHERE id = ?", (product_id,)).fetchone()[0]

    def test_multi_item_order_rollback_and_retry(self):
        invalid = {"request_id": "order-1", "items": [{"product_id":"apple", "quantity":2}, {"product_id":"banana", "quantity":19}]}
        with self.assertRaises(InventoryError):
            create_order(invalid, self.db)
        self.assertEqual(list_orders(db_path=self.db)["count"], 0)
        self.assertEqual(self.stock(), 24)
        valid = {**invalid, "items": [{"product_id":"banana", "quantity":3}, {"product_id":"apple", "quantity":2}]}
        result = create_order(valid, self.db)
        self.assertEqual(result["order"]["total_cents"], 2650)
        repeat = create_order({**valid, "items": valid["items"][::-1]}, self.db)
        self.assertTrue(repeat["duplicate"])
        self.assertEqual(repeat["order"]["id"], result["order"]["id"])
        self.assertEqual(self.stock(), 22)
        self.assertTrue(integrity_report(self.db)["ok"])

    def test_price_edit_version_and_order_snapshots(self):
        product = snapshot(self.db)["products"][0]
        order = create_order({"request_id":"price-order", "items":[{"product_id":"apple","quantity":2}]}, self.db)["order"]
        payload = {"name":"新名称", "price_cents":999, "zone":"A-01", "version":product["version"], "request_id":"edit-1"}
        edited = edit_product("apple", payload, self.db)
        self.assertEqual(edited["product"]["version"], 2)
        self.assertTrue(edit_product("apple", payload, self.db)["duplicate"])
        with self.assertRaises(InventoryError) as raised:
            edit_product("apple", {"price_cents":299, "version":1, "request_id":"stale-edit"}, self.db)
        self.assertEqual(raised.exception.status, 409)
        stored = list_orders(db_path=self.db)["orders"][0]
        self.assertEqual(stored["items"][0]["product_name"], "苹果")
        self.assertEqual(stored["items"][0]["unit_price_cents"], 590)
        cancel_order(order["id"], {"reason":"顾客退单", "request_id":"cancel-after-price"}, self.db)
        self.assertEqual(snapshot(self.db)["sales_cents"], 0)
        self.assertEqual(self.stock(), 24)
        self.assertEqual(audit_history(db_path=self.db)["count"], 3)
        self.assertTrue(integrity_report(self.db)["ok"])

    def test_concurrent_cancel_once_and_legacy_purchase_receipt(self):
        receipt = move_stock("purchase", "apple", 4, "single-buy", self.db)
        self.assertTrue(receipt["order_id"])
        def cancel(i):
            return cancel_order(receipt["order_id"], {"reason":"重复退单请求", "request_id":f"cancel-{i}"}, self.db)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(cancel, range(20)))
        self.assertEqual(sum(r["changed"] for r in results), 1)
        self.assertEqual(self.stock(), 24)
        history = inventory_history("apple", db_path=self.db)["entries"]
        self.assertEqual(sum(r["kind"] == "cancel" for r in history), 1)
        self.assertTrue(move_stock("purchase", "apple", 4, "single-buy", self.db)["duplicate"])
        self.assertTrue(integrity_report(self.db)["ok"])

    def test_adjust_reason_idempotency_and_bounds(self):
        payload = {"product_id":"apple", "delta":-3, "reason":"破损报损", "request_id":"loss-1"}
        result = adjust_inventory(payload, self.db)
        self.assertEqual(result["stock_after"], 21)
        self.assertTrue(adjust_inventory(payload, self.db)["duplicate"])
        for changes in ({"delta":0}, {"reason":""}, {"delta":-100}, {"delta":True}):
            with self.assertRaises(InventoryError):
                adjust_inventory({**payload, **changes, "request_id":"bad-adjust"}, self.db)
        self.assertEqual(self.stock(), 21)
        self.assertEqual(inventory_history("apple", db_path=self.db)["entries"][0]["reason"], "破损报损")
        self.assertTrue(integrity_report(self.db)["ok"])

    def test_unique_sku_import_100_atomic_and_search(self):
        goods = [{"sku":f"TEST-{i:03d}", "name":f"样品{i}", "price_cents":100+i, "stock":i%4, "category":"蔬菜"} for i in range(100)]
        result = import_products({"products":goods, "request_id":"import-100"}, self.db)
        self.assertEqual(result["count"], 100)
        self.assertTrue(import_products({"products":goods, "request_id":"import-100"}, self.db)["duplicate"])
        with self.assertRaises(InventoryError):
            import_products({"products":[{**goods[0], "sku":"NEW-001"}, goods[1]], "request_id":"bad-import"}, self.db)
        self.assertEqual(snapshot(self.db)["product_count"], 102)
        self.assertEqual(snapshot(self.db, search="NEW-001")["product_count"], 0)
        self.assertEqual(snapshot(self.db, search="test-042", category="蔬菜")["product_count"], 1)
        with self.assertRaises(InventoryError) as raised:
            create_product({"sku":"test-042", "name":"重复", "price_cents":100, "request_id":"sku-repeat"}, self.db)
        self.assertEqual(raised.exception.status, 409)
        self.assertTrue(integrity_report(self.db)["ok"])

    def test_migrate_historical_order_and_ledger_once(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as c:
            c.execute("CREATE TABLE products(id TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT NULL)")
            c.execute("INSERT INTO products VALUES ('apple','苹果',590,20)")
            c.execute("""CREATE TABLE movements(id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, action TEXT, product_id TEXT,
                quantity INTEGER, unit_price_cents INTEGER, stock_after INTEGER, created_at TEXT)""")
            c.execute("INSERT INTO movements VALUES (1,'old-buy','purchase','apple',4,590,20,'2026-09-29T00:00:00Z')")
            c.commit()
        initialize(legacy)
        initialize(legacy)
        self.assertEqual(list_orders(db_path=legacy)["count"], 1)
        history = inventory_history("apple", db_path=legacy)
        self.assertEqual(history["count"], 2)
        self.assertEqual(sum(e["delta"] for e in history["entries"]), 20)
        cancel_order("legacy-1", {"reason":"历史退单", "request_id":"old-cancel"}, legacy)
        self.assertEqual(snapshot(legacy)["products"][0]["stock"], 24)
        self.assertTrue(integrity_report(legacy)["ok"])

    def test_backup_restore_integrity_and_running_lease(self):
        backup = backup_database(self.db)
        move_stock("purchase", "apple", 2, "before-restore", self.db)
        with DatabaseLease(self.db):
            with self.assertRaises(RuntimeError):
                restore_database(backup, self.db)
            online_backup = backup_database(self.db)
        self.assertTrue(online_backup.exists())
        safety = restore_database(backup, self.db)
        self.assertTrue(safety.exists())
        self.assertEqual(self.stock(), 24)
        self.assertTrue(integrity_report(self.db)["ok"])
        # A physically valid but inconsistent backup must not replace data.
        corrupt = backup_database(self.db)
        with closing(sqlite3.connect(corrupt)) as c:
            c.execute("UPDATE products SET stock = stock + 1 WHERE id = 'apple'")
            c.commit()
        with self.assertRaises(ValueError):
            restore_database(corrupt, self.db)
        self.assertEqual(self.stock(), 24)

    def test_order_duplicate_items_and_request_conflict(self):
        with self.assertRaises(InventoryError):
            create_order({"request_id":"duplicated", "items":[{"product_id":"apple","quantity":1}]*2}, self.db)
        create_order({"request_id":"one-order", "items":[{"product_id":"apple","quantity":1}]}, self.db)
        with self.assertRaises(InventoryError) as raised:
            create_order({"request_id":"one-order", "items":[{"product_id":"apple","quantity":2}]}, self.db)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.stock(), 23)

    def test_concurrent_multi_item_orders_never_partially_sell(self):
        def buy(i):
            try:
                create_order({"request_id":f"multi-{i}", "items":[{"product_id":"apple","quantity":2},
                    {"product_id":"banana","quantity":2}]}, self.db)
                return True
            except InventoryError as error:
                self.assertEqual(error.status,409)
                return False
        with ThreadPoolExecutor(max_workers=16) as pool:
            sold = sum(pool.map(buy,range(20)))
        self.assertEqual(sold,9)
        self.assertEqual(self.stock(),6)
        self.assertEqual(self.stock("banana"),0)
        self.assertTrue(integrity_report(self.db)["ok"])

    def test_integrity_checks_historical_chain_and_cancel_links(self):
        order = create_order({"request_id":"corruption-case", "items":[{"product_id":"apple","quantity":1}]}, self.db)["order"]
        cancel_order(order["id"], {"reason":"测试退单", "request_id":"check-cancel"}, self.db)
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("UPDATE inventory_entries SET stock_after = stock_after + 1 WHERE kind = 'opening' AND product_id = 'apple'")
            c.execute("UPDATE inventory_entries SET order_id = NULL WHERE kind = 'cancel'")
            c.commit()
        report = integrity_report(self.db)
        self.assertFalse(report["ok"])
        self.assertTrue(report["ledger_chain_mismatches"])
        self.assertTrue(report["refund_mismatches"])

    def test_original_listing_request_still_replays_after_migration(self):
        import hashlib
        payload = {"name":"原始商品", "price_cents":350, "stock":8, "request_id":"old-listing"}
        product = create_product(payload,self.db)["product"]
        old_hash = hashlib.sha256(json.dumps(["原始商品","其他","件","",350,8,None],ensure_ascii=False).encode()).hexdigest()
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("UPDATE product_creations SET fingerprint = ? WHERE request_id = ?",(old_hash,"old-listing"))
            c.commit()
        repeated = create_product(payload,self.db)
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["product"]["id"],product["id"])


class BusinessHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "http.sqlite3"
        initialize(self.db)
        self.env = patch.dict("os.environ", {"SHOP_DB_BACKEND": "sqlite", "SHOP_DB_PATH":str(self.db)})
        self.env.start()
        self.server = ThreadingHTTPServer(("127.0.0.1",0), ShopHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.merchant, _ = login(self.base, "merchant")
        self.customer, _ = login(self.base, "customer")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.env.stop()
        self.temp.cleanup()

    def send(self, path, data, method="POST", opener=None):
        request = Request(self.base+path, json.dumps(data).encode(), headers={"Content-Type":"application/json"}, method=method)
        with (opener or self.merchant).open(request) as response:
            return json.load(response)

    def test_new_http_workflows_and_exports(self):
        order = self.send("/api/orders", {"request_id":"http-order", "items":[{"product_id":"apple","quantity":2}]}, opener=self.customer)["order"]
        self.send("/api/products/apple", {"price_cents":680, "version":1, "request_id":"http-edit"}, "PATCH")
        self.send(f"/api/orders/{order['id']}/cancel", {"reason":"测试取消", "request_id":"http-cancel"}, opener=self.customer)
        self.send("/api/inventory/adjust", {"product_id":"apple","delta":-1,"reason":"盘点差异","request_id":"http-adjust"})
        for path, field in (("/api/orders","orders"),("/api/inventory?product_id=apple","entries"),("/api/audit","events")):
            with self.merchant.open(self.base+path) as response:
                self.assertTrue(json.load(response)[field])
        for kind in ("products","orders","inventory","audit"):
            with self.merchant.open(self.base+"/api/export?kind="+kind) as response:
                rows = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
                self.assertGreater(len(rows),1)
        self.assertTrue(integrity_report(self.db)["ok"])

    def test_csv_formula_text_and_bad_payloads(self):
        self.send("/api/products", {"name":"=1+2", "price_cents":100,"request_id":"formula"})
        with self.merchant.open(self.base+"/api/export?kind=products") as response:
            text = response.read().decode("utf-8-sig")
            self.assertIn("'=1+2",text)
        with self.assertRaises(HTTPError) as raised:
            self.send("/api/orders", {"items":"not a list","request_id":"bad"}, opener=self.customer)
        self.assertEqual(raised.exception.code,400)
        with self.assertRaises(HTTPError) as raised:
            self.merchant.open(self.base+"/api/inventory?page=0")
        self.assertEqual(raised.exception.code,400)


if __name__ == "__main__":
    unittest.main()
