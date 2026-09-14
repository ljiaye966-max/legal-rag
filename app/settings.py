from __future__ import annotations

import os
import secrets
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(path: Path) -> None:
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.strip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent

load_dotenv(BASE_DIR / ".env")
load_dotenv(ROOT_DIR / "Agent+RAG项目开发" / ".env")

database_path_text = os.getenv("DATABASE_PATH", "storage/legal_rag.db")
DATABASE_PATH = Path(database_path_text)
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = BASE_DIR / DATABASE_PATH
EXPORT_DIR = BASE_DIR / "exports"
UPLOAD_DIR = BASE_DIR / "uploads"
CHROMA_PATH = BASE_DIR / os.getenv("CHROMA_PATH", "storage/chroma")
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "legal_knowledge")

DB_TYPE = os.getenv("DB_TYPE", "sqlite").lower()
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "123456")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "legal_rag")

def _load_secret_key() -> str:
    configured = os.getenv("SECRET_KEY")
    if configured:
        return configured
    secret_path = BASE_DIR / "storage" / "secret.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
    generated = secrets.token_urlsafe(48)
    secret_path.write_text(generated, encoding="utf-8")
    return generated


SECRET_KEY = _load_secret_key()
USE_LLM = os.getenv("USE_LLM", "false").lower() == "true"
CHAT_MODEL_NAME = os.getenv("CHAT_MODEL_NAME", "qwen-plus")
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "text-embedding-v4")
USE_DASHSCOPE_EMBEDDING = os.getenv("USE_DASHSCOPE_EMBEDDING", "false").lower() == "true"
DASHSCOPE_BASE_HTTP_API_URL = os.getenv("DASHSCOPE_BASE_HTTP_API_URL", "").strip()
DASHSCOPE_TIMEOUT = int(os.getenv("DASHSCOPE_TIMEOUT", "30"))
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "local").strip().lower()
SMS_CODE_TTL_SECONDS = int(os.getenv("SMS_CODE_TTL_SECONDS", "300"))
SMS_CODE_COOLDOWN_SECONDS = int(os.getenv("SMS_CODE_COOLDOWN_SECONDS", "60"))
ALIYUN_ACCESS_KEY_ID = os.getenv("ALIYUN_ACCESS_KEY_ID", "").strip()
ALIYUN_ACCESS_KEY_SECRET = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "").strip()
ALIYUN_SMS_VERIFY_ENDPOINT = os.getenv("ALIYUN_SMS_VERIFY_ENDPOINT", "dypnsapi.aliyuncs.com").strip()
ALIYUN_SMS_VERIFY_SIGN_NAME = os.getenv("ALIYUN_SMS_VERIFY_SIGN_NAME", "").strip()
ALIYUN_SMS_VERIFY_TEMPLATE_CODE = os.getenv("ALIYUN_SMS_VERIFY_TEMPLATE_CODE", "").strip()
ALIYUN_SMS_VERIFY_SCHEME_NAME = os.getenv("ALIYUN_SMS_VERIFY_SCHEME_NAME", "").strip()

KEYWORD_WEIGHT = float(os.getenv("KEYWORD_WEIGHT", "0.4"))
VECTOR_WEIGHT = float(os.getenv("VECTOR_WEIGHT", "0.6"))
TOP_K = int(os.getenv("TOP_K", "6"))
ASSISTANT_MIN_RELEVANCE_SCORE = float(os.getenv("ASSISTANT_MIN_RELEVANCE_SCORE", "0.24"))

for path in (DATABASE_PATH.parent, EXPORT_DIR, UPLOAD_DIR, CHROMA_PATH):
    path.mkdir(parents=True, exist_ok=True)
