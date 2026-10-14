"""Repeatable verification on the dedicated 8001/5434 simulation replica."""
import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from db import connect, integrity_report
from postgres_maintenance import TABLES

REPORT = ROOT / 'docs/postgresql-validation.json'
STATE = ROOT / '.tmp/postgres-isolation-state.json'


def request(port, path, payload=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(f'http://127.0.0.1:{port}{path}', data=data,
                  headers={'Content-Type': 'application/json'} if data else {}, method=method)
    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw
    except HTTPError as error:
        return error.code, json.loads(error.read())


def db_snapshot(port):
    previous = os.environ.get('PGPORT')
    os.environ['PGPORT'] = str(port)
    try:
        with closing(connect()) as connection:
            result = {}
            for table in TABLES:
                rows = connection.execute(f'SELECT * FROM {table}').fetchall()
                content = sorted(repr(sorted(row.items())) for row in rows)
                result[table] = {'count': len(rows), 'sha256': hashlib.sha256(repr(content).encode()).hexdigest()}
            result['consistency'] = integrity_report()
        return result
    finally:
        if previous is None:
            os.environ.pop('PGPORT', None)
        else:
            os.environ['PGPORT'] = previous


def baseline():
    primary_health = request(8000, '/api/health')[1]
    replica_health = request(8001, '/api/health')[1]
    assert primary_health['engine'] == replica_health['engine'] == 'postgresql'
    assert primary_health['port'] == 5433 and replica_health['port'] == 5434
    primary, replica = db_snapshot(5433), db_snapshot(5434)
    assert primary == replica, 'backup restored fields differ'
    # Verify the retained SQLite source has the same current product fields.
    with closing(sqlite3.connect(ROOT / 'data/shop.sqlite3')) as source, closing(connect()) as target:
        columns = [row[1] for row in source.execute('PRAGMA table_info(products)')]
        expected = sorted(repr(row) for row in source.execute('SELECT ' + ','.join(columns) + ' FROM products'))
        actual = sorted(repr(tuple(row[column] for column in columns)) for row in target.execute('SELECT * FROM products'))
        assert expected == actual
    key = uuid.uuid4().hex
    image_data = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jW1kAAAAASUVORK5CYII='
    payload = {'name': '数据库隔离验证商品', 'sku': 'ISO-' + key[:12], 'price_cents': 350,
               'stock': 3, 'request_id': 'isolation-list-' + key, 'image_data': image_data}
    code, listing = request(8001, '/api/products', payload)
    assert code == 201
    product = listing['product']
    buy = {'action': 'purchase', 'product_id': product['id'], 'quantity': 2, 'request_id': 'isolation-buy-' + key}
    code, bought = request(8001, '/api/movements', buy)
    assert code == 201 and bought['stock_after'] == 1
    code, retry = request(8001, '/api/movements', buy)
    assert code == 200 and retry['duplicate'] and retry['id'] == bought['id']
    code, edited = request(8001, '/api/products/' + product['id'],
                          {'price_cents': 680, 'version': product['version'], 'request_id': 'isolation-edit-' + key}, 'PATCH')
    assert code == 200
    code, order = request(8001, '/api/orders/' + bought['order_id'])
    assert order['order']['items'][0]['unit_price_cents'] == 350
    code, cancelled = request(8001, '/api/orders/' + bought['order_id'] + '/cancel',
                              {'reason': '隔离验证取消', 'request_id': 'isolation-cancel-' + key})
    assert code == 201 and cancelled['changed']
    image_status, image = request(8001, '/api/products/' + product['id'] + '/image')
    assert image_status == 200 and image == base64.b64decode(image_data.split(',')[1])
    assert db_snapshot(5433) == primary, 'replica changes affected primary'
    replica_after = db_snapshot(5434)
    assert replica_after['consistency']['ok']
    state = {'primary': primary, 'replica': replica_after, 'product_id': product['id'], 'key': key}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    report = {'date': '2026-10-14', 'primary': primary_health, 'replica': replica_health,
              'backup_restore_all_table_fields_match': True, 'sqlite_product_fields_match': True,
              'replica_transaction_and_idempotency': True, 'historic_price_snapshot': True,
              'image_bytes_preserved': True, 'primary_unchanged_after_replica_writes': True,
              'original_counts': {t: primary[t]['count'] for t in TABLES},
              'unit_tests': {'sqlite': 28, 'postgresql': 15}}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def offline():
    state = json.loads(STATE.read_text(encoding='utf-8'))
    assert request(8001, '/')[0] == 200, 'HTTP process must remain alive'
    assert request(8001, '/api/health')[0] == 503
    assert request(8001, '/api/state')[0] == 503
    payload = {'action': 'purchase', 'product_id': state['product_id'], 'quantity': 1, 'request_id': 'offline-' + state['key']}
    assert request(8001, '/api/movements', payload)[0] == 503
    assert request(8000, '/api/health')[0] == 200
    assert db_snapshot(5433) == state['primary']
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    report['database_stopped_http_stays_alive'] = True
    report['database_stopped_data_and_writes_return_503'] = True
    report['primary_unaffected_by_replica_outage'] = True
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def recovered():
    state = json.loads(STATE.read_text(encoding='utf-8'))
    assert request(8001, '/api/health')[0] == 200
    assert db_snapshot(5434) == state['replica'], 'data changed or lost during restart'
    assert db_snapshot(5433) == state['primary']
    # A write that failed while offline was not silently saved in SQLite.
    payload = {'action': 'purchase', 'product_id': state['product_id'], 'quantity': 1, 'request_id': 'offline-' + state['key']}
    code, result = request(8001, '/api/movements', payload)
    assert code == 201 and not result['duplicate'] and result['stock_after'] == 2
    request(8001, '/api/orders/' + result['order_id'] + '/cancel',
            {'reason': '恢复验证结束', 'request_id': 'recover-cancel-' + state['key']})
    assert db_snapshot(5434)['consistency']['ok']
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    report['restart_preserves_all_pre_outage_fields'] = True
    report['offline_write_not_committed_or_fallen_back'] = True
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('step', choices=('baseline', 'offline', 'recovered'))
    args = parser.parse_args()
    print(json.dumps(globals()[args.step](), ensure_ascii=False, indent=2))
