from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backend.app import csv_utils, hermes_client, main, prompts
from backend.app.config import settings
from backend.app.pipeline import run_turn
from backend.app.storage import Storage, storage
from sse_utils import parse_sse_lines



class ToolAvailabilityPromptTests(unittest.TestCase):
    def test_prompt_assigns_execute_code_for_analysis_and_terminal_for_network(self) -> None:
        self.assertIn("`execute_code` (toolset `code_execution`)", prompts.SYSTEM_PROMPT)
        self.assertIn("используй Hermes tool `terminal`", prompts.SYSTEM_PROMPT)
        self.assertIn("не заявляй, что выполнение Python запрещено", prompts.SYSTEM_PROMPT)

    def test_broad_csv_analysis_should_not_require_clarifying_goal(self) -> None:
        self.assertIn("не задавай уточняющий вопрос только потому", prompts.SYSTEM_PROMPT)
        self.assertIn("сводные статистики", prompts.SYSTEM_PROMPT)

    def test_toolset_setup_script_checks_core_analysis_tools(self) -> None:
        script = (ROOT / "scripts" / "check_hermes_toolsets.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('"execute_code", "terminal", "read_file"', script)
        self.assertIn("/toolsets", script)
        setup = (ROOT / "scripts" / "enable_hermes_tools.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("hermes tools", setup)
        self.assertIn("api_server", setup)

class CsvRegressionTests(unittest.TestCase):
    def test_row_count_is_exact_when_profile_sample_is_limited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data.csv"
            path.write_text("name,value\na,1\nb,2\nc,3\nd,4\ne,5\n", encoding="utf-8")
            profile = csv_utils.load_csv(path, preview_rows=2, max_rows=2)
        self.assertEqual(profile["row_count"], 5)
        self.assertEqual(len(profile["preview"]), 2)

    def test_date_type_requires_a_full_match(self) -> None:
        self.assertEqual(csv_utils._guess_type(["2026-01-01 trailing-text"]), "текст")


class HermesClientRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_uses_documented_fields_and_server_side_session_history(self) -> None:
        messages = [
            {"role": "system", "content": "base prompt overlay"},
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new request"},
        ]
        payload = hermes_client._openai_payload(messages)
        self.assertEqual(payload["model"], "hermes-agent")
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(set(payload), {"model", "messages", "stream"})
        self.assertFalse(payload["stream"])
        continued = hermes_client._openai_payload(messages, "local-session-id")
        self.assertEqual(continued["messages"], [messages[0], messages[-1]])
        self.assertEqual(
            hermes_client._headers("local-session-id", idempotency_key="turn-1")["X-Hermes-Session-Id"],
            "local-session-id",
        )
        self.assertEqual(hermes_client._headers(session_key="local-session-id")["X-Hermes-Session-Key"], "local-session-id")
        self.assertEqual(hermes_client._headers(idempotency_key="turn-1")["Idempotency-Key"], "turn-1")

    async def test_chat_requests_one_final_non_streaming_completion(self) -> None:
        fake = SimpleNamespace(url=None, payload=None, headers=None, trust_env=None, calls=0)
        final_payload = {
            "id": "chatcmpl-test",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Итог: файл обработан, результаты проверены.",
                    "reasoning_content": "Промежуточные размышления не должны попасть в UI.",
                },
                "finish_reason": "stop",
            }],
        }

        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "application/json; charset=utf-8"}
            text = json.dumps(final_payload, ensure_ascii=False)

            def json(self):
                return final_payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                fake.trust_env = kwargs.get("trust_env")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, **kwargs):
                return SimpleNamespace(status_code=404, text="", headers={}, json=lambda: {})

            async def post(self, url, **kwargs):
                fake.calls += 1
                fake.url = url
                fake.method = "POST"
                fake.payload = kwargs["json"]
                fake.headers = kwargs["headers"]
                return FakeResponse()

        with patch.object(hermes_client.httpx, "AsyncClient", FakeClient):
            answer = await hermes_client.chat(
                [{"role": "system", "content": "overlay"}, {"role": "user", "content": "analyze"}],
                "session-123",
                idempotency_key="turn-123",
            )
        self.assertEqual(answer, "Итог: файл обработан, результаты проверены.")
        self.assertNotIn("Промежуточные размышления", answer)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(fake.method, "POST")
        self.assertFalse(fake.trust_env, "localhost Hermes requests must bypass environment proxies")
        self.assertEqual(fake.url, f"{settings.hermes_base_url}/chat/completions")
        self.assertEqual(fake.payload["model"], settings.hermes_api_model)
        self.assertFalse(fake.payload["stream"])
        self.assertNotIn("temperature", fake.payload)
        self.assertNotIn("max_tokens", fake.payload)
        self.assertEqual(fake.headers["X-Hermes-Session-Id"], "session-123")
        self.assertEqual(fake.headers["Idempotency-Key"], "turn-123")
        self.assertEqual(fake.headers["Accept"], "application/json")

    async def test_503_is_reported_without_replaying_agent_turn(self) -> None:
        fake = SimpleNamespace(calls=0)

        class FakeResponse:
            status_code = 503
            headers = {"Content-Type": "application/json", "Retry-After": "1"}
            text = '{"error":{"message":"Gateway is draining","code":"gateway_draining"}}'

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, **_kwargs):
                if url.endswith("/capabilities"):
                    return SimpleNamespace(status_code=404, text="", headers={}, json=lambda: {})
                raise AssertionError("Unexpected GET " + url)

            async def post(self, *_args, **_kwargs):
                fake.calls += 1
                return FakeResponse()

        with patch.object(hermes_client.httpx, "AsyncClient", FakeClient):
            with self.assertRaises(hermes_client.HermesError) as raised:
                await hermes_client.chat(
                    [{"role": "user", "content": "calculate"}], idempotency_key="same-turn"
                )
        self.assertEqual(fake.calls, 1)
        self.assertIn("HTTP 503", str(raised.exception))
        self.assertIn("gateway_draining", str(raised.exception))
        self.assertIn("Retry-After=1", str(raised.exception))
        self.assertIn("Автоматический повтор отключён", str(raised.exception))

    async def test_runs_api_returns_only_terminal_completed_output(self) -> None:
        fake = SimpleNamespace(posts=[], gets=[], trust_env=None)
        states = iter([
            {"run_id": "run-123", "status": "running", "output": None},
            {"run_id": "run-123", "status": "completed", "output": "Финальный отчёт готов."},
        ])

        class Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
                self.headers = {"Content-Type": "application/json"}
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                fake.trust_env = kwargs.get("trust_env")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, **kwargs):
                fake.gets.append((url, kwargs))
                if url.endswith("/capabilities"):
                    return Response(200, {"features": {"run_submission": True, "run_status": True}})
                return Response(200, next(states))

            async def post(self, url, **kwargs):
                fake.posts.append((url, kwargs))
                return Response(202, {"run_id": "run-123", "status": "started"})

        messages = [
            {"role": "system", "content": "Только итог."},
            {"role": "user", "content": "Проанализируй файл."},
        ]
        with patch.object(hermes_client.httpx, "AsyncClient", FakeClient), patch.object(
            hermes_client.asyncio, "sleep", new_callable=AsyncMock
        ):
            answer = await hermes_client.chat(messages, "session-1", idempotency_key="turn-1")

        self.assertEqual(answer, "Финальный отчёт готов.")
        self.assertEqual(len(fake.posts), 1)
        self.assertTrue(fake.posts[0][0].endswith("/runs"))
        body = fake.posts[0][1]["json"]
        self.assertEqual(body["input"], "Проанализируй файл.")
        self.assertEqual(body["instructions"], "Только итог.")
        self.assertEqual(body["session_id"], "session-1")
        headers = fake.posts[0][1]["headers"]
        self.assertEqual(headers["Idempotency-Key"], "turn-1")
        self.assertNotIn("X-Hermes-Session-Id", headers)
        self.assertEqual(headers["X-Hermes-Session-Key"], "session-1")
        self.assertEqual(len(fake.gets), 3)  # capabilities plus two polls

    async def test_responses_api_ignores_tool_items_commentary_and_reasoning(self) -> None:
        answer = hermes_client._extract_responses_output({
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [{"text": "private"}]},
                {"type": "function_call", "name": "terminal", "arguments": "{}", "status": "completed"},
                {"type": "message", "role": "assistant", "phase": "commentary", "content": [
                    {"type": "output_text", "text": "Начинаю шаг 1"}
                ], "status": "completed"},
                {"type": "function_call_output", "output": "tool stdout", "status": "completed"},
                {"type": "message", "role": "assistant", "phase": "final", "content": [
                    {"type": "output_text", "text": "Проверенный итог."}
                ], "status": "completed"},
            ],
        })
        self.assertEqual(answer, "Проверенный итог.")

    async def test_non_final_tool_call_is_not_returned_as_assistant_answer(self) -> None:
        with self.assertRaises(hermes_client.HermesError) as raised:
            hermes_client._extract_final_completion({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "import pandas as pd",
                        "tool_calls": [{"type": "function", "function": {"name": "terminal"}}],
                    },
                    "finish_reason": "tool_calls",
                }]
            })
        self.assertIn("не вернул финальный текстовый ответ", str(raised.exception))

    async def test_non_streaming_result_requires_final_text_and_valid_finish_reason(self) -> None:
        result = hermes_client._extract_final_completion({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "Не показывать"},
                        {"type": "text", "text": "Финальная "},
                        {"type": "output_text", "text": "сводка."},
                    ],
                    "reasoning_content": "Не показывать и это",
                },
                "finish_reason": "stop",
            }]
        })
        self.assertEqual(result, "Финальная \nсводка.")
        with self.assertRaises(hermes_client.HermesError):
            hermes_client._extract_final_completion({
                "choices": [{"message": {"content": "partial"}, "finish_reason": "tool_calls"}]
            })

    async def test_health_does_not_hide_upstream_error_body(self) -> None:
        class FakeResponse:
            status_code = 503
            text = '{"error":"gateway temporarily unavailable"}'

            def json(self):
                return {"error": "gateway temporarily unavailable"}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, headers=None):
                self.urls = getattr(self, "urls", []) + [url]
                if url.endswith("/health/detailed"):
                    return SimpleNamespace(status_code=200, json=lambda: {"status": "ok", "gateway_state": "running"}, text="{}")
                return FakeResponse()

        fake = FakeClient()
        with patch.object(hermes_client.httpx, "AsyncClient", return_value=fake):
            result = await hermes_client.health()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["body"], {"error": "gateway temporarily unavailable"})
        self.assertEqual(result["detailed"]["gateway_state"], "running")
        self.assertTrue(fake.urls[-1].endswith("/health/detailed"))

    async def test_loopback_and_private_urls_bypass_environment_proxies(self) -> None:
        for url in (
            "http://127.0.0.1:8642/v1/health",
            "http://localhost:8642/v1/health",
            "http://[::1]:8642/v1/health",
            "http://192.168.1.20:8642/v1/health",
            "http://host.docker.internal:8642/v1/health",
        ):
            with self.subTest(url=url):
                self.assertFalse(hermes_client._trust_env_for_url(url))
        self.assertTrue(hermes_client._trust_env_for_url("https://api.example.com/v1/health"))

    async def test_url_normalization_supports_root_or_v1_base(self) -> None:
        original = settings.hermes_base_url
        try:
            settings.hermes_base_url = "http://127.0.0.1:8642/"
            self.assertEqual(hermes_client._v1_base_url(), "http://127.0.0.1:8642/v1")
            self.assertEqual(hermes_client._gateway_base_url(), "http://127.0.0.1:8642")
            settings.hermes_base_url = "http://127.0.0.1:8642/v1/"
            self.assertEqual(hermes_client._v1_base_url(), "http://127.0.0.1:8642/v1")
        finally:
            settings.hermes_base_url = original


class PromptRegressionTests(unittest.TestCase):
    def test_network_file_operations_are_routed_to_terminal(self) -> None:
        self.assertIn("Hermes tool `terminal`", prompts.SYSTEM_PROMPT)
        self.assertIn("Не выполняй сетевые операции из `execute_code`", prompts.SYSTEM_PROMPT)
        self.assertIn("не проси загрузить файл повторно", prompts.SYSTEM_PROMPT)
        self.assertIn("Не завершай ход только планом", prompts.SYSTEM_PROMPT)

    def test_unfinished_plan_detector_matches_reported_failure_without_flagging_final_result(self) -> None:
        plan = (
            "Plan: [ ] 1. Скачать файл\n"
            "Шаг 1: создать директорию\n"
            "Начинаю с шага 1."
        )
        final = "Анализ завершён. Выручка рассчитана, отчёт сохранён и проверен."
        self.assertTrue(prompts.looks_like_unfinished_plan(plan))
        self.assertFalse(prompts.looks_like_unfinished_plan(final))
        self.assertFalse(prompts.looks_like_unfinished_plan("План выполнен: файл прочитан, выводы готовы."))

    def test_detector_catches_screenshot_plan_md_and_textual_execute_code(self) -> None:
        screenshot_failure = (
            "Планирую обработку файла 01_Продажи.csv.\n\n"
            "Plan.md: ЦЕЛЬ: Проанализировать файл данных.\n"
            "[ ] 1. Скачать CSV\n[ ] 2. Прочитать CSV\n"
            "Я начну с первого шага.\n"
            "execute_code: import sys, os, subprocess, importlib\n"
            "from pathlib import Path\nWORK = Path(r'D:\\work')"
        )
        self.assertTrue(prompts.looks_like_unfinished_plan(screenshot_failure))
        self.assertTrue(prompts.looks_like_unfinished_plan("execute_code: import pandas as pd\nprint('hello')"))
        pseudo_tool_json = "Обычный текст перед псевдовызовом.\n\n{\n  \"tool_name\": \"terminal\",\n  \"tool_input\": {\"command\": \"curl.exe -o sales.csv\"}\n}"
        self.assertTrue(prompts.looks_like_unfinished_plan(pseudo_tool_json))
        self.assertTrue(prompts.looks_like_unfinished_plan('Проверка: {"tool_name":"execute_code","tool_input":{"code":"print(42)"}}'))
        self.assertFalse(prompts.looks_like_unfinished_plan("Готово. Отчёт рассчитан, сохранён и проверен."))

    def test_html_cache_busts_composer_styles_and_scripts(self) -> None:
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("styles.css?v=11.0.0", html)
        self.assertIn("app.js?v=11.0.0", html)
        self.assertIn('width="21" height="21"', html)

    def test_prompt_contains_only_task_scoped_file_and_artifact_endpoints(self) -> None:
        previous = settings.hermes_callback_base_url
        try:
            settings.hermes_callback_base_url = "http://127.0.0.1:8000/api"
            session = SimpleNamespace(session_id="session-id", tool_token="task-token")
            file = SimpleNamespace(
                id="file-id",
                name="sales.csv",
                size=120,
                rows=3,
                columns=["date", "revenue"],
                column_types=["дата", "число"],
            )
            prompt = prompts.build_user_message("Посчитай выручку", [file], session)
            self.assertIn("/sessions/session-id/files/file-id/content", prompt)
            self.assertIn("/sessions/session-id/artifacts", prompt)
            self.assertIn("task-token", prompt)
            self.assertIn("Посчитай выручку", prompt)
            self.assertIn("только инструментом terminal", prompt)
            self.assertIn("не через execute_code", prompt)
        finally:
            settings.hermes_callback_base_url = previous


class PipelineRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_calls_hermes_directly_and_returns_uploaded_artifacts(self) -> None:
        previous_data_dir = settings.data_dir
        original_chat = hermes_client.chat
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                settings.data_dir = Path(temp_dir)
                test_storage = Storage()
                session = test_storage.create_session()
                captured = {}

                async def fake_chat(messages, session_id, *, idempotency_key=None):
                    captured["messages"] = messages
                    captured["session_id"] = session_id
                    captured["idempotency_key"] = idempotency_key
                    test_storage.register_artifact(
                        session,
                        "result.csv",
                        content=b"name,value\na,1\n",
                        mime="text/csv",
                    )
                    return "Готово. Результат проверен."

                hermes_client.chat = fake_chat
                events = [event async for event in run_turn(session, "Подсчитай значения", [])]
                result = next(event for event in events if event["event"] == "result")["data"]["message"]
                self.assertEqual(result["text"], "Готово. Результат проверен.")
                self.assertEqual(result["artifacts"][0]["name"], "result.csv")
                self.assertEqual(captured["session_id"], session.session_id)
                self.assertTrue(captured["idempotency_key"])
                self.assertIn("собственные инструменты", prompts.SYSTEM_PROMPT)
                self.assertIn("Не выполняй сетевые операции из `execute_code`", prompts.SYSTEM_PROMPT)
                self.assertIn("Hermes tool `terminal`", prompts.SYSTEM_PROMPT)
                self.assertIn("не проси загрузить файл повторно", prompts.SYSTEM_PROMPT)
                self.assertNotIn("python-блок", prompts.SYSTEM_PROMPT.lower())
        finally:
            hermes_client.chat = original_chat
            settings.data_dir = previous_data_dir

    async def test_attached_file_plan_triggers_one_continuation_and_only_final_result_is_returned(self) -> None:
        previous_data_dir = settings.data_dir
        original_chat = hermes_client.chat
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                settings.data_dir = Path(temp_dir)
                test_storage = Storage()
                session = test_storage.create_session()
                file_path, file_info = test_storage.save_upload(
                    session,
                    "01_Продажи.csv",
                    "product,revenue\nA,100\nB,200\n".encode("utf-8"),
                )
                test_storage.attach_file(session, file_info.id, file_path, file_info)
                calls = []
                plan = (
                    "Plan: [ ] 1. Скачать файл\n"
                    "Шаг 1: прочитать CSV\n"
                    "Начинаю с шага 1."
                )

                async def fake_chat(messages, session_id, *, idempotency_key=None):
                    calls.append({
                        "messages": messages,
                        "session_id": session_id,
                        "idempotency_key": idempotency_key,
                    })
                    if len(calls) == 1:
                        return plan
                    test_storage.register_artifact(
                        session,
                        "sales_report.md",
                        b"# Sales\n\nRevenue: 300 RUB\n",
                        mime="text/markdown",
                    )
                    return "Анализ завершён. Общая выручка — 300 руб.; отчёт сохранён."

                hermes_client.chat = fake_chat
                events = [event async for event in run_turn(session, "Проанализируй продажи", [file_info.id])]
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0]["session_id"], calls[1]["session_id"])
                self.assertNotEqual(calls[0]["idempotency_key"], calls[1]["idempotency_key"])
                self.assertEqual(calls[0]["idempotency_key"], calls[1]["idempotency_key"].removesuffix("-continue-1"))
                self.assertTrue(any(event["event"] == "stage" and event["data"].get("stage") == "continuing" for event in events))
                result = next(event for event in events if event["event"] == "result")["data"]["message"]
                self.assertEqual(result["text"], "Анализ завершён. Общая выручка — 300 руб.; отчёт сохранён.")
                self.assertEqual(result["artifacts"][0]["name"], "sales_report.md")
                self.assertNotIn("Plan:", result["text"])
        finally:
            hermes_client.chat = original_chat
            settings.data_dir = previous_data_dir

    async def test_two_unfinished_plans_return_error_not_successful_result(self) -> None:
        previous_data_dir = settings.data_dir
        original_chat = hermes_client.chat
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                settings.data_dir = Path(temp_dir)
                test_storage = Storage()
                session = test_storage.create_session()
                file_path, file_info = test_storage.save_upload(session, "sales.csv", b"a,b\n1,2\n")
                test_storage.attach_file(session, file_info.id, file_path, file_info)

                async def fake_chat(*_args, **_kwargs):
                    return "Plan: [ ] Step 1. Starting with step 1."

                hermes_client.chat = fake_chat
                events = [event async for event in run_turn(session, "Проанализируй файл", [file_info.id])]
                self.assertFalse(any(event["event"] == "result" for event in events))
                error = next(event for event in events if event["event"] == "error")
                self.assertIn("дважды вернул план", error["data"]["message"])
                self.assertIn("`terminal`", error["data"]["message"])
                self.assertIn("`code_execution`", error["data"]["message"])
                self.assertIn("`execute_code`", error["data"]["message"])
        finally:
            hermes_client.chat = original_chat
            settings.data_dir = previous_data_dir

    async def test_closing_sse_generator_does_not_raise(self) -> None:
        previous_data_dir = settings.data_dir
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                settings.data_dir = Path(temp_dir)
                test_storage = Storage()
                session = test_storage.create_session()
                generator = run_turn(session, "Короткий вопрос", [])
                first = await anext(generator)
                self.assertEqual(first["event"], "accepted")
                await generator.aclose()
        finally:
            settings.data_dir = previous_data_dir


class AppFileExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_data_dir = settings.data_dir
        self.temp_dir = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self.temp_dir.name)
        self._old_sessions = storage._sessions.copy()
        storage._sessions.clear()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        storage._sessions.clear()
        storage._sessions.update(self._old_sessions)
        settings.data_dir = self.previous_data_dir
        self.temp_dir.cleanup()

    async def test_uploaded_file_requires_task_token(self) -> None:
        session = storage.create_session()
        response = await self.client.post(
            f"/api/sessions/{session.session_id}/files",
            files={"file": ("sales.csv", b"name,value\na,1\n", "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        file_id = response.json()["id"]
        denied = await self.client.get(f"/api/sessions/{session.session_id}/files/{file_id}/content")
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(storage.try_start_turn(session))
        active_token = session.tool_token
        allowed = await self.client.get(
            f"/api/sessions/{session.session_id}/files/{file_id}/content",
            headers={"X-Hermes-Task-Token": active_token},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertIn(b"name,value", allowed.content)
        storage.finish_turn(session)
        self.assertNotEqual(session.tool_token, active_token)
        expired = await self.client.get(
            f"/api/sessions/{session.session_id}/files/{file_id}/content",
            headers={"X-Hermes-Task-Token": active_token},
        )
        self.assertEqual(expired.status_code, 403)

    async def test_artifact_upload_is_limited_to_current_task(self) -> None:
        session = storage.create_session()
        self.assertTrue(storage.try_start_turn(session))
        missing_token = await self.client.put(
            f"/api/sessions/{session.session_id}/artifacts/result.csv",
            content=b"name,value\na,1\n",
            headers={"Content-Type": "text/csv"},
        )
        self.assertEqual(missing_token.status_code, 403)
        result = await self.client.put(
            f"/api/sessions/{session.session_id}/artifacts/result.csv",
            content=b"name,value\na,1\n",
            headers={
                "Content-Type": "text/csv",
                "X-Hermes-Task-Token": session.tool_token,
            },
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["kind"], "table")
        self.assertEqual(result.json()["table"]["columns"], ["name", "value"])
        image_result = await self.client.put(
            f"/api/sessions/{session.session_id}/artifacts/chart.png",
            content=b"\x89PNG\r\n\x1a\n",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Hermes-Task-Token": session.tool_token,
            },
        )
        self.assertEqual(image_result.status_code, 200)
        self.assertEqual(image_result.json()["kind"], "image")
        self.assertEqual(image_result.json()["mime"], "image/png")

    async def test_complete_task_uses_hermes_tools_for_files_and_results(self) -> None:
        session = (await self.client.post("/api/sessions")).json()
        session_id = session["session_id"]
        uploaded = await self.client.post(
            f"/api/sessions/{session_id}/files",
            files={"file": ("sales.csv", b"product,total\nA,10\nB,20\n", "text/csv")},
        )
        self.assertEqual(uploaded.status_code, 200)
        file_id = uploaded.json()["id"]
        task_session = storage.get(session_id)
        self.assertIsNotNone(task_session)

        async def fake_chat(messages, hermes_session_id, *, idempotency_key=None):
            self.assertEqual(hermes_session_id, session_id)
            self.assertIn("/files/" + file_id + "/content", messages[-1]["content"])
            downloaded = await self.client.get(
                f"/api/sessions/{session_id}/files/{file_id}/content",
                headers={"X-Hermes-Task-Token": task_session.tool_token},
            )
            self.assertEqual(downloaded.status_code, 200)
            self.assertIn(b"product,total", downloaded.content)
            artifact_response = await self.client.put(
                f"/api/sessions/{session_id}/artifacts/summary.csv",
                content=b"product,total\nB,20\nA,10\n",
                headers={
                    "Content-Type": "text/csv",
                    "X-Hermes-Task-Token": task_session.tool_token,
                },
            )
            self.assertEqual(artifact_response.status_code, 200)
            return "Сводка готова."

        with patch.object(hermes_client, "chat", fake_chat):
            response = await self.client.post(
                f"/api/sessions/{session_id}/messages",
                json={"text": "Суммируй файл", "file_ids": [file_id]},
            )
        self.assertEqual(response.status_code, 200)
        events = parse_sse_lines(response.text.splitlines())
        result = next(payload["message"] for name, payload in events if name == "result")
        self.assertEqual(result["text"], "Сводка готова.")
        self.assertEqual(result["artifacts"][0]["name"], "summary.csv")


class SseParserRegressionTests(unittest.TestCase):
    def test_parser_handles_multiline_data_and_missing_trailing_blank_line(self) -> None:
        lines = [
            "event: result",
            'data: {"message":',
            'data: {"text": "готово"}}',
            "",
            "event: done",
            'data: {"duration_ms": 42}',
        ]
        events = parse_sse_lines(lines)
        self.assertEqual(events[0], ("result", {"message": {"text": "готово"}}))
        self.assertEqual(events[1], ("done", {"duration_ms": 42}))


class FrontendAssetRegressionTests(unittest.TestCase):
    def test_layout_and_feedback_components_have_styles(self) -> None:
        css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        for selector in (
            ".app {", ".sidebar {", ".chat {", ".messages {", ".btn {",
            ".composer {", ".composer-row:focus-within", ".attachments {", ".chip {",
            ".dropzone:hover", ".spinner {", ".error-box {", ".table-wrap {",
            ".lightbox {", "@media (max-width: 740px)",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, css)
        self.assertIn('id="composer-hint"', html)
        self.assertIn('composerHint.textContent = "Загружаю файлы…"', js)
        self.assertIn("var failed = [];", js)



class StorageRegressionTests(unittest.TestCase):
    def test_delete_cleans_files_and_busy_turn_is_exclusive(self) -> None:
        previous_data_dir = settings.data_dir
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                settings.data_dir = Path(temp_dir)
                test_storage = Storage()
                session = test_storage.create_session()
                session_file = session.uploads_dir / "input.csv"
                session_file.write_text("a,b\n1,2\n", encoding="utf-8")
                self.assertTrue(test_storage.try_start_turn(session))
                self.assertFalse(test_storage.try_start_turn(session))
                self.assertFalse(test_storage.delete(session.session_id))
                test_storage.finish_turn(session)
                self.assertTrue(test_storage.delete(session.session_id))
                self.assertFalse(session.uploads_dir.parent.exists())
        finally:
            settings.data_dir = previous_data_dir


if __name__ == "__main__":
    unittest.main()
