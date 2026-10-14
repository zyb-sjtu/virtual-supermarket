"""Run against an isolated, disposable shop_test PostgreSQL database only."""
import base64
import os
import unittest
from contextlib import closing
from unittest.mock import patch

import tests.test_business as business_cases
import tests.test_shop as shop_cases
from db import (connect, initialize, create_product, product_image, snapshot, export_records,
                edit_product, integrity_report, move_stock)
from postgres_schema import initialize as initialize_schema
from postgres_maintenance import TABLES


@unittest.skipUnless(os.environ.get('SHOP_TEST_POSTGRES') == '1', 'set SHOP_TEST_POSTGRES=1 for the dedicated PostgreSQL test DB')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'SHOP_DB_BACKEND': 'postgresql', 'PGDATABASE': 'shop_test'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.db = self.db_path = None
        initialize_schema(seed=False)
        with closing(connect()) as connection:
            self.assertEqual(connection.execute('SELECT current_database()').fetchone()[0], 'shop_test')
            connection.execute('TRUNCATE ' + ','.join(TABLES) + ' RESTART IDENTITY CASCADE')
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
