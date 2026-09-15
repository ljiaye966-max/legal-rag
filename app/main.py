from __future__ import annotations

import asyncio
import hashlib
import html
import csv
import hmac
import io
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.security import create_session, hash_password, read_session, verify_password
from app.services import config_store, rag, search, sms_verification, vector_store
from app.settings import (
    BASE_DIR,
    CHAT_MODEL_NAME,
    DB_TYPE,
    EXPORT_DIR,
    SECRET_KEY,
    SMS_CODE_COOLDOWN_SECONDS,
    SMS_CODE_TTL_SECONDS,
    SMS_PROVIDER,
    UPLOAD_DIR,
)


app = FastAPI(title="AI 法律案例推理系统")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def markdown_html(markdown: str) -> str:
    escaped = html.escape(markdown)
    escaped = re.sub(r"^## (.+)$", r"<h2>\1</h2>", escaped, flags=re.MULTILINE)
    return escaped.replace("\n", "<br>")


templates.env.filters["markdown_html"] = markdown_html


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    token = db.set_request_id(request_id)
    try:
        response = await call_next(request)
    finally:
        db.reset_request_id(token)
    response.headers["x-request-id"] = request_id
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", db.current_request_id())
    if exc.status_code == 303 and isinstance(exc.headers, dict) and exc.headers.get("Location"):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    user = current_user(request)
    if exc.status_code in {401, 403}:
        db.audit(user["id"] if user else None, "access_denied", request.url.path, None, str(exc.status_code), request_id)
    if request.url.path.startswith("/api"):
        return JSONResponse(
            {"code": exc.status_code, "message": exc.detail, "request_id": request_id},
            status_code=exc.status_code,
        )
    return JSONResponse(
        {"detail": exc.detail, "request_id": request_id},
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", db.current_request_id())
    user = current_user(request)
    db.audit(user["id"] if user else None, "server_error", request.url.path, None, "failed", request_id)
    return JSONResponse(
        {"code": 500, "message": "服务器内部错误", "request_id": request_id},
        status_code=500,
    )


@app.on_event("startup")
def startup() -> None:
    db.init_db()


def current_user(request: Request) -> dict | None:
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
    payload = read_session(token or request.cookies.get("session"))
    if not payload:
        return None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, username, display_name, role, status FROM users WHERE id = ? AND username = ? AND status = 'active'",
                (payload.get("id"), payload.get("username")),
            ).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        if request.url.path.startswith("/api"):
            raise HTTPException(status_code=401, detail="请先登录")
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_role(*roles: str):
    def dependency(request: Request) -> dict:
        user = require_user(request)
        if user["role"] not in roles:
            db.audit(user["id"], "access_denied", request.url.path, None, "403")
            raise HTTPException(status_code=403, detail="无权限访问该页面")
        return user

    return dependency


def render(request: Request, name: str, context: dict | None = None) -> HTMLResponse:
    context = context or {}
    context.setdefault("user", current_user(request))
    context.setdefault("sms_demo_mode", SMS_PROVIDER == "local")
    return templates.TemplateResponse(request, name, context)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _read_document_file(path: Path, data: bytes) -> str:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _decode_text(data)
    if suffix == ".csv":
        text = _decode_text(data)
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames:
            rows = []
            for row in reader:
                rows.append("；".join(f"{key}：{value}" for key, value in row.items() if value))
            return "\n\n".join(rows)
        return text
    if suffix == ".json":
        try:
            data_obj = json.loads(_decode_text(data))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"JSON 解析失败：{exc}") from exc
        return _json_to_text(data_obj)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"PDF 解析失败：{exc}") from exc
    raise HTTPException(status_code=400, detail="暂时只支持 TXT、PDF、CSV、JSON 文件")


def _read_case_file(path: Path, data: bytes) -> str:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _decode_text(data)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"PDF 解析失败：{exc}") from exc
    raise HTTPException(status_code=400, detail="案情材料暂时只支持 TXT、PDF 文件")


def _json_to_text(value, prefix: str = "") -> str:
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            parts.append(_json_to_text(item, name))
        return "\n".join(part for part in parts if part)
    if isinstance(value, list):
        return "\n\n".join(_json_to_text(item, prefix) for item in value)
    if value is None:
        return ""
    return f"{prefix}：{value}" if prefix else str(value)


def _content_hash(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc) or exc.__class__.__name__


MAX_KB_IMPORT_FILES = 50


PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")


def _normalize_phone(value: str) -> str:
    return re.sub(r"[\s-]+", "", value or "")


def _require_phone(value: str) -> str:
    phone = _normalize_phone(value)
    if not PHONE_PATTERN.fullmatch(phone):
        raise HTTPException(status_code=400, detail="请输入有效的 11 位中国大陆手机号")
    return phone


def _verification_code_hash(phone: str, purpose: str, code: str) -> str:
    payload = f"{phone}:{purpose}:{code}".encode("utf-8")
    return hmac.new(SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _create_phone_verification_code(phone: str, purpose: str = "register") -> dict:
    now = datetime.now()
    cooldown_since = _timestamp(now - timedelta(seconds=SMS_CODE_COOLDOWN_SECONDS))
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="该手机号已注册，请直接登录")
        recent = conn.execute(
            """
            SELECT id FROM sms_verification_codes
            WHERE phone = ? AND purpose = ? AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (phone, purpose, cooldown_since),
        ).fetchone()
        if recent:
            raise HTTPException(status_code=429, detail=f"请在 {SMS_CODE_COOLDOWN_SECONDS} 秒后再获取验证码")
        if SMS_PROVIDER == "local":
            code = f"{secrets.randbelow(1_000_000):06d}"
            code_hash = _verification_code_hash(phone, purpose, code)
        elif SMS_PROVIDER == "aliyun_sms_verify":
            try:
                sms_verification.send_code(
                    phone,
                    code_length=6,
                    valid_time=SMS_CODE_TTL_SECONDS,
                    interval=SMS_CODE_COOLDOWN_SECONDS,
                )
            except sms_verification.SmsVerificationProviderError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            code_hash = f"aliyun_sms_verify:{secrets.token_hex(16)}"
        else:
            raise HTTPException(status_code=500, detail="未知的短信服务配置")
        expires_at = _timestamp(now + timedelta(seconds=SMS_CODE_TTL_SECONDS))
        conn.execute(
            """
            INSERT INTO sms_verification_codes(phone, purpose, code_hash, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (phone, purpose, code_hash, expires_at, _timestamp(now)),
        )
    payload = {
        "phone": phone,
        "purpose": purpose,
        "expires_in": SMS_CODE_TTL_SECONDS,
        "delivery": SMS_PROVIDER,
    }
    if SMS_PROVIDER == "local":
        payload["demo_code"] = code
    return payload


def _register_phone_user(phone: str, verification_code: str, password: str, display_name: str = "") -> dict:
    code = str(verification_code or "").strip()
    password = str(password or "")
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=400, detail="请输入 6 位验证码")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码长度不能少于 6 位")

    now = db.now()
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="该手机号已注册，请直接登录")
        verification = conn.execute(
            """
            SELECT * FROM sms_verification_codes
            WHERE phone = ? AND purpose = 'register' AND used_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (phone,),
        ).fetchone()
        if not verification:
            raise HTTPException(status_code=400, detail="请先获取验证码")
        if str(verification["expires_at"]) < now:
            raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
        stored_code_hash = str(verification["code_hash"])
        if stored_code_hash.startswith("aliyun_sms_verify:"):
            try:
                verified = sms_verification.verify_code(phone, code)
            except sms_verification.SmsVerificationProviderError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if not verified:
                raise HTTPException(status_code=400, detail="验证码错误或已失效")
        else:
            expected_hash = _verification_code_hash(phone, "register", code)
            if not hmac.compare_digest(expected_hash, stored_code_hash):
                raise HTTPException(status_code=400, detail="验证码错误")

        username = f"phone_{phone}"
        username_exists = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if username_exists:
            raise HTTPException(status_code=409, detail="该手机号对应账户已存在，请直接登录")
        clean_display_name = display_name.strip()[:80] or f"用户{phone[-4:]}"
        cur = conn.execute(
            """
            INSERT INTO users(username, phone, password_hash, display_name, role, status, created_at)
            VALUES (?, ?, ?, ?, 'user', 'active', ?)
            """,
            (username, phone, hash_password(password), clean_display_name, now),
        )
        conn.execute("UPDATE sms_verification_codes SET used_at = ? WHERE id = ?", (now, verification["id"]))
        user_id = cur.lastrowid
        row = conn.execute(
            "SELECT id, username, phone, display_name, role, status FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row)


def _login_user(account: str, password: str) -> dict | None:
    account = str(account or "").strip()
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM users
            WHERE (username = ? OR phone = ?) AND status = 'active'
            """,
            (account, _normalize_phone(account)),
        ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return None
        user = dict(row)
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.now(), user["id"]))
    return user


def _insert_chunks(conn, doc_id: int, chunks: list[str], metadata: dict) -> None:
    for index, chunk in enumerate(chunks, start=1):
        cur = conn.execute(
            """
            INSERT INTO document_chunks(document_id, chunk_no, content, section_name, paragraph_no, vector_id, metadata_json)
            VALUES (?, ?, ?, '正文', ?, ?, ?)
            """,
            (
                doc_id,
                index,
                chunk,
                index,
                f"doc-{doc_id}-chunk-{index}",
                json.dumps(metadata | {"chunk_no": index}, ensure_ascii=False),
            ),
        )
        chunk_id = cur.lastrowid
        vector_store.upsert_chunk(chunk_id, chunk, metadata | {"chunk_id": chunk_id, "document_id": doc_id, "status": "published"})


def _task_status(task_id: int, status: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE reasoning_tasks SET status = ?, updated_at = ? WHERE id = ?", (status, db.now(), task_id))


def _task_error_result(task_id: int, message: str) -> None:
    with db.connect() as conn:
        version = conn.execute("SELECT COUNT(*) AS c FROM reasoning_results WHERE task_id = ?", (task_id,)).fetchone()["c"] + 1
        conn.execute(
            """
            INSERT INTO reasoning_results(task_id, version_no, result_markdown, result_json, model_name, retrieval_config_json, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                version,
                f"## 推理失败\n{message}",
                json.dumps({"validation": {"passed": False, "warnings": [message]}}, ensure_ascii=False),
                db.get_config("chat_model", CHAT_MODEL_NAME),
                rag.retrieval_config(),
                0,
                db.now(),
            ),
        )


def _run_reasoning_job(task_id: int, user_id: int, request_id: str | None = None) -> None:
    try:
        with db.connect() as conn:
            task = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ?", (task_id, user_id)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="任务不存在")
            case_text = task["case_text"]
            facts = json.loads(task["extracted_facts_json"])
            filters = json.loads(task["filters_json"])

        _task_status(task_id, "running")
        _task_status(task_id, "retrieving")
        evidence = search.search(
            case_text,
            case_cause=filters.get("case_cause", ""),
            region=filters.get("region", ""),
            year=filters.get("year", ""),
        )
        case_evidence = search.search(
            case_text,
            case_cause=filters.get("case_cause", ""),
            region=filters.get("region", ""),
            year=filters.get("year", ""),
            document_type="case",
            search_mode="hybrid",
            limit=5,
        )
        case_metrics = rag.case_retrieval_metrics(facts.get("案由", ""), case_evidence)
        _task_status(task_id, "generating")
        result_markdown, latency_ms, validation = rag.generate_reasoning(case_text, facts, evidence, case_metrics)
        _task_status(task_id, "validating")
        with db.connect() as conn:
            version = conn.execute("SELECT COUNT(*) AS c FROM reasoning_results WHERE task_id = ?", (task_id,)).fetchone()["c"] + 1
            cur = conn.execute(
                """
                INSERT INTO reasoning_results(task_id, version_no, result_markdown, result_json, model_name, retrieval_config_json, latency_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    version,
                    result_markdown,
                    json.dumps({"facts": facts, "validation": validation}, ensure_ascii=False),
                    db.get_config("chat_model", CHAT_MODEL_NAME),
                    rag.retrieval_config(),
                    latency_ms,
                    db.now(),
                ),
            )
            result_id = cur.lastrowid
            for index, item in enumerate(evidence, start=1):
                conn.execute(
                    """
                    INSERT INTO citations(result_id, citation_no, document_id, chunk_id, quote_text, relevance_score, keyword_score, vector_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result_id,
                        index,
                        item["document_id"],
                        item["chunk_id"],
                        item["content"],
                        item["relevance_score"],
                        item["keyword_score"],
                        item["vector_score"],
                    ),
                )
            conn.execute("UPDATE reasoning_tasks SET status = 'completed', updated_at = ? WHERE id = ?", (db.now(), task_id))
        db.audit(user_id, "generate_reasoning", "reasoning_task", task_id, "success" if validation.get("passed") else "warning", request_id)
    except Exception as exc:
        detail = _error_detail(exc)
        _task_status(task_id, "failed")
        _task_error_result(task_id, detail)
        db.audit(user_id, "generate_reasoning", "reasoning_task", task_id, "failed", request_id)


def _api_ok(data=None, **extra):
    payload = {"code": 0, "message": "ok", "data": data}
    payload.update(extra)
    return payload


def _config_for_display(item: dict) -> dict:
    config = dict(item)
    if config.get("key") == "dashscope_api_key":
        config["value"] = "已配置" if config_store.api_key_configured() else "未配置"
        config["is_secret"] = True
    return config


def _parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_positive_int(value: str, default: int, minimum: int = 1, maximum: int = 50) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _parse_weight(value: str, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _quality_metrics(user_id: int | None = None) -> dict:
    filters = []
    params: list = []
    if user_id is not None:
        filters.append("t.user_id = ?")
        params.append(user_id)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with db.connect() as conn:
        rows = db.rows_to_dicts(
            conn.execute(
                f"""
                SELECT r.result_json
                FROM reasoning_results r
                JOIN reasoning_tasks t ON t.id = r.task_id
                {where}
                """,
                params,
            ).fetchall()
        )
    total_references = 0
    verified_references = 0
    hallucinated_references = 0
    recall_values: list[float] = []
    for row in rows:
        try:
            payload = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            continue
        validation = payload.get("validation") or {}
        audit = validation.get("reference_audit") or {}
        total_references += int(audit.get("total") or 0)
        verified_references += int(audit.get("verified") or 0)
        hallucinated_references += int(audit.get("unverified") or 0)
        metrics = validation.get("case_retrieval_metrics") or {}
        if metrics.get("top5_count"):
            recall_values.append(float(metrics.get("recall_at_5") or 0.0))
    return {
        "sample_size": len(rows),
        "law_case_reference_count": total_references,
        "citation_accuracy": round(verified_references / total_references * 100, 2) if total_references else None,
        "hallucination_rate": round(hallucinated_references / total_references * 100, 2) if total_references else None,
        "average_recall_at_5": round(sum(recall_values) / len(recall_values), 4) if recall_values else None,
        "targets": {
            "citation_accuracy": "> 95%",
            "hallucination_rate": "< 3%",
            "recall_at_5": "按同案由命中统计",
        },
    }


def _validity_fields(data: dict, fallback: dict | None = None) -> tuple[str, str, str]:
    fallback = fallback or {}
    status = str(data.get("validity_status") or fallback.get("validity_status") or "active").strip().lower()
    if status not in {"active", "revised", "repealed"}:
        status = "active"
    date = str(data.get("validity_date") or fallback.get("validity_date") or "").strip()
    note = str(data.get("validity_note") or fallback.get("validity_note") or "").strip()
    return status, date[:32], note[:1000]


def _case_compare_rows(case_ids: list[int]) -> list[dict]:
    if not case_ids:
        return []
    placeholders = ",".join("?" for _ in case_ids)
    with db.connect() as conn:
        rows = db.rows_to_dicts(
            conn.execute(
                f"""
                SELECT d.id, d.title, d.case_cause, d.region, d.year, d.source_name,
                       c.section_name, c.content, c.chunk_no
                FROM legal_documents d
                JOIN document_chunks c ON c.document_id = d.id
                WHERE d.id IN ({placeholders}) AND d.document_type = 'case' AND d.status = 'published'
                ORDER BY d.id, c.chunk_no
                """,
                case_ids,
            ).fetchall()
        )
    grouped: dict[int, dict] = {}
    for row in rows:
        item = grouped.setdefault(
            int(row["id"]),
            {
                "id": row["id"],
                "title": row["title"],
                "case_cause": row["case_cause"],
                "region": row["region"],
                "year": row["year"],
                "source_name": row["source_name"],
                "facts": "",
                "focus": "",
                "law": "",
                "result": "",
                "difference": "",
                "chunks": [],
            },
        )
        content = row["content"]
        item["chunks"].append(content)
        section = row["section_name"] or ""
        if "事实" in section and not item["facts"]:
            item["facts"] = content
        elif ("说理" in section or "条文" in section) and not item["law"]:
            item["law"] = content
        elif ("结果" in section or "裁判" in section) and not item["result"]:
            item["result"] = content
    for item in grouped.values():
        fallback = item["chunks"][0] if item["chunks"] else ""
        item["facts"] = item["facts"] or fallback
        item["focus"] = f"{item['case_cause']}中的事实认定、证据链和责任承担"
        item["law"] = item["law"] or fallback
        item["result"] = item["result"] or "知识库未单独标注裁判结果，可结合原文片段人工核对。"
        item["difference"] = f"地域/年份：{item['region']} {item['year']}；需与目标案情的证据完整性和履行情况对照。"
    return [grouped[case_id] for case_id in case_ids if case_id in grouped]


async def _request_data(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    form = await request.form()
    return {key: form.get(key) for key in form.keys()}


@app.get("/")
def index(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html", {"error": ""})


@app.post("/login")
async def login_with_request(request: Request):
    form = await request.form()
    account = str(form.get("account") or form.get("username") or "")
    password = str(form.get("password", ""))
    user = _login_user(account, password)
    if not user:
        db.audit(None, "login", "user", None, "failed")
        return render(request, "login.html", {"error": "用户名、手机号或密码错误"})
    db.audit(user["id"], "login", "user", user["id"], "success")
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("session", create_session(user), httponly=True, samesite="lax")
    return response


@app.get("/register")
def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "register.html", {"error": "", "demo_code": ""})


@app.post("/register")
async def register_with_request(request: Request):
    form = await request.form()
    try:
        user = _register_phone_user(
            _require_phone(str(form.get("phone") or "")),
            str(form.get("verification_code") or ""),
            str(form.get("password") or ""),
            str(form.get("display_name") or ""),
        )
    except HTTPException as exc:
        return render(request, "register.html", {"error": str(exc.detail), "demo_code": ""})
    db.audit(user["id"], "register_phone", "user", user["id"], "success")
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("session", create_session(user), httponly=True, samesite="lax")
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/dashboard")
def dashboard(request: Request, user: dict = Depends(require_user)):
    with db.connect() as conn:
        stats = {
            "tasks": conn.execute("SELECT COUNT(*) AS c FROM reasoning_tasks WHERE status != 'deleted'").fetchone()["c"],
            "docs": conn.execute("SELECT COUNT(*) AS c FROM legal_documents WHERE status = 'published'").fetchone()["c"],
            "feedback": conn.execute("SELECT COUNT(*) AS c FROM feedback").fetchone()["c"],
            "chunks": conn.execute("SELECT COUNT(*) AS c FROM document_chunks").fetchone()["c"],
        }
        recent = db.rows_to_dicts(
            conn.execute(
                """
                SELECT id, title, status, updated_at FROM reasoning_tasks
                WHERE user_id = ? AND status != 'deleted'
                ORDER BY updated_at DESC LIMIT 6
                """,
                (user["id"],),
            ).fetchall()
        )
    return render(request, "dashboard.html", {"stats": stats, "recent": recent})


@app.get("/search")
def search_page(
    request: Request,
    q: str = "",
    mode: str = "hybrid",
    case_cause: str = "全部",
    region: str = "全部",
    year: str = "全部",
    document_type: str = "全部",
    user: dict = Depends(require_user),
):
    results = []
    mode = search.normalize_search_mode(mode)
    if q.strip():
        results = search.search(
            q.strip(),
            case_cause=case_cause,
            region=region,
            year=year,
            document_type=document_type,
            search_mode=mode,
        )
        db.audit(user["id"], "search", "legal_document", None, "success")
    return render(
        request,
        "search.html",
        {
            "q": q,
            "mode": mode,
            "case_cause": case_cause,
            "region": region,
            "year": year,
            "document_type": document_type,
            "results": results,
        },
    )


@app.get("/api/health")
def api_health():
    with db.connect() as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        docs = conn.execute("SELECT COUNT(*) AS c FROM legal_documents WHERE status = 'published'").fetchone()["c"]
        chunks = conn.execute("SELECT COUNT(*) AS c FROM document_chunks").fetchone()["c"]
    return {
        "status": "ok",
        "database": "connected",
        "db_type": DB_TYPE,
        "chroma": "connected",
        "chroma_vectors": vector_store.count(),
        "users": users,
        "documents": docs,
        "chunks": chunks,
    }


@app.post("/api/login")
async def api_login(request: Request):
    data = await _request_data(request)
    account = str(data.get("account") or data.get("username") or data.get("phone") or "")
    password = str(data.get("password", ""))
    user = _login_user(account, password)
    if not user:
        db.audit(None, "api_login", "user", None, "failed")
        raise HTTPException(status_code=401, detail="用户名、手机号或密码错误")
    db.audit(user["id"], "api_login", "user", user["id"], "success")
    session_token = create_session(user)
    response = JSONResponse(
        _api_ok(
            {
                "id": user["id"],
                "username": user["username"],
                "phone": user.get("phone") or "",
                "display_name": user["display_name"],
                "role": user["role"],
                "session_token": session_token,
            }
        )
    )
    response.set_cookie("session", session_token, httponly=True, samesite="lax")
    return response


@app.post("/api/auth/send-code")
async def api_send_verification_code(request: Request):
    data = await _request_data(request)
    purpose = str(data.get("purpose") or "register").strip()
    if purpose != "register":
        raise HTTPException(status_code=400, detail="暂不支持该验证码用途")
    phone = _require_phone(str(data.get("phone") or ""))
    result = _create_phone_verification_code(phone, purpose)
    db.audit(None, "send_verification_code", "phone", None, "success")
    return _api_ok(result)


@app.post("/api/register")
async def api_register(request: Request):
    data = await _request_data(request)
    user = _register_phone_user(
        _require_phone(str(data.get("phone") or "")),
        str(data.get("verification_code") or data.get("code") or ""),
        str(data.get("password") or ""),
        str(data.get("display_name") or ""),
    )
    db.audit(user["id"], "api_register_phone", "user", user["id"], "success")
    session_token = create_session(user)
    response = JSONResponse(
        _api_ok(
            {
                "id": user["id"],
                "username": user["username"],
                "phone": user["phone"],
                "display_name": user["display_name"],
                "role": user["role"],
                "session_token": session_token,
            }
        )
    )
    response.set_cookie("session", session_token, httponly=True, samesite="lax")
    return response


@app.get("/api/search")
def api_search(
    q: str,
    mode: str = "hybrid",
    case_cause: str = "全部",
    region: str = "全部",
    year: str = "全部",
    document_type: str = "全部",
    user: dict = Depends(require_user),
):
    results = search.search(
            q,
            case_cause=case_cause,
            region=region,
            year=year,
            document_type=document_type,
            search_mode=mode,
        )
    db.audit(user["id"], "api_search", "legal_document", None, "success")
    return _api_ok(results)


@app.post("/api/assistant")
async def api_assistant(request: Request, user: dict = Depends(require_user)):
    data = await _request_data(request)
    question = str(data.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="请输入想咨询的法律问题")
    if len(question) > 3000:
        raise HTTPException(status_code=400, detail="问题内容不能超过 3000 个字符")

    history: list[dict[str, str]] = []
    raw_history = data.get("history")
    if isinstance(raw_history, list):
        for item in raw_history[-6:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role in {"用户", "助手"} and content:
                history.append({"role": role, "content": content[:1200]})

    retrieved_evidence = search.search(
        question,
        case_cause=str(data.get("case_cause") or "全部"),
        region=str(data.get("region") or "全部"),
        year=str(data.get("year") or "全部"),
        document_type=str(data.get("document_type") or "全部"),
        search_mode="hybrid",
        limit=4,
    )
    evidence, retrieval_warnings = rag.select_assistant_evidence(retrieved_evidence)
    answer, latency_ms, diagnostics = rag.generate_assistant_reply(question, evidence, history)
    sources = [
        {
            "document_id": item["document_id"],
            "title": item["title"],
            "source_name": item["source_name"],
            "document_type": item["document_type"],
            "relevance_score": item["relevance_score"],
            "snippet": item["snippet"],
            "validity_status": item.get("validity_status") or "active",
            "validity_date": item.get("validity_date") or "",
            "validity_note": item.get("validity_note") or "",
        }
        for item in evidence
    ]
    db.audit(user["id"], "assistant_answer", "legal_document", None, "success")
    return _api_ok(
        {
            "answer": answer,
            "sources": sources,
            "latency_ms": latency_ms,
            "warnings": list(dict.fromkeys(retrieval_warnings + diagnostics["warnings"])),
            "reference_audit": diagnostics["question_reference_audit"],
            "answer_reference_audit": diagnostics["reference_audit"],
        }
    )


@app.get("/api/tasks")
def api_tasks(user: dict = Depends(require_user)):
    with db.connect() as conn:
        tasks = db.rows_to_dicts(
            conn.execute(
                """
                SELECT id, title, status, created_at, updated_at
                FROM reasoning_tasks
                WHERE user_id = ? AND status != 'deleted'
                ORDER BY updated_at DESC
                """,
                (user["id"],),
            ).fetchall()
        )
    return _api_ok(tasks)


@app.post("/api/tasks")
async def api_create_task(request: Request, user: dict = Depends(require_user)):
    data = await _request_data(request)
    case_text = str(data.get("case_text", "")).strip()
    if not case_text:
        raise HTTPException(status_code=400, detail="case_text 不能为空")
    facts = rag.extract_facts(case_text)
    filters = {
        "case_cause": str(data.get("case_cause") or facts["案由"]),
        "region": str(data.get("region") or "全部"),
        "year": str(data.get("year") or "全部"),
    }
    title = str(data.get("title") or f"{facts['案由']}辅助推理")
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO reasoning_tasks(user_id, title, case_text, extracted_facts_json, filters_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (user["id"], title, case_text, json.dumps(facts, ensure_ascii=False), json.dumps(filters, ensure_ascii=False), db.now(), db.now()),
        )
        task_id = cur.lastrowid
    db.audit(user["id"], "api_create_task", "reasoning_task", task_id, "success")
    return _api_ok({"task_id": task_id})


@app.get("/api/tasks/{task_id}")
def api_task_detail(task_id: int, user: dict = Depends(require_user)):
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ? AND status != 'deleted'", (task_id, user["id"])).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        result = conn.execute("SELECT * FROM reasoning_results WHERE task_id = ? ORDER BY version_no DESC LIMIT 1", (task_id,)).fetchone()
        citations = []
        if result:
            citations = db.rows_to_dicts(
                conn.execute(
                    """
                    SELECT c.*, d.title, d.document_type, d.case_cause, d.region, d.year, d.source_name
                    FROM citations c
                    JOIN legal_documents d ON d.id = c.document_id
                    WHERE c.result_id = ?
                    ORDER BY c.citation_no
                    """,
                    (result["id"],),
                ).fetchall()
            )
    return _api_ok({"task": dict(task), "result": dict(result) if result else None, "citations": citations})


@app.get("/api/evaluations/similar-cases")
def api_similar_case_evaluation(
    q: str,
    case_cause: str = "全部",
    user: dict = Depends(require_user),
):
    expected_cause = case_cause if case_cause != "全部" else rag.extract_facts(q)["案由"]
    candidates = search.search(q, document_type="case", search_mode="hybrid", limit=5)
    metrics = rag.case_retrieval_metrics(expected_cause, candidates)
    db.audit(user["id"], "evaluate_similar_cases", "legal_document", None, "success")
    return _api_ok(metrics)


@app.get("/api/evaluations/metrics")
def api_quality_metrics(user: dict = Depends(require_role("sys_admin"))):
    return _api_ok(_quality_metrics())


@app.post("/api/tasks/{task_id}/reason")
async def api_start_reasoning(
    task_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_user),
):
    data = await _request_data(request)
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ? AND status != 'deleted'", (task_id, user["id"])).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
    old_facts = json.loads(task["extracted_facts_json"])
    old_filters = json.loads(task["filters_json"])
    case_text = str(data.get("case_text") or task["case_text"]).strip()
    if not case_text:
        raise HTTPException(status_code=400, detail="案情正文不能为空")
    facts = old_facts | rag.extract_facts(case_text)
    filters = {
        "case_cause": str(data.get("case_cause") or old_filters.get("case_cause") or facts["案由"]),
        "region": str(data.get("region") or old_filters.get("region") or "全部"),
        "year": str(data.get("year") or old_filters.get("year") or "全部"),
    }
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE reasoning_tasks
            SET title = ?, case_text = ?, extracted_facts_json = ?, filters_json = ?, status = 'queued', updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                str(data.get("title") or task["title"] or f"{facts['案由']}辅助推理"),
                case_text,
                json.dumps(facts, ensure_ascii=False),
                json.dumps(filters, ensure_ascii=False),
                db.now(),
                task_id,
                user["id"],
            ),
        )
    background_tasks.add_task(_run_reasoning_job, task_id, user["id"], getattr(request.state, "request_id", None))
    db.audit(user["id"], "api_queue_reasoning", "reasoning_task", task_id, "success")
    return _api_ok({"task_id": task_id, "status": "queued"})


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: int, user: dict = Depends(require_user)):
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM reasoning_tasks WHERE id = ? AND user_id = ? AND status != 'deleted'", (task_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        conn.execute("UPDATE reasoning_tasks SET status = 'deleted', updated_at = ? WHERE id = ?", (db.now(), task_id))
    db.audit(user["id"], "api_delete_task", "reasoning_task", task_id, "success")
    return _api_ok({"task_id": task_id, "status": "deleted"})


@app.get("/api/tasks/{task_id}/export")
def api_export_task(task_id: int, user: dict = Depends(require_user)):
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ? AND status != 'deleted'", (task_id, user["id"])).fetchone()
        result = conn.execute("SELECT * FROM reasoning_results WHERE task_id = ? ORDER BY version_no DESC LIMIT 1", (task_id,)).fetchone()
        if not task or not result:
            raise HTTPException(status_code=404, detail="报告不存在")
    markdown = f"# {task['title']}\n\n{result['result_markdown']}\n\n> 本报告仅供教学辅助分析，不构成法律意见。\n"
    db.audit(user["id"], "api_export_report", "reasoning_task", task_id, "success")
    return _api_ok({"filename": f"task-{task_id}-report.md", "markdown": markdown})


@app.get("/api/kb/documents")
def api_kb_documents(user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    with db.connect() as conn:
        docs = db.rows_to_dicts(
            conn.execute(
                """
                SELECT d.id, d.title, d.document_type, d.case_cause, d.region, d.year,
                       d.source_name, d.validity_status, d.validity_date, d.validity_note,
                       d.status, COUNT(c.id) AS chunk_count
                FROM legal_documents d
                LEFT JOIN document_chunks c ON c.document_id = d.id
                WHERE d.status != 'deleted'
                GROUP BY d.id, d.title, d.document_type, d.case_cause, d.region, d.year,
                         d.source_name, d.validity_status, d.validity_date, d.validity_note, d.status
                ORDER BY d.updated_at DESC
                """
            ).fetchall()
        )
    return _api_ok(docs)


@app.get("/api/kb/documents/{doc_id}")
def api_kb_document_detail(doc_id: int, user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    with db.connect() as conn:
        document = conn.execute("SELECT * FROM legal_documents WHERE id = ?", (doc_id,)).fetchone()
        if not document:
            raise HTTPException(status_code=404, detail="文档不存在")
        chunks = db.rows_to_dicts(
            conn.execute(
                """
                SELECT id, chunk_no, content, section_name, paragraph_no
                FROM document_chunks
                WHERE document_id = ?
                ORDER BY chunk_no
                """,
                (doc_id,),
            ).fetchall()
        )
    return _api_ok({"document": dict(document), "chunks": chunks})


@app.post("/api/kb/documents")
async def api_create_kb_document(request: Request, user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    data = await _request_data(request)
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="title 和 content 不能为空")
    document_type = str(data.get("document_type") or "case")
    case_cause = str(data.get("case_cause") or "合同纠纷")
    region = str(data.get("region") or "全国")
    year = _parse_positive_int(str(data.get("year") or "2026"), 2026, 1900, 2100)
    validity_status, validity_date, validity_note = _validity_fields(data)
    file_hash = _content_hash(content)
    job_id = db.create_import_job("api_content", file_hash, "deduplicating", "running", user["id"])
    with db.connect() as conn:
        duplicate = conn.execute("SELECT id, title FROM legal_documents WHERE file_hash = ? LIMIT 1", (file_hash,)).fetchone()
    if duplicate:
        message = f"重复导入，已存在文档：{duplicate['title']}（ID {duplicate['id']}）"
        db.update_import_job(job_id, stage="duplicate", status="skipped", error_summary=message)
        db.audit(user["id"], "api_import_kb", "legal_document", duplicate["id"], "skipped")
        return _api_ok({"document_id": duplicate["id"], "job_id": job_id, "duplicated": True})
    chunks = search.chunk_text(content)
    if not chunks:
        db.update_import_job(job_id, stage="chunked", status="failed", error_summary="知识库正文不能为空")
        raise HTTPException(status_code=400, detail="知识库正文不能为空")
    metadata = {
        "title": title,
        "document_type": document_type,
        "case_cause": case_cause,
        "region": region,
        "year": year,
        "validity_status": validity_status,
    }
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO legal_documents(title, document_type, case_cause, region, year, source_name, validity_status, validity_date, validity_note, file_hash, status, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'api_content', ?, ?, ?, ?, 'published', ?, ?, ?)
            """,
            (title, document_type, case_cause, region, year, validity_status, validity_date, validity_note, file_hash, user["id"], db.now(), db.now()),
        )
        doc_id = cur.lastrowid
        _insert_chunks(conn, doc_id, chunks, metadata)
    db.update_import_job(job_id, stage="published", status="success", total_chunks=len(chunks), error_summary="")
    db.audit(user["id"], "api_import_kb", "legal_document", doc_id, "success")
    return _api_ok({"document_id": doc_id, "job_id": job_id, "chunk_count": len(chunks)})


@app.put("/api/kb/documents/{doc_id}")
async def api_update_kb_document(doc_id: int, request: Request, user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    data = await _request_data(request)
    content = str(data.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content 不能为空")
    chunks = search.chunk_text(content)
    if not chunks:
        raise HTTPException(status_code=400, detail="知识库正文不能为空")
    with db.connect() as conn:
        doc = conn.execute("SELECT * FROM legal_documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        title = str(data.get("title") or doc["title"])
        document_type = str(data.get("document_type") or doc["document_type"])
        case_cause = str(data.get("case_cause") or doc["case_cause"] or "合同纠纷")
        region = str(data.get("region") or doc["region"] or "全国")
        year = _parse_positive_int(str(data.get("year") or doc["year"] or "2026"), 2026, 1900, 2100)
        validity_status, validity_date, validity_note = _validity_fields(data, dict(doc))
        file_hash = _content_hash(content)
        duplicate = conn.execute("SELECT id FROM legal_documents WHERE file_hash = ? AND id != ? LIMIT 1", (file_hash, doc_id)).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail=f"内容与文档 {duplicate['id']} 重复")
        conn.execute(
            """
            UPDATE legal_documents
            SET title = ?, document_type = ?, case_cause = ?, region = ?, year = ?, validity_status = ?,
                validity_date = ?, validity_note = ?, file_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, document_type, case_cause, region, year, validity_status, validity_date, validity_note, file_hash, db.now(), doc_id),
        )
        vector_store.delete_document(doc_id)
        conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (doc_id,))
        _insert_chunks(
            conn,
            doc_id,
            chunks,
            {
                "title": title,
                "document_type": document_type,
                "case_cause": case_cause,
                "region": region,
                "year": year,
                "validity_status": validity_status,
            },
        )
    db.audit(user["id"], "api_update_kb", "legal_document", doc_id, "success")
    return _api_ok({"document_id": doc_id, "chunk_count": len(chunks)})


@app.delete("/api/kb/documents/{doc_id}")
def api_delete_kb_document(doc_id: int, user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM legal_documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文档不存在")
        vector_store.delete_document(doc_id)
        # 保留历史引用所需的文档和分块，使用软删除避免外键破坏历史推理结果。
        conn.execute("UPDATE legal_documents SET status = 'deleted', updated_at = ? WHERE id = ?", (db.now(), doc_id))
    db.audit(user["id"], "api_delete_kb", "legal_document", doc_id, "success")
    return _api_ok({"document_id": doc_id, "status": "deleted"})


@app.post("/api/feedback")
async def api_feedback(request: Request, user: dict = Depends(require_user)):
    data = await _request_data(request)
    task_id = _parse_positive_int(str(data.get("task_id", "0")), 0)
    result_id = _parse_positive_int(str(data.get("result_id", "0")), 0)
    rating = str(data.get("rating") or "").strip()
    if rating not in {"helpful", "unhelpful"}:
        raise HTTPException(status_code=400, detail="rating 必须是 helpful 或 unhelpful")
    with db.connect() as conn:
        task = conn.execute("SELECT id FROM reasoning_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
        result = conn.execute("SELECT id FROM reasoning_results WHERE id = ? AND task_id = ?", (result_id, task_id)).fetchone()
        if not task or not result:
            raise HTTPException(status_code=404, detail="任务或结果不存在")
        cur = conn.execute(
            """
            INSERT INTO feedback(user_id, task_id, result_id, rating, reason, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], task_id, result_id, rating, str(data.get("reason") or ""), str(data.get("comment") or ""), db.now()),
        )
        feedback_id = cur.lastrowid
    db.audit(user["id"], "api_submit_feedback", "reasoning_task", task_id, "success")
    return _api_ok({"feedback_id": feedback_id})


@app.get("/api/configs")
def api_configs(user: dict = Depends(require_role("sys_admin"))):
    with db.connect() as conn:
        configs = [_config_for_display(item) for item in db.rows_to_dicts(conn.execute("SELECT * FROM system_configs ORDER BY `key`").fetchall())]
    return _api_ok(configs)


@app.post("/api/configs")
async def api_update_configs(request: Request, user: dict = Depends(require_role("sys_admin"))):
    data = await _request_data(request)
    allowed = {
        "chat_model",
        "embedding_model",
        "use_llm",
        "use_dashscope_embedding",
        "dashscope_api_key",
        "dashscope_base_http_api_url",
        "dashscope_timeout",
        "top_k",
        "keyword_weight",
        "vector_weight",
    }
    updated: dict[str, str] = {}
    for key in allowed:
        if key not in data:
            continue
        value = str(data[key]).strip()
        if key == "top_k":
            value = str(_parse_positive_int(value, 6, 1, 20))
        elif key in {"keyword_weight", "vector_weight"}:
            value = f"{_parse_weight(value, db.get_float_config(key, 0.5)):.2f}"
        elif key in {"use_llm", "use_dashscope_embedding"}:
            value = "true" if _parse_bool(value) else "false"
        elif key == "dashscope_timeout":
            value = str(_parse_positive_int(value, 30, 5, 120))
        elif key == "dashscope_api_key":
            if _parse_bool(data.get("clear_dashscope_api_key")):
                value = ""
            elif value:
                value = config_store.encrypt_secret(value)
            else:
                continue
        elif not value:
            continue
        db.update_config(key, value, user["id"])
        updated[key] = "已配置" if key == "dashscope_api_key" and value else ("未配置" if key == "dashscope_api_key" else value)
    if _parse_bool(data.get("clear_dashscope_api_key")):
        db.update_config("dashscope_api_key", "", user["id"])
        updated["dashscope_api_key"] = "未配置"
    db.audit(user["id"], "api_update_config", "system_config", None, "success")
    return _api_ok(updated)


@app.get("/api/documents/{doc_id}")
def api_document_detail(doc_id: int, user: dict = Depends(require_user)):
    with db.connect() as conn:
        document = conn.execute(
            """
            SELECT id, title, document_type, case_cause, region, year, source_name, validity_status,
                   validity_date, validity_note, status, created_at, updated_at
            FROM legal_documents
            WHERE id = ? AND status = 'published'
            """,
            (doc_id,),
        ).fetchone()
        if not document:
            raise HTTPException(status_code=404, detail="文档不存在或已下线")
        chunks = db.rows_to_dicts(
            conn.execute(
                """
                SELECT id, chunk_no, content, section_name, paragraph_no
                FROM document_chunks
                WHERE document_id = ?
                ORDER BY chunk_no
                """,
                (doc_id,),
            ).fetchall()
        )
    db.audit(user["id"], "api_view_document", "legal_document", doc_id, "success")
    return _api_ok({"document": dict(document), "chunks": chunks})


@app.get("/api/compare")
def api_compare(
    user: dict = Depends(require_user),
    task_id: int | None = None,
    case_ids: str = "",
):
    task = None
    query_text = ""
    selected_ids = [_parse_positive_int(item, 0) for item in case_ids.split(",") if item.strip()]
    selected_ids = [item for item in selected_ids if item > 0][:5]
    if task_id:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="任务不存在")
            task = dict(row)
            query_text = task["case_text"]

    candidates = search.search(query_text or "合同 违约 借贷 劳动", document_type="case", search_mode="hybrid", limit=8)
    seen: set[int] = set()
    candidate_cases = []
    for item in candidates:
        doc_id = int(item["document_id"])
        if doc_id in seen:
            continue
        seen.add(doc_id)
        candidate_cases.append(item)
    if not selected_ids:
        selected_ids = [int(item["document_id"]) for item in candidate_cases[:5]]
    if len(selected_ids) == 1:
        selected_ids = [int(item["document_id"]) for item in candidate_cases[:2]]
    selected_ids = selected_ids[:5]
    cases = _case_compare_rows(selected_ids)
    db.audit(user["id"], "api_compare_cases", "reasoning_task", task_id, "success")
    return _api_ok(
        {
            "task": task,
            "candidate_cases": candidate_cases,
            "selected_ids": selected_ids,
            "cases": cases,
        }
    )


@app.get("/documents/{doc_id}")
def document_detail_page(doc_id: int, request: Request, user: dict = Depends(require_user)):
    with db.connect() as conn:
        doc = conn.execute("SELECT * FROM legal_documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        chunks = db.rows_to_dicts(
            conn.execute(
                """
                SELECT id, chunk_no, content, section_name, paragraph_no
                FROM document_chunks
                WHERE document_id = ?
                ORDER BY chunk_no
                """,
                (doc_id,),
            ).fetchall()
        )
        citation_count = conn.execute("SELECT COUNT(*) AS c FROM citations WHERE document_id = ?", (doc_id,)).fetchone()["c"]
    db.audit(user["id"], "view_document", "legal_document", doc_id, "success")
    return render(
        request,
        "document_detail.html",
        {"doc": dict(doc), "chunks": chunks, "citation_count": citation_count},
    )


@app.get("/cases/new")
def new_case_page(request: Request, user: dict = Depends(require_user)):
    return render(request, "case_new.html")


@app.post("/cases/confirm")
async def confirm_case(
    request: Request,
    case_text: str = Form(""),
    case_file: UploadFile | None = File(None),
    case_cause: str = Form("全部"),
    region: str = Form("全部"),
    year: str = Form("全部"),
    user: dict = Depends(require_user),
):
    case_text = (case_text or "").strip()
    if case_file and case_file.filename:
        safe_name = Path(case_file.filename).name
        data = await case_file.read()
        if data:
            upload_path = UPLOAD_DIR / f"case-{db.now().replace('-', '').replace(':', '').replace(' ', '')}-{safe_name}"
            upload_path.write_bytes(data)
            uploaded_text = _read_case_file(upload_path, data).strip()
            case_text = f"{case_text}\n\n{uploaded_text}".strip() if case_text else uploaded_text
    if not case_text:
        raise HTTPException(status_code=400, detail="请粘贴案情文本，或上传 TXT/PDF 案情材料")

    facts = rag.extract_facts(case_text)
    filters = {"case_cause": case_cause if case_cause != "全部" else facts["案由"], "region": region, "year": year}
    title = f"{facts['案由']}辅助推理"
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO reasoning_tasks(user_id, title, case_text, extracted_facts_json, filters_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (user["id"], title, case_text, json.dumps(facts, ensure_ascii=False), json.dumps(filters, ensure_ascii=False), db.now(), db.now()),
        )
        task_id = cur.lastrowid
    db.audit(user["id"], "create_task", "reasoning_task", task_id, "success")
    return render(request, "case_confirm.html", {"task_id": task_id, "case_text": case_text, "facts": facts, "filters": filters})


@app.post("/tasks/{task_id}/reason")
def reason(
    task_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    agreed: str = Form(""),
    case_text: str = Form(""),
    fact_cause: str = Form(""),
    fact_parties: str = Form(""),
    fact_core: str = Form(""),
    fact_claim: str = Form(""),
    fact_evidence: str = Form(""),
    fact_focus: str = Form(""),
    fact_sensitive: str = Form(""),
    case_cause: str = Form(""),
    region: str = Form("全部"),
    year: str = Form("全部"),
    user: dict = Depends(require_user),
):
    if agreed != "yes":
        return RedirectResponse("/cases/new", status_code=303)
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        old_facts = json.loads(task["extracted_facts_json"])
        old_filters = json.loads(task["filters_json"])

    case_text = (case_text or task["case_text"]).strip()
    if not case_text:
        raise HTTPException(status_code=400, detail="案情正文不能为空")
    facts = {
        "案由": fact_cause.strip() or old_facts.get("案由", "合同纠纷"),
        "主体": fact_parties.strip() or old_facts.get("主体", ""),
        "核心事实": fact_core.strip() or old_facts.get("核心事实", ""),
        "诉求": fact_claim.strip() or old_facts.get("诉求", ""),
        "证据": fact_evidence.strip() or old_facts.get("证据", ""),
        "争议焦点": fact_focus.strip() or old_facts.get("争议焦点", ""),
        "疑似敏感信息": [item.strip() for item in re.split(r"[、,，\n]+", fact_sensitive) if item.strip()]
        or old_facts.get("疑似敏感信息", []),
    }
    filters = {
        "case_cause": case_cause.strip() or old_filters.get("case_cause", facts["案由"]),
        "region": region or old_filters.get("region", "全部"),
        "year": year or old_filters.get("year", "全部"),
    }
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE reasoning_tasks
            SET title = ?, case_text = ?, extracted_facts_json = ?, filters_json = ?, status = 'queued', updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                f"{facts['案由']}辅助推理",
                case_text,
                json.dumps(facts, ensure_ascii=False),
                json.dumps(filters, ensure_ascii=False),
                db.now(),
                task_id,
                user["id"],
            ),
        )
    background_tasks.add_task(_run_reasoning_job, task_id, user["id"], getattr(request.state, "request_id", None))
    db.audit(user["id"], "queue_reasoning", "reasoning_task", task_id, "success")
    return RedirectResponse(f"/tasks/{task_id}/result", status_code=303)


@app.get("/tasks/{task_id}/result")
def result_page(task_id: int, request: Request, user: dict = Depends(require_user)):
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        result = conn.execute("SELECT * FROM reasoning_results WHERE task_id = ? ORDER BY version_no DESC LIMIT 1", (task_id,)).fetchone()
        if not result:
            return render(request, "pending_result.html", {"task": dict(task)})
        citations = db.rows_to_dicts(
            conn.execute(
                """
                SELECT c.*, d.title, d.document_type, d.case_cause, d.region, d.year, d.source_name
                FROM citations c JOIN legal_documents d ON d.id = c.document_id
                WHERE c.result_id = ? ORDER BY c.citation_no
                """,
                (result["id"],),
            ).fetchall()
        )
    result_data = dict(result)
    try:
        result_payload = json.loads(result_data.get("result_json") or "{}")
    except json.JSONDecodeError:
        result_payload = {}
    return render(
        request,
        "result.html",
        {
            "task": dict(task),
            "result": result_data,
            "citations": citations,
            "validation": result_payload.get("validation", {"passed": True, "warnings": []}),
        },
    )


@app.get("/citations/{citation_id}")
def citation_page(citation_id: int, request: Request, user: dict = Depends(require_user)):
    with db.connect() as conn:
        citation = conn.execute(
            """
            SELECT c.*, d.title, d.document_type, d.case_cause, d.region, d.year,
                   d.source_name, d.validity_status, d.validity_date, d.validity_note,
                   ch.section_name, ch.paragraph_no
            FROM citations c
            JOIN legal_documents d ON d.id = c.document_id
            JOIN document_chunks ch ON ch.id = c.chunk_id
            JOIN reasoning_results r ON r.id = c.result_id
            JOIN reasoning_tasks t ON t.id = r.task_id
            WHERE c.id = ? AND t.user_id = ?
            """,
            (citation_id, user["id"]),
        ).fetchone()
        if not citation:
            raise HTTPException(status_code=404, detail="引用不存在")
    return render(request, "citation.html", {"citation": dict(citation)})


@app.get("/history")
def history(request: Request, user: dict = Depends(require_user), q: str = "", status: str = "全部"):
    filters = ["user_id = ?", "status != 'deleted'"]
    params: list = [user["id"]]
    if q:
        filters.append("title LIKE ?")
        params.append(f"%{q}%")
    if status != "全部":
        filters.append("status = ?")
        params.append(status)
    with db.connect() as conn:
        tasks = db.rows_to_dicts(
            conn.execute(
                f"SELECT * FROM reasoning_tasks WHERE {' AND '.join(filters)} ORDER BY updated_at DESC",
                params,
            ).fetchall()
        )
    return render(request, "history.html", {"tasks": tasks, "q": q, "status": status})


@app.post("/tasks/{task_id}/delete")
def delete_task(task_id: int, user: dict = Depends(require_user)):
    with db.connect() as conn:
        conn.execute("UPDATE reasoning_tasks SET status = 'deleted', updated_at = ? WHERE id = ? AND user_id = ?", (db.now(), task_id, user["id"]))
    db.audit(user["id"], "delete_task", "reasoning_task", task_id, "success")
    return RedirectResponse("/history", status_code=303)


@app.get("/tasks/{task_id}/export")
def export_task(task_id: int, user: dict = Depends(require_user)):
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
        result = conn.execute("SELECT * FROM reasoning_results WHERE task_id = ? ORDER BY version_no DESC LIMIT 1", (task_id,)).fetchone()
        if not task or not result:
            raise HTTPException(status_code=404, detail="报告不存在")
    path = EXPORT_DIR / f"task-{task_id}-report.md"
    path.write_text(f"# {task['title']}\n\n{result['result_markdown']}\n\n> 本报告仅供教学辅助分析，不构成法律意见。\n", encoding="utf-8")
    db.audit(user["id"], "export_report", "reasoning_task", task_id, "success")
    return FileResponse(path, filename=path.name, media_type="text/markdown")


@app.get("/compare")
def compare_page(request: Request, user: dict = Depends(require_user), task_id: int | None = None, case_ids: str = ""):
    task = None
    query_text = ""
    selected_ids = [_parse_positive_int(item, 0) for item in case_ids.split(",") if item.strip()]
    selected_ids = [item for item in selected_ids if item > 0][:5]
    if task_id:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM reasoning_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="任务不存在")
            task = dict(row)
            query_text = task["case_text"]
    candidates = search.search(query_text or "合同 违约 借贷 劳动", document_type="case", search_mode="hybrid", limit=8)
    seen: set[int] = set()
    candidate_cases = []
    for item in candidates:
        doc_id = int(item["document_id"])
        if doc_id in seen:
            continue
        seen.add(doc_id)
        candidate_cases.append(item)
    if not selected_ids:
        selected_ids = [int(item["document_id"]) for item in candidate_cases[:5]]
    if len(selected_ids) == 1:
        selected_ids = [int(item["document_id"]) for item in candidate_cases[:2]]
    selected_ids = selected_ids[:5]
    cases = _case_compare_rows(selected_ids)
    db.audit(user["id"], "compare_cases", "reasoning_task", task_id, "success")
    return render(
        request,
        "compare.html",
        {
            "task": task,
            "task_id": task_id,
            "candidate_cases": candidate_cases,
            "selected_ids": selected_ids,
            "cases": cases,
        },
    )


@app.get("/kb")
def kb_page(request: Request, user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    with db.connect() as conn:
        docs = db.rows_to_dicts(
            conn.execute(
                """
                SELECT d.id, d.title, d.document_type, d.case_cause, d.region, d.year,
                       d.source_name, d.validity_status, d.validity_date, d.validity_note,
                       d.status, d.created_at, d.updated_at,
                       COUNT(c.id) AS chunk_count
                FROM legal_documents d LEFT JOIN document_chunks c ON c.document_id = d.id
                GROUP BY d.id, d.title, d.document_type, d.case_cause, d.region, d.year,
                         d.source_name, d.validity_status, d.validity_date, d.validity_note,
                         d.status, d.created_at, d.updated_at
                ORDER BY d.updated_at DESC
                """
            ).fetchall()
        )
        import_jobs = db.rows_to_dicts(
            conn.execute(
                """
                SELECT j.*, u.username
                FROM import_jobs j
                LEFT JOIN users u ON u.id = j.created_by
                ORDER BY j.updated_at DESC
                LIMIT 8
                """
            ).fetchall()
        )
    return render(request, "kb.html", {"docs": docs, "import_jobs": import_jobs})


async def _import_single_kb_document(
    *,
    title: str,
    document_type: str,
    case_cause: str,
    region: str,
    year: int,
    validity_status: str,
    validity_date: str,
    validity_note: str,
    content: str,
    file: UploadFile | None,
    user: dict,
) -> dict:
    """Import one document and keep its job state independent from other files."""
    source_name = "manual_input"
    stage = "received"
    job_id: int | None = None
    doc_id: int | None = None
    file_hash: str | None = None
    try:
        if file and file.filename:
            safe_name = Path(file.filename).name
            data = await file.read()
            source_name = safe_name
            file_hash = hashlib.sha256(data).hexdigest()
            job_id = db.create_import_job(source_name, file_hash, "uploaded", "running", user["id"])
            if not data:
                raise HTTPException(status_code=400, detail="上传文件不能为空")
            stage = "stored"
            upload_path = UPLOAD_DIR / f"{uuid.uuid4().hex}-{safe_name}"
            upload_path.write_bytes(data)
            db.update_import_job(job_id, stage=stage)
            stage = "parsed"
            content = _read_document_file(upload_path, data)
        else:
            file_hash = _content_hash(content)
            job_id = db.create_import_job(source_name, file_hash, stage, "running", user["id"])
            stage = "parsed"

        file_hash = _content_hash(content)
        stage = "deduplicating"
        db.update_import_job(job_id, file_hash=file_hash, stage=stage)
        with db.connect() as conn:
            duplicate = conn.execute(
                "SELECT id, title, status FROM legal_documents WHERE file_hash = ? LIMIT 1",
                (file_hash,),
            ).fetchone()
        if duplicate:
            message = f"重复导入，已存在文档：{duplicate['title']}（ID {duplicate['id']}）"
            db.update_import_job(job_id, stage="duplicate", status="skipped", total_chunks=0, error_summary=message)
            db.audit(user["id"], "import_kb", "legal_document", duplicate["id"], "skipped")
            return {"status": "skipped", "job_id": job_id, "document_id": duplicate["id"]}

        stage = "chunked"
        chunks = search.chunk_text(content)
        if not chunks:
            raise HTTPException(status_code=400, detail="知识库正文不能为空")
        db.update_import_job(job_id, stage=stage, total_chunks=len(chunks))

        validity_status, validity_date, validity_note = _validity_fields(
            {
                "validity_status": validity_status,
                "validity_date": validity_date,
                "validity_note": validity_note,
            }
        )
        metadata = {
            "title": title,
            "document_type": document_type,
            "case_cause": case_cause,
            "region": region,
            "year": year,
            "validity_status": validity_status,
        }
        stage = "indexed"
        with db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO legal_documents(title, document_type, case_cause, region, year, source_name, validity_status, validity_date, validity_note, file_hash, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, ?)
                """,
                (title, document_type, case_cause, region, year, source_name, validity_status, validity_date, validity_note, file_hash, user["id"], db.now(), db.now()),
            )
            doc_id = cur.lastrowid
            _insert_chunks(conn, doc_id, chunks, metadata)

        stage = "published"
        db.update_import_job(job_id, stage=stage, status="success", total_chunks=len(chunks), error_summary="")
        db.audit(user["id"], "import_kb", "legal_document", doc_id, "success")
        return {"status": "success", "job_id": job_id, "document_id": doc_id, "chunk_count": len(chunks)}
    except Exception as exc:
        if doc_id is not None:
            try:
                vector_store.delete_document(doc_id)
            except Exception:
                pass
        detail = _error_detail(exc)
        if job_id is not None:
            db.update_import_job(job_id, file_hash=file_hash, stage=stage, status="failed", error_summary=detail)
        db.audit(user["id"], "import_kb", "legal_document", doc_id, "failed")
        raise


def _run_kb_batch_import(files: list[tuple[str, bytes]], fields: dict, user: dict) -> None:
    """Process a submitted batch outside the request-response timeout window."""
    async def process() -> None:
        for file_name, data in files:
            safe_name = Path(file_name).name
            stem = Path(safe_name).stem or "未命名文档"
            title = fields["title"].strip() or stem
            if len(files) > 1 and fields["title"].strip():
                title = f"{fields['title'].strip()} - {stem}"
            upload = UploadFile(file=io.BytesIO(data), filename=safe_name)
            try:
                await _import_single_kb_document(
                    title=title,
                    document_type=fields["document_type"],
                    case_cause=fields["case_cause"],
                    region=fields["region"],
                    year=fields["year"],
                    validity_status=fields["validity_status"],
                    validity_date=fields["validity_date"],
                    validity_note=fields["validity_note"],
                    content="",
                    file=upload,
                    user=user,
                )
            except Exception:
                # Each file has its own import job and can fail independently.
                continue

    asyncio.run(process())


@app.post("/kb/import")
async def import_kb(
    background_tasks: BackgroundTasks,
    title: str = Form(""),
    document_type: str = Form(...),
    case_cause: str = Form("合同纠纷"),
    region: str = Form("全国"),
    year: int = Form(2026),
    validity_status: str = Form("active"),
    validity_date: str = Form(""),
    validity_note: str = Form(""),
    content: str = Form(""),
    file: list[UploadFile] | None = File(None),
    user: dict = Depends(require_role("kb_admin", "sys_admin")),
):
    uploads = [item for item in (file or []) if item and item.filename]
    if len(uploads) > MAX_KB_IMPORT_FILES:
        raise HTTPException(status_code=400, detail=f"单次最多导入 {MAX_KB_IMPORT_FILES} 个文件")
    if not uploads and not content.strip():
        raise HTTPException(status_code=400, detail="请上传文件或填写正文")
    if not uploads:
        if not title.strip():
            raise HTTPException(status_code=400, detail="手动录入时标题不能为空")
        try:
            await _import_single_kb_document(
                title=title.strip(), document_type=document_type, case_cause=case_cause,
                region=region, year=year, validity_status=validity_status,
                validity_date=validity_date, validity_note=validity_note, content=content,
                file=None, user=user,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"导入失败：{_error_detail(exc)}") from exc
        return RedirectResponse("/kb", status_code=303)

    payloads = [(Path(upload.filename).name, await upload.read()) for upload in uploads]
    background_tasks.add_task(
        _run_kb_batch_import,
        payloads,
        {
            "title": title,
            "document_type": document_type,
            "case_cause": case_cause,
            "region": region,
            "year": year,
            "validity_status": validity_status,
            "validity_date": validity_date,
            "validity_note": validity_note,
        },
        user,
    )
    return RedirectResponse("/kb", status_code=303)


@app.get("/kb/{doc_id}/edit")
def edit_kb_page(doc_id: int, request: Request, user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    with db.connect() as conn:
        doc = conn.execute("SELECT * FROM legal_documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        chunks = db.rows_to_dicts(conn.execute("SELECT content FROM document_chunks WHERE document_id = ? ORDER BY chunk_no", (doc_id,)).fetchall())
    content = "\n\n".join(item["content"] for item in chunks)
    return render(request, "kb_edit.html", {"doc": dict(doc), "content": content})


@app.post("/kb/{doc_id}/update")
def update_kb(
    doc_id: int,
    title: str = Form(...),
    document_type: str = Form(...),
    case_cause: str = Form("合同纠纷"),
    region: str = Form("全国"),
    year: int = Form(2026),
    validity_status: str = Form("active"),
    validity_date: str = Form(""),
    validity_note: str = Form(""),
    content: str = Form(...),
    user: dict = Depends(require_role("kb_admin", "sys_admin")),
):
    chunks = search.chunk_text(content)
    if not chunks:
        raise HTTPException(status_code=400, detail="知识库正文不能为空")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM legal_documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文档不存在")
        validity_status, validity_date, validity_note = _validity_fields(
            {
                "validity_status": validity_status,
                "validity_date": validity_date,
                "validity_note": validity_note,
            },
            dict(row),
        )
        metadata = {
            "title": title,
            "document_type": document_type,
            "case_cause": case_cause,
            "region": region,
            "year": year,
            "validity_status": validity_status,
        }
        conn.execute(
            """
            UPDATE legal_documents
            SET title = ?, document_type = ?, case_cause = ?, region = ?, year = ?, validity_status = ?,
                validity_date = ?, validity_note = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, document_type, case_cause, region, year, validity_status, validity_date, validity_note, db.now(), doc_id),
        )
        vector_store.delete_document(doc_id)
        conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (doc_id,))
        _insert_chunks(conn, doc_id, chunks, metadata)
    db.audit(user["id"], "update_kb", "legal_document", doc_id, "success")
    return RedirectResponse("/kb", status_code=303)


@app.post("/kb/{doc_id}/delete")
def delete_kb(doc_id: int, user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM legal_documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文档不存在")
        vector_store.delete_document(doc_id)
        conn.execute("UPDATE legal_documents SET status = 'deleted', updated_at = ? WHERE id = ?", (db.now(), doc_id))
    db.audit(user["id"], "delete_kb", "legal_document", doc_id, "success")
    return RedirectResponse("/kb", status_code=303)


@app.post("/kb/{doc_id}/toggle")
def toggle_kb(doc_id: int, user: dict = Depends(require_role("kb_admin", "sys_admin"))):
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM legal_documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文档不存在")
        next_status = "offline" if row["status"] == "published" else "published"
        conn.execute("UPDATE legal_documents SET status = ?, updated_at = ? WHERE id = ?", (next_status, db.now(), doc_id))
        vector_store.delete_document(doc_id)
        if next_status == "published":
            chunks = db.rows_to_dicts(
                conn.execute(
                    """
                    SELECT c.id AS chunk_id, c.content, c.section_name, c.paragraph_no,
                           d.id AS document_id, d.title, d.document_type, d.case_cause,
                           d.region, d.year, d.source_name, d.validity_status, d.validity_date,
                           d.validity_note, d.status
                    FROM document_chunks c
                    JOIN legal_documents d ON d.id = c.document_id
                    WHERE d.id = ?
                    """,
                    (doc_id,),
                ).fetchall()
            )
            for chunk in chunks:
                metadata = {key: chunk.get(key) for key in chunk.keys() if key != "content"}
                vector_store.upsert_chunk(int(chunk["chunk_id"]), chunk["content"], metadata)
    db.audit(user["id"], "toggle_kb_status", "legal_document", doc_id, next_status)
    return RedirectResponse("/kb", status_code=303)


@app.post("/feedback")
def feedback(
    task_id: int = Form(...),
    result_id: int = Form(...),
    rating: str = Form(...),
    reason: str = Form(""),
    comment: str = Form(""),
    user: dict = Depends(require_user),
):
    if rating not in {"helpful", "unhelpful"}:
        raise HTTPException(status_code=400, detail="rating 必须是 helpful 或 unhelpful")
    with db.connect() as conn:
        task = conn.execute("SELECT id FROM reasoning_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
        result = conn.execute("SELECT id FROM reasoning_results WHERE id = ? AND task_id = ?", (result_id, task_id)).fetchone()
        if not task or not result:
            db.audit(user["id"], "submit_feedback", "reasoning_task", task_id, "denied")
            raise HTTPException(status_code=404, detail="任务或结果不存在")
        conn.execute(
            """
            INSERT INTO feedback(user_id, task_id, result_id, rating, reason, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], task_id, result_id, rating, reason, comment, db.now()),
        )
    db.audit(user["id"], "submit_feedback", "reasoning_task", task_id, "success")
    return RedirectResponse(f"/tasks/{task_id}/result", status_code=303)


@app.get("/system")
def system_page(request: Request, user: dict = Depends(require_role("sys_admin"))):
    with db.connect() as conn:
        users = db.rows_to_dicts(conn.execute("SELECT id, username, display_name, role, status, created_at, last_login_at FROM users").fetchall())
        configs = [_config_for_display(item) for item in db.rows_to_dicts(conn.execute("SELECT * FROM system_configs ORDER BY `key`").fetchall())]
        audits = db.rows_to_dicts(
            conn.execute(
                """
                SELECT a.*, u.username FROM audit_logs a LEFT JOIN users u ON u.id = a.actor_id
                ORDER BY a.created_at DESC LIMIT 30
                """
            ).fetchall()
        )
        feedbacks = db.rows_to_dicts(
            conn.execute(
                """
                SELECT f.*, u.username, t.title FROM feedback f
                JOIN users u ON u.id = f.user_id
                JOIN reasoning_tasks t ON t.id = f.task_id
                ORDER BY f.created_at DESC LIMIT 20
                """
            ).fetchall()
        )
    return render(
        request,
        "system.html",
        {
            "users": users,
            "configs": configs,
            "config_map": {item["key"]: item for item in configs},
            "api_key_configured": config_store.api_key_configured(),
            "audits": audits,
            "feedbacks": feedbacks,
            "quality_metrics": _quality_metrics(),
        },
    )


@app.post("/system/configs")
def update_system_configs(
    chat_model: str = Form(...),
    embedding_model: str = Form(...),
    use_llm: str = Form("false"),
    use_dashscope_embedding: str = Form("false"),
    dashscope_api_key: str = Form(""),
    clear_dashscope_api_key: str | None = Form(None),
    dashscope_base_http_api_url: str = Form(""),
    dashscope_timeout: str = Form("30"),
    top_k: str = Form(...),
    keyword_weight: str = Form(...),
    vector_weight: str = Form(...),
    user: dict = Depends(require_role("sys_admin")),
):
    values = {
        "chat_model": chat_model.strip() or "qwen-plus",
        "embedding_model": embedding_model.strip() or "text-embedding-v4",
        "use_llm": "true" if _parse_bool(use_llm) else "false",
        "use_dashscope_embedding": "true" if _parse_bool(use_dashscope_embedding) else "false",
        "dashscope_base_http_api_url": dashscope_base_http_api_url.strip(),
        "dashscope_timeout": str(_parse_positive_int(dashscope_timeout, 30, 5, 120)),
        "top_k": str(_parse_positive_int(top_k, 6, 1, 20)),
        "keyword_weight": f"{_parse_weight(keyword_weight, 0.4):.2f}",
        "vector_weight": f"{_parse_weight(vector_weight, 0.6):.2f}",
    }
    for key, value in values.items():
        db.update_config(key, value, user["id"])
    if clear_dashscope_api_key:
        db.update_config("dashscope_api_key", "", user["id"])
    elif dashscope_api_key.strip():
        db.update_config("dashscope_api_key", config_store.encrypt_secret(dashscope_api_key.strip()), user["id"])
    db.audit(user["id"], "update_config", "system_config", None, "success")
    return RedirectResponse("/system", status_code=303)


@app.post("/system/users")
def create_user(
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    role: str = Form("user"),
    user: dict = Depends(require_role("sys_admin")),
):
    with db.connect() as conn:
        exists = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="用户名已存在")
        cur = conn.execute(
            """
            INSERT INTO users(username, password_hash, display_name, role, status, created_at)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (username, hash_password(password), display_name, role, db.now()),
        )
    db.audit(user["id"], "create_user", "user", cur.lastrowid, "success")
    return RedirectResponse("/system", status_code=303)


@app.post("/system/users/{target_user_id}/toggle")
def toggle_user(target_user_id: int, user: dict = Depends(require_role("sys_admin"))):
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        next_status = "disabled" if row["status"] == "active" else "active"
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (next_status, target_user_id))
    db.audit(user["id"], "toggle_user", "user", target_user_id, next_status)
    return RedirectResponse("/system", status_code=303)


@app.post("/system/users/{target_user_id}/password")
def reset_user_password(
    target_user_id: int,
    password: str = Form(...),
    user: dict = Depends(require_role("sys_admin")),
):
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), target_user_id))
    db.audit(user["id"], "reset_password", "user", target_user_id, "success")
    return RedirectResponse("/system", status_code=303)
