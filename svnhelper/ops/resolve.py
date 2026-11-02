"""Как по названию или пути найти папку проекта и какие папки помощнику открывать нельзя.

Принимаются: название папки проекта (без учёта регистра) или название из project.json среди проектов в папках
для проектов, либо полный путь (в том числе вида `/c/Users/...`, который дают Git Bash и WSL-подобные оболочки).
Отказ (код 6): `..`, сетевые пути (UNC, `\\\\?\\`), сетевой диск, ссылка (junction, symlink), папка вне папок для
проектов. Одно имя в двух папках: ошибка `ambiguous` со списком.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterator

from .. import checks, importer, notifier, projectmeta
from . import texts
from .context import OpsContext
from .errors import OpsError, refused
from .result import EXIT_OTHER

_GIT_BASH_RE = re.compile(r"^/([A-Za-z])(?:/(.*))?$")
_PATH_CHARS = ("\\", "/", ":")
_DATE_PREFIX_RE = re.compile(r"^\d{6,8}[_ -]+")      # дата в начале названия папки: 261028_отбраковщик
MAX_FOLDERS = 200           # подпапок в списке проектов


@dataclass(frozen=True)
class ProjectRef:
    name: str               # название папки
    path: str               # полный путь (normpath)


def from_git_bash(ref: str) -> str:
    """`/c/Users/me/проект` -> `C:\\Users\\me\\проект`; остальное без изменений."""
    match = _GIT_BASH_RE.match(ref)
    if not match:
        return ref
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{match.group(1).upper()}:\\{rest}"


def looks_like_path(ref: str) -> bool:
    return any(ch in ref for ch in _PATH_CHARS) or ref.startswith((".", "~"))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _real(path: str) -> str:
    """Путь с раскрытыми ссылками, без `\\\\?\\`, normcase."""
    real = os.path.realpath(path)
    if real.startswith("\\\\?\\UNC\\"):
        real = "\\\\" + real[8:]
    elif real.startswith("\\\\?\\"):
        real = real[4:]
    return _norm(real)


def is_link_path(path: str) -> bool:
    """Последняя часть пути - ссылка на другое место (junction, symlink)."""
    parent, name = os.path.split(os.path.normpath(path))
    try:
        with os.scandir(parent) as entries:
            return any(e.name.casefold() == name.casefold() and importer.is_link(e) for e in entries)
    except OSError:
        return False


def folder_bases(ctx: OpsContext) -> list:
    return [b for b in ctx.project_dirs if b and os.path.isdir(b)]


def iter_folders(bases: list) -> Iterator[tuple]:
    """(путь, название) подпапок в папках для проектов; скрытые и ссылки пропускаются. Без повторов."""
    seen = set()
    for base in bases:
        try:
            with os.scandir(base) as entries:
                items = sorted(entries, key=lambda e: e.name.casefold())
        except OSError:
            continue
        for entry in items:
            path = os.path.join(base, entry.name)
            key = _norm(path)
            if key in seen or entry.name.startswith(".") or not _is_dir(entry) or importer.is_link(entry):
                continue
            seen.add(key)
            yield os.path.normpath(path), entry.name


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def known_projects(ctx: OpsContext) -> list:
    """Проекты в папках для проектов: скачанные (есть .svn, `notifier.discover_projects`) и папки с project.json."""
    bases = folder_bases(ctx)
    found = {_norm(path): ProjectRef(name, path) for path, name in notifier.discover_projects(bases)
             if not is_link_path(path)}
    for path, name in iter_folders(bases):
        if _norm(path) not in found and projectmeta.meta_path(path).is_file():
            found[_norm(path)] = ProjectRef(name, path)
    return sorted(found.values(), key=lambda p: (p.name.casefold(), p.path))


def resolve_project(ctx: OpsContext, ref: str) -> ProjectRef:
    """Папка проекта по названию или пути. Бросает OpsError: project_not_found, ambiguous, path_refused."""
    text = (ref or "").strip().strip('"')
    if not text:
        raise OpsError("invalid_args", 2, texts.EMPTY_REF)
    if looks_like_path(text):
        return _check_path(ctx, text)
    return _by_name(ctx, text)


def _by_name(ctx: OpsContext, name: str) -> ProjectRef:
    projects = known_projects(ctx)
    wanted = name.casefold()
    # от точного совпадения к приблизительному; первый шаг, давший результат, решает
    steps = (lambda p: p.name.casefold() == wanted,                              # название папки
             lambda p: _meta_name(p.path).casefold() == wanted,                  # название из project.json
             lambda p: _DATE_PREFIX_RE.sub("", p.name).casefold() == wanted,     # «отбраковщик» для «261028_отбраковщик»
             lambda p: wanted in p.name.casefold())                              # часть названия
    matches = []
    for step in steps:
        matches = [p for p in projects if step(p)]
        if matches:
            break
    if not matches:
        raise OpsError("project_not_found", EXIT_OTHER,
                       next_step="" if ctx.project_dirs else texts.NO_PROJECT_DIRS)
    if len(matches) > 1:
        raise OpsError("ambiguous", EXIT_OTHER, data={"candidates": [{"name": p.name, "path": p.path} for p in matches]})
    return _check_path(ctx, matches[0].path)


def _meta_name(path: str) -> str:
    meta = projectmeta.read(path)
    return meta.name if meta else ""


def _check_path(ctx: OpsContext, ref: str) -> ProjectRef:
    path = from_git_bash(ref).replace("/", "\\")
    if path.startswith("\\\\"):
        raise refused(texts.REFUSE_UNC, ref)
    if ".." in path.split("\\"):
        raise refused(texts.REFUSE_PARENT, ref)
    if not re.match(r"^[A-Za-z]:\\", path):
        raise refused(texts.REFUSE_RELATIVE, ref)
    path = os.path.normpath(path)
    if checks.drive_type(path) == checks.DRIVE_REMOTE:
        raise refused(texts.REFUSE_NETWORK_DRIVE, ref)
    if not os.path.isdir(path):
        raise refused(texts.REFUSE_NOT_DIR, ref)
    if is_link_path(path):
        raise refused(texts.REFUSE_LINK, ref)
    if not _inside_bases(ctx, path):
        raise refused(texts.REFUSE_OUTSIDE if folder_bases(ctx) else texts.NO_PROJECT_DIRS, ref)
    return ProjectRef(os.path.basename(path), path)


def _inside_bases(ctx: OpsContext, path: str) -> bool:
    """Папка лежит прямо в одной из папок для проектов; ссылка внутри пути (junction, subst) наружу не выводит."""
    real = _real(path)
    parent, name = os.path.split(real)
    for base in folder_bases(ctx):
        if _real(base) == parent and name == _norm(os.path.basename(path)):
            return True
    return False
