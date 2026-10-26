"""Run against an isolated, disposable shop_test PostgreSQL database only."""
import base64
import os
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import auth
import agent_confirmations
import tests.test_business as business_cases
import tests.test_shop as shop_cases
from agent_demo import BuyCancelPlanner
from agent_runtime import AgentClient, Runtime
from db import (connect, initialize, create_product, product_image, snapshot, export_records,
                edit_product, integrity_report, move_stock, get_order, get_product, InventoryError)
from postgres_schema import initialize as initialize_schema
from postgres_maintenance import TABLES, EPHEMERAL_TABLES
from server import ShopHandler


@unittest.skipUnless(os.environ.get('SHOP_TEST_POSTGRES') == '1', 'set SHOP_TEST_POSTGRES=1 for the dedicated PostgreSQL test DB')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        auth.reset_sessions()
        self.addCleanup(auth.reset_sessions)
        self.environment = patch.dict(os.environ, {'SHOP_DB_BACKEND': 'postgresql', 'PGDATABASE': 'shop_test'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.db = self.db_path = None
        initialize_schema(seed=False)
        with closing(connect()) as connection:
            self.assertEqual(connection.execute('SELECT current_database()').fetchone()[0], 'shop_test')
            connection.execute('TRUNCATE ' + ','.join(TABLES + EPHEMERAL_TABLES) + ' RESTART IDENTITY CASCADE')
        initialize()

    def stock(self, product_id='apple'):
        with closing(connect()) as connection:
            return connection.execute('SELECT stock FROM products WHERE id=?', (product_id,)).fetchone()[0]

    product_payload = shop_cases.DatabaseTests.product_payload
    test_multi_item_order_rollback_and_retry = business_cases.BusinessTests.test_multi_item_order_rollback_and_retry
    test_price_edit_version_and_order_snapshots = business_cases.BusinessTests.test_price_edit_version_and_order_snapshots
    test_concurrent_cancel_once_and_legacy_purchase_receipt = business_cases.BusinessTests.test_concurrent_cancel_once_and_legacy_purchase_receipt
    test_adjust_reason_idempotency_and_bounds = business_cases.BusinessTests.test_adjust_reason_idempotency_and_bounds
    test_unique_sku_import_100_atomic_and_search = business_cases.BusinessTests.test_unique_sku_import_100_atomic_and_search
    test_order_duplicate_items_and_request_conflict = business_cases.BusinessTests.test_order_duplicate_items_and_request_conflict
    test_concurrent_multi_item_orders_never_partially_sell = business_cases.BusinessTests.test_concurrent_multi_item_orders_never_partially_sell
    test_seed_and_purchase_restock = shop_cases.DatabaseTests.test_seed_and_purchase_restock
    test_idempotency_and_request_conflict = shop_cases.DatabaseTests.test_idempotency_and_request_conflict
    test_concurrent_purchases_cannot_oversell = shop_cases.DatabaseTests.test_concurrent_purchases_cannot_oversell
    test_custom_listing_purchase_and_pagination = shop_cases.DatabaseTests.test_custom_listing_purchase_and_pagination
    test_concurrent_listing_is_idempotent = shop_cases.DatabaseTests.test_concurrent_listing_is_idempotent

    def test_image_export_and_literal_search(self):
        image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jW1kAAAAASUVORK5CYII=')
        product = create_product(self.product_payload(name="百分号%商品_", image_data='data:image/png;base64,' + base64.b64encode(image).decode()))['product']
        self.assertEqual(product_image(product['id'])[0], image)
        self.assertEqual(snapshot(search='%商品_')['product_count'], 1)
        self.assertEqual(snapshot(search="%' OR 1=1 --")['product_count'], 0)
        headers, values = export_records('products')
        self.assertIn(product['name'], [row[headers.index('name')] for row in values])
        edit_product(product['id'], {'image_data': None, 'request_id': 'remove-image', 'version': product['version']})
        self.assertIsNone(snapshot(search='%商品_')['products'][0]['image_url'])
        self.assertTrue(integrity_report()['ok'])

    def test_business_role_has_no_admin_privileges(self):
        with closing(connect()) as connection:
            role = connection.execute('SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
            self.assertEqual(list(role.values()), [False, False, False])

    def test_reconnect_persists_and_diagnostics_detect_corruption(self):
        move_stock('purchase', 'apple', 2, 'persist')
        self.assertEqual(self.stock(), 22)
        with closing(connect()) as connection:
            connection.execute("UPDATE inventory_entries SET stock_after=stock_after+1 WHERE kind='opening' AND product_id='apple'")
        report = integrity_report()
        self.assertFalse(report['ok'])
        self.assertTrue(report['ledger_chain_mismatches'])

    def test_snapshot_filters_and_product_detail_pg(self):
        self.assertEqual(snapshot(min_price_cents=500)['product_count'], 1)
        self.assertEqual(get_product('apple')['price_cents'], 590)
        with self.assertRaises(InventoryError):
            get_product('missing')
        zero = create_product({'name': '缺货菜', 'category': '蔬菜', 'price_cents': 200, 'stock': 0,
                               'sku': 'PG-ZERO', 'request_id': 'pg-zero'})['product']
        self.assertEqual(snapshot(in_stock=False)['products'][0]['id'], zero['id'])
        self.assertEqual([product['id'] for product in snapshot(in_stock=True, sort='price_asc')['products']],
                         ['banana', 'apple'])
        with self.assertRaises(InventoryError):
            snapshot(min_price_cents=600, max_price_cents=500)

    def test_agent_confirmation_store_pg(self):
        arguments = {'product_id': 'apple', 'quantity': 1}
        token = agent_confirmations.ConfirmationStore().create(
            'customer.commit_buy_now', 'customer', 'pg-buyer', arguments,
            {'items': [], 'total_cents': 590})
        store = agent_confirmations.ConfirmationStore()
        self.assertEqual(store.get(token['id'], 'customer', 'pg-buyer')['state'], 'pending')
        store.approve(token['id'], 'customer', 'pg-buyer')
        claimed = store.validate_for_commit(token['id'], 'customer', 'pg-buyer',
                                            'customer.commit_buy_now', arguments, 'pg-req')
        self.assertEqual(claimed['state'], 'consumed')
        self.assertEqual(agent_confirmations.ConfirmationStore().get(
            token['id'], 'customer', 'pg-buyer')['state'], 'consumed')
        store.release(token['id'], 'customer', 'pg-buyer', 'pg-req')
        self.assertEqual(store.get(token['id'], 'customer', 'pg-buyer')['state'], 'approved')
        with self.assertRaises(agent_confirmations.ConfirmationError) as raised:
            store.get(token['id'], 'customer', 'other-buyer')
        self.assertEqual(raised.exception.code, 'CONFIRMATION_NOT_FOUND')

    def test_agent_buy_cancel_http_pg(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), ShopHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = AgentClient(f'http://127.0.0.1:{server.server_port}')
            client.login('customer')
            planner = BuyCancelPlanner(query='苹果', quantity=1, reason='PG 演示取消')
            report = Runtime(client, planner, lambda action, confirmation: True,
                             run_id='pg-demo').run()
            self.assertTrue(report.ok, report.error)
            self.assertEqual(get_order(planner.order_id)['status'], 'cancelled')
            self.assertEqual(self.stock(), 24)
            with closing(connect()) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
