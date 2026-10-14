"""Bootstrap local credentials and non-superuser business databases."""
import argparse
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'


def credentials():
    LOCAL.mkdir(exist_ok=True)
    path = LOCAL / 'postgres-credentials.json'
    if not path.exists():
        path.write_text(json.dumps({'admin': secrets.token_hex(24), 'app': secrets.token_hex(24)}), encoding='utf-8')
    data = json.loads(path.read_text(encoding='utf-8'))
    (LOCAL / 'init-password').write_text(data['admin'] + '\n', encoding='utf-8')
    # Local simulation only: passwords remain outside source control.
    (LOCAL / 'pgpass.conf').write_text(f"127.0.0.1:*:*:shop_app:{data['app']}\n", encoding='utf-8')
    return data


def provision(port):
    import psycopg
    from psycopg import sql
    data = credentials()
    with psycopg.connect(host='127.0.0.1', port=port, user='postgres', password=data['admin'],
                        dbname='postgres', autocommit=True, connect_timeout=5) as connection:
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname='shop_app'").fetchone():
            connection.execute(sql.SQL('CREATE ROLE shop_app LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Literal(data['app'])))
        for database in ('shop', 'shop_test'):
            if not connection.execute('SELECT 1 FROM pg_database WHERE datname=%s', (database,)).fetchone():
                connection.execute(sql.SQL('CREATE DATABASE {} OWNER shop_app').format(sql.Identifier(database)))
    print(f'PostgreSQL ready: 127.0.0.1:{port}; databases shop, shop_test; role shop_app (non-superuser)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('credentials', 'provision'))
    parser.add_argument('--port', type=int, default=5433)
    args = parser.parse_args()
    if args.command == 'credentials':
        credentials()
        print('Local credential files prepared; passwords not printed')
    else:
        provision(args.port)
