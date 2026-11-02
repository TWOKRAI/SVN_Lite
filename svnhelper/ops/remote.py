"""Операции чтения, где в ответе есть слова коллег или данные сервера: incoming, history, locks, templates,
server_projects.

Всё, что написали коллеги (комментарии, имена авторов, файлов, папок, шаблонов), проходит `result.clean_text`
(без управляющих знаков, комментарий до 500 знаков), перечисляется в `untrusted_fields`, записей не больше 50, а в
текстовом ответе оборачивается словами «данные коллег». Папки на сервере только просматриваются: ничего не
скачивается и не записывается.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import unquote, urlparse

from .. import server, templates
from ..svn_core import ErrorKind, LogEntry, SvnError, WcInfo
from . import texts
from .context import OpsContext, deadline
from .errors import OpsError, invalid_args
from .projects import (KIND_LOCAL_HISTORY, KIND_LOCAL_ONLY, KIND_PLAIN, KIND_PROJECT, busy_state, display_path, probe_folder,
                       read_status)
from .registry import operation
from .resolve import resolve_project
from .result import MAX_ENTRIES, Result, clean_text

NAME_LIMIT = 260
FILES_PER_ENTRY = 20
HISTORY_DEFAULT = 20
SERVER_MAX_ENTRIES = 100          # папок в одном ответе server_projects
SERVER_MAX_DEPTH = 3
SERVER_THREADS = 6
_ACTIONS = {"A": "added", "M": "modified", "D": "deleted", "R": "replaced"}
_MARKERS = ("branches", "tags")


# --------------------------------------------------------------------------
# Общее
# --------------------------------------------------------------------------

def _date(value) -> tuple:
    """(ISO-дата UTC для программы, дата для человека)."""
    if value is None:
        return "", ""
    return value.isoformat(), f"{value.astimezone():%d.%m.%Y %H:%M}"


def _wc_prefix(info: Optional[WcInfo]) -> str:
    """Путь корня копии внутри хранилища, например «/trunk» (по нему из путей журнала убирается лишнее)."""
    if info is None or not info.url:
        return ""
    url_path = unquote(urlparse(info.url).path).rstrip("/")
    root_path = unquote(urlparse(info.root).path).rstrip("/") if info.root else ""
    return url_path[len(root_path):] if url_path.startswith(root_path) else ""


def _repo_relative(path: str, prefix: str) -> str:
    """Путь файла из журнала относительно папки проекта: `/proj/trunk/a.txt` -> `a.txt`."""
    if prefix and path.startswith(prefix + "/"):
        return path[len(prefix) + 1:]
    parts = [p for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if part == "trunk":
            return "/".join(parts[i + 1:])
        if part in _MARKERS and i + 1 < len(parts):
            return "/".join(parts[i + 2:])
    return "/".join(parts)


def _log_row(entry: LogEntry, prefix: str, number: bool) -> dict:
    iso, human = _date(entry.date)
    files = [{"path": clean_text(_repo_relative(p.path, prefix), NAME_LIMIT), "action": _ACTIONS.get(p.action, p.action)}
             for p in entry.paths[:FILES_PER_ENTRY]]
    row = {"author": clean_text(entry.author, 100) or "?", "date": iso, "date_ru": human,
           "comment": clean_text(entry.message), "files": files, "files_total": len(entry.paths)}
    if number:
        row["number"] = entry.revision
    return row


def _copy_probe(ctx: OpsContext, project: str, tool: str, warnings: list) -> tuple:
    """(папка, ответ-заглушка). Заглушка не None: у папки нет связи с сервером или она повреждена."""
    probe = probe_folder(ctx, resolve_project(ctx, project))
    if probe.kind in (KIND_PLAIN, KIND_LOCAL_ONLY):
        return probe, Result(tool, data={"name": probe.name, "kind": probe.kind},
                             text=texts.state_message(probe.name, "plain", kind=probe.kind))
    if probe.kind not in (KIND_PROJECT, KIND_LOCAL_HISTORY):
        return probe, Result(tool, data={"name": probe.name, "kind": probe.kind},
                             text=texts.state_message(probe.name, "broken", kind=probe.kind))
    busy, _holder = busy_state(probe.path)
    if busy:
        warnings.append(texts.WARN_BUSY_READ.format(name=probe.name))
    return probe, None


# --------------------------------------------------------------------------
# incoming
# --------------------------------------------------------------------------

@operation("incoming")
def incoming(ctx: OpsContext, project: str) -> Result:
    warnings: list = []
    probe, stub = _copy_probe(ctx, project, "incoming", warnings)
    if stub is not None:
        return stub
    if probe.kind != KIND_PROJECT:        # у истории на компьютере сервера нет, нового там не бывает
        return Result("incoming", data={"name": probe.name, "kind": probe.kind, "total": 0, "entries": []},
                      text=texts.incoming_text(probe.name, [], 0))
    with deadline(ctx) as token:
        entries = ctx.client.incoming_log(probe.path, token)
    prefix = _wc_prefix(probe.info)
    rows = [_log_row(e, prefix, number=False) for e in reversed(entries)]       # новые первыми
    shown = rows[:MAX_ENTRIES]
    if len(rows) > len(shown):
        warnings.append(texts.WARN_TRUNCATED)
    data = {"name": probe.name, "kind": probe.kind, "total": len(rows), "entries": shown, "truncated": len(rows) > len(shown)}
    return Result("incoming", data=data, text=texts.incoming_text(probe.name, shown, len(rows)), warnings=warnings,
                  untrusted_fields=["entries[].author", "entries[].comment", "entries[].files[].path"])


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------

@operation("history")
def history(ctx: OpsContext, project: str, limit: int = HISTORY_DEFAULT) -> Result:
    limit = max(1, min(int(limit), MAX_ENTRIES))
    warnings: list = []
    probe, stub = _copy_probe(ctx, project, "history", warnings)
    if stub is not None:
        return stub
    with deadline(ctx) as token:
        entries = ctx.client.recent_log(probe.path, limit, token)
    prefix = _wc_prefix(probe.info)
    rows = [_log_row(e, prefix, number=True) for e in entries[:limit]]
    data = {"name": probe.name, "kind": probe.kind, "limit": limit, "entries": rows}
    return Result("history", data=data, text=texts.history_text(probe.name, rows), warnings=warnings,
                  untrusted_fields=["entries[].author", "entries[].comment", "entries[].files[].path"])


# --------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------

def _lock_row(lock, root: str, owner: bool) -> dict:
    iso, _human = _date(lock.created)
    row = {"file": display_path(lock.path, root), "since": iso, "age_days": lock.age_days,
           "age_ru": texts.age_ru(lock.age_days), "comment": clean_text(lock.comment)}
    if owner:
        row["owner"] = clean_text(lock.owner, 100) or "?"
    return row


@operation("locks")
def locks(ctx: OpsContext, project: str) -> Result:
    warnings: list = []
    probe, stub = _copy_probe(ctx, project, "locks", warnings)
    if stub is not None:
        return stub
    busy, _holder = busy_state(probe.path)
    read = read_status(ctx, probe, probe.kind == KIND_PROJECT and not busy)
    if probe.kind == KIND_PROJECT and not read.checked and not busy:
        warnings.append(texts.WARN_OFFLINE)
    mine = [_lock_row(lock, probe.path, False) for lock in read.status.my_locks][:MAX_ENTRIES]
    others = [_lock_row(lock, probe.path, True) for lock in read.status.other_locks][:MAX_ENTRIES]
    data = {"name": probe.name, "server_checked": read.checked, "mine": mine, "others": others}
    return Result("locks", data=data, text=texts.locks_text(probe.name, mine, others), warnings=warnings,
                  untrusted_fields=["mine[].file", "mine[].comment", "others[].file", "others[].owner",
                                    "others[].comment"])


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

@operation("templates")
def templates_list(ctx: OpsContext) -> Result:
    root = ctx.config.templates_root.strip()
    if not root:
        return Result("templates", data={"configured": False, "offline": False, "templates": []},
                      text=texts.TEMPLATES_NOT_CONFIGURED)
    with deadline(ctx) as token:
        found = templates.load_templates(ctx.client, root, cancel=token)
    rows = [{"id": clean_text(t.id, 100), "title": clean_text(t.title, 200),
             "description": clean_text(t.description)} for t in found.templates[:MAX_ENTRIES]]
    warnings = [texts.WARN_TEMPLATES_OFFLINE] if found.offline else []
    data = {"configured": True, "offline": found.offline, "templates": rows}
    return Result("templates", data=data, text=texts.templates_text(rows, found.offline), warnings=warnings,
                  svn={"templates_root": root}, untrusted_fields=["templates[].id", "templates[].title",
                                                                  "templates[].description"])


# --------------------------------------------------------------------------
# server_projects
# --------------------------------------------------------------------------

def _server_segments(ctx: OpsContext, path: str) -> list:
    """Части пути на сервере. Первая - одна из корневых папок из настроек. Бросает OpsError."""
    text = (path or "").strip().strip('"')
    prefix = ctx.config.server.rstrip("/")
    if prefix and text.lower().startswith(prefix.lower() + "/"):
        text = text[len(prefix):]
    if "://" in text:
        raise invalid_args(texts.SERVER_PATH_BAD)
    parts = [unquote(p) for p in re.split(r"[\\/]", text) if p]
    if any(p in (".", "..") for p in parts):
        raise invalid_args(texts.SERVER_PATH_BAD)
    if not parts:
        return []
    roots = {r.casefold(): r for r in ctx.config.repos}
    if parts[0].casefold() not in roots:
        raise OpsError("not_found", 1, texts.SERVER_ROOT_UNKNOWN)
    parts[0] = roots[parts[0].casefold()]
    return parts


def _list_dirs(ctx: OpsContext, segments: list, token) -> dict:
    """Подпапки папки на сервере: имя -> запись (кто и когда менял последним)."""
    url = server.build_url(ctx.config.server, segments)
    return {e.name: e for e in ctx.client.list_entries(url + "@", cancel=token) if e.kind == "dir"}


@operation("server_projects")
def server_projects(ctx: OpsContext, path: str = "", depth: int = 1) -> Result:
    base = _server_segments(ctx, path)
    warnings: list = []
    levels = max(1, min(int(depth), SERVER_MAX_DEPTH))
    if levels != depth:
        warnings.append(texts.WARN_DEPTH_CUT)
    rows: list = []
    truncated = False
    with deadline(ctx) as token:
        if base:
            level_dirs = [(base, _list_dirs(ctx, base, token))]
            itself = server.TRUNK in level_dirs[0][1]        # папка сама проект: внутри неё trunk, ветки и версии
            if itself:
                level_dirs = []
        else:
            itself = False
            level_dirs = [([], {repo: None for repo in ctx.config.repos})]
        with ThreadPoolExecutor(SERVER_THREADS) as pool:
            for level in range(1, levels + 1):
                pending = []
                for parent, names in level_dirs:
                    for name in sorted(names, key=str.casefold):
                        if len(rows) >= SERVER_MAX_ENTRIES:
                            truncated = True
                            break
                        entry = names[name]
                        child = [*parent, name]
                        row = {"path": clean_text("/".join(child[len(base):]), NAME_LIMIT), "name": clean_text(name, NAME_LIMIT),
                               "is_project": False, "accessible": True, "depth": level,
                               "author": clean_text(entry.author, 100) if entry else "",
                               "date": entry.date.isoformat() if entry and entry.date else ""}
                        rows.append(row)
                        pending.append((row, child))
                found = list(pool.map(lambda item: _probe_child(ctx, item[1], token), pending))
                level_dirs = []
                for (row, child), names in zip(pending, found):
                    if names is None:
                        row["accessible"] = False
                    elif server.TRUNK in names:
                        row["is_project"] = True
                    elif level < levels:
                        level_dirs.append((child, names))
    data = {"path": "/".join(base), "depth": levels, "is_project": itself, "entries": rows, "truncated": truncated}
    if truncated:
        warnings.append(texts.WARN_TRUNCATED)
    text = texts.PROJECT_ITSELF.format(path=clean_text("/".join(base), NAME_LIMIT)) if itself \
        else texts.server_projects_text("/".join(base), rows, truncated)
    return Result("server_projects", data=data, text=text, warnings=warnings,
                  svn={"server": ctx.config.server}, untrusted_fields=["entries[].name", "entries[].path",
                                                                       "entries[].author"])


def _probe_child(ctx: OpsContext, segments: list, token) -> Optional[dict]:
    """Подпапки вложенной папки. Нет прав или папка исчезла: None (папка в списке остаётся, но закрыта)."""
    try:
        return _list_dirs(ctx, segments, token)
    except SvnError as exc:
        if exc.kind in (ErrorKind.AUTHZ, ErrorKind.NOT_FOUND):
            return None
        raise
