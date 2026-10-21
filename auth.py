"""In-memory demo role sessions. No accounts, passwords, or persisted identities."""
from __future__ import annotations

import secrets
import threading
import time

MERCHANT = "merchant"
CUSTOMER = "customer"
ROLES = (MERCHANT, CUSTOMER)
COOKIE_NAME = "shop_session"
SESSION_TTL_SECONDS = 12 * 60 * 60

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def create_session(role: str) -> tuple[str, dict]:
    if role not in ROLES:
        raise ValueError("无效身份")
    now = time.time()
    token = secrets.token_urlsafe(32)
    session = {
        "role": role,
        "buyer_key": secrets.token_urlsafe(16) if role == CUSTOMER else None,
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
    }
    with _lock:
        _prune(now)
        _sessions[token] = session
    return token, dict(session)


def get_session(token: str | None) -> dict | None:
    if not token:
        return None
    now = time.time()
    with _lock:
        session = _sessions.get(token)
        if session is None:
            return None
        if session["expires_at"] <= now:
            _sessions.pop(token, None)
            return None
        return dict(session)


def delete_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def reset_sessions() -> None:
    with _lock:
        _sessions.clear()


def _prune(now: float) -> None:
    for token, session in list(_sessions.items()):
        if session["expires_at"] <= now:
            _sessions.pop(token, None)
