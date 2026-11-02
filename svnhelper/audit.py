"""Проверка содержимого проекта перед скачиванием: мусор, крупные файлы, длинные пути.

По мотивам svn_audit.py, но работает со списком файлов с сервера (svn list -R --xml),
а правила берёт из секции "filter" файла svn_helper.json. Без tkinter.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Sequence

from .config import FilterRules
from .messages import format_number, format_size, plural
from .svn_core import ListEntry

WINDOWS_PATH_LIMIT = 260
MB = 1024 * 1024
REPORT_LIMIT = 15


@dataclass
class AuditResult:
    junk_paths: list[str] = field(default_factory=list)    # верхние мусорные папки/файлы, '/' в путях
    junk_files: int = 0
    junk_size: int = 0
    big_files: list[tuple[str, int]] = field(default_factory=list)
    too_long: list[tuple[str, int]] = field(default_factory=list)   # длиннее лимита Windows
    long: list[tuple[str, int]] = field(default_factory=list)       # близко к лимиту
    skip_junk: bool = True
    warn_length: int = 200

    @property
    def needs_attention(self) -> bool:
        return bool(self.junk_paths or self.big_files or self.too_long or self.long)

    def report_lines(self) -> list[str]:
        out: list[str] = []
        if self.junk_paths:
            files = f"{format_number(self.junk_files)} {plural(self.junk_files, ('файл', 'файла', 'файлов'))}"
            fate = ("в рабочую папку НЕ попадут" if self.skip_junk
                    else "будут скачаны (фильтр отключён в настройках)")
            out.append(f"Лишние файлы: {files}, {format_size(self.junk_size)}. Они {fate}:")
            out += [f"  {p}" for p in self.junk_paths[:REPORT_LIMIT]]
            if len(self.junk_paths) > REPORT_LIMIT:
                out.append(f"  … и ещё {len(self.junk_paths) - REPORT_LIMIT}")
            out.append("")
        if self.too_long:
            out.append(f"Путь длиннее {WINDOWS_PATH_LIMIT} символов (Windows может не создать или не открыть "
                       f"файл), таких файлов: {len(self.too_long)}. Выберите более короткую папку:")
            out += [f"  {n}  {p}" for p, n in self.too_long[:REPORT_LIMIT]]
            out.append("")
        if self.long:
            out.append(f"Длинные пути (больше {self.warn_length} символов), "
                       f"таких файлов: {len(self.long)}. Пример:")
            out += [f"  {n}  {p}" for p, n in self.long[:5]]
            out.append("")
        if self.big_files:
            out.append("Крупные файлы:")
            out += [f"  {format_size(s):>10}  {p}" for p, s in self.big_files[:REPORT_LIMIT]]
            out.append("")
        return out[:-1] if out else out


def _junk_reason(name: str, rel: str, is_dir: bool, rules: FilterRules) -> bool:
    low = name.casefold()
    if is_dir:
        if low in rules.junk_dirs:
            return True
    else:
        if (os.path.splitext(low)[1] in rules.junk_extensions or low in rules.junk_names
                or any(low.startswith(p) for p in rules.junk_prefixes)):
            return True
    low_rel = rel.casefold()
    return any(fnmatch.fnmatchcase(low_rel, g) for g in rules.junk_globs)


def analyze(entries: Sequence[ListEntry], rules: FilterRules, dest: str) -> AuditResult:
    """entries — результат `svn list -R` (пути относительные, через '/'); dest — будущая папка проекта."""
    result = AuditResult(skip_junk=rules.skip_junk, warn_length=rules.path_warn_length)
    junk_dirs: set[str] = set()
    for e in sorted(entries, key=lambda e: e.name.count("/")):  # родители раньше детей
        rel = e.name
        parts = rel.split("/")
        if any("/".join(parts[:i]) in junk_dirs for i in range(1, len(parts))):
            if e.kind == "file":
                result.junk_files += 1
                result.junk_size += e.size
            continue
        if _junk_reason(parts[-1], rel, e.kind == "dir", rules):
            result.junk_paths.append(rel)
            if e.kind == "dir":
                junk_dirs.add(rel)
            else:
                result.junk_files += 1
                result.junk_size += e.size
            continue
        if e.kind != "file":
            continue
        length = len(dest) + 1 + len(rel)
        if length >= WINDOWS_PATH_LIMIT:
            result.too_long.append((rel, length))
        elif length > rules.path_warn_length:
            result.long.append((rel, length))
        if e.size >= rules.big_file_mb * MB:
            result.big_files.append((rel, e.size))
    result.junk_paths.sort(key=str.casefold)
    result.big_files.sort(key=lambda x: -x[1])
    result.too_long.sort(key=lambda x: -x[1])
    result.long.sort(key=lambda x: -x[1])
    return result


def junk_among(paths: Sequence[str], rules: FilterRules, wc_root: str) -> list[str]:
    """Из путей, добавленных Update, выбирает мусор (относительные пути с '/') для исключения.

    paths — как напечатал svn (абсолютные или относительные к рабочей копии).
    """
    root = os.path.abspath(wc_root)
    found: list[str] = []
    for raw in paths:
        full = raw if os.path.isabs(raw) else os.path.join(root, raw)
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        if rel.startswith(".."):
            continue
        if any(rel == j or rel.startswith(j + "/") for j in found):
            continue
        parts = rel.split("/")
        if any(p.casefold() in rules.junk_dirs for p in parts[:-1]) or _junk_reason(
                parts[-1], rel, os.path.isdir(full), rules):
            found.append(rel)
    return found
