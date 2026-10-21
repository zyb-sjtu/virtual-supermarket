"""In-memory confirmation tokens for high-risk Agent tool writes."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time


class ConfirmationError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


def _fingerprint(arguments) -> str:
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConfirmationStore:
    def __init__(self):
        self._records: dict[str, dict] = {}
        self._lock = threading.Lock()

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
        with self._lock:
            self._prune(now)
            self._records[confirmation_id] = record
        return self.public(record)

    def get(self, confirmation_id: str, role: str, buyer_key: str | None) -> dict:
        with self._lock:
            record = self._owned(confirmation_id, role, buyer_key)
            self._expire(record)
            return self.public(record)

    def approve(self, confirmation_id: str, role: str, buyer_key: str | None) -> dict:
        now = time.time()
        with self._lock:
            record = self._owned(confirmation_id, role, buyer_key)
            self._expire(record)
            if record["state"] == "consumed":
                raise ConfirmationError("CONFIRMATION_USED", "确认已使用", 409)
            if record["state"] == "expired":
                raise ConfirmationError("CONFIRMATION_EXPIRED", "确认已过期", 409)
            if record["state"] == "pending":
                record["state"] = "approved"
                record["approved_at"] = now
            return self.public(record)

    def validate_for_commit(self, confirmation_id: str, role: str, buyer_key: str | None,
                            tool: str, arguments: dict, request_id: str | None) -> dict:
        now = time.time()
        with self._lock:
            record = self._owned(confirmation_id, role, buyer_key)
            if record["tool"] != tool:
                raise ConfirmationError("CONFIRMATION_MISMATCH", "确认与当前工具不匹配", 409)
            if record["state"] == "consumed":
                if request_id and record["consumed_request_id"] == request_id:
                    return dict(record)
                raise ConfirmationError("CONFIRMATION_USED", "确认已使用", 409)
            self._expire(record)
            if record["state"] == "expired":
                raise ConfirmationError("CONFIRMATION_EXPIRED", "确认已过期", 409)
            if record["state"] == "pending":
                raise ConfirmationError("CONFIRMATION_NOT_APPROVED", "确认尚未批准", 409)
            if record["arguments_fingerprint"] != _fingerprint(arguments):
                raise ConfirmationError("CONFIRMATION_MISMATCH", "提交参数与预览不一致", 409)
            return dict(record)

    def consume(self, confirmation_id: str, role: str, buyer_key: str | None, request_id: str | None) -> None:
        now = time.time()
        with self._lock:
            record = self._owned(confirmation_id, role, buyer_key)
            if record["state"] == "consumed":
                if request_id and record["consumed_request_id"] == request_id:
                    return
                raise ConfirmationError("CONFIRMATION_USED", "确认已使用", 409)
            if record["state"] != "approved":
                raise ConfirmationError("CONFIRMATION_NOT_APPROVED", "确认尚未批准", 409)
            record["state"] = "consumed"
            record["consumed_at"] = now
            record["consumed_request_id"] = request_id

    def reset(self) -> None:
        with self._lock:
            self._records.clear()

    @staticmethod
    def public(record: dict) -> dict:
        return {
            "id": record["id"],
            "tool": record["tool"],
            "state": record["state"],
            "preview": record["preview"],
            "expires_at": record["expires_at"],
        }

    def _owned(self, confirmation_id: str, role: str, buyer_key: str | None) -> dict:
        record = self._records.get(confirmation_id)
        if record is None or record["role"] != role or record["buyer_key"] != buyer_key:
            raise ConfirmationError("CONFIRMATION_NOT_FOUND", "确认不存在", 404)
        return record

    @staticmethod
    def _expire(record: dict) -> None:
        if record["state"] in ("pending", "approved") and record["expires_at"] <= time.time():
            record["state"] = "expired"

    def _prune(self, now: float) -> None:
        for confirmation_id, record in list(self._records.items()):
            if record["state"] in ("pending", "approved") and record["expires_at"] <= now:
                self._records.pop(confirmation_id, None)


store = ConfirmationStore()
