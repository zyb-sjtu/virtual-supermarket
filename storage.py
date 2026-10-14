"""Database drivers and transaction boundaries shared by the business layer."""
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

try:
    import psycopg
except ImportError:
    psycopg = None

DATABASE_ERRORS = (sqlite3.Error,) + ((psycopg.Error,) if psycopg else ())
ROOT = Path(__file__).resolve().parent
# All application writes use the same transaction lock, including across
# application processes. It preserves the demo's serial write semantics.
WRITE_LOCK = 73591022001


def backend(db_path=None):
    if db_path is not None:
        return "sqlite"
    value = os.environ.get("SHOP_DB_BACKEND", "postgresql").lower()
    if value not in ("postgresql", "sqlite"):
        raise RuntimeError("SHOP_DB_BACKEND 只接受 postgresql 或 sqlite")
    return value


def postgres_options():
    options = dict(host=os.environ.get("PGHOST", "127.0.0.1"),
                   port=int(os.environ.get("PGPORT", "5433")),
                   dbname=os.environ.get("PGDATABASE", "shop"),
                   user=os.environ.get("PGUSER", "shop_app"),
                   connect_timeout=3, application_name="shop-backend")
    if os.environ.get("PGPASSWORD"):
        options["password"] = os.environ["PGPASSWORD"]
    else:
        options["passfile"] = os.environ.get("PGPASSFILE", str(ROOT / ".local" / "pgpass.conf"))
    if os.environ.get("PGSSLMODE"):
        options["sslmode"] = os.environ["PGSSLMODE"]
    return options


class Row(dict):
    """Mapping rows with positional access for aggregate queries."""
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, (int(v) if isinstance(v, Decimal) else v for v in values)))


def placeholders(statement):
    """Translate our qmark parameters, leaving quoted SQL literals intact."""
    chunks = statement.split("'")
    for index in range(0, len(chunks), 2):
        chunks[index] = chunks[index].replace("%", "%%").replace("?", "%s")
    # Psycopg percent literals also need escaping inside quoted strings.
    for index in range(1, len(chunks), 2):
        chunks[index] = chunks[index].replace("%", "%%")
    return "'".join(chunks)


class PostgresConnection:
    dialect = "postgresql"

    def __init__(self):
        if psycopg is None:
            raise RuntimeError("缺少 PostgreSQL 驱动，请运行 python -m pip install -r requirements.txt")
        self.raw = psycopg.connect(**postgres_options(), autocommit=True, row_factory=row_factory)
        self.raw.execute("SET statement_timeout = '12s'")
        self.raw.execute("SET lock_timeout = '10s'")
        self.raw.execute("SET synchronous_commit = on")

    def execute(self, statement, parameters=None):
        return self.raw.execute(placeholders(statement) if parameters is not None else statement, parameters)

    def executemany(self, statement, values):
        cursor = self.raw.cursor()
        cursor.executemany(placeholders(statement), values)
        return cursor

    def commit(self):
        self.raw.execute("COMMIT")

    def rollback(self):
        self.raw.execute("ROLLBACK")

    def close(self):
        self.raw.close()


def is_postgres(connection):
    return getattr(connection, "dialect", "sqlite") == "postgresql"


def begin_write(connection):
    if is_postgres(connection):
        connection.execute("BEGIN")
        connection.execute("SELECT pg_advisory_xact_lock(?)", (WRITE_LOCK,))
    else:
        connection.execute("BEGIN IMMEDIATE")


def begin_read(connection):
    connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" if is_postgres(connection) else "BEGIN")


def insert_id(connection, statement, parameters):
    if is_postgres(connection):
        return connection.execute(statement + " RETURNING id", parameters).fetchone()[0]
    return connection.execute(statement, parameters).lastrowid


def product_order(connection):
    return "sort_order" if is_postgres(connection) else "rowid"
