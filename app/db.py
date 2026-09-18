from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Iterable

from app.security import hash_password
from app.settings import (
    DATABASE_PATH,
    DB_TYPE,
    DASHSCOPE_BASE_HTTP_API_URL,
    DASHSCOPE_TIMEOUT,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    USE_DASHSCOPE_EMBEDDING,
    USE_LLM,
)


_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def set_request_id(request_id: str | None):
    return _request_id.set(request_id)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()


def _mysql_module():
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 pymysql，请先运行 install.bat 安装依赖。") from exc
    return pymysql, DictCursor


def _mysql_sql(sql: str) -> str:
    sql = sql.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO")
    return re.sub(r"\?", "%s", sql)


class DbConnection:
    def __init__(self) -> None:
        self.kind = DB_TYPE
        if self.kind == "mysql":
            pymysql, DictCursor = _mysql_module()
            self.raw = pymysql.connect(
                host=MYSQL_HOST,
                port=MYSQL_PORT,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                database=MYSQL_DATABASE,
                charset="utf8mb4",
                autocommit=False,
                cursorclass=DictCursor,
            )
        else:
            DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.raw = sqlite3.connect(DATABASE_PATH)
            self.raw.row_factory = sqlite3.Row
            self.raw.execute("PRAGMA foreign_keys = ON")

    def __enter__(self) -> "DbConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.raw.rollback()
        else:
            self.raw.commit()
        self.raw.close()

    def execute(self, sql: str, params: Iterable[Any] | None = None):
        params = tuple(params or ())
        if self.kind == "mysql":
            cursor = self.raw.cursor()
            cursor.execute(_mysql_sql(sql), params)
            return cursor
        return self.raw.execute(sql, params)


def connect() -> DbConnection:
    return DbConnection()


def rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def init_db() -> None:
    if DB_TYPE == "mysql":
        _ensure_mysql_database()
    _create_tables()
    _ensure_columns()
    seed_users()
    seed_configs()
    seed_knowledge()
    seed_reference_documents()
    backfill_document_hashes()
    sync_chroma_index()


def _ensure_mysql_database() -> None:
    pymysql, _ = _mysql_module()
    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        conn.close()


def _create_tables() -> None:
    with connect() as conn:
        for statement in MYSQL_SCHEMA if DB_TYPE == "mysql" else SQLITE_SCHEMA:
            conn.execute(statement)


def _has_column(conn: DbConnection, table: str, column: str) -> bool:
    if DB_TYPE == "mysql":
        rows = conn.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,)).fetchall()
        return bool(rows)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _ensure_columns() -> None:
    additions = {
        "users": [
            ("phone", "TEXT" if DB_TYPE != "mysql" else "VARCHAR(20)"),
        ],
        "legal_documents": [
            ("validity_date", "TEXT" if DB_TYPE != "mysql" else "VARCHAR(32)"),
            ("validity_note", "TEXT"),
        ],
    }
    with connect() as conn:
        for table, columns in additions.items():
            for column, definition in columns:
                if _has_column(conn, table, column):
                    continue
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if DB_TYPE == "mysql":
            index_rows = conn.execute("SHOW INDEX FROM users WHERE Key_name = %s", ("idx_users_phone_unique",)).fetchall()
            if not index_rows:
                conn.execute("CREATE UNIQUE INDEX idx_users_phone_unique ON users(phone)")
        else:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone) WHERE phone IS NOT NULL AND phone != ''"
            )


SQLITE_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, phone TEXT UNIQUE, password_hash TEXT NOT NULL, display_name TEXT, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, last_login_at TEXT)",
    "CREATE TABLE IF NOT EXISTS sms_verification_codes (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, purpose TEXT NOT NULL, code_hash TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS legal_documents (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, document_type TEXT NOT NULL, case_cause TEXT, region TEXT, year INTEGER, source_name TEXT, validity_status TEXT DEFAULT 'active', validity_date TEXT, validity_note TEXT, file_hash TEXT, status TEXT NOT NULL DEFAULT 'published', created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(created_by) REFERENCES users(id))",
    "CREATE TABLE IF NOT EXISTS document_chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL, chunk_no INTEGER NOT NULL, content TEXT NOT NULL, section_name TEXT, paragraph_no INTEGER, vector_id TEXT, metadata_json TEXT, FOREIGN KEY(document_id) REFERENCES legal_documents(id) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS import_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT NOT NULL, file_hash TEXT, stage TEXT NOT NULL, status TEXT NOT NULL, total_chunks INTEGER NOT NULL DEFAULT 0, error_summary TEXT, created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(created_by) REFERENCES users(id))",
    "CREATE TABLE IF NOT EXISTS reasoning_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT NOT NULL, case_text TEXT NOT NULL, extracted_facts_json TEXT, filters_json TEXT, status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id))",
    "CREATE TABLE IF NOT EXISTS reasoning_results (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, version_no INTEGER NOT NULL, result_markdown TEXT NOT NULL, result_json TEXT, model_name TEXT NOT NULL, retrieval_config_json TEXT NOT NULL, latency_ms INTEGER, created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES reasoning_tasks(id))",
    "CREATE TABLE IF NOT EXISTS citations (id INTEGER PRIMARY KEY AUTOINCREMENT, result_id INTEGER NOT NULL, citation_no INTEGER NOT NULL, document_id INTEGER NOT NULL, chunk_id INTEGER NOT NULL, quote_text TEXT NOT NULL, relevance_score REAL, keyword_score REAL, vector_score REAL, FOREIGN KEY(result_id) REFERENCES reasoning_results(id), FOREIGN KEY(document_id) REFERENCES legal_documents(id), FOREIGN KEY(chunk_id) REFERENCES document_chunks(id))",
    "CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, task_id INTEGER NOT NULL, result_id INTEGER NOT NULL, citation_id INTEGER, rating TEXT NOT NULL, reason TEXT, comment TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(task_id) REFERENCES reasoning_tasks(id), FOREIGN KEY(result_id) REFERENCES reasoning_results(id), FOREIGN KEY(citation_id) REFERENCES citations(id))",
    "CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL, object_type TEXT, object_id INTEGER, result TEXT NOT NULL, request_id TEXT, created_at TEXT NOT NULL, FOREIGN KEY(actor_id) REFERENCES users(id))",
    "CREATE TABLE IF NOT EXISTS system_configs (`key` TEXT PRIMARY KEY, value TEXT NOT NULL, description TEXT, updated_by INTEGER, updated_at TEXT NOT NULL)",
]


MYSQL_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users (id BIGINT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(80) UNIQUE NOT NULL, phone VARCHAR(20) UNIQUE, password_hash VARCHAR(255) NOT NULL, display_name VARCHAR(80), role VARCHAR(40) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'active', created_at DATETIME NOT NULL, last_login_at DATETIME NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS sms_verification_codes (id BIGINT PRIMARY KEY AUTO_INCREMENT, phone VARCHAR(20) NOT NULL, purpose VARCHAR(40) NOT NULL, code_hash VARCHAR(128) NOT NULL, expires_at DATETIME NOT NULL, used_at DATETIME NULL, created_at DATETIME NOT NULL, INDEX idx_sms_codes_lookup(phone, purpose, created_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS legal_documents (id BIGINT PRIMARY KEY AUTO_INCREMENT, title VARCHAR(255) NOT NULL, document_type VARCHAR(40) NOT NULL, case_cause VARCHAR(80), region VARCHAR(80), year INT, source_name VARCHAR(255), validity_status VARCHAR(40) DEFAULT 'active', validity_date VARCHAR(32), validity_note TEXT, file_hash VARCHAR(128), status VARCHAR(40) NOT NULL DEFAULT 'published', created_by BIGINT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, FOREIGN KEY(created_by) REFERENCES users(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS document_chunks (id BIGINT PRIMARY KEY AUTO_INCREMENT, document_id BIGINT NOT NULL, chunk_no INT NOT NULL, content TEXT NOT NULL, section_name VARCHAR(120), paragraph_no INT, vector_id VARCHAR(120), metadata_json TEXT, FOREIGN KEY(document_id) REFERENCES legal_documents(id) ON DELETE CASCADE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS import_jobs (id BIGINT PRIMARY KEY AUTO_INCREMENT, file_name VARCHAR(255) NOT NULL, file_hash VARCHAR(128), stage VARCHAR(80) NOT NULL, status VARCHAR(40) NOT NULL, total_chunks INT NOT NULL DEFAULT 0, error_summary TEXT, created_by BIGINT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, FOREIGN KEY(created_by) REFERENCES users(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS reasoning_tasks (id BIGINT PRIMARY KEY AUTO_INCREMENT, user_id BIGINT NOT NULL, title VARCHAR(255) NOT NULL, case_text MEDIUMTEXT NOT NULL, extracted_facts_json TEXT, filters_json TEXT, status VARCHAR(40) NOT NULL DEFAULT 'draft', created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS reasoning_results (id BIGINT PRIMARY KEY AUTO_INCREMENT, task_id BIGINT NOT NULL, version_no INT NOT NULL, result_markdown MEDIUMTEXT NOT NULL, result_json TEXT, model_name VARCHAR(120) NOT NULL, retrieval_config_json TEXT NOT NULL, latency_ms INT, created_at DATETIME NOT NULL, FOREIGN KEY(task_id) REFERENCES reasoning_tasks(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS citations (id BIGINT PRIMARY KEY AUTO_INCREMENT, result_id BIGINT NOT NULL, citation_no INT NOT NULL, document_id BIGINT NOT NULL, chunk_id BIGINT NOT NULL, quote_text MEDIUMTEXT NOT NULL, relevance_score DOUBLE, keyword_score DOUBLE, vector_score DOUBLE, FOREIGN KEY(result_id) REFERENCES reasoning_results(id), FOREIGN KEY(document_id) REFERENCES legal_documents(id), FOREIGN KEY(chunk_id) REFERENCES document_chunks(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS feedback (id BIGINT PRIMARY KEY AUTO_INCREMENT, user_id BIGINT NOT NULL, task_id BIGINT NOT NULL, result_id BIGINT NOT NULL, citation_id BIGINT, rating VARCHAR(40) NOT NULL, reason VARCHAR(255), comment TEXT, created_at DATETIME NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(task_id) REFERENCES reasoning_tasks(id), FOREIGN KEY(result_id) REFERENCES reasoning_results(id), FOREIGN KEY(citation_id) REFERENCES citations(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS audit_logs (id BIGINT PRIMARY KEY AUTO_INCREMENT, actor_id BIGINT, action VARCHAR(80) NOT NULL, object_type VARCHAR(80), object_id BIGINT, result VARCHAR(80) NOT NULL, request_id VARCHAR(120), created_at DATETIME NOT NULL, FOREIGN KEY(actor_id) REFERENCES users(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE IF NOT EXISTS system_configs (`key` VARCHAR(80) PRIMARY KEY, value TEXT NOT NULL, description VARCHAR(255), updated_by BIGINT, updated_at DATETIME NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
]


SEED_DOCUMENTS = [
    {
        "title": "买卖合同尾款纠纷模拟案例",
        "document_type": "case",
        "case_cause": "合同纠纷",
        "region": "江苏",
        "year": 2023,
        "source_name": "contract_sale_case_003.txt",
        "sections": [
            ("事实", "张某与李某签订货物买卖合同，约定货到验收后三日内支付尾款。李某收到货物后未按期付款，张某提交合同、物流签收记录和聊天记录。"),
            ("说理", "在买卖合同关系成立且货物已经交付的情况下，买受人应按照约定支付价款。聊天记录、发货单和签收信息能够相互印证时，可形成较完整的履行证据链。"),
            ("结果", "法院支持出卖人要求支付剩余货款的主要诉求，并根据合同约定酌定违约责任。"),
        ],
    },
    {
        "title": "民间借贷利息争议模拟案例",
        "document_type": "case",
        "case_cause": "民间借贷",
        "region": "浙江",
        "year": 2022,
        "source_name": "loan_interest_case_002.txt",
        "sections": [
            ("事实", "王某向赵某出具借条，载明借款本金五万元，但未明确约定利息。赵某通过银行转账交付款项，王某到期未还。"),
            ("说理", "民间借贷案件中，出借人应证明借贷合意和款项交付。自然人之间借款未约定利息的，通常不支持借期内利息，但可依法主张逾期利息。"),
            ("结果", "法院支持返还本金，并结合逾期情况处理资金占用损失。"),
        ],
    },
    {
        "title": "劳动合同解除赔偿模拟案例",
        "document_type": "case",
        "case_cause": "劳动争议",
        "region": "广东",
        "year": 2024,
        "source_name": "labor_dismiss_case_001.txt",
        "sections": [
            ("事实", "公司以员工绩效不达标为由解除劳动合同，但未提交明确考核制度、考核结果送达记录和培训调岗证据。"),
            ("说理", "用人单位解除劳动合同应证明解除依据、程序和事实均符合法律规定。缺少制度公示、考核告知和改进机会的，解除行为存在违法风险。"),
            ("结果", "仲裁机构倾向认定解除依据不足，并支持劳动者主张赔偿金。"),
        ],
    },
    {
        "title": "民法典合同编模拟条文",
        "document_type": "statute",
        "case_cause": "合同纠纷",
        "region": "全国",
        "year": 2021,
        "source_name": "civil_code_contract_articles.txt",
        "sections": [
            ("条文", "依法成立的合同，对当事人具有法律约束力。当事人应当按照约定全面履行自己的义务。"),
            ("条文", "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"),
        ],
    },
]


def seed_users() -> None:
    users = [
        ("user01", "普通用户", "user"),
        ("kb_admin", "知识库管理员", "kb_admin"),
        ("admin", "系统管理员", "sys_admin"),
    ]
    with connect() as conn:
        for username, display_name, role in users:
            conn.execute(
                "INSERT OR IGNORE INTO users(username, password_hash, display_name, role, status, created_at) VALUES (?, ?, ?, ?, 'active', ?)",
                (username, hash_password("123456"), display_name, role, now()),
            )


def seed_configs() -> None:
    configs = [
        ("chat_model", "qwen-plus", "通义千问对话模型"),
        ("embedding_model", "text-embedding-v4", "通义千问向量模型，可关闭后使用本地向量"),
        ("use_llm", "true" if USE_LLM else "false", "是否启用通义千问生成"),
        ("use_dashscope_embedding", "true" if USE_DASHSCOPE_EMBEDDING else "false", "是否启用通义千问向量模型"),
        ("dashscope_api_key", "", "通义千问 API Key（加密保存，仅显示配置状态）"),
        ("dashscope_base_http_api_url", DASHSCOPE_BASE_HTTP_API_URL, "通义千问 API 地址，可留空使用默认地址"),
        ("dashscope_timeout", str(DASHSCOPE_TIMEOUT), "通义千问请求超时时间（秒）"),
        ("top_k", "6", "检索返回证据数量"),
        ("keyword_weight", "0.35", "关键词检索权重"),
        ("vector_weight", "0.65", "语义向量检索权重"),
    ]
    with connect() as conn:
        for key, value, desc in configs:
            conn.execute(
                "INSERT OR IGNORE INTO system_configs(`key`, value, description, updated_at) VALUES (?, ?, ?, ?)",
                (key, value, desc, now()),
            )


def seed_knowledge() -> None:
    from app.services import vector_store

    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS total FROM legal_documents").fetchone()["total"]
        if count:
            return
        admin = conn.execute("SELECT id FROM users WHERE username = 'kb_admin'").fetchone()
        admin_id = admin["id"] if admin else None
        for doc in SEED_DOCUMENTS:
            cur = conn.execute(
                """
                INSERT INTO legal_documents(title, document_type, case_cause, region, year, source_name, validity_status, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', 'published', ?, ?, ?)
                """,
                (doc["title"], doc["document_type"], doc["case_cause"], doc["region"], doc["year"], doc["source_name"], admin_id, now(), now()),
            )
            document_id = cur.lastrowid
            for index, (section, content) in enumerate(doc["sections"], start=1):
                metadata = {
                    "title": doc["title"],
                    "document_type": doc["document_type"],
                    "case_cause": doc["case_cause"],
                    "region": doc["region"],
                    "year": doc["year"],
                    "section_name": section,
                }
                chunk_cur = conn.execute(
                    """
                    INSERT INTO document_chunks(document_id, chunk_no, content, section_name, paragraph_no, vector_id, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (document_id, index, content, section, index, f"doc-{document_id}-chunk-{index}", json.dumps(metadata, ensure_ascii=False)),
                )
                chunk_id = chunk_cur.lastrowid
                metadata["document_id"] = document_id
                metadata["chunk_id"] = chunk_id
                metadata["status"] = "published"
                vector_store.upsert_chunk(chunk_id, content, metadata)


def seed_reference_documents() -> None:
    """Add verified, auditable statute entries even when an older database is already populated."""
    from app.services import vector_store

    documents = [
        {
            "title": "《中华人民共和国民法典》合同履行与违约责任条文",
            "document_type": "statute",
            "case_cause": "合同纠纷",
            "year": 2021,
            "source_name": "seed_civil_code_contract_articles",
            "validity_status": "active",
            "validity_date": "",
            "validity_note": "",
            "sections": [
                ("条文", "《中华人民共和国民法典》第五百零九条：当事人应当按照约定全面履行自己的义务。"),
                ("条文", "《中华人民共和国民法典》第五百七十七条：当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"),
            ],
        },
        {
            "title": "《中华人民共和国合同法》历史条文示例",
            "document_type": "statute",
            "case_cause": "合同纠纷",
            "year": 1999,
            "source_name": "seed_repealed_contract_law",
            "validity_status": "repealed",
            "validity_date": "2021-01-01",
            "validity_note": "该法已于 2021-01-01 废止，检索命中时仅可作为历史材料，不应直接作为现行法律依据。",
            "sections": [
                ("历史条文", "《中华人民共和国合同法》第六十条：当事人应当按照约定全面履行自己的义务。"),
            ],
        },
    ]
    with connect() as conn:
        admin = conn.execute("SELECT id FROM users WHERE username = 'kb_admin'").fetchone()
        admin_id = admin["id"] if admin else None
        for document in documents:
            exists = conn.execute(
                "SELECT id FROM legal_documents WHERE source_name = ? LIMIT 1",
                (document["source_name"],),
            ).fetchone()
            if exists:
                continue
            cur = conn.execute(
                """
                INSERT INTO legal_documents(
                    title, document_type, case_cause, region, year, source_name,
                    validity_status, validity_date, validity_note, status, created_by, created_at, updated_at
                )
                VALUES (?, ?, ?, '全国', ?, ?, ?, ?, ?, 'published', ?, ?, ?)
                """,
                (
                    document["title"],
                    document["document_type"],
                    document["case_cause"],
                    document["year"],
                    document["source_name"],
                    document["validity_status"],
                    document["validity_date"],
                    document["validity_note"],
                    admin_id,
                    now(),
                    now(),
                ),
            )
            document_id = cur.lastrowid
            for index, (section, content) in enumerate(document["sections"], start=1):
                metadata = {
                    "title": document["title"],
                    "document_type": document["document_type"],
                    "case_cause": document["case_cause"],
                    "region": "全国",
                    "year": document["year"],
                    "section_name": section,
                    "validity_status": document["validity_status"],
                }
                chunk = conn.execute(
                    """
                    INSERT INTO document_chunks(document_id, chunk_no, content, section_name, paragraph_no, vector_id, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (document_id, index, content, section, index, f"doc-{document_id}-chunk-{index}", json.dumps(metadata, ensure_ascii=False)),
                )
                metadata["document_id"] = document_id
                metadata["chunk_id"] = chunk.lastrowid
                metadata["status"] = "published"
                vector_store.upsert_chunk(chunk.lastrowid, content, metadata)


def _content_hash(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def backfill_document_hashes() -> None:
    with connect() as conn:
        docs = conn.execute("SELECT id FROM legal_documents WHERE file_hash IS NULL OR file_hash = ''").fetchall()
        for doc in docs:
            chunks = conn.execute(
                "SELECT content FROM document_chunks WHERE document_id = ? ORDER BY chunk_no",
                (doc["id"],),
            ).fetchall()
            content = "\n\n".join(chunk["content"] for chunk in chunks)
            if content.strip():
                conn.execute("UPDATE legal_documents SET file_hash = ? WHERE id = ?", (_content_hash(content), doc["id"]))


def sync_chroma_index() -> None:
    from app.services import vector_store

    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT c.id AS chunk_id, c.content, c.section_name, c.paragraph_no,
                       d.id AS document_id, d.title, d.document_type, d.case_cause,
                       d.region, d.year, d.source_name, d.validity_status, d.validity_date,
                       d.validity_note, d.status
                FROM document_chunks c
                JOIN legal_documents d ON d.id = c.document_id
                WHERE d.status = 'published'
                """
            ).fetchall()
        )
    target_ids = {int(row["chunk_id"]) for row in rows}
    stale_ids = vector_store.list_chunk_ids() - target_ids
    vector_store.delete_chunks(sorted(stale_ids))
    for start in range(0, len(rows), vector_store.EMBEDDING_BATCH_SIZE):
        batch = rows[start : start + vector_store.EMBEDDING_BATCH_SIZE]
        vector_store.upsert_chunks(
            [
                (
                    int(row["chunk_id"]),
                    row["content"],
                    {key: row.get(key) for key in row.keys() if key != "content"},
                )
                for row in batch
            ]
        )


def rebuild_chroma_index() -> None:
    """Recreate the active embedding collection from the database source of truth."""
    from app.services import vector_store

    vector_store.reset_collection()
    sync_chroma_index()


def audit(actor_id: int | None, action: str, object_type: str, object_id: int | None, result: str, request_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_logs(actor_id, action, object_type, object_id, result, request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (actor_id, action, object_type, object_id, result, request_id or current_request_id(), now()),
        )


def get_config(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM system_configs WHERE `key` = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def get_int_config(key: str, default: int) -> int:
    try:
        return int(get_config(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_float_config(key: str, default: float) -> float:
    try:
        return float(get_config(key, str(default)))
    except (TypeError, ValueError):
        return default


def update_config(key: str, value: str, updated_by: int | None = None) -> None:
    with connect() as conn:
        row = conn.execute("SELECT `key` FROM system_configs WHERE `key` = ?", (key,)).fetchone()
        if not row:
            raise ValueError(f"未知配置项：{key}")
        conn.execute(
            "UPDATE system_configs SET value = ?, updated_by = ?, updated_at = ? WHERE `key` = ?",
            (value, updated_by, now(), key),
        )


def create_import_job(file_name: str, file_hash: str | None, stage: str, status: str, created_by: int | None) -> int:
    timestamp = now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO import_jobs(file_name, file_hash, stage, status, total_chunks, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (file_name, file_hash, stage, status, created_by, timestamp, timestamp),
        )
        return int(cur.lastrowid)


def update_import_job(
    job_id: int,
    *,
    file_hash: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    total_chunks: int | None = None,
    error_summary: str | None = None,
) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if file_hash is not None:
        fields.append("file_hash = ?")
        params.append(file_hash)
    if stage is not None:
        fields.append("stage = ?")
        params.append(stage)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if total_chunks is not None:
        fields.append("total_chunks = ?")
        params.append(total_chunks)
    if error_summary is not None:
        fields.append("error_summary = ?")
        params.append(error_summary[:1000])
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(now())
    params.append(job_id)
    with connect() as conn:
        conn.execute(f"UPDATE import_jobs SET {', '.join(fields)} WHERE id = ?", params)
