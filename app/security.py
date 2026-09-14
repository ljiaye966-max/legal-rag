from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from app.settings import SECRET_KEY


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt_text, digest_text = stored_hash.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session(user: dict[str, Any], ttl_seconds: int = 60 * 60 * 8) -> str:
    payload = {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "exp": int(time.time()) + ttl_seconds,
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
    return f"{raw}.{_sign(raw)}"


def read_session(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    raw, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw), signature):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()).decode("utf-8"))
    except Exception:
        return None
    if payload.get("exp", 0) < int(time.time()):
        return None
    return payload
