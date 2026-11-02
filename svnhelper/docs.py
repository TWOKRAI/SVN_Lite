"""Папка с документацией на сервере (info_path в общем svn_helper.json): какие файлы в ней лежат. Без tkinter.

Показываем только документы: страницы, PDF, офисные файлы, текст и картинки. Программы и скрипты
в список не попадают, потому что путь приходит из общего конфига, а пункты меню запускают файлы.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DOC_EXTENSIONS = {".html", ".htm", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
                  ".rtf", ".txt", ".md", ".png", ".jpg", ".jpeg"}
MAX_DOCS = 60
HELP_NAME = "шпаргалка"       # файл с таким именем (любое расширение) открывает пункт «Шпаргалка»


@dataclass(frozen=True)
class DocEntry:
    title: str     # имя без расширения: показывается в меню
    path: str


def scan_info_folder(folder: str) -> list[DocEntry]:
    """Файлы-документы прямо в папке (без вложенных), по алфавиту. Пустой список, если папки нет или нет доступа."""
    folder = os.path.expandvars(folder.strip()).strip('"')
    if not folder:
        return []
    found: list[DocEntry] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() not in DOC_EXTENSIONS or name.startswith(("~$", ".")):
            continue
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            found.append(DocEntry(stem, path))
    found.sort(key=lambda e: e.title.casefold())
    return found[:MAX_DOCS]


def find_help(entries: list[DocEntry]) -> str:
    """Путь к «шпаргалке» среди найденных файлов или пустая строка."""
    for entry in entries:
        if entry.title.casefold() == HELP_NAME:
            return entry.path
    return ""
