"""Локальная история проекта: проект без сервера, но с сохранениями на этом компьютере (Task T.6). Без tkinter.

История лежит в `<папка проектов>\\.svnlite\\history\\<короткий id>`: на том же диске, что и проект, в скрытой папке
с пояснением (README.txt). `create_local_repo` создаёт её командой `svnadmin create`, кладёт в неё три папки
(trunk, branches, tags), загружает туда файлы проекта (мусор отсеивается теми же шаблонами, что и при создании
проекта на сервере) и привязывает саму папку проекта к ней (`checkout --force`, файлы на диске не трогаются).
Путь к истории записывается в settings.json (`Settings.local_histories`), а не в project.json: тот файл уходит
на сервер, а `C:\\Users\\...` там бессмыслен.

Журнал задачи `data_dir()/create_jobs/<id>.json` (kind=local_history, запись - `jobs.py`) пишется после каждого шага.
Сбой: созданная история и `.svn` удаляются, папка проекта остаётся нетронутой. Убили процесс: то же сделает
`recover_jobs` при следующем запуске программы (задачи, которые ещё идут, он пропускает).

После отправки проекта на сервер (publish.publish) история остаётся на диске архивом: запись в settings.json жива,
удалить её можно только явно (`delete_history`). Пропавшая история: папку проектов переименовали или перенесли -
`restore_history` («Восстановить историю») находит хранилище по его опознавательному номеру (uuid) и перенацеливает
папку на новое место (`svn relocate`); хранилища нет совсем - `detach_history` («Продолжить без истории») убирает
`.svn`, файлы остаются.

Вызывать только из фонового потока.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from . import checks, fsutil, importer, jobs, messages, next_step, publish
from .config import FilterRules, SettingsStore
from .svn_core import (CREATE_NO_WINDOW, CancelToken, CheckoutProgress, CommitResult, ErrorKind, SvnClient, SvnError,
                       WcStatus, decode_output)

log = logging.getLogger("svnhelper.localrepo")

HISTORY_DIR = ".svnlite"
HISTORY_SUB = "history"
README_NAME = "README.txt"
README_TEXT = (
    "Здесь SVN-lite хранит историю ваших проектов на этом компьютере (папка history).\r\n"
    "Не удаляйте и не переименовывайте эту папку: без неё прежние сохранения проектов будут потеряны.\r\n"
    "Сами файлы проектов лежат в своих папках и от этой папки не зависят.\r\n")
JOB_KIND = "local_history"
SVNADMIN_TIMEOUT = 120

# стадии журнала по порядку
STAGE_STARTED = "started"             # проверки пройдены, истории ещё может не быть
STAGE_REPO = "repo_created"           # папка истории создана
STAGE_LAYOUT = "layout_created"       # в ней есть trunk, branches, tags
STAGE_IMPORTED = "imported"           # файлы проекта загружены
STAGE_CHECKING_OUT = "checking_out"   # идёт привязка папки проекта: в ней может быть недоделанная .svn
STAGE_CHECKED_OUT = "checked_out"     # папка привязана, осталось записать путь в settings.json и убрать журнал

# коды отказов (LocalRepoError.code)
NO_FOLDER = "no_folder"
NETWORK_DRIVE = "network_drive"
NO_SVNADMIN = "no_svnadmin"
ALREADY_VERSIONED = "already_versioned"
IN_PROGRESS = "in_progress"
CANCELLED = "cancelled"
BUSY = "folder_busy"
NOT_LOCAL = "not_local"
HISTORY_EXISTS = "history_exists"
BROKEN_COPY = "broken_copy"
IN_USE = "in_use"
HISTORY_NOT_FOUND = "history_not_found"
FAILED = "failed"

# тексты для человека (без терминов svn)
WHERE_TEXT = "Хранится только на этом компьютере"
# строки состояния живут в messages (там их переводит в слова state_line по коду next_step)
MISSING_TEXT = messages.HISTORY_MISSING_TEXT
MISSING_TODO = messages.HISTORY_RESTORE_TODO              # история найдена на диске
NOT_FOUND_TEXT = messages.HISTORY_NOT_FOUND_TEXT
NOT_FOUND_TODO = messages.HISTORY_NOT_FOUND_TODO
SAVE_BUTTON = "Сохранить у себя"
PUBLISH_BUTTON = "Отправить на сервер…"
RESTORE_BUTTON = "Восстановить историю"
CONTINUE_BUTTON = "Продолжить без истории"
RESTORE_DONE = "Готово: история проекта восстановлена."
RESTORING_TEXT = "Восстанавливаю историю…"
SAVING_TEXT = "Сохраняю у себя…"
NOTHING_TO_SAVE = "Сохранять было нечего: файлы не изменились."
SAVE_TITLE = "Сохранить у себя"
NO_TABS_TEXT = "Проект хранится только на этом компьютере."
CONTINUE_ASK = ("История этого проекта на компьютере не найдена: прежние сохранения вернуть нельзя. "
                "Файлы проекта останутся как есть, а программа перестанет вести их историю. "
                "Позже проект можно отправить на сервер.\n\nТочно продолжить без истории?")
CONTINUE_ASK_FOUND = ("История этого проекта найдена на компьютере, её можно вернуть кнопкой «Восстановить историю». "
                      "Если продолжить без неё, программа перестанет вести историю проекта, а прежние сохранения "
                      "останутся только в скрытой папке .svnlite рядом с проектами: из программы их будет не достать."
                      "\n\nТочно продолжить без истории?")
CONTINUE_DONE = "Готово: файлы проекта остались как были, история больше не ведётся."
SAVE_NOW_ASK = "Сохранить их у себя и сразу отправить проект на сервер?"
DELETE_ARCHIVE_MENU = "Удалить прежние сохранения на этом компьютере…"
DELETE_ARCHIVE_ASK = ("После отправки на сервер на этом компьютере остались прежние сохранения проекта «{name}». "
                      "Файлы проекта и проект на сервере не пострадают, но вернуть эти сохранения будет нельзя."
                      "\n\nУдалить их?")
DELETE_ARCHIVE_DONE = "Прежние сохранения проекта удалены с этого компьютера."

MESSAGES = {
    NO_FOLDER: "Папка проекта не найдена.",
    NETWORK_DRIVE: ("Проект лежит на сетевом диске, историю на компьютере для него включить нельзя. "
                    "Перенесите папку на диск компьютера."),
    NO_SVNADMIN: "Не найдена программа для ведения истории на компьютере (она ставится вместе с TortoiseSVN).",
    ALREADY_VERSIONED: "Эта папка уже связана с историей проекта (в ней есть служебная папка .svn).",
    IN_PROGRESS: "С этим проектом сейчас идёт другая работа. Дождитесь её окончания.",
    CANCELLED: "Остановлено. Файлы проекта не изменены.",
    BUSY: publish.MESSAGES[publish.SVN_BUSY],
    NOT_LOCAL: "Эта папка не ведёт историю на компьютере.",
    HISTORY_EXISTS: "История этого проекта на месте, отказываться от неё не нужно.",
    BROKEN_COPY: publish.MESSAGES[publish.BROKEN_COPY],
    IN_USE: "Эти сохранения сейчас нужны проекту, удалить их нельзя.",
    HISTORY_NOT_FOUND: NOT_FOUND_TEXT + ".",
    FAILED: "Не удалось включить историю на этом компьютере. Файлы проекта не изменены.",
}


class LocalRepoError(Exception):
    """Отказ или сбой. code - одна из констант выше; message по-русски; rolled_back: убрано ли созданное
    (False: что-то осталось, программа повторит уборку при запуске)."""

    def __init__(self, code: str, message: str = "", *, error: Optional[BaseException] = None,
                 rolled_back: bool = True):
        super().__init__(message or MESSAGES.get(code, MESSAGES[FAILED]))
        self.code = code
        self.message = str(self)
        self.error = error
        self.rolled_back = rolled_back

    def to_dict(self) -> dict:
        """Для CLI и MCP: код, текст по-русски и признак отката. Без подробностей svn (self.error)."""
        return {"code": self.code, "message": self.message, "rolled_back": self.rolled_back}


# --------------------------------------------------------------------------
# Адреса и пути
# --------------------------------------------------------------------------

def history_root(base: str) -> str:
    """`<папка проектов>\\.svnlite\\history`."""
    return os.path.join(base, HISTORY_DIR, HISTORY_SUB)


def is_local_url(url: str, server_url: str = "") -> bool:
    """Адрес рабочей копии - история на этом компьютере (file://), а не проект на сервере.

    server_url - адрес сервера из настроек: если он сам file:// (тесты, демо) и копия лежит в нём, это сервер."""
    if not url.lower().startswith("file://"):
        return False
    if server_url:
        base = unquote(server_url).rstrip("/").lower()
        here = unquote(url).rstrip("/").lower()
        if here == base or here.startswith(base + "/"):
            return False
    return True


def path_from_url(url: str) -> str:
    """file:///C:/папка/проект -> C:\\папка\\проект. Пустая строка, если это не file://."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "file":
        return ""
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = "//" + parsed.netloc + path            # file://сервер/папка
    return os.path.normpath(url2pathname(path))


def repo_exists(root_url: str) -> bool:
    """Папка истории по адресу корня (svn info) на месте. Не file:// - считается, что на месте."""
    path = path_from_url(root_url)
    return not path or os.path.isdir(path)


def repo_uuid(repo: str) -> str:
    """Опознавательный номер хранилища истории (первая строка db/uuid). Пусто, если прочитать не вышло."""
    try:
        with open(os.path.join(repo, "db", "uuid"), encoding="ascii") as fh:
            return fh.readline().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _history_dirs(base: str) -> list[str]:
    """Папки хранилищ истории в `<base>\\.svnlite\\history`."""
    root = history_root(base)
    try:
        return [os.path.join(root, name) for name in sorted(os.listdir(root))]
    except OSError:
        return []


def find_history(store: SettingsStore, folder: str, uuid: str) -> str:
    """Хранилище истории проекта по его uuid (из `svn info` папки) или пустая строка, если его нет на диске.

    Ищет там, где проект лежит сейчас (`<папка проектов>\\.svnlite\\history\\*`: папку проектов переименовали или
    перенесли вместе с историей), в остальных папках проектов из настроек и по путям из settings.json."""
    if not uuid:
        return ""
    folder = os.path.normpath(os.path.abspath(folder))
    candidates = [repo for base in (os.path.dirname(folder), *store.settings.project_dirs)
                  for repo in _history_dirs(base)]
    candidates += list(store.settings.local_histories.values())
    seen: set[str] = set()
    for repo in candidates:
        key = _key(repo)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(repo) and repo_uuid(repo) == uuid:
            return os.path.normpath(repo)
    return ""


def _is_history_repo(repo: str) -> bool:
    """Папка вида `...\\.svnlite\\history\\<id>`: удалять разрешено только такие."""
    repo = os.path.normpath(repo)
    parent = os.path.dirname(repo)
    return (os.path.basename(parent).casefold() == HISTORY_SUB
            and os.path.basename(os.path.dirname(parent)).casefold() == HISTORY_DIR)


def _key(folder: str) -> str:
    return os.path.normcase(os.path.normpath(folder))


# --------------------------------------------------------------------------
# Запись в settings.json
# --------------------------------------------------------------------------

def register(store: SettingsStore, folder: str, repo: str) -> None:
    """Запоминает, где лежит история проекта (под замком настроек: другая программа не потеряет свои записи)."""
    key, path = _key(folder), os.path.normpath(repo)
    store.update(lambda s: setattr(s, "local_histories", {**s.local_histories, key: path}))


def rebind(store: SettingsStore, folder: str, repo: str, old_repo: str = "") -> None:
    """История проекта нашлась на новом месте: запись по ключу-папке переписывается, устаревшие записи этой же
    истории (ключ - прежнее имя папки, значение - прежний путь к ней) убираются."""
    stale = {_key(repo), _key(old_repo)} if old_repo else {_key(repo)}
    key, path = _key(folder), os.path.normpath(repo)

    def apply(s) -> None:
        kept = {k: v for k, v in s.local_histories.items() if k != key and _key(v) not in stale}
        s.local_histories = {**kept, key: path}

    store.update(apply)


def unregister(store: SettingsStore, folder: str) -> None:
    key = _key(folder)
    store.update(lambda s: setattr(s, "local_histories", {k: v for k, v in s.local_histories.items() if k != key}))


def archive_path(store: SettingsStore, folder: str) -> str:
    """Папка с прежними сохранениями проекта, если она записана и лежит на диске, иначе пустая строка."""
    repo = store.settings.local_histories.get(_key(folder), "")
    return repo if repo and os.path.isdir(repo) else ""


# --------------------------------------------------------------------------
# Журнал задачи
# --------------------------------------------------------------------------

@dataclass
class LocalRepoJob:
    id: str
    folder: str
    repo: str
    stage: str = STAGE_STARTED
    updated: str = ""
    kind: str = JOB_KIND
    path: str = ""                  # файл журнала (в сам журнал не пишется)


def _save_job(job: LocalRepoJob) -> None:
    jobs.save(job)
    log.info("История %s: стадия %s", job.folder, job.stage)


def _drop_job(job: LocalRepoJob) -> None:
    jobs.remove(job.path)


def _job_ok(job: LocalRepoJob) -> bool:
    """Журнал указывает только на нашу историю рядом с проектом: иначе по нему можно удалить чужое."""
    return os.path.isabs(job.folder) and jobs.is_child(job.repo, history_root(os.path.dirname(job.folder)))


def pending_jobs(jobs_dir: Optional[os.PathLike] = None, include_active: bool = False) -> list[LocalRepoJob]:
    """Прерванные задачи истории; задачи, которые сейчас идут, пропускаются (если не include_active)."""
    found = []
    for job in jobs.load_jobs(LocalRepoJob, jobs_dir, JOB_KIND, include_active):
        if not _job_ok(job):
            log.warning("Журнал истории %s пропущен: пути %s, %s не наши", job.path, job.folder, job.repo)
            continue
        found.append(job)
    return found


# --------------------------------------------------------------------------
# Создание
# --------------------------------------------------------------------------

@dataclass
class LocalRepoResult:
    repo: str                       # папка истории
    repo_url: str                   # её адрес file:///
    revision: Optional[int]         # на каком сохранении теперь папка проекта
    entries: int                    # сколько файлов и папок загружено


def _check_folder(folder: str) -> None:
    if folder.startswith(("\\\\", "//")) or checks.drive_type(folder) == checks.DRIVE_REMOTE:
        raise LocalRepoError(NETWORK_DRIVE, checks.NETWORK_MESSAGE)
    if not os.path.isdir(folder):
        raise LocalRepoError(NO_FOLDER, f"Папка проекта не найдена: {folder}")
    if os.path.lexists(os.path.join(folder, ".svn")):
        raise LocalRepoError(ALREADY_VERSIONED)


def _ensure_root(base: str) -> None:
    """`.svnlite\\history` с пояснением внутри; сама `.svnlite` скрытая."""
    root = os.path.join(base, HISTORY_DIR)
    os.makedirs(history_root(base), exist_ok=True)
    readme = os.path.join(root, README_NAME)
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8", newline="") as fh:
            fh.write(README_TEXT)
    publish._hide(root)         # в Проводнике не мешает; не вышло - не страшно


def _free_repo_dir(base: str) -> str:
    for _attempt in range(100):
        path = os.path.join(history_root(base), uuid.uuid4().hex[:8])
        if not os.path.lexists(path):
            return path
    raise LocalRepoError(FAILED, "Не удалось подобрать папку для истории рядом с проектами.")


def _svnadmin_create(svnadmin: str, repo: str) -> None:
    try:
        proc = subprocess.run([svnadmin, "create", repo], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=SVNADMIN_TIMEOUT, creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        raise LocalRepoError(NO_SVNADMIN, error=exc) from exc
    except subprocess.TimeoutExpired as exc:
        raise LocalRepoError(FAILED, error=exc) from exc
    if proc.returncode != 0:
        text = decode_output(proc.stderr)
        log.error("svnadmin create завершился с кодом %s:\n%s", proc.returncode, text)
        raise LocalRepoError(FAILED, error=SvnError(ErrorKind.OTHER, text, proc.returncode))


def _raise_if_cancelled(cancel: Optional[CancelToken]) -> None:
    if cancel and cancel.cancelled:
        raise LocalRepoError(CANCELLED)


def create_local_repo(client: SvnClient, folder: str, svnadmin: str, rules: Optional[FilterRules] = None,
                      store: Optional[SettingsStore] = None, progress: Optional[CheckoutProgress] = None,
                      cancel: Optional[CancelToken] = None, jobs_dir: Optional[os.PathLike] = None,
                      message: str = "Начало истории проекта") -> LocalRepoResult:
    """Включает историю на этом компьютере для папки проекта. Бросает LocalRepoError.

    Сбой, остановка: история и `.svn` удалены, папка проекта осталась как была (LocalRepoError.rolled_back=False,
    если убрать не вышло: уборку повторит recover_jobs). Убили процесс: уборка при запуске программы.
    store: куда записать путь к истории (settings.json); без него запись не делается.
    """
    rules = rules or FilterRules()
    progress = progress or CheckoutProgress()
    folder = os.path.normpath(os.path.abspath(folder))
    base = os.path.dirname(folder)
    try:
        with publish.mark_busy(folder):
            _check_folder(folder)
            _ensure_root(base)
            repo = _free_repo_dir(base)
            repo_url = Path(repo).as_uri()
            job_id = jobs.new_id()
            job = LocalRepoJob(job_id, folder, repo, path=str(jobs.job_file(jobs_dir, job_id)))
            with jobs.active(job.id):
                try:
                    _save_job(job)           # журнал раньше папки истории: следа без записи не остаётся
                    return _build(client, job, svnadmin, repo_url, rules, store, progress, cancel, message)
                except Exception as exc:
                    raise _undo_and_wrap(job, exc) from exc
    except publish.PublishError as exc:            # mark_busy: проект уже занят
        raise LocalRepoError(IN_PROGRESS, error=exc) from exc


def _step(job: LocalRepoJob, stage: str) -> None:
    job.stage = stage
    _save_job(job)


def _build(client: SvnClient, job: LocalRepoJob, svnadmin: str, repo_url: str, rules: FilterRules,
           store: Optional[SettingsStore], progress: CheckoutProgress, cancel: Optional[CancelToken],
           message: str) -> LocalRepoResult:
    folder = job.folder
    trunk = f"{repo_url}/{importer.LAYOUT[0]}"
    _raise_if_cancelled(cancel)
    _svnadmin_create(svnadmin, job.repo)
    _step(job, STAGE_REPO)
    _raise_if_cancelled(cancel)
    client.mkdir_urls([f"{repo_url}/{name}" for name in importer.LAYOUT], message)
    _step(job, STAGE_LAYOUT)
    patterns = importer.name_patterns(rules)
    _scan, links = importer.scan_source(folder, patterns, cancel)       # ссылки на чужие папки не загружаем
    patterns = list(dict.fromkeys([*patterns, *links]))
    progress.count, progress.last_path = 0, ""
    entries = client.import_dir(folder, trunk, message, importer.svn_ignores(patterns), progress, cancel)
    _step(job, STAGE_IMPORTED)
    _raise_if_cancelled(cancel)
    _step(job, STAGE_CHECKING_OUT)
    progress.count, progress.last_path = 0, ""
    checked = client.checkout(trunk, folder, progress, cancel, force=True)
    _step(job, STAGE_CHECKED_OUT)
    if store is not None:
        register(store, folder, job.repo)
    _drop_job(job)
    log.info("История проекта %s включена: %s", folder, job.repo)
    return LocalRepoResult(job.repo, repo_url, checked.revision, entries)


def _undo_and_wrap(job: LocalRepoJob, exc: Exception) -> LocalRepoError:
    """Сбой посередине: убирает созданную историю и `.svn`, возвращает LocalRepoError для вызывающего."""
    log.error("История проекта %s не создана (стадия %s): %s", job.folder, job.stage, exc)
    clean = _undo(job)
    if clean:
        _drop_job(job)
    if isinstance(exc, LocalRepoError):
        exc.rolled_back = clean
        return exc
    cancelled = isinstance(exc, SvnError) and exc.kind is ErrorKind.CANCELLED
    return LocalRepoError(CANCELLED if cancelled else FAILED, error=exc, rolled_back=clean)


def _undo(job: LocalRepoJob) -> bool:
    """Удаляет то, что создала задача: `.svn` проекта (только если привязка начиналась) и папку истории.
    True: убрано всё. Файлы проекта не трогаются никогда."""
    ok = True
    svn_dir = os.path.join(job.folder, ".svn")
    if job.stage in (STAGE_CHECKING_OUT, STAGE_CHECKED_OUT) and os.path.lexists(svn_dir):
        ok = _remove(svn_dir, job.folder) and ok
    if os.path.lexists(job.repo):
        ok = _remove(job.repo, os.path.dirname(job.repo)) and ok
    _cleanup_root(os.path.dirname(os.path.dirname(os.path.dirname(job.repo))))
    return ok


def _remove(path: str, base: str) -> bool:
    try:
        fsutil.remove_partial_dir(path, base)
        return not os.path.lexists(path)
    except (OSError, ValueError):
        log.exception("Не удалось удалить %s", path)
        return False


def _cleanup_root(base: str) -> None:
    """Если после отката в `.svnlite` не осталось ничего, кроме нашего README, убираем и её."""
    root = os.path.join(base, HISTORY_DIR)
    history = os.path.join(root, HISTORY_SUB)
    try:
        if os.path.isdir(history) and not os.listdir(history):
            os.rmdir(history)
        if os.path.isdir(root) and os.listdir(root) == [README_NAME]:
            os.remove(os.path.join(root, README_NAME))
            os.rmdir(root)
    except OSError:
        log.warning("Не удалось убрать пустую папку %s", root, exc_info=True)


def recover_jobs(jobs_dir: Optional[os.PathLike] = None, store: Optional[SettingsStore] = None) -> int:
    """При запуске: доделывает или откатывает прерванные задачи. Возвращает число откатов.

    Привязка закончена (checked_out) - только запись в settings.json и закрытие журнала. Иначе история и недоделанная
    `.svn` удаляются. Папку держит другая работа или уборка не вышла - журнал остаётся до следующего запуска.
    """
    removed = 0
    for job in pending_jobs(jobs_dir):
        try:
            with publish.mark_busy(job.folder):
                if job.stage == STAGE_CHECKED_OUT:
                    if store is not None and os.path.isdir(job.repo):
                        register(store, job.folder, job.repo)
                    _drop_job(job)
                    continue
                if _undo(job):
                    _drop_job(job)
                    removed += 1
        except publish.PublishError:
            log.info("Папка %s занята, историю разберём при следующем запуске", job.folder)
    return removed


# --------------------------------------------------------------------------
# «Продолжить без истории» и удаление архива
# --------------------------------------------------------------------------

def restore_history(client: SvnClient, folder: str, store: SettingsStore) -> str:
    """«Восстановить историю»: хранилище переехало (папку проектов переименовали или перенесли) - находит его по
    uuid и перенацеливает папку проекта на новое место (`svn relocate`), файлы не трогаются. Возвращает путь к
    хранилищу. Бросает LocalRepoError: HISTORY_EXISTS (история на месте), HISTORY_NOT_FOUND (на диске её нет)."""
    folder = os.path.normpath(os.path.abspath(folder))
    try:
        with publish.mark_busy(folder):
            try:
                info = client.info(folder)
            except SvnError as exc:
                raise LocalRepoError(BROKEN_COPY, error=exc) from exc
            if not info.url.lower().startswith("file://"):
                raise LocalRepoError(NOT_LOCAL)
            root = info.root or info.url
            if repo_exists(root):
                raise LocalRepoError(HISTORY_EXISTS)
            repo = find_history(store, folder, info.uuid)
            if not repo:
                raise LocalRepoError(HISTORY_NOT_FOUND)
            tail = info.url[len(info.root):] if info.root and info.url.startswith(info.root) else "/" + importer.LAYOUT[0]
            try:
                client.relocate(folder, Path(repo).as_uri() + tail)     # as_uri кодирует «%» и пробелы в пути
            except SvnError as exc:
                raise LocalRepoError(FAILED, error=exc) from exc
            rebind(store, folder, repo, path_from_url(root))
            log.info("История проекта %s восстановлена: %s", folder, repo)
            return repo
    except publish.PublishError as exc:
        raise LocalRepoError(IN_PROGRESS, error=exc) from exc


def detach_history(client: SvnClient, folder: str, store: Optional[SettingsStore] = None) -> None:
    """История пропала: убирает `.svn` проекта, файлы остаются. Бросает LocalRepoError.

    Работает, только если папка действительно привязана к файловой истории, которой уже нет на диске."""
    folder = os.path.normpath(os.path.abspath(folder))
    try:
        with publish.mark_busy(folder):
            try:
                info = client.info(folder)
            except SvnError as exc:
                raise LocalRepoError(BROKEN_COPY, error=exc) from exc
            if not info.url.lower().startswith("file://"):
                raise LocalRepoError(NOT_LOCAL)
            if repo_exists(info.root or info.url):
                raise LocalRepoError(HISTORY_EXISTS)
            try:
                fsutil.remove_partial_dir(os.path.join(folder, ".svn"), folder)
            except OSError as exc:
                raise LocalRepoError(BUSY, error=exc) from exc
            if store is not None:
                unregister(store, folder)
            log.info("Папка %s продолжает жить без истории", folder)
    except publish.PublishError as exc:
        raise LocalRepoError(IN_PROGRESS, error=exc) from exc


def delete_history(store: SettingsStore, folder: str, client: Optional[SvnClient] = None) -> None:
    """Явное удаление прежних сохранений проекта (архива после отправки на сервер). Бросает LocalRepoError.

    Если папка проекта всё ещё привязана к этой истории, удалить её нельзя."""
    folder = os.path.normpath(os.path.abspath(folder))
    repo = store.settings.local_histories.get(_key(folder), "")
    if not repo or not _is_history_repo(repo):
        raise LocalRepoError(NOT_LOCAL)
    try:
        with publish.mark_busy(folder):
            if client is not None and os.path.isdir(os.path.join(folder, ".svn")):
                try:
                    root = client.info(folder).root
                except SvnError:
                    root = ""
                if root and _key(path_from_url(root)) == _key(repo):
                    raise LocalRepoError(IN_USE)
            try:
                if os.path.isdir(repo):
                    fsutil.remove_partial_dir(repo, os.path.dirname(repo))
            except (OSError, ValueError) as exc:
                raise LocalRepoError(BUSY, error=exc) from exc
            _cleanup_root(os.path.dirname(os.path.dirname(os.path.dirname(repo))))
            unregister(store, folder)
            log.info("Прежние сохранения проекта %s удалены: %s", folder, repo)
    except publish.PublishError as exc:
        raise LocalRepoError(IN_PROGRESS, error=exc) from exc


# --------------------------------------------------------------------------
# Тексты состояния и сохранения
# --------------------------------------------------------------------------

def status_line(status: Optional[WcStatus], checking: bool, missing: bool, found: bool = False) -> tuple[str, str, str]:
    """Состояние проекта с историей на компьютере: (текст, тег цвета ok|new|bad, подсказка «что сделать»).

    missing: истории на месте нет; found: она найдена в другом месте (можно восстановить).
    Код состояния даёт next_step, слова - messages.state_line (как у проектов с сервера)."""
    return messages.state_line(next_step.Facts(state="svn", status=status, local=True, checking=bool(checking),
                                               history_missing=bool(missing), history_found=bool(found)))


def saved_text(count: int) -> str:
    return f"Сохранено у себя: {messages.files_count(count)}"


def local_rules(rules: FilterRules) -> FilterRules:
    """Правила мусора для проекта с историей на компьютере: служебные папки других программ (.git и т.п.) в папке
    не считаются новыми файлами, иначе проект никогда не был бы «всё сохранено» и они попали бы в историю."""
    return dataclasses.replace(rules, junk_dirs=list(dict.fromkeys([*rules.junk_dirs, *importer.ALWAYS_SKIP])))


def save(client: SvnClient, folder: str, rules: Optional[FilterRules] = None, message: str = "",
         cancel: Optional[CancelToken] = None) -> Optional[CommitResult]:
    """«Сохранить у себя»: все изменения папки (новые файлы тоже) в историю на компьютере. Нечего сохранять - None.

    После сохранения папка обновляется до последнего состояния: иначе её корень остался бы на прежнем номере
    и список прежних сохранений не показывал бы только что сделанное."""
    changes = client.status(folder, rules=local_rules(rules or FilterRules()), cancel=cancel).local_changes
    if not changes:
        return None
    result = client.commit(folder, [c.path for c in changes], message.strip() or auto_comment(), cancel)
    client.update(folder, cancel=cancel)
    return result


def auto_comment(now: Optional[datetime] = None) -> str:
    """Комментарий к сохранению, если человек его не написал."""
    return f"Сохранено у себя {(now or datetime.now()):%d.%m.%Y %H:%M}"
