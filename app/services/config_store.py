from __future__ import annotations

import base64
import hashlib
import os

from app import db
from app.settings import (
    CHAT_MODEL_NAME,
    DASHSCOPE_BASE_HTTP_API_URL,
    DASHSCOPE_TIMEOUT,
    EMBEDDING_MODEL_NAME,
    USE_DASHSCOPE_EMBEDDING,
    USE_LLM,
    SECRET_KEY,
)


_ENCRYPTED_PREFIX = "fernet:v1:"


def _fernet():
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_ENCRYPTED_PREFIX}{token}"


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_ENCRYPTED_PREFIX):
        # Allows one-time compatibility with older plaintext configuration rows.
        return value
    try:
        token = value[len(_ENCRYPTED_PREFIX) :]
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _config_bool(key: str, default: bool) -> bool:
    value = db.get_config(key, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def dashscope_api_key() -> str:
    stored = decrypt_secret(db.get_config("dashscope_api_key", ""))
    return stored or os.getenv("DASHSCOPE_API_KEY", "").strip()


def use_llm() -> bool:
    return _config_bool("use_llm", USE_LLM)


def use_dashscope_embedding() -> bool:
    return _config_bool("use_dashscope_embedding", USE_DASHSCOPE_EMBEDDING)


def chat_model() -> str:
    return db.get_config("chat_model", CHAT_MODEL_NAME).strip() or CHAT_MODEL_NAME


def embedding_model() -> str:
    return db.get_config("embedding_model", EMBEDDING_MODEL_NAME).strip() or EMBEDDING_MODEL_NAME


def dashscope_base_url() -> str:
    return db.get_config("dashscope_base_http_api_url", DASHSCOPE_BASE_HTTP_API_URL).strip() or DASHSCOPE_BASE_HTTP_API_URL


def dashscope_timeout() -> int:
    return db.get_int_config("dashscope_timeout", DASHSCOPE_TIMEOUT)


def api_key_configured() -> bool:
    return bool(dashscope_api_key())
