"""Общие утилиты для чтения Server-Sent Events в CLI-скриптах."""

from __future__ import annotations

import json
from collections.abc import Iterable


def parse_sse_lines(lines: Iterable[str]) -> list[tuple[str, dict]]:
    """Разбирает строки SSE в список (имя события, JSON-данные)."""
    events: list[tuple[str, dict]] = []
    event_name = "message"
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if data_lines:
            events.append((event_name, json.loads("\n".join(data_lines))))
        event_name, data_lines = "message", []

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            flush()
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    flush()  # сервер может закрыть поток без завершающей пустой строки
    return events
