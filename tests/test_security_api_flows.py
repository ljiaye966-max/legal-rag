from __future__ import annotations

import os
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_root = Path(tempfile.mkdtemp(prefix="legal-rag-tests-"))
os.environ["DATABASE_PATH"] = str(_root / "legal_rag_test.db")
os.environ["CHROMA_PATH"] = str(_root / "chroma")
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["USE_LLM"] = "false"
os.environ["USE_DASHSCOPE_EMBEDDING"] = "false"
os.environ["SMS_PROVIDER"] = "local"

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import vector_store


class SecurityAndApiFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        shutil.rmtree(_root, ignore_errors=True)

    def setUp(self) -> None:
        self.client.cookies.clear()

    def login(self, username: str, password: str = "123456") -> None:
        response = self.client.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    def test_api_search_requires_login(self) -> None:
        response = self.client.get("/api/search", params={"q": "合同"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], 401)
        self.assertIn("request_id", response.json())

    def test_assistant_requires_login_and_returns_sources(self) -> None:
        unauthorized = self.client.post("/api/assistant", json={"question": "拖欠尾款怎么办？"})
        self.assertEqual(unauthorized.status_code, 401)

        self.login("user01")
        response = self.client.post("/api/assistant", json={"question": "对方拖欠合同尾款怎么办？"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertTrue(payload["data"]["answer"])
        self.assertIsInstance(payload["data"]["sources"], list)

    def test_assistant_admits_when_a_statute_does_not_exist(self) -> None:
        self.login("user01")
        response = self.client.post(
            "/api/assistant",
            json={"question": "《中华人民共和国民法典》第9999条规定了什么？"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn("未找到", data["answer"])
        self.assertIn("不会据此编造", data["answer"])
        self.assertEqual(data["reference_audit"]["unverified"], 1)

    def test_phone_code_registration_and_phone_login(self) -> None:
        phone = "13900001234"
        send_response = self.client.post(
            "/api/auth/send-code",
            json={"phone": phone, "purpose": "register"},
        )
        self.assertEqual(send_response.status_code, 200)
        code = send_response.json()["data"]["demo_code"]
        self.assertRegex(code, r"^\d{6}$")

        wrong_code = self.client.post(
            "/api/register",
            json={"phone": phone, "verification_code": "000000", "password": "secure123"},
        )
        self.assertEqual(wrong_code.status_code, 400)

        register_response = self.client.post(
            "/api/register",
            json={
                "phone": phone,
                "verification_code": code,
                "password": "secure123",
                "display_name": "手机号测试用户",
            },
        )
        self.assertEqual(register_response.status_code, 200)
        self.assertEqual(register_response.json()["data"]["phone"], phone)
        self.assertTrue(register_response.json()["data"]["session_token"])

        self.client.cookies.clear()
        login_response = self.client.post("/api/login", json={"account": phone, "password": "secure123"})
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(login_response.json()["data"]["phone"], phone)

        duplicate_code = self.client.post("/api/auth/send-code", json={"phone": phone, "purpose": "register"})
        self.assertEqual(duplicate_code.status_code, 409)

    def test_aliyun_sms_registration_never_returns_code_and_uses_provider_validation(self) -> None:
        phone = "13900004567"
        with (
            patch("app.main.SMS_PROVIDER", "aliyun_sms_verify"),
            patch("app.main.sms_verification.send_code", return_value={"request_id": "request-1", "biz_id": "biz-1"}) as send_code,
        ):
            send_response = self.client.post(
                "/api/auth/send-code",
                json={"phone": phone, "purpose": "register"},
            )
        self.assertEqual(send_response.status_code, 200)
        self.assertNotIn("demo_code", send_response.json()["data"])
        send_code.assert_called_once()

        with (
            patch("app.main.SMS_PROVIDER", "aliyun_sms_verify"),
            patch("app.main.sms_verification.verify_code", return_value=True) as verify_code,
        ):
            register_response = self.client.post(
                "/api/register",
                json={
                    "phone": phone,
                    "verification_code": "123456",
                    "password": "secure123",
                    "display_name": "短信认证用户",
                },
            )
        self.assertEqual(register_response.status_code, 200)
        verify_code.assert_called_once_with(phone, "123456")

    def test_assistant_rejects_low_relevance_retrieval_instead_of_hallucinating(self) -> None:
        self.login("user01")
        response = self.client.post(
            "/api/assistant",
            json={"question": "火星房地产继承纠纷应适用什么规定？"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["sources"], [])
        self.assertIn("没有在当前知识库中找到足够相关的依据", data["answer"])
        self.assertTrue(any("未采用低相关材料" in warning for warning in data["warnings"]))
        self.assertTrue(any("已跳过千问调用" in warning for warning in data["warnings"]))
        self.assertFalse(any("千问服务连接失败" in warning for warning in data["warnings"]))

    def test_bearer_session_authenticates_mini_program_requests(self) -> None:
        login = self.client.post("/api/login", json={"username": "user01", "password": "123456"})
        token = login.json()["data"]["session_token"]
        self.assertTrue(token)
        self.client.cookies.clear()
        response = self.client.get(
            "/api/search",
            params={"q": "合同", "mode": "hybrid"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    def test_admin_qwen_config_is_encrypted_and_masked(self) -> None:
        self.login("admin")
        response = self.client.post(
            "/api/configs",
            json={"use_llm": True, "dashscope_api_key": "sk-test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["dashscope_api_key"], "已配置")

        with db.connect() as conn:
            stored = conn.execute(
                "SELECT value FROM system_configs WHERE `key` = 'dashscope_api_key'"
            ).fetchone()["value"]
        self.assertTrue(stored.startswith("fernet:v1:"))
        self.assertNotIn("sk-test-secret", stored)

        configs = self.client.get("/api/configs")
        self.assertEqual(configs.status_code, 200)
        api_key = next(item for item in configs.json()["data"] if item["key"] == "dashscope_api_key")
        self.assertEqual(api_key["value"], "已配置")
        self.assertNotIn("sk-test-secret", configs.text)

        self.client.post("/api/configs", json={"clear_dashscope_api_key": True})

    def test_embedding_config_change_rebuilds_active_chroma_index(self) -> None:
        self.login("admin")
        db.update_config("use_dashscope_embedding", "false", 1)
        with patch("app.main.db.rebuild_chroma_index") as rebuild:
            response = self.client.post("/api/configs", json={"use_dashscope_embedding": True})
        self.assertEqual(response.status_code, 200)
        rebuild.assert_called_once()

    def test_compare_page_and_api_return_case_candidates(self) -> None:
        self.login("user01")
        page = self.client.get("/compare")
        self.assertEqual(page.status_code, 200)
        self.assertIn("类案对比", page.text)

        response = self.client.get("/api/compare")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertGreaterEqual(len(data["candidate_cases"]), 2)
        self.assertGreaterEqual(len(data["cases"]), 2)

    def test_chroma_sync_batches_embedding_requests(self) -> None:
        with (
            patch("app.services.vector_store.list_chunk_ids", return_value=set()),
            patch("app.services.vector_store.delete_chunks"),
            patch("app.services.vector_store.upsert_chunks") as upsert,
        ):
            db.sync_chroma_index()
        self.assertGreater(upsert.call_count, 0)
        self.assertTrue(
            all(len(call.args[0]) <= vector_store.EMBEDDING_BATCH_SIZE for call in upsert.call_args_list)
        )

    def test_disabled_user_existing_session_is_rejected(self) -> None:
        self.login("user01")
        with db.connect() as conn:
            conn.execute("UPDATE users SET status = 'disabled' WHERE username = 'user01'")
        try:
            response = self.client.get("/dashboard", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login")
        finally:
            with db.connect() as conn:
                conn.execute("UPDATE users SET status = 'active' WHERE username = 'user01'")

    def test_form_feedback_checks_task_owner(self) -> None:
        with db.connect() as conn:
            admin = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
            cur = conn.execute(
                """
                INSERT INTO reasoning_tasks(user_id, title, case_text, extracted_facts_json, filters_json, status, created_at, updated_at)
                VALUES (?, '他人任务', '合同尾款争议', '{}', '{}', 'completed', ?, ?)
                """,
                (admin["id"], db.now(), db.now()),
            )
            task_id = cur.lastrowid
            result = conn.execute(
                """
                INSERT INTO reasoning_results(task_id, version_no, result_markdown, result_json, model_name, retrieval_config_json, latency_ms, created_at)
                VALUES (?, 1, 'result', '{}', 'local', '{}', 1, ?)
                """,
                (task_id, db.now()),
            )
            result_id = result.lastrowid
        self.login("user01")
        response = self.client.post(
            "/feedback",
            data={"task_id": task_id, "result_id": result_id, "rating": "helpful"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 404)

    def test_api_reasoning_and_task_delete_flow(self) -> None:
        self.login("user01")
        create_response = self.client.post("/api/tasks", json={"case_text": "张某签订买卖合同后，对方拖欠尾款并有签收记录。"})
        task_id = create_response.json()["data"]["task_id"]
        reason_response = self.client.post(f"/api/tasks/{task_id}/reason")
        self.assertEqual(reason_response.status_code, 200)
        detail = self.client.get(f"/api/tasks/{task_id}").json()["data"]
        self.assertIn(detail["task"]["status"], {"queued", "running", "retrieving", "generating", "validating", "completed", "failed"})
        delete_response = self.client.delete(f"/api/tasks/{task_id}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["data"]["status"], "deleted")

    def test_reasoning_keeps_a_traceable_chain_and_recall_metric(self) -> None:
        self.login("user01")
        create_response = self.client.post(
            "/api/tasks",
            json={"case_text": "张某签订买卖合同后，对方拖欠尾款并有合同、签收记录和聊天记录。"},
        )
        task_id = create_response.json()["data"]["task_id"]
        self.client.post(f"/api/tasks/{task_id}/reason")
        detail = self.client.get(f"/api/tasks/{task_id}").json()["data"]
        self.assertIsNotNone(detail["result"])
        validation = __import__("json").loads(detail["result"]["result_json"])["validation"]
        self.assertEqual(len(validation["reasoning_chain"]), 4)
        self.assertIn("recall_at_5", validation["case_retrieval_metrics"])

    def test_similar_case_evaluation_returns_recall_at_5(self) -> None:
        self.login("user01")
        response = self.client.get(
            "/api/evaluations/similar-cases",
            params={"q": "买卖合同拖欠尾款，已经签收", "case_cause": "合同纠纷"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("recall_at_5", response.json()["data"])

    def test_kb_api_create_update_delete_flow(self) -> None:
        self.login("kb_admin")
        payload = {
            "title": "API 测试案例",
            "document_type": "case",
            "case_cause": "合同纠纷",
            "region": "全国",
            "year": 2026,
            "content": "甲乙签订合同，乙方逾期付款，甲方提交合同和转账记录。",
        }
        create_response = self.client.post("/api/kb/documents", json=payload)
        self.assertEqual(create_response.status_code, 200)
        doc_id = create_response.json()["data"]["document_id"]
        update_payload = payload | {"content": "甲乙签订合同，乙方逾期付款，甲方提交合同、发票和签收记录。"}
        update_response = self.client.put(f"/api/kb/documents/{doc_id}", json=update_payload)
        self.assertEqual(update_response.status_code, 200)
        delete_response = self.client.delete(f"/api/kb/documents/{doc_id}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["data"]["status"], "deleted")

    def test_web_kb_import_accepts_multiple_files_and_records_each_job(self) -> None:
        self.login("kb_admin")
        response = self.client.post(
            "/kb/import",
            data={
                "title": "批量测试",
                "document_type": "case",
                "case_cause": "合同纠纷",
                "region": "全国",
                "year": "2026",
                "validity_status": "active",
            },
            files=[
                ("file", ("case-one.txt", io.BytesIO("甲乙签订买卖合同，乙方逾期付款。".encode()), "text/plain")),
                ("file", ("case-two.txt", io.BytesIO("甲乙签订服务合同，甲方已经完成交付。".encode()), "text/plain")),
            ],
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.connect() as conn:
            docs = conn.execute(
                "SELECT title, source_name FROM legal_documents WHERE title IN (?, ?) ORDER BY title",
                ("批量测试 - case-one", "批量测试 - case-two"),
            ).fetchall()
            jobs = conn.execute(
                "SELECT file_name, status FROM import_jobs WHERE file_name IN (?, ?) ORDER BY file_name",
                ("case-one.txt", "case-two.txt"),
            ).fetchall()
        self.assertEqual(len(docs), 2)
        self.assertEqual(len(jobs), 2)
        self.assertEqual({job["status"] for job in jobs}, {"success"})

    def test_web_kb_batch_import_continues_after_one_file_fails(self) -> None:
        self.login("kb_admin")
        response = self.client.post(
            "/kb/import",
            data={
                "title": "混合批量测试",
                "document_type": "case",
                "case_cause": "合同纠纷",
                "region": "全国",
                "year": "2026",
                "validity_status": "active",
            },
            files=[
                ("file", ("unsupported.exe", io.BytesIO(b"not a supported document"), "application/octet-stream")),
                ("file", ("valid-case.txt", io.BytesIO("甲乙签订合同，乙方未按期付款。".encode()), "text/plain")),
            ],
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.connect() as conn:
            failed = conn.execute(
                "SELECT status, error_summary FROM import_jobs WHERE file_name = ? ORDER BY id DESC LIMIT 1",
                ("unsupported.exe",),
            ).fetchone()
            valid = conn.execute(
                "SELECT id FROM legal_documents WHERE source_name = ? ORDER BY id DESC LIMIT 1",
                ("valid-case.txt",),
            ).fetchone()
        self.assertEqual(failed["status"], "failed")
        self.assertIn("只支持", failed["error_summary"])
        self.assertIsNotNone(valid)


if __name__ == "__main__":
    unittest.main()
