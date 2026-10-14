"""Safe local backup, restore, and database consistency inspection."""

import argparse
import json
import sqlite3
import uuid
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from db import database_path, initialize, integrity_report
from runtime import DatabaseLease


def backup_database(source, output=None):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("源数据库不存在")
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = source.parent / "backups" / f"shop-{stamp}-{uuid.uuid4().hex[:6]}.sqlite3"
    output = Path(output).resolve()
    if output == source or output.exists():
        raise ValueError("备份目标必须是尚不存在的新文件")
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
        with closing(sqlite3.connect(output)) as destination:
            connection.backup(destination)
    return output


def restore_database(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or not source.is_file():
        raise ValueError("请选择独立且存在的备份文件")
    # Validate before touching the target. Restores accept this project's modern
    # and original schemas; initialize will migrate an older valid backup.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as backup:
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库损坏")
        required = {"products": {"id", "name", "price_cents", "stock"},
                    "movements": {"id", "request_id", "action", "product_id", "quantity", "unit_price_cents", "stock_after", "created_at"}}
        for table, columns in required.items():
            actual = {row[1] for row in backup.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                raise ValueError("备份不属于本项目的数据库")
        if backup.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("备份数据库外键校验失败")
    # Stage and fully validate the business migration before replacing data.
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.parent / f".restore-{uuid.uuid4().hex}.sqlite3"
    with DatabaseLease(target):
        try:
            backup_database(source, staged)
            initialize(staged)
            if not integrity_report(staged)["ok"]:
                raise ValueError("备份库存或订单数据不一致，恢复已取消")
            safety_backup = backup_database(target) if target.exists() else None
            with closing(sqlite3.connect(staged)) as candidate:
                with closing(sqlite3.connect(target)) as destination:
                    candidate.backup(destination)
            return safety_backup
        finally:
            for path in (staged, Path(str(staged) + "-wal"), Path(str(staged) + "-shm")):
                if path.exists():
                    path.unlink()


def main():
    # Keep the original CLI useful after PostgreSQL becomes the default.
    # --database still explicitly addresses a retained SQLite file.
    from storage import backend
    if backend() == 'postgresql' and '--database' not in sys.argv:
        from postgres_maintenance import main as postgres_main
        return postgres_main()
    parser = argparse.ArgumentParser(description="超市数据库维护工具")
    parser.add_argument("command", choices=("backup", "restore", "check"))
    parser.add_argument("path", nargs="?", help="backup 的目标路径或 restore 的来源路径")
    parser.add_argument("--database", default=str(database_path()), help="要维护的数据库")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            print(str(backup_database(args.database, args.path)))
        elif args.command == "restore":
            if not args.path:
                parser.error("restore 必须提供备份路径")
            original = restore_database(args.path, args.database)
            print(json.dumps({"restored": True, "safety_backup": str(original) if original else None}, ensure_ascii=False))
        else:
            if not Path(args.database).is_file():
                raise ValueError("数据库不存在")
            report = integrity_report(Path(args.database))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        return 0
    except (ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"维护失败：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
