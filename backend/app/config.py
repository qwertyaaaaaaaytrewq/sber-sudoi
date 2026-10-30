"""Конфигурация backend-сервиса из переменных окружения."""

from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


class Settings:
    """Настройки приложения (singleton по факту использования)."""

    def __init__(self) -> None:
        # --- хранилище ---
        self.data_dir = Path(_str("DATA_DIR", "./data")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # --- Hermes API ---
        self.hermes_base_url = _str("HERMES_BASE_URL", "http://127.0.0.1:8642/v1").rstrip("/")
        self.hermes_api_key = _str("HERMES_API_KEY", "change-me-local-dev")
        self.hermes_model = _str("HERMES_MODEL", "pvx-ai-test:latest")
        self.hermes_api_model = _str("HERMES_API_MODEL", "hermes-agent")
        self.hermes_timeout_s = _float("HERMES_TIMEOUT_S", 1800.0)
        self.hermes_callback_base_url = _str(
            "HERMES_CALLBACK_BASE_URL", "http://127.0.0.1:8000/api"
        ).rstrip("/")
        self.artifact_max_mb = _int("ARTIFACT_MAX_MB", 12)

        # --- Загрузка файлов ---
        self.max_upload_mb = _int("MAX_UPLOAD_MB", 20)
        self.preview_rows = _int("PREVIEW_ROWS", 5)
        self.max_table_rows = _int("MAX_TABLE_ROWS", 200)
        self.max_table_cols = _int("MAX_TABLE_COLS", 40)

        # --- Прочее ---
        self.cors_origins = [
            origin.strip()
            for origin in _str("CORS_ORIGINS", "*").split(",")
            if origin.strip()
        ]
        self.log_level = _str("LOG_LEVEL", "INFO")


settings = Settings()
