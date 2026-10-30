"""Хранилище сессий чата.

Состояние сессий держим в памяти (демо-режим хакатона), а файлы и артефакты —
на диске в DATA_DIR, чтобы их можно было отдать интерфейсу по HTTP.
"""

from __future__ import annotations

import mimetypes
import secrets
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .schemas import Artifact, ChatMessage, FileInfo, SessionState

MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


def now_iso() -> str:
    """Текущее время в ISO-8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def guess_kind(mime: str, suffix: str) -> str:
    """Определяет тип артефакта для интерфейса."""
    if mime.startswith("image/"):
        return "image"
    if suffix in {".csv", ".tsv"}:
        return "table"
    return "file"


@dataclass
class Session:
    """Одна беседа: файлы, сообщения и артефакты."""

    session_id: str
    created_at: str
    uploads_dir: Path
    artifacts_dir: Path
    tool_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    files: dict[str, FileInfo] = field(default_factory=dict)
    file_paths: dict[str, Path] = field(default_factory=dict)
    messages: list[ChatMessage] = field(default_factory=list)
    artifact_index: dict[str, Artifact] = field(default_factory=dict)
    artifact_paths: dict[str, Path] = field(default_factory=dict)
    busy: bool = False


class Storage:
    """Потокобезопасный менеджер сессий."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def create_session(self) -> Session:
        with self._lock:
            session_id = uuid.uuid4().hex
            base = settings.data_dir / "sessions" / session_id
            session = Session(
                session_id=session_id,
                created_at=now_iso(),
                uploads_dir=base / "uploads",
                artifacts_dir=base / "artifacts",
            )
            session.uploads_dir.mkdir(parents=True, exist_ok=True)
            session.artifacts_dir.mkdir(parents=True, exist_ok=True)
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.busy:
                return False
            try:
                shutil.rmtree(session.uploads_dir.parent, ignore_errors=False)
            except FileNotFoundError:
                pass
            self._sessions.pop(session_id, None)
            return True

    def try_start_turn(self, session: Session) -> bool:
        """Атомарно резервирует сессию для одного запроса чата."""
        with self._lock:
            if session.busy or self._sessions.get(session.session_id) is not session:
                return False
            session.busy = True
            return True

    def finish_turn(self, session: Session) -> None:
        """Освобождает сессию после завершения или отмены SSE-потока."""
        with self._lock:
            session.busy = False
            session.tool_token = secrets.token_urlsafe(32)

    # --- файлы пользователя ---
    def save_upload(self, session: Session, filename: str, content: bytes) -> tuple[Path, FileInfo]:
        """Кладёт загруженный файл на диск и возвращает (путь, черновое описание)."""
        safe_name = Path(filename.replace("\\", "/")).name or "data.csv"
        safe_name = safe_name.replace("\x00", "") or "data.csv"
        file_id = uuid.uuid4().hex
        target = session.uploads_dir / f"{file_id}_{safe_name}"
        target.write_bytes(content)
        info = FileInfo(
            id=file_id,
            name=safe_name,
            size=len(content),
            rows=0,
            columns=[],
            column_types=[],
            encoding="",
            delimiter="",
            preview=[],
        )
        return target, info

    def attach_file(self, session: Session, file_id: str, path: Path, info: FileInfo) -> None:
        with self._lock:
            session.file_paths[file_id] = path
            session.files[file_id] = info

    def remove_file(self, session: Session, file_id: str) -> bool:
        with self._lock:
            path = session.file_paths.pop(file_id, None)
            session.files.pop(file_id, None)
        if path and path.exists():
            path.unlink(missing_ok=True)
        return path is not None

    # --- артефакты ---
    def register_artifact(
        self,
        session: Session,
        name: str,
        content: bytes,
        mime: str | None = None,
        table: dict | None = None,
        text: str | None = None,
    ) -> Artifact:
        """Сохраняет артефакт и возвращает его описание для интерфейса."""
        artifact_id = uuid.uuid4().hex[:12]
        suffix = Path(name).suffix.lower()
        guessed_mime = mime or MIME_BY_EXTENSION.get(suffix) or mimetypes.guess_type(name)[0]
        guessed_mime = guessed_mime or "application/octet-stream"
        kind = guess_kind(guessed_mime, suffix)

        target = session.artifacts_dir / f"{artifact_id}_{Path(name).name}"
        target.write_bytes(content)

        artifact = Artifact(
            id=artifact_id,
            name=Path(name).name,
            kind=kind,
            mime=guessed_mime,
            size=target.stat().st_size if target.exists() else 0,
            url=f"/api/artifacts/{session.session_id}/{artifact_id}",
            table=table,
            text=text,
        )
        with self._lock:
            session.artifact_index[artifact_id] = artifact
            session.artifact_paths[artifact_id] = target
        return artifact

    def artifact_path(self, session: Session, artifact_id: str) -> Path | None:
        with self._lock:
            return session.artifact_paths.get(artifact_id)

    def artifacts_since(self, session: Session, previous_ids: set[str]) -> list[Artifact]:
        with self._lock:
            return [
                artifact
                for artifact_id, artifact in session.artifact_index.items()
                if artifact_id not in previous_ids
            ]

    # --- сообщения ---
    def add_message(self, session: Session, message: ChatMessage) -> ChatMessage:
        with self._lock:
            session.messages.append(message)
        return message

    def state(self, session: Session) -> SessionState:
        with self._lock:
            return SessionState(
                session_id=session.session_id,
                created_at=session.created_at,
                files=list(session.files.values()),
                messages=list(session.messages),
                busy=session.busy,
            )


storage = Storage()
