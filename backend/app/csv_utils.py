"""Разбор CSV без внешних зависимостей: кодировка, разделитель, профиль колонок.

Профиль колонок уходит в промпт для Hermes, а preview — в интерфейс, чтобы
пользователь видел, что именно загрузил.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")
DELIMITERS = (";", ",", "\t", "|")

_NUM_RE = re.compile(r"^-?\d+([.,]\d+)?$")
_DATE_RE = re.compile(r"^\d{1,4}[-./]\d{1,2}[-./]\d{1,4}$")
_MONEY_RE = re.compile(r"^-?\d[\d\s\u00a0]*(?:[.,]\d+)?$")


def _decode(raw: bytes) -> tuple[str, str]:
    """Возвращает (текст, использованная кодировка)."""
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\ufffd" not in text:
            return text, encoding
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(DELIMITERS))
        if dialect.delimiter in DELIMITERS:
            return dialect.delimiter
    except csv.Error:
        pass
    # Ручной фолбэк: выбираем разделитель с самым стабильным числом вхождений.
    best, best_score = ";", -1.0
    for delimiter in DELIMITERS:
        counts = [line.count(delimiter) for line in sample.splitlines() if line.strip()]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        score = counts.count(mode) + mode
        if mode > 0 and score > best_score:
            best, best_score = delimiter, score
    return best


def _guess_type(values: list[str]) -> str:
    non_empty = [value for value in values if value not in ("", None)]
    if not non_empty:
        return "пусто"
    if all(_NUM_RE.match(value.strip()) or _MONEY_RE.match(value.strip()) for value in non_empty):
        return "число"
    if all(_DATE_RE.match(value.strip()) for value in non_empty):
        return "дата"
    return "текст"


def _read_rows(text: str, delimiter: str, limit: int) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    # limit — число строк данных; плюс одна строка заголовка.
    max_records = max(1, limit + 1)
    for row in reader:
        if not row or all(cell.strip() == "" for cell in row):
            continue
        if len(rows) >= max_records:
            break
        rows.append([cell.strip() for cell in row])
    return rows


def _count_data_rows(text: str, delimiter: str) -> int:
    """Считает все строки данных, не сохраняя весь CSV в памяти повторно."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    non_empty_rows = (
        row for row in reader if row and any(cell.strip() for cell in row)
    )
    return max(0, sum(1 for _ in non_empty_rows) - 1)


def load_csv(path: Path, preview_rows: int = 5, max_rows: int = 5000) -> dict:
    """Читает CSV и возвращает профиль: колонки, типы, число строк, preview."""
    raw = path.read_bytes()
    text, encoding = _decode(raw)
    delimiter = _sniff_delimiter(text)
    rows = _read_rows(text, delimiter, max_rows)
    row_count = _count_data_rows(text, delimiter)

    if not rows:
        return {
            "encoding": encoding,
            "delimiter": delimiter,
            "columns": [],
            "column_types": [],
            "row_count": 0,
            "preview": [],
            "numeric_columns": [],
            "date_columns": [],
        }

    header = rows[0]
    data_rows = rows[1:]
    width = max(len(header), max((len(row) for row in data_rows), default=0))
    header = header + [f"column_{index + 1}" for index in range(len(header), width)]

    column_types: list[str] = []
    numeric_columns: list[str] = []
    date_columns: list[str] = []
    for index, name in enumerate(header):
        values = [row[index] for row in data_rows if index < len(row)]
        column_type = _guess_type(values)
        column_types.append(column_type)
        if column_type == "число":
            numeric_columns.append(name)
        elif column_type == "дата":
            date_columns.append(name)

    return {
        "encoding": encoding,
        "delimiter": delimiter,
        "columns": header,
        "column_types": column_types,
        # Типы и preview определяются по выборке, а счётчик остаётся точным.
        "row_count": row_count,
        "preview": data_rows[:preview_rows],
        "numeric_columns": numeric_columns,
        "date_columns": date_columns,
    }


def read_table(path: Path, max_rows: int = 200, max_cols: int = 40) -> dict | None:
    """Читает CSV-артефакт и превращает его в таблицу для интерфейса."""
    try:
        raw = path.read_bytes()
        text, _ = _decode(raw)
        delimiter = _sniff_delimiter(text)
        rows = _read_rows(text, delimiter, max_rows)
    except (OSError, csv.Error, UnicodeError):
        return None

    if not rows:
        return None

    columns = rows[0][:max_cols]
    body = [row[:max_cols] for row in rows[1 : max_rows + 1]]
    if not columns:
        return None
    return {"columns": columns, "rows": body}


