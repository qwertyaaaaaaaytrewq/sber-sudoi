from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import settings

logger = logging.getLogger("hermes.client")


class HermesError(RuntimeError):
    """A protocol, transport, or upstream Hermes API error safe to show in the UI."""


def _v1_base_url() -> str:
    """Normalize HERMES_BASE_URL to the OpenAI-compatible /v1 base URL."""
    base = settings.hermes_base_url.strip().rstrip("/")
    if not base:
        raise HermesError("HERMES_BASE_URL не задан.")
    return base if base.endswith("/v1") else f"{base}/v1"


def _gateway_base_url() -> str:
    """Return the gateway root, which hosts /health/detailed and /api/sessions."""
    base = _v1_base_url()
    return base[:-3] if base.endswith("/v1") else base


def _trust_env_for_url(url: str) -> bool:
    """Use environment proxies only for non-local destinations.

    HTTPX defaults to trust_env=True. On Windows, a globally configured HTTP_PROXY/ALL_PROXY
    can therefore intercept requests to 127.0.0.1 unless NO_PROXY is also set correctly. That
    produces proxy-originated 503s even while the Hermes listener itself is healthy.
    """
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost") or host == "host.docker.internal":
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Keep normal proxy support for public DNS names and unknown hostnames.
        return True
    # Local, private, and link-local IP destinations should bypass outbound proxies.
    return not (address.is_loopback or address.is_private or address.is_link_local)


def _headers(
    session_id: str | None = None,
    *,
    session_key: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.hermes_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if session_id:
        headers["X-Hermes-Session-Id"] = session_id
    if session_key:
        if len(session_key) > 256 or re.search(r"[\r\n\x00]", session_key):
            raise HermesError("Некорректный X-Hermes-Session-Key.")
        headers["X-Hermes-Session-Key"] = session_key
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _extract_final_completion(payload: Any) -> str:
    """Extract only the completed assistant message from a non-streaming Chat Completion.

    Do not read reasoning_content, tool progress frames, tool arguments, or text deltas
    from intermediate agent iterations. The API server owns the tool loop; this client
    accepts only its final Chat Completion object.
    """
    if not isinstance(payload, dict):
        raise HermesError("Hermes Agent вернул JSON не в формате объекта Chat Completion.")

    upstream_error = payload.get("error")
    if upstream_error:
        if isinstance(upstream_error, dict):
            detail = upstream_error.get("message") or upstream_error.get("code") or str(upstream_error)
            code = upstream_error.get("code")
            prefix = f"code={code}: " if code else ""
        else:
            detail = str(upstream_error)
            prefix = ""
        raise HermesError(f"Hermes Agent сообщил об ошибке: {prefix}{str(detail)[:500]}")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise HermesError("Hermes Agent не вернул choices с завершённым ответом.")

    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "error":
        error = choice.get("error") or payload.get("error") or "неизвестная ошибка Gateway"
        raise HermesError(f"Hermes Agent завершил задачу с ошибкой: {str(error)[:500]}")
    if finish_reason == "cancelled":
        raise HermesError("Выполнение задачи в Hermes Agent было отменено.")
    if finish_reason not in {"stop", "length"}:
        raise HermesError(
            "Hermes Agent не вернул финальный текстовый ответ "
            f"(finish_reason={finish_reason or 'отсутствует'}). Результат не будет подменён выводом инструментов."
        )

    message = choice.get("message")
    if not isinstance(message, dict):
        raise HermesError("Hermes Agent завершил запрос без объекта message.")
    content = message.get("content")
    if isinstance(content, str):
        answer = content.strip()
    elif isinstance(content, list):
        answer = "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"text", "output_text"}
            and isinstance(part.get("text"), str)
            and part["text"].strip()
        ).strip()
    else:
        answer = ""

    if not answer:
        raise HermesError("Финальный ответ Hermes Agent не содержит текста.")
    if finish_reason == "length":
        logger.warning("Hermes final response ended with finish_reason=length; output may be incomplete")
    return answer


def _openai_payload(
    messages: list[dict[str, Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build only fields consumed by the Hermes Agent Chat Completions route.

    This application deliberately requests stream=false. Hermes owns the tool loop and
    returns one completed Chat Completion; streaming intermediate assistant turns would
    concatenate plans/tool-adjacent text with the final user-facing answer.
    """
    if not isinstance(messages, list) or not messages:
        raise HermesError("В запросе Hermes отсутствует массив messages.")
    if any(
        not isinstance(message, dict)
        or not isinstance(message.get("role"), str)
        or "content" not in message
        for message in messages
    ):
        raise HermesError("Некорректная структура messages для Hermes Chat Completions API.")

    normalized_messages = [dict(message) for message in messages]
    if session_id:
        if len(session_id) > 256 or re.search(r"[\r\n\x00]", session_id):
            raise HermesError("Некорректный X-Hermes-Session-Id.")
        system_messages = [message for message in normalized_messages if message["role"] == "system"]
        latest_user = next(
            (message for message in reversed(normalized_messages) if message["role"] == "user"),
            None,
        )
        if latest_user is None:
            raise HermesError("Для продолжения сессии Hermes требуется новое сообщение role=user.")
        normalized_messages = system_messages + [latest_user]
    elif not any(message["role"] == "user" for message in normalized_messages):
        raise HermesError("Для Hermes Chat Completions требуется сообщение role=user.")

    # Keep payload strict: the v0.22.5 Chat Completions adapter consumes model/messages/stream.
    return {
        "model": settings.hermes_api_model,
        "messages": normalized_messages,
        "stream": False,
    }


def _json_error_details(body: str) -> tuple[str | None, str | None]:
    if not body.strip():
        return None, None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, body[:700].replace("\r", " ").replace("\n", " ").strip()
    if not isinstance(payload, dict):
        return None, body[:700].replace("\r", " ").replace("\n", " ").strip()
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or error.get("type")
        code_text = str(code).strip() if code is not None else None
        message_text = str(message).strip() if message is not None else None
        return code_text or None, message_text or None
    message = payload.get("message")
    return None, str(message).strip()[:700] if message is not None else None


def _http_error_message(
    status_code: int,
    body: str,
    *,
    body_read_error: str | None = None,
    retry_after: str | None = None,
) -> str:
    code, detail = _json_error_details(body)
    if status_code in {401, 403}:
        detail = "Hermes отклонил API-ключ или доступ к endpoint. Проверьте HERMES_API_KEY и API_SERVER_KEY."
    pieces = [f"Hermes Agent вернул HTTP {status_code}" ]
    if code:
        pieces.append(f"code={code}")
    if detail:
        pieces.append(detail)
    if retry_after:
        pieces.append(f"Retry-After={retry_after}")
    if body_read_error:
        pieces.append(f"тело ответа не удалось прочитать ({body_read_error})")
    if not detail and not body_read_error:
        pieces.append("тело ошибки пустое")
    pieces.append("Автоматический повтор отключён, чтобы не запустить агентную задачу дважды.")
    return "; ".join(pieces)


def _message_parts(messages: list[dict[str, Any]]) -> tuple[str, Any, list[dict[str, Any]]]:
    system_text = "\n\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "system"
    ).strip()
    user_messages = [message for message in messages if message.get("role") == "user"]
    if not user_messages:
        raise HermesError("Для запуска Hermes требуется сообщение role=user.")
    latest_user = user_messages[-1].get("content", "")
    history = [
        {"role": message["role"], "content": message.get("content", "")}
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]
    return system_text, latest_user, history


def _extract_responses_output(payload: Any) -> str:
    """Extract final assistant message(s), never commentary or tool/reasoning items."""
    if not isinstance(payload, dict):
        raise HermesError("Hermes Responses API вернул JSON не в формате объекта.")
    error = payload.get("error")
    if error:
        if isinstance(error, dict):
            detail = error.get("message") or error.get("code") or str(error)
        else:
            detail = str(error)
        raise HermesError(f"Hermes Responses API: {str(detail)[:500]}")
    status = payload.get("status")
    if status not in {None, "completed"}:
        detail = payload.get("incomplete_details") or payload.get("error") or status
        raise HermesError(f"Hermes Responses API не завершил задачу (status={status}): {str(detail)[:400]}")

    output = payload.get("output")
    if not isinstance(output, list):
        raise HermesError("Hermes Responses API не вернул итоговый массив output.")
    final_messages: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("role", "assistant") != "assistant":
            continue
        if item.get("phase") in {"analysis", "commentary", "justify", "confidence"}:
            continue
        if item.get("status") not in {None, "completed"}:
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        text_parts = [
            part.get("text", "")
            for part in parts
            if isinstance(part, dict)
            and part.get("type") in {"output_text", "text"}
            and isinstance(part.get("text"), str)
            and part.get("text", "").strip()
        ]
        if text_parts:
            final_messages.append("\n".join(text_parts).strip())
    answer = "\n\n".join(final_messages).strip()
    if not answer:
        raise HermesError("Hermes Responses API завершился без финального текстового ответа.")
    return answer


def _extract_run_output(payload: Any) -> str | None:
    """Return final output on completion; None means the run is still active."""
    if not isinstance(payload, dict):
        raise HermesError("Hermes Runs API вернул JSON не в формате объекта.")
    status = payload.get("status")
    if status == "completed":
        output = payload.get("output")
        if isinstance(output, str) and output.strip():
            return output.strip()
        if isinstance(output, dict):
            return _extract_responses_output(output)
        raise HermesError("Hermes Run завершился, но поле output пустое.")
    if status in {"failed", "cancelled", "interrupted"}:
        detail = payload.get("error") or payload.get("message") or status
        raise HermesError(f"Hermes Run завершился со статусом {status}: {str(detail)[:500]}")
    if status == "unknown":
        raise HermesError(
            "Hermes сообщил status=unknown: результат выполнения неизвестен после перезапуска Gateway. "
            "Проверьте состояние задачи, прежде чем запускать её повторно."
        )
    if status in {"waiting_for_approval", "waiting_for_clarification"}:
        raise HermesError(
            f"Hermes ожидает вмешательства пользователя (status={status}); автоматическое продолжение невозможно."
        )
    if status in {"started", "queued", "running", "stopping", "pending"}:
        return None
    raise HermesError(f"Hermes Runs API вернул неизвестный статус: {status!r}.")


async def _capability_features(client: httpx.AsyncClient) -> dict[str, Any]:
    """Discover stable API features; older Gateways may not expose /capabilities."""
    try:
        response = await client.get(f"{_v1_base_url()}/capabilities", headers=_headers())
        if response.status_code != 200:
            return {}
        payload = response.json()
        features = payload.get("features", {}) if isinstance(payload, dict) else {}
        return features if isinstance(features, dict) else {}
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return {}


async def _run_via_runs(
    client: httpx.AsyncClient,
    messages: list[dict[str, Any]],
    session_id: str | None,
    headers: dict[str, str],
) -> str:
    base = _v1_base_url()
    instructions, latest_user, history = _message_parts(messages)
    request_body: dict[str, Any] = {
        "model": settings.hermes_api_model,
        "input": latest_user,
        "instructions": instructions,
    }
    if session_id:
        request_body["session_id"] = session_id
    elif len(history) > 1:
        request_body["conversation_history"] = history[:-1]

    response = await client.post(
        f"{base}/runs",
        json=request_body,
        headers=_headers(session_key=session_id, idempotency_key=headers.get("Idempotency-Key")),
    )
    if response.status_code >= 400:
        raise HermesError(_http_error_message(
            response.status_code,
            response.text,
            retry_after=response.headers.get("Retry-After"),
        ))
    try:
        created = response.json()
    except (ValueError, json.JSONDecodeError) as error:
        raise HermesError("Hermes Runs API вернул некорректный JSON при создании задачи.") from error
    if not isinstance(created, dict) or not isinstance(created.get("run_id"), str) or not created["run_id"]:
        raise HermesError("Hermes Runs API не вернул run_id; задача не будет отправлена повторно.")

    run_url = f"{base}/runs/{created['run_id']}"
    delay = 0.5
    while True:
        await asyncio.sleep(delay)
        status_response = await client.get(run_url, headers=_headers())
        if status_response.status_code >= 400:
            raise HermesError(_http_error_message(
                status_response.status_code,
                status_response.text,
                retry_after=status_response.headers.get("Retry-After"),
            ))
        try:
            run_state = status_response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise HermesError("Hermes Runs API вернул некорректный JSON при проверке статуса.") from error
        final = _extract_run_output(run_state)
        if final is not None:
            return final
        delay = min(delay * 1.35, 2.0)


async def _run_via_responses(
    client: httpx.AsyncClient,
    messages: list[dict[str, Any]],
    session_id: str | None,
    headers: dict[str, str],
) -> str:
    base = _v1_base_url()
    instructions, latest_user, history = _message_parts(messages)
    request_body: dict[str, Any] = {
        "model": settings.hermes_api_model,
        "input": latest_user if session_id else history,
        "instructions": instructions,
        "store": True,
    }
    if session_id:
        request_body["conversation"] = session_id
    response = await client.post(
        f"{base}/responses",
        json=request_body,
        headers=_headers(session_key=session_id, idempotency_key=headers.get("Idempotency-Key")),
    )
    if response.status_code >= 400:
        raise HermesError(_http_error_message(
            response.status_code,
            response.text,
            retry_after=response.headers.get("Retry-After"),
        ))
    try:
        result = response.json()
    except (ValueError, json.JSONDecodeError) as error:
        raise HermesError("Hermes Responses API вернул некорректный JSON.") from error
    return _extract_responses_output(result)


async def chat(
    messages: list[dict[str, Any]],
    session_id: str | None = None,
    *,
    idempotency_key: str | None = None,
) -> str:
    """Run one Hermes turn; prefer Runs API and expose only the final assistant output."""
    url = f"{_v1_base_url()}/chat/completions"
    payload = _openai_payload(messages, session_id)
    if idempotency_key is not None and (
        not idempotency_key.strip()
        or len(idempotency_key) > 255
        or re.search(r"[^\x21-\x7e]", idempotency_key)
    ):
        raise HermesError("Некорректный Idempotency-Key для Hermes API: допускаются 1–255 печатных ASCII-символов.")
    request_key = idempotency_key or uuid.uuid4().hex
    headers = _headers(session_id, idempotency_key=request_key)
    timeout = httpx.Timeout(connect=15.0, read=None, write=60.0, pool=15.0)

    try:
        async with asyncio.timeout(settings.hermes_timeout_s):
            async with httpx.AsyncClient(timeout=timeout, trust_env=_trust_env_for_url(url)) as client:
                features = await _capability_features(client)
                if features.get("run_submission") and features.get("run_status"):
                    # Runs API has a durable run_id and a distinct completed output. This avoids
                    # confusing interim assistant text or tool events with the final answer.
                    logger.info("Используется Hermes Runs API: ожидание статуса completed и финального output")
                    return await _run_via_runs(client, messages, session_id, headers)
                if features.get("responses_api"):
                    # Responses separates function_call/tool output/commentary from final messages.
                    logger.info("Используется Hermes Responses API: промежуточные tool/reasoning items исключены")
                    return await _run_via_responses(client, messages, session_id, headers)

                # Compatibility path for older Hermes Gateway versions.
                logger.warning("Gateway не рекламирует Runs/Responses API; используется legacy Chat Completions")
                response = await client.post(url, json=payload, headers=_headers(session_id, idempotency_key=request_key))
                if response.status_code >= 400:
                    raise HermesError(_http_error_message(
                        response.status_code,
                        response.text,
                        retry_after=response.headers.get("Retry-After"),
                    ))
                content_type = response.headers.get("Content-Type", "").lower()
                if content_type and "application/json" not in content_type and "+json" not in content_type:
                    raise HermesError(
                        "Hermes вернул HTTP 200, но вместо JSON Chat Completion пришёл "
                        f"Content-Type={content_type or 'не указан'}."
                    )
                try:
                    result = response.json()
                except (ValueError, json.JSONDecodeError) as error:
                    excerpt = response.text[:500].replace("\r", " ").replace("\n", " ").strip()
                    raise HermesError(
                        "Hermes вернул HTTP 200 с некорректным JSON Chat Completion"
                        + (f": {excerpt}" if excerpt else ".")
                    ) from error
                return _extract_final_completion(result)
    except TimeoutError as error:
        raise HermesError(
            f"Задача превысила общий лимит ожидания HERMES_TIMEOUT_S={settings.hermes_timeout_s:g} секунд. "
            "Hermes мог продолжить выполнение; проверьте состояние задачи перед повторным запуском."
        ) from error
    except httpx.HTTPError as error:
        raise HermesError(
            "Не удалось получить корректный HTTP-ответ от Hermes Agent API "
            f"({type(error).__name__}: {error}). Результат выполнения неизвестен; "
            "автоматический повтор отключён."
        ) from error


async def health() -> dict[str, Any]:
    """Probe the documented health endpoint and preserve enough detail to diagnose failures."""
    url = f"{_v1_base_url()}/health"
    result: dict[str, Any] = {"ok": False, "status": None, "url": url}
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=_trust_env_for_url(url)) as client:
            response = await client.get(url, headers=_headers())
            result["status"] = response.status_code
            try:
                body: Any = response.json()
            except (ValueError, json.JSONDecodeError):
                body = response.text[:1000]
            if response.status_code == 200:
                result["body"] = body
                result["ok"] = isinstance(body, dict) and body.get("status") == "ok"
            else:
                result["body"] = body
                logger.warning(
                    "Hermes health probe failed: HTTP %s; body=%s",
                    response.status_code,
                    json.dumps(body, ensure_ascii=False)[:500] if isinstance(body, (dict, list)) else str(body)[:500],
                )
                # /health/detailed is a gateway-root endpoint, not /v1/health/detailed.
                try:
                    detailed_url = f"{_gateway_base_url()}/health/detailed"
                    detailed = await client.get(detailed_url, headers=_headers())
                    result["detailed_status"] = detailed.status_code
                    if detailed.status_code == 200:
                        try:
                            result["detailed"] = detailed.json()
                        except ValueError:
                            result["detailed"] = detailed.text[:1000]
                    else:
                        result["detailed"] = detailed.text[:1000]
                except httpx.HTTPError as detail_error:
                    result["detailed_error"] = f"{type(detail_error).__name__}: {detail_error}"
    except httpx.HTTPError as error:
        result["error"] = f"{type(error).__name__}: {error}"
    return result


async def delete_session(session_id: str) -> None:
    """Best-effort cleanup of the Hermes-owned transcript matching the UI session id."""
    url = f"{_gateway_base_url()}/api/sessions/{session_id}"
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=_trust_env_for_url(url)) as client:
            response = await client.delete(url, headers=_headers())
        if response.status_code not in {200, 204, 404}:
            logger.debug("Не удалось удалить сессию Hermes %s: HTTP %s", session_id, response.status_code)
    except httpx.HTTPError as error:
        logger.debug("Не удалось удалить сессию Hermes %s: %s", session_id, error)
