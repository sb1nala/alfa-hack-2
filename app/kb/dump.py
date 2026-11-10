"""Чтение таблиц из дампа PostgreSQL (формат custom) без сервера БД."""

import re
from pathlib import Path

import pgdumplib

Row = dict[str, str | None]
_COPY_COLUMNS = re.compile(r"\((.*?)\)\s+FROM", re.S)


def read_tables(backup_path: Path, names: set[str]) -> dict[str, list[Row]]:
    """Строки запрошенных таблиц. Значения — как в дампе: строки, 't'/'f', None для NULL."""
    dump = pgdumplib.load(str(backup_path))
    tables: dict[str, list[Row]] = {}
    for entry in dump.entries:
        if entry.desc != "TABLE DATA" or entry.tag not in names:
            continue
        match = _COPY_COLUMNS.search(entry.copy_stmt or "")
        if match is None:
            raise ValueError(f"Не удалось определить столбцы таблицы {entry.tag}")
        columns = [column.strip().strip('"') for column in match.group(1).split(",")]
        tables[entry.tag] = [
            dict(zip(columns, row)) for row in dump.table_data(entry.namespace, entry.tag)
        ]
    missing = names - tables.keys()
    if missing:
        raise ValueError(f"В выгрузке нет таблиц: {', '.join(sorted(missing))}")
    return tables
