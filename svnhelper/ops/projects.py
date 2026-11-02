"""Операции чтения над папками проектов на этом компьютере: projects, status, changes, project_info.

Ничего не пишет (ни в проекты, ни в settings.json). Проверка сервера идёт одним `svn status -u`; серверная часть
ответа (что нового, чужие занятые файлы) запоминается в памяти процесса на STATUS_TTL секунд, а правки на компьютере
читаются каждый раз. Папку, которую сейчас обрабатывает другая программа, сервер не проверяется (предупреждение в
ответе). Сбой одного проекта не роняет список: строка получает state=unknown и error.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from .. import folderlock, localrepo, projectmeta, publish, server
from ..svn_core import ErrorKind, SvnError, WcInfo, WcStatus
from . import texts
from .context import OpsContext, deadline
from .errors import OpsError, from_exception, svn_block
from .registry import operation
from .resolve import (MAX_FOLDERS, ProjectRef, folder_bases, iter_folders, known_projects, make_ref, match_warnings,
                      resolve_project)
from .result import MAX_ENTRIES, NAME_LIMIT, Result, clean_name

log = logging.getLogger("svnhelper.ops")

STATUS_TTL = 60.0           # секунд, сколько серверная часть ответа о состоянии проекта считается свежей
POOL_THREADS = 4            # проектов, которые проверяются одновременно
_NETWORK_KINDS = (ErrorKind.NO_CONNECTION, ErrorKind.TIMEOUT)
_UNEXPECTED_INFO_KINDS = (ErrorKind.NO_CONNECTION, ErrorKind.TIMEOUT, ErrorKind.AUTH, ErrorKind.AUTHZ)
_LINE_NAMES = {"trunk": "main", "branch": "branch", "tag": "tag"}

# kind папки
KIND_PROJECT = "project"
KIND_LOCAL_HISTORY = "local_history"
KIND_LOCAL_ONLY = "local_only"
KIND_PLAIN = "plain"
KIND_BROKEN = "broken"
KIND_HISTORY_MISSING = "history_missing"
KIND_UNKNOWN = "unknown"            # узнать не удалось (ошибка в строке списка)
_WITH_COPY = (KIND_PROJECT, KIND_LOCAL_HISTORY)         # папки, у которых есть что спросить у svn

_warn_lock = threading.Lock()


def add_warning(warnings: list, text: str) -> None:
    """Предупреждение без повторов; безопасно из нескольких потоков."""
    with _warn_lock:
        if text not in warnings:
            warnings.append(text)


# --------------------------------------------------------------------------
# Что за папка
# --------------------------------------------------------------------------

@dataclass
class Probe:
    ref: ProjectRef
    kind: str
    line: str = "main"                       # main | branch | tag
    number: Optional[int] = None             # номер сохранения, на котором папка (всегда в data.number)
    info: Optional[WcInfo] = None
    has_archive: bool = False
    svn: dict = field(default_factory=dict)  # подробности для блока svn

    @property
    def read_only(self) -> bool:
        return self.line == "tag"

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def path(self) -> str:
        return self.ref.path

    def row(self) -> dict:
        return {"name": self.name, "path": clean_name(self.path), "kind": self.kind, "line": self.line,
                "number": self.number, "read_only": self.read_only}


def probe_folder(ctx: OpsContext, ref: ProjectRef) -> Probe:
    """Что за папка: проект с сервера, проект с историей на компьютере, обычная или повреждённая.

    Сеть не нужна (`svn info` читает запись о копии). Связь, вход и права в ответе `info` невозможны, поэтому такие
    ошибки считаются ошибкой операции (так ведёт себя и заглушка в тестах), а не «повреждённой папкой»."""
    archive = bool(localrepo.archive_path(ctx.store, ref.path))
    if not os.path.isdir(os.path.join(ref.path, ".svn")):
        kind = KIND_LOCAL_ONLY if projectmeta.meta_path(ref.path).is_file() else KIND_PLAIN
        return Probe(ref, kind, has_archive=archive)
    try:
        info = ctx.client.info(ref.path)
    except SvnError as exc:
        if exc.kind in _UNEXPECTED_INFO_KINDS:
            raise
        kind = KIND_HISTORY_MISSING if exc.kind is ErrorKind.LOCAL_HISTORY_MISSING else KIND_BROKEN
        return Probe(ref, kind, has_archive=archive, svn=svn_block(exc))
    svn = {"url": info.url, "root": info.root, "uuid": info.uuid}
    if localrepo.is_local_url(info.url, ctx.config.server):
        kind = KIND_LOCAL_HISTORY if localrepo.repo_exists(info.root or info.url) else KIND_HISTORY_MISSING
        return Probe(ref, kind, number=info.revision, info=info, has_archive=archive, svn=svn)
    found = server.line_from_url(info.url, info.root)
    line = _LINE_NAMES.get(found[1].kind, "main") if found else "main"
    return Probe(ref, KIND_PROJECT, line, info.revision, info, archive, svn)


def busy_state(path: str) -> tuple:
    """(занята ли папка, кем): другая программа или другая работа этого процесса."""
    if not publish.is_busy(path):
        return False, None
    holder = folderlock.holder_info(path) or {}
    return True, {"program": clean_name(holder.get("program", ""), 80), "since": clean_name(holder.get("since", ""), 40)}


def _in_pool(function, items: list) -> list:
    """Результаты function по items в том же порядке; несколько элементов считаются в нескольких потоках."""
    if len(items) < 2:
        return [function(item) for item in items]
    with ThreadPoolExecutor(min(POOL_THREADS, len(items))) as pool:
        return [f.result() for f in [pool.submit(function, item) for item in items]]


def display_path(path: str, root: str) -> str:
    """Путь файла относительно папки проекта, косые черты прямые, одна строка."""
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        relative = path
    return clean_name(relative.replace("\\", "/"), NAME_LIMIT)


def _error_row(ref: ProjectRef, exc: SvnError, warnings: list, full: bool) -> dict:
    """Строка списка для проекта, который не удалось проверить: остальные строки остаются."""
    error = from_exception(exc)
    add_warning(warnings, texts.WARN_PROJECT_FAILED.format(name=ref.name, message=error.message))
    row = {"name": ref.name, "path": clean_name(ref.path), "kind": KIND_UNKNOWN, "line": "main", "number": None,
           "read_only": False}
    if full:
        row.update(_blank_status())
        row["state"] = "unknown"
    row.update({"next_action": error.next_action or "nothing", "error": error.to_dict(),
                "message": texts.state_message(ref.name, "unknown")})
    return row


def _blank_status() -> dict:
    return {"state": "up_to_date", "local_changes": 0, "incoming": 0, "conflicts": 0, "my_locks": 0, "others_locks": 0,
            "busy": False, "busy_by": None, "server_checked": False, "cached": False}


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------

def _list_row(ctx: OpsContext, ref: ProjectRef, warnings: list) -> dict:
    try:
        return probe_folder(ctx, ref).row()
    except SvnError as exc:
        return _error_row(ref, exc, warnings, full=False)


@operation("projects")
def projects(ctx: OpsContext) -> Result:
    warnings: list = []
    bases = folder_bases(ctx, warnings)
    for base in ctx.project_dirs:
        if not os.path.isdir(base):
            add_warning(warnings, texts.WARN_BASE_MISSING.format(path=clean_name(base)))
    folders = []
    for path, name in iter_folders(bases):
        if len(folders) >= MAX_FOLDERS:
            add_warning(warnings, texts.WARN_TRUNCATED)
            break
        folders.append(make_ref(path, name))
    rows = _in_pool(lambda ref: _list_row(ctx, ref, warnings), folders)
    text = texts.projects_text(rows) if ctx.project_dirs else texts.NO_PROJECT_DIRS
    return Result("projects", data={"projects": rows}, text=text, warnings=warnings,
                  untrusted_fields=["projects[].name", "projects[].path"])


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

_cache: dict = {}
_cache_lock = threading.Lock()
_clock = time.monotonic          # в тестах подменяется


@dataclass
class ServerPart:
    """То, что о проекте известно только серверу (его и запоминаем): правки на компьютере читаются каждый раз."""
    head: Optional[int]
    incoming: int
    incoming_files: list
    other_locks: list
    locked_by_others: list

    @classmethod
    def of(cls, status: WcStatus) -> "ServerPart":
        return cls(status.head, status.incoming, list(status.incoming_files), list(status.other_locks),
                   list(status.locked_by_others))

    def apply(self, local: WcStatus) -> WcStatus:
        merged = copy.copy(local)
        merged.head, merged.incoming = self.head, self.incoming
        merged.incoming_files, merged.other_locks = list(self.incoming_files), list(self.other_locks)
        merged.locked_by_others = list(self.locked_by_others)
        return merged


def clear_status_cache(path: Optional[str] = None) -> None:
    """Забывает запомненную серверную часть ответа (всю или одной папки). Вызывает запись, когда папка изменилась."""
    with _cache_lock:
        if path is None:
            _cache.clear()
            return
        _cache.pop(os.path.normcase(os.path.normpath(path)), None)


def _cache_get(key: str) -> Optional[ServerPart]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and _clock() - hit[0] <= STATUS_TTL:
            return hit[1]
        _cache.pop(key, None)
    return None


def _cache_put(key: str, value: ServerPart) -> None:
    with _cache_lock:
        _cache[key] = (_clock(), value)


@dataclass
class StatusRead:
    status: WcStatus
    checked: bool = False        # сервер проверялся
    offline: bool = False        # хотели проверить сервер, но связи нет
    cached: bool = False         # серверная часть из памяти процесса (не старше STATUS_TTL)


def read_status(ctx: OpsContext, probe: Probe, want_server: bool, down: Optional[threading.Event] = None) -> StatusRead:
    """Состояние папки по svn. Правки на компьютере читаются каждый раз; серверная часть (`status -u`) запоминается на
    STATUS_TTL секунд. Нет связи: ответ строится по тому, что на компьютере (offline=True)."""
    rules = localrepo.local_rules(ctx.config.filter) if probe.kind == KIND_LOCAL_HISTORY else ctx.config.filter
    key = os.path.normcase(os.path.normpath(probe.path))
    part = _cache_get(key) if want_server else None
    if part is not None:
        with deadline(ctx) as token:
            local = ctx.client.status(probe.path, check_server=False, rules=rules, cancel=token)
        return StatusRead(part.apply(local), True, False, True)
    try:
        with deadline(ctx) as token:
            status = ctx.client.status(probe.path, check_server=want_server, rules=rules, cancel=token)
    except SvnError as exc:
        if not (want_server and exc.kind in _NETWORK_KINDS):
            raise
        if down is not None:
            down.set()
        with deadline(ctx, fresh=True) as token:         # бюджет операции ушёл на ожидание сервера
            return StatusRead(ctx.client.status(probe.path, check_server=False, rules=rules, cancel=token),
                              False, True)
    if want_server:
        _cache_put(key, ServerPart.of(status))
    return StatusRead(status, want_server)


def _needs_lock_missing(status: WcStatus) -> bool:
    return any(c.needs_lock and not c.locked_by_me for c in status.local_changes)


def _state(status: WcStatus, checked: bool, kind: str) -> tuple:
    """(state, число своих правок, число файлов с новым на сервере)."""
    local = len(status.local_changes)
    incoming = max(len(status.incoming_files), status.incoming) if checked else 0
    if status.conflicts:
        return "conflict", local, incoming
    if incoming and local:
        return "both", local, incoming
    if incoming:
        return "incoming", local, incoming
    if local:
        return "local_changes", local, incoming
    return ("up_to_date" if checked or kind == KIND_LOCAL_HISTORY else "unknown_offline"), local, incoming


def _next_action(state: str, status: Optional[WcStatus], busy: bool, read_only: bool, kind: str) -> str:
    if busy:
        return "wait"
    if state == "conflict":
        return "human_resolve_conflict"
    if state in ("incoming", "both"):
        return "get_new"
    if state == "local_changes":
        if read_only:
            return "nothing"
        return "take_lock" if status is not None and _needs_lock_missing(status) else "save"
    if state == "broken":
        return "human_resolve_conflict"
    if state == "plain" and kind == KIND_LOCAL_ONLY:
        return "send_to_server"
    return "nothing"


def _status_row(ctx: OpsContext, ref: ProjectRef, check_server: bool, down: threading.Event, warnings: list) -> dict:
    probe = probe_folder(ctx, ref)
    row = probe.row()
    row.update(_blank_status())
    row["matched_by"] = ref.matched_by
    status: Optional[WcStatus] = None
    if probe.kind in (KIND_PLAIN, KIND_LOCAL_ONLY):
        row["state"] = "plain"
    elif probe.kind in (KIND_BROKEN, KIND_HISTORY_MISSING):
        row["state"] = "broken"
    else:
        busy, holder = busy_state(probe.path)
        row["busy"], row["busy_by"] = busy, holder
        want_server = check_server and probe.kind == KIND_PROJECT and not busy and not down.is_set()
        if busy and check_server and probe.kind == KIND_PROJECT:
            add_warning(warnings, texts.WARN_BUSY_SKIPPED.format(name=probe.name))
        try:
            read = read_status(ctx, probe, want_server, down)
        except SvnError:
            if not busy:
                raise
            row["state"] = "unknown"           # папку держит другая программа, и svn не ответил: это не ошибка проекта
            add_warning(warnings, texts.WARN_BUSY_READ.format(name=probe.name))
        else:
            status = read.status
            if check_server and probe.kind == KIND_PROJECT and not busy and not read.checked:
                add_warning(warnings, texts.WARN_OFFLINE)
            state, local, incoming = _state(status, read.checked, probe.kind)
            row.update({"state": state, "local_changes": local, "incoming": incoming, "conflicts": status.conflicts,
                        "my_locks": len(status.my_locks), "others_locks": len(status.other_locks),
                        "server_checked": read.checked, "cached": read.cached})
    row["next_action"] = _next_action(row["state"], status, row["busy"], probe.read_only, probe.kind)
    row["message"] = texts.state_message(probe.name, row["state"], row["incoming"], row["local_changes"],
                                         probe.read_only, probe.kind)
    return row


@operation("status")
def status(ctx: OpsContext, projects: Optional[list] = None, all_projects: bool = False,
           check_server: bool = True) -> Result:
    warnings: list = []
    if all_projects and projects:
        raise OpsError("invalid_args", 2, texts.ARG_BOTH_PROJECTS)
    if all_projects:
        refs = known_projects(ctx, warnings)
    elif projects:
        refs = list({r.path: r for r in (resolve_project(ctx, name) for name in projects[:MAX_FOLDERS])}.values())
        for ref in refs:
            for text in match_warnings(ref):
                add_warning(warnings, text)
    else:
        raise OpsError("invalid_args", 2, texts.ARG_NEED_PROJECT)
    if len(refs) > MAX_FOLDERS:
        add_warning(warnings, texts.WARN_TRUNCATED)
    refs = refs[:MAX_FOLDERS]
    explicit_one = not all_projects and len(refs) == 1          # один названный проект: ошибка остаётся ошибкой
    down = threading.Event()

    def one(ref: ProjectRef) -> dict:
        try:
            return _status_row(ctx, ref, check_server, down, warnings)
        except SvnError as exc:
            if explicit_one:
                raise
            return _error_row(ref, exc, warnings, full=True)

    rows = _in_pool(one, refs)
    text = texts.status_text(rows) if rows else texts.projects_text([])
    return Result("status", data={"projects": rows, "server_checked": any(row["server_checked"] for row in rows)},
                  text=text, warnings=warnings, untrusted_fields=["projects[].name", "projects[].path",
                                                                  "projects[].message"])


# --------------------------------------------------------------------------
# changes
# --------------------------------------------------------------------------

@operation("changes")
def changes(ctx: OpsContext, project: str) -> Result:
    ref = resolve_project(ctx, project)
    probe = probe_folder(ctx, ref)
    warnings = match_warnings(ref)
    data = {"name": probe.name, "kind": probe.kind, "line": probe.line, "read_only": probe.read_only,
            "matched_by": ref.matched_by, "total": 0, "files": [], "truncated": False}
    if probe.kind not in _WITH_COPY:
        state = "plain" if probe.kind in (KIND_PLAIN, KIND_LOCAL_ONLY) else "broken"
        return Result("changes", data=data, text=texts.state_message(probe.name, state, kind=probe.kind),
                      warnings=warnings)
    busy, _holder = busy_state(probe.path)
    if busy:
        add_warning(warnings, texts.WARN_BUSY_READ.format(name=probe.name))
    status = read_status(ctx, probe, False).status
    files = sorted(({"path": display_path(c.path, probe.path), "kind": c.kind, "needs_lock": c.needs_lock,
                     "locked_by_me": c.locked_by_me} for c in status.local_changes), key=lambda f: f["path"].casefold())
    kinds = {kind: sum(1 for f in files if f["kind"] == kind) for kind in texts.CHANGE_KIND_RU}
    data.update(total=len(files), files=files[:MAX_ENTRIES], truncated=len(files) > MAX_ENTRIES, kinds=kinds)
    if data["truncated"]:
        add_warning(warnings, texts.WARN_TRUNCATED)
    text = texts.changes_text(probe.name, len(files), kinds, data["truncated"])
    return Result("changes", data=data, text=text, warnings=warnings, untrusted_fields=["files[].path"])


# --------------------------------------------------------------------------
# project_info
# --------------------------------------------------------------------------

def _meta_data(meta: Optional[projectmeta.ProjectMeta]) -> Optional[dict]:
    if meta is None:
        return None
    fields = {clean_name(k, 80): clean_name(v, 200) for k, v in list(meta.fields.items())[:MAX_ENTRIES]}
    return {"name": clean_name(meta.name, 200), "folder": clean_name(meta.folder, 200),
            "created": clean_name(meta.created, 40), "author": clean_name(meta.author, 100),
            "template": {"id": clean_name(meta.template.get("id", ""), 100),
                         "title": clean_name(meta.template.get("title", ""), 200)},
            "fields": fields}


@operation("project_info")
def project_info(ctx: OpsContext, project: str) -> Result:
    ref = resolve_project(ctx, project)
    probe = probe_folder(ctx, ref)
    meta = projectmeta.read(probe.path)
    data = {**probe.row(), "matched_by": ref.matched_by, "has_archive": probe.has_archive, "meta": _meta_data(meta)}
    svn = dict(probe.svn)
    if meta is not None and meta.svn:
        svn["meta_svn"] = meta.svn
    author = data["meta"]["author"] if meta else ""
    created = data["meta"]["created"] if meta else ""
    text = texts.project_info_text(probe.name, probe.kind, probe.line, author, created)
    return Result("project_info", data=data, text=text, svn=svn, warnings=match_warnings(ref),
                  untrusted_fields=["name", "path", "meta.name", "meta.folder", "meta.author", "meta.template.title",
                                    "meta.fields"])
