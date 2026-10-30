from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator

from . import hermes_client, prompts
from .config import settings
from .schemas import Artifact, ChatMessage
from .storage import Session, now_iso, storage

logger = logging.getLogger("hermes.pipeline")


def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": data}


def _resolve_files(session: Session, file_ids: list[str] | None) -> list:
    if file_ids is not None:
        return [session.files[file_id] for file_id in file_ids if file_id in session.files]
    return list(session.files.values())


def _dump(message: ChatMessage) -> dict:
    return json.loads(message.model_dump_json())


async def run_turn(session: Session, text: str, file_ids: list[str] | None) -> AsyncIterator[dict]:
    started = time.perf_counter()
    try:
        files = _resolve_files(session, file_ids)
        user_message = ChatMessage(
            id=uuid.uuid4().hex,
            role="user",
            text=text,
            created_at=now_iso(),
            file_ids=[item.id for item in files],
        )
        storage.add_message(session, user_message)
        yield _sse("accepted", {"message": _dump(user_message)})

        existing_artifacts = set(session.artifact_index)
        yield _sse("stage", {"stage": "working", "message": "Hermes выполняет задачу своими инструментами…"})
        response_text = await hermes_client.chat(
            [
                {"role": "system", "content": prompts.SYSTEM_PROMPT},
                {"role": "user", "content": prompts.build_user_message(text, files, session)},
            ],
            session.session_id,
            idempotency_key=user_message.id,
        )
        collected = storage.artifacts_since(session, existing_artifacts)
        if files and not collected and prompts.looks_like_unfinished_plan(response_text):
            logger.warning(
                "Hermes returned a plan instead of a completed file task; requesting one execution continuation"
            )
            yield _sse(
                "stage",
                {
                    "stage": "continuing",
                    "message": "Hermes вернул только план. Продолжаю выполнение и проверяю фактический результат…",
                },
            )
            continuation_text = (
                "ПРОДОЛЖЕНИЕ НЕЗАВЕРШЁННОЙ ЗАДАЧИ. Предыдущий ответ содержал план, текст `Plan.md` и/или строку `execute_code:` "
                "вместо проверенного результата. Это НЕ является выполнением инструмента. Не повторяй план, не печатай код "
                "как будто он запущен. Сейчас вызови нужный инструмент штатным tool-call механизмом Hermes, проверь его реальный "
                "результат и продолжи исходную задачу до конца. Сохрани и загрузи итоговые артефакты, если они уместны. "
                "Если вызов инструмента недоступен, остановись и сообщи конкретный блокер. Верни только проверенный итог.\n\n"
                f"Исходная задача пользователя: {text}"
            )
            response_text = await hermes_client.chat(
                [
                    {"role": "system", "content": prompts.SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": prompts.build_user_message(continuation_text, files, session),
                    },
                ],
                session.session_id,
                idempotency_key=f"{user_message.id}-continue-1",
            )
            collected = storage.artifacts_since(session, existing_artifacts)
            if not collected and prompts.looks_like_unfinished_plan(response_text):
                raise hermes_client.HermesError(
                    "Hermes дважды вернул план или имитацию вызова инструмента обычным текстом вместо результата. Этот текст не показан как ответ. "
                    "JSON с `tool_name`/`tool_input` не является выполнением инструмента. Хотя GET /v1/toolsets может показывать `terminal` "
                    "и `code_execution` (с `execute_code`) включёнными, это не доказывает реальный вызов. Проверьте прямой POST /v1/responses "
                    "и журналы Gateway. "
                    "Если короткий прямой запрос выполняет инструмент, а Hermes Chat печатает псевдовызов, проверьте "
                    "`hermes config set agent.tool_use_enforcement true`, перезапустите Gateway и сравните системные инструкции."
                )
        final_text = response_text.strip() or "Hermes завершил запрос без текстового ответа."
        assistant_message = ChatMessage(
            id=uuid.uuid4().hex[:12],
            role="assistant",
            text=final_text,
            created_at=now_iso(),
            artifacts=collected,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        storage.add_message(session, assistant_message)
        yield _sse("result", {"message": _dump(assistant_message)})
    except hermes_client.HermesError as error:
        logger.warning("Сбой обработки запроса: %s", error)
        yield _sse("error", {"message": str(error)})
    except Exception:
        logger.exception("Непредвиденная ошибка обработки запроса")
        yield _sse("error", {"message": "Внутренняя ошибка сервиса. Попробуйте ещё раз."})
    yield _sse("done", {"duration_ms": int((time.perf_counter() - started) * 1000)})
