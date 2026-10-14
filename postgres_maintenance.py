"""Portable PostgreSQL dumps, safe restores and one-time SQLite migration."""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from storage import ROOT, PostgresConnection, begin_write, postgres_options, DATABASE_ERRORS
from postgres_schema import initialize

TABLES = ('products', 'orders', 'movements', 'order_items', 'inventory_entries',
          'audit_events', 'api_requests', 'product_creations', 'product_status_changes')
IDENTITIES = {'products': 'sort_order', 'movements': 'id', 'order_items': 'id',
              'inventory_entries': 'id', 'audit_events': 'id', 'product_status_changes': 'id'}


def binary(name):
    directory = Path(os.environ.get('SHOP_PG_BIN', str(ROOT / '.tools/postgresql/pgsql/bin')))
    local = directory / (name + '.exe' if os.name == 'nt' else name)
    if local.is_file():
        return str(local)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f'缺少 {name}，请设置 SHOP_PG_BIN 指向 PostgreSQL bin 目录')
    return found


def pg_command(name, arguments):
    options = postgres_options()
    environment = os.environ.copy()
    environment['PGPASSFILE'] = str(options.get('passfile', ''))
    if options.get('password'):
        environment['PGPASSWORD'] = options['password']
    command = [binary(name), '-h', options['host'], '-p', str(options['port']),
               '-U', options['user'], '-d', options['dbname'], '-w'] + list(arguments)
    subprocess.run(command, env=environment, check=True, capture_output=True)


def backup_postgres(output=None):
    if output is None:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = ROOT / 'data/backups' / f'shop-pg-{stamp}.dump'
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the filename exclusively; never overwrite an existing backup.
    with output.open('xb'):
        pass
    try:
        pg_command('pg_dump', ['-Fc', '--no-owner', '--no-acl', '-f', str(output)])
    except Exception:
        output.unlink()
        raise
    return output


def restore_postgres(source):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError('备份不存在')
    # Inspect the archive before executing; accept an empty target only. This
    # supports moves without risking an existing production database.
    subprocess.run([binary('pg_restore'), '--list', str(source)], check=True, capture_output=True)
    with closing(PostgresConnection()) as connection:
        count = connection.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]
        if count:
            raise ValueError('恢复目标必须是空数据库；请使用新的独立数据库，原数据库保持不变')
    pg_command('pg_restore', ['--single-transaction', '--exit-on-error', '--no-owner', '--no-acl', str(source)])
    from db import integrity_report
    report = integrity_report()
    if not report['ok']:
        raise ValueError('恢复后业务校验失败，请不要切换服务到该目标库')
    return report


def transfer_sqlite(source):
    from db import initialize as initialize_sqlite, integrity_report
    from maintenance import backup_database
    source = Path(source).resolve()
    with tempfile.TemporaryDirectory() as directory:
        snapshot = Path(directory) / 'source.sqlite3'
        backup_database(source, snapshot)
        initialize_sqlite(snapshot)
        if not integrity_report(snapshot)['ok']:
            raise ValueError('SQLite 库存或订单不一致，迁移已取消')
        initialize(seed=False)
        counts = {}
        with closing(sqlite3.connect(snapshot)) as origin, closing(PostgresConnection()) as target:
            origin.row_factory = sqlite3.Row
            begin_write(target)
            try:
                for table in TABLES:
                    if target.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:
                        raise ValueError('PostgreSQL 目标已有业务数据，拒绝覆盖')
                for table in TABLES:
                    rows = origin.execute(f'SELECT {"rowid AS sort_order, *" if table == "products" else "*"} FROM {table} ORDER BY rowid').fetchall()
                    columns = [r[1] for r in origin.execute(f'PRAGMA table_info({table})')]
                    if table == 'products':
                        columns.insert(0, 'sort_order')
                    if rows:
                        parameters = ','.join('?' for _ in columns)
                        target.executemany(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({parameters})',
                                           [tuple(row[column] for column in columns) for row in rows])
                    counts[table] = len(rows)
                    # Exact column comparison, including binary images and
                    # request fingerprints, before committing the transfer.
                    actual = target.execute(f'SELECT {",".join(columns)} FROM {table}').fetchall()
                    expected = sorted(repr(tuple(row[column] for column in columns)) for row in rows)
                    copied = sorted(repr(tuple(row[column] for column in columns)) for row in actual)
                    if expected != copied:
                        raise ValueError(f'{table} 迁移字段核对失败')
                for table, column in IDENTITIES.items():
                    target.execute(f'SELECT setval(pg_get_serial_sequence(?,?), COALESCE(MAX({column}),1), COUNT(*) > 0) FROM {table}', (table, column))
                report = integrity_report(connection=target)
                if not report['ok']:
                    raise ValueError('迁移后业务一致性检查失败')
                target.commit()
            except Exception:
                target.rollback()
                raise
    return {'migrated': True, 'counts': counts, 'consistency': report}


def main():
    parser = argparse.ArgumentParser(description='PostgreSQL 数据迁移、备份与检查')
    parser.add_argument('command', choices=('init', 'check', 'backup', 'restore', 'import-sqlite'))
    parser.add_argument('path', nargs='?')
    args = parser.parse_args()
    try:
        if args.command == 'init':
            initialize()
            result = {'initialized': True}
        elif args.command == 'check':
            from db import integrity_report
            result = integrity_report()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['ok'] else 1
        elif args.command == 'backup':
            result = {'backup': str(backup_postgres(args.path))}
        else:
            if not args.path:
                raise ValueError('请提供迁移来源或备份路径')
            result = restore_postgres(args.path) if args.command == 'restore' else transfer_sqlite(args.path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except DATABASE_ERRORS:
        print('维护失败：数据库连接或 SQL 操作失败；原始驱动错误未输出，避免泄露连接配置')
        return 1
    except subprocess.CalledProcessError:
        print('维护失败：PostgreSQL 备份/恢复工具返回错误，请检查权限、版本与归档文件')
        return 1
    except (ValueError, RuntimeError, OSError) as error:
        print(f'维护失败：{error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
