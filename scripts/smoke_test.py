"""Сквозная проверка пайплайна: сессия → CSV → запрос → SSE → артефакты.

Запуск (сервисы должны быть запущены):

    python scripts/smoke_test.py --base http://127.0.0.1:8000 --csv samples/sales.csv

Скрипт проверяет сквозной вызов Hermes, ответ и отсутствие утечки кода в итоговый текст.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from sse_utils import parse_sse_lines

CODE_MARKERS = (
    "```python",
    "plt.savefig",
    "import pandas as pd",
    "import sys, os, subprocess, importlib",
    "subprocess.run([sys.executable",
    "def ensure(",
    "from pathlib import Path",
    "Создаю директорию и PLAN.md",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Сквозная проверка чата Hermes")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--csv", default="samples/sales.csv")
    parser.add_argument(
        "--question",
        default="Построй график выручки по категориям и покажи топ-5",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Нет файла {csv_path}", file=sys.stderr)
        return 2

    client = httpx.Client(base_url=args.base, timeout=1800.0)
    health = client.get("/api/health").json()
    print("Здоровье:", json.dumps(health, ensure_ascii=False))

    session = client.post("/api/sessions").json()
    session_id = session["session_id"]
    print("Сессия:", session_id)

    with csv_path.open("rb") as handle:
        uploaded = client.post(
            f"/api/sessions/{session_id}/files",
            files={"file": (csv_path.name, handle, "text/csv")},
        )
    uploaded.raise_for_status()
    info = uploaded.json()
    print(
        f"Файл: {info['name']} — {info['rows']} строк, "
        f"{len(info['columns'])} колонок, кодировка {info['encoding']}, "
        f"разделитель '{info['delimiter']}'"
    )
    print("Колонки:", ", ".join(f"{name} ({kind})" for name, kind in zip(info["columns"], info["column_types"])))

    stages: list[str] = []
    final_message: dict | None = None
    error: str | None = None
    with client.stream(
        "POST",
        f"/api/sessions/{session_id}/messages",
        json={"text": args.question, "file_ids": [info["id"]]},
    ) as response:
        response.raise_for_status()
        for name, payload in parse_sse_lines(response.iter_lines()):
            if name == "stage":
                stages.append(payload.get("message", ""))
                print(f"  стадия [{payload.get('stage')}]: {payload.get('message')}")
            elif name == "result":
                final_message = payload["message"]
            elif name == "error":
                error = payload.get("message")
                print("  ошибка:", error)
            elif name == "accepted":
                print("  запрос принят:", payload["message"]["text"])

    if final_message is None:
        print("Нет итогового сообщения", file=sys.stderr)
        return 1

    text = final_message["text"]
    artifacts = final_message["artifacts"]
    kinds = [artifact["kind"] for artifact in artifacts]

    print("\n--- ОТВЕТ ПОЛЬЗОВАТЕЛЮ ---")
    print(text)
    print(f"\nАртефакты: {kinds or 'нет'} · время: {final_message['duration_ms']} мс")

    for artifact in artifacts:
        if artifact["kind"] == "table" and artifact["table"]:
            rows = artifact["table"]["rows"]
            print(f"  таблица {artifact['name']}: {len(rows)} строк, "
                  f"колонки {artifact['table']['columns']}")
        elif artifact["kind"] == "image":
            image = client.get(artifact["url"])
            print(f"  картинка {artifact['name']}: {len(image.content)} байт, "
                  f"{image.headers.get('content-type')}")

        elif artifact["kind"] == "file":
            document = client.get(artifact["url"])
            print(f"  текст {artifact['name']}: {len(document.content)} байт, "
                  f"превью {len(artifact.get('text') or '')} символов")
    leaked = [marker for marker in CODE_MARKERS if marker in text]
    checks = {
        "итоговый текст непустой": bool(text.strip()),
        "код не показан пользователю": not leaked,
        "ошибок не было": error is None,
    }
    print("\n--- ПРОВЕРКИ ---")
    for name, ok in checks.items():
        print(("  [OK]  " if ok else "  [FAIL] ") + name)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
