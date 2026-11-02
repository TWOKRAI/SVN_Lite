"""Журналы фоновых задач `data_dir()/create_jobs/<id>.json`: запись, чтение, удаление и «жива ли задача». Без tkinter.

Один формат для всех задач (создание проекта, отправка на сервер, история на компьютере): JSON с полями дата-класса
задачи (кроме `path`) и полями владельца `pid` и `started` (время старта процесса, FILETIME). Запись атомарная
(`<id>.json.tmp` + `os.replace`); если файл на миг занят (антивирус, индексатор, разбор журналов при запуске:
WinError 5/32), запись, удаление и чтение повторяются несколько раз, прежде чем считать это сбоем.

Задача жива (`is_active`), если она в реестре задач этого процесса (`active`) или её `pid` принадлежит другому
живому процессу с тем же временем старта. Разбор журналов при запуске живые задачи пропускает (`load_jobs`).
Журнал без `pid` (записан прежней версией) считается брошенным, как раньше. Журнал с `pid` этого же процесса,
но не в реестре, тоже брошен: все живые задачи процесса в реестре, а такой журнал задача оставила нарочно
(например, откат на сервере не удался и его надо повторить).
"""
from __future__ import annotations

import ctypes
import dataclasses
import errno
import functools
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import projectmeta

log = logging.getLogger("svnhelper.jobs")

JOBS_DIR_NAME = "create_jobs"
ATTEMPTS = 6                    # попыток записи, удаления и чтения занятого файла
RETRY_DELAY = 0.05              # секунд перед второй попыткой; дальше пауза растёт: 0.05, 0.10, 0.15...
WINERROR_ACCESS_DENIED = 5      # файл держит другая программа (или он помечен на удаление)
WINERROR_SHARING_VIOLATION = 32
_BUSY_WINERRORS = (WINERROR_ACCESS_DENIED, WINERROR_SHARING_VIOLATION)
OWNER_FIELDS = ("pid", "started")

# WinAPI для проверки процесса-владельца
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87   # процесса с таким pid нет


def default_dir() -> Path:
    from .config import data_dir     # отложенно: учитывает SVNLITE_DATA_DIR на момент вызова
    return data_dir() / JOBS_DIR_NAME


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def job_file(jobs_dir: Optional[os.PathLike], job_id: str) -> Path:
    return (Path(jobs_dir) if jobs_dir else default_dir()) / f"{job_id}.json"


# --------------------------------------------------------------------------
# Запись, удаление, чтение
# --------------------------------------------------------------------------

def _is_busy(exc: OSError) -> bool:
    if isinstance(exc, FileNotFoundError):
        return False
    return (isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in _BUSY_WINERRORS
            or exc.errno in (errno.EACCES, errno.EBUSY))


def _retry(action: Callable, *args) -> Any:
    """action(*args); файл занят -> ещё попытки с растущей паузой, последняя ошибка пробрасывается."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return action(*args)
        except OSError as exc:
            if not _is_busy(exc) or attempt == ATTEMPTS:
                raise
            log.debug("Журнал задачи занят (%s), попытка %d из %d", exc, attempt, ATTEMPTS)
            time.sleep(RETRY_DELAY * attempt)
    return None


def write(path: os.PathLike, data: dict) -> None:
    """Атомарная запись журнала. Файл занят дольше всех попыток -> OSError (сбой не скрывается)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _retry(tmp.write_text, text, "utf-8")
    _retry(os.replace, tmp, path)


def owner() -> dict:
    """Кто ведёт задачу: pid и время старта этого процесса (None, если узнать не удалось)."""
    return {"pid": os.getpid(), "started": _own_started()}


def save(job: Any) -> None:
    """Пишет дата-класс задачи (поля `path` и `updated` обязательны): все поля, кроме `path`, плюс владелец."""
    job.updated = projectmeta.now_iso()
    data = {k: v for k, v in dataclasses.asdict(job).items() if k != "path" and k not in OWNER_FIELDS}
    data.update(owner())
    write(job.path, data)


def remove(path: os.PathLike) -> bool:
    """Удаляет журнал (с повторами). True: файла больше нет. Сбой записывается в журнал программы."""
    try:
        _retry(os.remove, path)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("Не удалось удалить журнал задачи %s", path)
        return False
    return True


def read(path: os.PathLike) -> Optional[dict]:
    """Содержимое журнала или None: файла нет (задача закончилась), он битый или занят дольше всех попыток.

    Запись атомарная, поэтому битый файл - не «ещё пишется», а испорчен: повторов для него нет."""
    try:
        data = json.loads(_retry(Path(path).read_text, "utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Журнал задачи %s не прочитан: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def from_data(cls: type, data: dict, path: os.PathLike) -> Any:
    """Дата-класс задачи из журнала: известные поля и path; лишнее (pid, started, поля новых версий) пропускается.

    ValueError: нет обязательного поля или строковое поле не строка."""
    names = [f.name for f in dataclasses.fields(cls) if f.name != "path"]
    try:
        job = cls(**{k: data[k] for k in names if k in data}, path=str(path))
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    for f in dataclasses.fields(cls):
        if f.type in ("str", str) and not isinstance(getattr(job, f.name), str):
            raise ValueError(f"поле {f.name}: не строка")
    return job


def load_jobs(cls: type, jobs_dir: Optional[os.PathLike], kind: str, include_active: bool = False) -> list:
    """Задачи вида kind из папки журналов (по имени файла). Живые задачи пропускаются, если не include_active."""
    folder = Path(jobs_dir) if jobs_dir else default_dir()
    found = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        if not include_active and _registered(path.stem):     # до чтения: задача этого процесса ещё идёт
            continue
        data = read(path)
        if data is None or data.get("kind") != kind:
            continue
        if not include_active and is_active(data):
            log.info("Задача %s ещё идёт (процесс %s): разбор пропущен", path, data.get("pid"))
            continue
        try:
            found.append(from_data(cls, data, path))
        except ValueError as exc:
            log.warning("Журнал задачи %s не разобран: %s", path, exc)
    return found


# --------------------------------------------------------------------------
# Пути в журнале
# --------------------------------------------------------------------------

def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def is_child(path: Any, parent: str, pattern: Optional[str] = None) -> bool:
    """path - абсолютный путь прямо внутри parent, имя подходит под pattern (без учёта регистра).

    По журналу программа удаляет папки: так журнал с чужими путями ничего чужого не удалит."""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return False
    path = os.path.normpath(path)
    if not same_path(os.path.dirname(path), parent):
        return False
    return pattern is None or re.fullmatch(pattern, os.path.basename(path), re.IGNORECASE) is not None


# --------------------------------------------------------------------------
# Живые задачи
# --------------------------------------------------------------------------

_active: dict[str, int] = {}
_active_lock = threading.Lock()


@contextmanager
def active(job_id: str) -> Iterator[None]:
    """Задача идёт в этом процессе: разбор журналов при запуске её не трогает. Вход - до первой записи журнала."""
    with _active_lock:
        _active[job_id] = _active.get(job_id, 0) + 1
    try:
        yield
    finally:
        with _active_lock:
            if _active.get(job_id, 0) > 1:
                _active[job_id] -= 1
            else:
                _active.pop(job_id, None)


def _registered(job_id: str) -> bool:
    with _active_lock:
        return job_id in _active


def is_active(job: dict) -> bool:
    """Жива ли задача из журнала: в реестре этого процесса или её ведёт другой живой процесс (pid + время старта)."""
    if _registered(str(job.get("id", ""))):
        return True
    pid = job.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False                    # журнал прежней версии: владелец неизвестен, задача брошена
    if pid == os.getpid():
        return False                    # свой процесс: все его живые задачи в реестре
    alive, started = process_info(pid)
    if not alive:
        return False
    recorded = job.get("started")
    if not isinstance(recorded, int) or started is None:
        return True                     # время старта неизвестно: судим только по pid
    return recorded == started          # иначе pid достался другому процессу


@functools.lru_cache(maxsize=1)
def _own_started() -> Optional[int]:
    return process_info(os.getpid())[1]


@functools.lru_cache(maxsize=1)
def _kernel32():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)       # свой экземпляр: argtypes не мешают другим модулям
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def process_info(pid: int) -> tuple[bool, Optional[int]]:
    """(жив ли процесс, время его старта в единицах FILETIME или None).

    Не удалось проверить (нет доступа к процессу, нет WinAPI) - процесс считается живым: лучше отложить разбор
    журнала до следующего запуска, чем удалить работу живой задачи."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)             # только вне Windows: там os.kill завершает процесс
        except ProcessLookupError:
            return False, None
        except OSError:
            return True, None
        return True, None
    try:
        from ctypes import wintypes
        kernel = _kernel32()
    except (AttributeError, OSError):
        log.warning("Проверка процессов недоступна: задачи с pid считаются живыми")
        return True, None
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER, None
    try:
        code = wintypes.DWORD()
        if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            return False, None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return True, None
        created = times[0]
        return True, (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


# --------------------------------------------------------------------------
# Номер правки каркаса проекта на сервере
# --------------------------------------------------------------------------

def layout_recorder(job: Any) -> Callable[[Any], None]:
    """Обратный вызов для `importer.create_project(on_layout=...)`: пишет в журнал номер правки, которой созданы
    три папки проекта (и автора, если логин не был известен). По номеру разбор после сбоя отличает «загрузка дошла»
    от «на сервере только каркас», не полагаясь на логин."""
    def record(entry) -> None:
        job.layout_revision = entry.revision
        if not job.author:
            job.author = entry.author or ""
        save(job)
    return record
