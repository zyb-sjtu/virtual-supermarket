"""Database-backed confirmation tokens for high-risk Agent tool writes."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextlib import closing

import db
from storage import backend, postgres_options

PRUNE_SECONDS = 24 * 60 * 60


class ConfirmationError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


def _fingerprint(arguments) -> str:
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConfirmationStore:
    def __init__(self, db_path=None):
        self._db_path = db_path

    def create(self, tool: str, role: str, buyer_key: str | None, arguments: dict, preview: dict,
               ttl_seconds: int = 300) -> dict:
        now = time.time()
        confirmation_id = "cfm_" + secrets.token_urlsafe(24)
        record = {
            "id": confirmation_id,
            "tool": tool,
            "role": role,
            "buyer_key": buyer_key,
            "arguments_fingerprint": _fingerprint(arguments),
            "preview": preview,
            "state": "pending",
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "approved_at": None,
            "consumed_at": None,
            "consumed_request_id": None,
        }
        with closing(db.connect(self._db_path)) as connection:
            db.begin_write(connection)
            try:
                connection.execute("DELETE FROM agent_confirmations WHERE expires_at < ?", (now - PRUNE_SECONDS,))
                connection.execute(
                    """INSERT INTO agent_confirmations
                       (id, tool, role, buyer_key, arguments_fingerprint, preview_json, state,
                        created_at, expires_at, approved_at, consumed_at, consumed_request_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (record["id"], record["tool"], record["role"], record["buyer_key"],
                     record["arguments_fingerprint"], json.dumps(preview, ensure_ascii=False),
                     record["state"], record["created_at"], record["expires_at"], None, None, None))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.public(record)

    def get(self, confirmation_id: str, role: str, buyer_key: str | None) -> dict:
        with closing(db.connect(self._db_path)) as connection:
            db.begin_read(connection)
            record = self._load(connection, confirmation_id, role, buyer_key)
        self._derive_expiry(record)
        return self.public(record)

    def approve(self, confirmation_id: str, role: str, buyer_key: str | None) -> dict:
        now = time.time()
        error = None
        with closing(db.connect(self._db_path)) as connection:
            db.begin_write(connection)
            try:
                record = self._load(connection, confirmation_id, role, buyer_key)
                state = self._derive_expiry(record)
                if state == "consumed":
                    error = ConfirmationError("CONFIRMATION_USED", "确认已使用", 409)
                elif state == "expired":
                    self._persist_expired(connection, record)
                    error = ConfirmationError("CONFIRMATION_EXPIRED", "确认已过期", 409)
                elif state == "pending":
                    connection.execute(
                        "UPDATE agent_confirmations SET state = 'approved', approved_at = ? WHERE id = ?",
                        (now, confirmation_id))
                    record["state"] = "approved"
                    record["approved_at"] = now
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if error:
            raise error
        return self.public(record)

    def validate_for_commit(self, confirmation_id: str, role: str, buyer_key: str | None,
                            tool: str, arguments: dict, request_id: str | None) -> dict:
        """Validate and atomically claim a confirmation for one commit."""
        now = time.time()
        error = None
        with closing(db.connect(self._db_path)) as connection:
            db.begin_write(connection)
            try:
                record = self._load(connection, confirmation_id, role, buyer_key)
                if record["tool"] != tool:
                    error = ConfirmationError("CONFIRMATION_MISMATCH", "确认与当前工具不匹配", 409)
                elif record["arguments_fingerprint"] != _fingerprint(arguments):
                    error = ConfirmationError("CONFIRMATION_MISMATCH", "提交参数与预览不一致", 409)
                else:
                    state = self._derive_expiry(record)
                    if state == "consumed":
                        if request_id and record["consumed_request_id"] == request_id:
                            pass
                        else:
                            error = ConfirmationError("CONFIRMATION_USED", "确认已使用", 409)
                    elif state == "expired":
                        self._persist_expired(connection, record)
                        error = ConfirmationError("CONFIRMATION_EXPIRED", "确认已过期", 409)
                    elif state == "pending":
                        error = ConfirmationError("CONFIRMATION_NOT_APPROVED", "确认尚未批准", 409)
                    else:
                        cursor = connection.execute(
                            """UPDATE agent_confirmations SET state = 'consumed', consumed_at = ?,
                               consumed_request_id = ? WHERE id = ? AND state = 'approved' AND expires_at > ?""",
                            (now, request_id, confirmation_id, now))
                        if cursor.rowcount != 1:
                            error = ConfirmationError("CONFIRMATION_USED", "确认已使用", 409)
                        else:
                            record["state"] = "consumed"
                            record["consumed_at"] = now
                            record["consumed_request_id"] = request_id
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if error:
            raise error
        return record

    def release(self, confirmation_id: str, role: str, buyer_key: str | None, request_id: str | None) -> None:
        """Return a claimed token after a failed handler; best effort."""
        owner = "buyer_key IS NULL" if buyer_key is None else "buyer_key = ?"
        parameters = [confirmation_id, role] + ([] if buyer_key is None else [buyer_key])
        parameters.append(request_id)
        with closing(db.connect(self._db_path)) as connection:
            db.begin_write(connection)
            try:
                connection.execute(
                    f"""UPDATE agent_confirmations SET state = 'approved', consumed_at = NULL,
                        consumed_request_id = NULL
                        WHERE id = ? AND role = ? AND {owner} AND state = 'consumed'
                        AND consumed_request_id = ?""", parameters)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def reset(self) -> None:
        if self._db_path is None and backend() == "postgresql":
            if postgres_options()["dbname"] != "shop_test":
                raise RuntimeError("拒绝清空非测试 PostgreSQL 的确认表")
        with closing(db.connect(self._db_path)) as connection:
            db.begin_write(connection)
            try:
                connection.execute("DELETE FROM agent_confirmations")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def public(record: dict) -> dict:
        return {
            "id": record["id"],
            "tool": record["tool"],
            "state": record["state"],
            "preview": record["preview"],
            "expires_at": record["expires_at"],
        }

    def _load(self, connection, confirmation_id: str, role: str, buyer_key: str | None) -> dict:
        row = connection.execute("SELECT * FROM agent_confirmations WHERE id = ?", (confirmation_id,)).fetchone()
        if row is None:
            raise ConfirmationError("CONFIRMATION_NOT_FOUND", "确认不存在", 404)
        record = dict(row)
        record["preview"] = json.loads(record.pop("preview_json"))
        if record["role"] != role or record["buyer_key"] != buyer_key:
            raise ConfirmationError("CONFIRMATION_NOT_FOUND", "确认不存在", 404)
        return record

    @staticmethod
    def _derive_expiry(record: dict) -> str:
        if record["state"] in ("pending", "approved") and record["expires_at"] <= time.time():
            record["state"] = "expired"
        return record["state"]

    @staticmethod
    def _persist_expired(connection, record: dict) -> None:
        connection.execute("UPDATE agent_confirmations SET state = 'expired' WHERE id = ?", (record["id"],))
        record["state"] = "expired"


store = ConfirmationStore()
