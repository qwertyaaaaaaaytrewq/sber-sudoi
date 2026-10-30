from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    id: str
    name: str
    size: int
    rows: int
    columns: list[str]
    column_types: list[str]
    encoding: str
    delimiter: str
    preview: list[list[str]] = Field(default_factory=list)


class Artifact(BaseModel):
    id: str
    name: str
    kind: Literal["image", "table", "file"]
    mime: str
    size: int
    url: str
    table: dict[str, Any] | None = None
    text: str | None = None


class ChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "system"]
    text: str
    created_at: str
    file_ids: list[str] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None


class SessionState(BaseModel):
    session_id: str
    created_at: str
    files: list[FileInfo] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    busy: bool = False


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    file_ids: list[str] | None = None


class PublicConfig(BaseModel):
    hermes_model: str
    hermes_base_url: str
    max_upload_mb: int
    max_artifact_mb: int


class HealthResponse(BaseModel):
    status: str
    hermes: dict[str, Any]
