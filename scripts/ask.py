"""Быстрый ручной запрос к чату: сессия (при необходимости с CSV) и один вопрос.

Примеры:

    py -3.14 scripts/ask.py --question "Что такое медиана?"
    py -3.14 scripts/ask.py --csv samples/sales.csv --question "Топ-5 категорий по выручке"

Скрипт печатает стадии обработки, итоговый текст и список артефактов.
Полезен, когда нужно проверить разные формулировки вопросов.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from sse_utils import parse_sse_lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Один вопрос к чату Hermes")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--csv", default="", help="необязательный CSV для загрузки")
    parser.add_argument("--question", required=True)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base, timeout=1800.0)
    session_id = client.post("/api/sessions").json()["session_id"]

    file_ids: list[str] = []
    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"Нет файла {csv_path}", file=sys.stderr)
            return 2
        with csv_path.open("rb") as handle:
            uploaded = client.post(
                f"/api/sessions/{session_id}/files",
                files={"file": (csv_path.name, handle, "text/csv")},
            )
        uploaded.raise_for_status()
        info = uploaded.json()
        file_ids = [info["id"]]
        print(
            f"Файл: {info['name']} — {info['rows']} строк, "
            f"{len(info['columns'])} колонок, кодировка {info['encoding']}, "
            f"разделитель '{info['delimiter']}'"
        )

    print("Вопрос:", args.question)
    with client.stream(
        "POST",
        f"/api/sessions/{session_id}/messages",
        json={"text": args.question, "file_ids": file_ids},
    ) as response:
        response.raise_for_status()
        for name, payload in parse_sse_lines(response.iter_lines()):
            if name == "stage":
                print(f"  [{payload.get('stage')}] {payload.get('message')}")
            elif name == "result":
                message = payload["message"]
                print("\n--- ОТВЕТ ---")
                print(message["text"])
                print(
                    f"\nАртефакты: {[a['kind'] for a in message['artifacts']] or 'нет'}"
                    f" · {message['duration_ms']} мс"
                )
            elif name == "error":
                print("  ошибка:", payload.get("message"), file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
