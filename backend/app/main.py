from __future__ import annotations

import csv
import json
import logging
import mimetypes
import secrets
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from . import csv_utils, hermes_client, pipeline
from .config import settings
from .schemas import Artifact, FileInfo, HealthResponse, MessageRequest, PublicConfig, SessionState
from .storage import storage

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("hermes.api")

ALLOWED_SUFFIXES = {
    ".csv", ".tsv", ".txt", ".md", ".json", ".jsonl", ".yaml", ".yml", ".xml",
    ".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".zip", ".py", ".js", ".ts", ".html", ".css", ".sql",
}
BLOCKED_ARTIFACT_SUFFIXES = {".bat", ".cmd", ".com", ".dll", ".exe", ".js", ".msi", ".ps1", ".py", ".sh"}
app = FastAPI(title="Hermes Chat API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_session(session_id: str):
    session = storage.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена. Создайте новую.")
    return session


def _verify_task_token(session, token: str) -> None:
    if not token or not secrets.compare_digest(session.tool_token.encode(), token.encode()):
        raise HTTPException(status_code=403, detail="Недействительный токен задачи.")


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    hermes_state = await hermes_client.health()
    return HealthResponse(status="ok" if hermes_state.get("ok") else "degraded", hermes=hermes_state)


@app.get("/api/config", response_model=PublicConfig)
async def public_config() -> PublicConfig:
    return PublicConfig(
        hermes_model=settings.hermes_model,
        hermes_base_url=settings.hermes_base_url,
        max_upload_mb=settings.max_upload_mb,
        max_artifact_mb=settings.artifact_max_mb,
    )


@app.post("/api/sessions", response_model=SessionState)
async def create_session() -> SessionState:
    return storage.state(storage.create_session())


@app.get("/api/sessions/{session_id}", response_model=SessionState)
async def get_session(session_id: str) -> SessionState:
    return storage.state(_require_session(session_id))


@app.post("/api/sessions/{session_id}/files", response_model=FileInfo)
async def upload_file(session_id: str, file: UploadFile = File(...)) -> FileInfo:
    session = _require_session(session_id)
    if session.busy:
        raise HTTPException(status_code=409, detail="Нельзя менять файлы во время обработки запроса.")
    upload_limit = settings.max_upload_mb * 1024 * 1024
    raw = await file.read(upload_limit + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="Файл пустой.")
    if len(raw) > upload_limit:
        raise HTTPException(status_code=413, detail=f"Файл больше {settings.max_upload_mb} МБ.")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Тип этого файла не поддерживается.")
    if storage.get(session_id) is not session:
        raise HTTPException(status_code=404, detail="Сессия не найдена. Создайте новую.")
    if session.busy:
        raise HTTPException(status_code=409, detail="Нельзя менять файлы во время обработки запроса.")
    path, info = storage.save_upload(session, file.filename or "input.bin", raw)
    if suffix in {".csv", ".tsv"}:
        try:
            profile = csv_utils.load_csv(path, settings.preview_rows, max_rows=5000)
        except (OSError, csv.Error, UnicodeError, ValueError) as error:
            path.unlink(missing_ok=True)
            logger.info("Не удалось разобрать таблицу %s: %s", info.name, error)
            raise HTTPException(status_code=400, detail="Не удалось прочитать табличные данные в файле.") from error
        if not profile["columns"]:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Не удалось разобрать таблицу в файле.")
        info = info.model_copy(
            update={
                "rows": profile["row_count"],
                "columns": profile["columns"],
                "column_types": profile["column_types"],
                "encoding": profile["encoding"],
                "delimiter": profile["delimiter"],
                "preview": profile["preview"],
            }
        )
    storage.attach_file(session, info.id, path, info)
    logger.info("Файл %s (%s строк) загружен в сессию %s", info.name, info.rows, session_id)
    return info


@app.get("/api/sessions/{session_id}/files/{file_id}/content")
async def get_file_content(
    session_id: str,
    file_id: str,
    x_hermes_task_token: str = Header(default=""),
) -> FileResponse:
    session = _require_session(session_id)
    _verify_task_token(session, x_hermes_task_token)
    if not session.busy:
        raise HTTPException(status_code=409, detail="Входные файлы доступны только во время выполнения задачи.")
    path = session.file_paths.get(file_id)
    file_info = session.files.get(file_id)
    if path is None or file_info is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден.")
    media_type = mimetypes.guess_type(file_info.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=file_info.name, headers={"Cache-Control": "no-store"})


@app.delete("/api/sessions/{session_id}/files/{file_id}")
async def remove_file(session_id: str, file_id: str) -> dict:
    session = _require_session(session_id)
    if session.busy:
        raise HTTPException(status_code=409, detail="Нельзя менять файлы во время обработки запроса.")
    if not storage.remove_file(session, file_id):
        raise HTTPException(status_code=404, detail="Файл не найден.")
    return {"deleted": True}


@app.put("/api/sessions/{session_id}/artifacts/{artifact_name}", response_model=Artifact)
async def upload_artifact(
    session_id: str,
    artifact_name: str,
    request: Request,
    x_hermes_task_token: str = Header(default=""),
) -> Artifact:
    session = _require_session(session_id)
    _verify_task_token(session, x_hermes_task_token)
    if not session.busy:
        raise HTTPException(status_code=409, detail="Загрузка результатов разрешена только во время выполнения задачи.")
    safe_name = Path(artifact_name.replace("\\", "/")).name
    if (
        not safe_name
        or safe_name in {".", ".."}
        or safe_name != artifact_name
        or any(ord(character) < 32 for character in safe_name)
    ):
        raise HTTPException(status_code=400, detail="Некорректное имя файла.")
    if Path(safe_name).suffix.lower() in BLOCKED_ARTIFACT_SUFFIXES:
        raise HTTPException(status_code=415, detail="Этот тип файла нельзя загружать в результаты.")
    size_limit = settings.artifact_max_mb * 1024 * 1024
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > size_limit:
        raise HTTPException(status_code=413, detail=f"Файл больше {settings.artifact_max_mb} МБ.")
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > size_limit:
            raise HTTPException(status_code=413, detail=f"Файл больше {settings.artifact_max_mb} МБ.")
        chunks.append(chunk)
    if not total:
        raise HTTPException(status_code=400, detail="Файл результата пустой.")
    content = b"".join(chunks)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    guessed_mime = mimetypes.guess_type(safe_name)[0]
    mime = guessed_mime if content_type in {"", "application/octet-stream"} and guessed_mime else content_type
    mime = mime or "application/octet-stream"
    artifact = storage.register_artifact(session, safe_name, content=content, mime=mime)
    path = storage.artifact_path(session, artifact.id)
    if path and artifact.kind == "table":
        artifact.table = csv_utils.read_table(path, settings.max_table_rows, settings.max_table_cols)
    if path and (mime.startswith("text/") or mime == "application/json") and total <= 100_000:
        try:
            artifact.text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            artifact.text = content.decode("utf-8", errors="replace")
    return artifact


@app.get("/api/artifacts/{session_id}/{artifact_id}")
async def get_artifact(session_id: str, artifact_id: str) -> FileResponse:
    session = _require_session(session_id)
    path = storage.artifact_path(session, artifact_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Артефакт не найден.")
    artifact = session.artifact_index.get(artifact_id)
    return FileResponse(
        path,
        media_type=artifact.mime if artifact else None,
        filename=artifact.name if artifact else path.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _sse_format(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/sessions/{session_id}/messages")
async def post_message(session_id: str, request: MessageRequest) -> StreamingResponse:
    session = _require_session(session_id)
    file_ids = list(dict.fromkeys(request.file_ids)) if request.file_ids is not None else None
    missing_file_ids = set(file_ids or ()) - session.files.keys()
    if missing_file_ids:
        raise HTTPException(status_code=400, detail="Один или несколько прикреплённых файлов не найдены.")
    if not storage.try_start_turn(session):
        raise HTTPException(status_code=409, detail="Предыдущий запрос ещё обрабатывается.")

    async def event_stream():
        try:
            async for event in pipeline.run_turn(session, request.text, file_ids):
                yield _sse_format(event["event"], event["data"])
        finally:
            storage.finish_turn(session)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    if storage.get(session_id) is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена.")
    if not storage.delete(session_id):
        raise HTTPException(status_code=409, detail="Нельзя удалить сессию во время обработки запроса.")
    await hermes_client.delete_session(session_id)
    return {"deleted": True}
