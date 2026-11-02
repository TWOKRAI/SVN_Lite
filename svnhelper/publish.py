"""Отправка проекта с компьютера на сервер («Отправить позже») и привязка папки на месте. Без tkinter.

Порядок (docs/PLAN_templates.md, Task T.4):
1. проверки (`check_publish`): папка не на сетевом диске, не привязана к серверу, у локальной истории (file://)
   нет несохранённого, место на сервере есть, имя свободно;
2. папка помечается занятой (`is_busy`, `busy_paths`): окно и фоновая проверка её не трогают;
3. источник во временной соседней папке `<база>\\.<имя>.publishN`: для file:// `svn export` самой копии,
   иначе копия без .svn/.git, мусора и ссылок на другие папки; в копии обновляется project.json;
4. `importer.create_project` (три папки, загрузка, откат при сбое);
5. привязка на месте: `.svn` (если была) переносится в `<база>\\.<имя>.svn-old`, `checkout --force`,
   «только для чтения» для файлов с svn:needs-lock, `revert project.json`, `.svn-old` удаляется.
   Сбой привязки возвращает `.svn` на место.

Исходная папка до успешной загрузки только читается. Журнал задачи (`data_dir()/create_jobs/<id>.json`, запись и
«жива ли задача» - `jobs.py`) пишется после каждого шага, в нём и номер правки, которой созданы три папки проекта;
по нему после сбоя или падения программы `recover_job` возвращает папку к исходному виду и решает, что предложить:
«Завершить отправку» (`resume_publish`) или ничего. Откат каркаса на сервере не удался - журнал остаётся на стадии
uploading, откат повторит разбор при следующем запуске. Вызывать только из фонового потока.
"""
from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import unquote

from . import checks, fsutil, importer, jobs, projectmeta, server
from .config import FilterRules
from .svn_core import CancelToken, CheckoutProgress, ErrorKind, SvnClient, SvnError

log = logging.getLogger("svnhelper.publish")

JOBS_DIR_NAME = jobs.JOBS_DIR_NAME
JOB_KIND = "publish"
RENAME_ATTEMPTS = 3
RETRY_DELAY = 1.0           # секунд между попытками перенести занятую .svn

# стадии журнала по порядку
STAGE_STARTED = "started"           # проверки пройдены, временной папки может ещё не быть
STAGE_SOURCE = "source_ready"       # временная копия готова
STAGE_UPLOADING = "uploading"       # идёт create_project: на сервере может быть недоделанный проект
STAGE_UPLOADED = "uploaded"         # проект на сервере, папка ещё не привязана
STAGE_BINDING = "binding"           # .svn перенесена в .svn-old (если была), идёт привязка
STAGE_BOUND = "bound"               # привязка удалась, осталось убрать .svn-old и журнал
_BEFORE_UPLOAD = (STAGE_STARTED, STAGE_SOURCE, STAGE_UPLOADING)

# коды отказов (PublishError.code): по ним окно выбирает кнопки
NO_FOLDER = "no_folder"
NETWORK_DRIVE = "network_drive"
BAD_NAME = "bad_name"
ALREADY_ON_SERVER = "already_on_server"
BROKEN_COPY = "broken_copy"
LOCAL_CHANGES = "local_changes"         # окно предлагает «Сохранить у себя»
UNFINISHED = "unfinished"               # от прошлой отправки осталась .svn-old
IN_PROGRESS = "in_progress"             # эта папка отправляется прямо сейчас
INSIDE_PROJECT = "inside_project"       # папка лежит внутри скачанного проекта
LOCAL_HISTORY_MISSING = "local_history_missing"
NO_PARENT = "no_parent"
EXISTS = "exists"
RESUMABLE = "resumable"                 # окно предлагает «Завершить отправку» (PublishError.job)
SVN_BUSY = "folder_busy"
NO_RIGHTS = "no_rights"
NEED_LOGIN = "need_login"
NO_CONNECTION = "no_connection"
CANCELLED = "cancelled"
FAILED = "failed"

MESSAGES = {
    NO_FOLDER: "Папка проекта не найдена.",
    NETWORK_DRIVE: ("Проект лежит на сетевом диске. Отправить его отсюда нельзя: перенесите папку "
                    "на диск компьютера и повторите."),
    ALREADY_ON_SERVER: "Этот проект уже на сервере.",
    BROKEN_COPY: ("Служебные данные проекта повреждены, отправить его нельзя. "
                  "Обратитесь к администратору."),
    LOCAL_CHANGES: "В проекте есть несохранённые изменения. Сначала сохраните их у себя, затем отправляйте.",
    UNFINISHED: ("Прошлая отправка этого проекта не закончена. Перезапустите программу: "
                 "она предложит завершить её или убрать недоделанное."),
    IN_PROGRESS: "Этот проект уже отправляется.",
    INSIDE_PROJECT: ("Эта папка лежит внутри проекта, который уже связан с сервером. Отправить можно "
                     "только отдельную папку проекта."),
    LOCAL_HISTORY_MISSING: ("История проекта на этом компьютере потеряна. Это не мешает отправке: "
                            "повторите её, проект уйдёт на сервер как есть."),
    NO_PARENT: "Такого места на сервере нет. Выберите другое место или обратитесь к администратору.",
    EXISTS: "На сервере в этом месте уже есть проект с таким названием. Выберите другое название.",
    RESUMABLE: ("Проект уже отправлен на сервер, но папка на компьютере ещё не связана с ним. "
                "Нажмите «Завершить отправку»."),
    SVN_BUSY: ("Папку проекта сейчас держит другая программа. Закройте окна Проводника и TortoiseSVN "
               "с этим проектом и повторите."),
    NO_RIGHTS: ("Нет прав создавать проекты в этой папке сервера. Проект остался на компьютере: отправить "
                "можно позже или попросить администратора."),
    NEED_LOGIN: "Нужно войти на сервер.",
    NO_CONNECTION: "Нет связи с сервером. Подключитесь к сети или VPN и повторите.",
    CANCELLED: "Отправка остановлена.",
    FAILED: "Не удалось отправить проект на сервер.",
}

# откат каркаса на сервере не удался: журнал остаётся, разбор при следующем запуске повторит откат
ROLLBACK_LATER_TEXT = ("Проект не удалось убрать с сервера полностью; он будет убран при следующем запуске "
                       "программы.")
RECOVER_RETRY_TEXT = ("Недоделанный проект на сервере пока убрать не удалось. Программа повторит это при "
                      "следующем запуске.")

# что осталось на сервере после прерванной загрузки (upload_outcome)
OUTCOME_LANDED = "landed"       # загрузка дошла: проект на сервере готов
OUTCOME_CLEAR = "clear"         # нашего на сервере нет (не создавали, откатили сейчас или проект чужой)
OUTCOME_RETRY = "retry"         # каркас наш, но откатить его сейчас не вышло: журнал остаётся до следующего запуска
OUTCOME_UNKNOWN = "unknown"     # ни номера правки, ни логина: чей проект, неизвестно, сервер не трогаем


def unknown_owner_text(project_url: str) -> str:
    """Текст, когда по журналу нельзя понять, чей недоделанный проект на сервере."""
    name = unquote(project_url).rstrip("/").rsplit("/", 1)[-1]
    return (f"Не удалось выяснить, чей недоделанный проект «{name}» на сервере, поэтому программа его не трогает. "
            "Если он пустой и не нужен, сообщите администратору.")

_SVN_KIND_CODES = {
    ErrorKind.AUTHZ: NO_RIGHTS,
    ErrorKind.AUTH: NEED_LOGIN,
    ErrorKind.NO_CONNECTION: NO_CONNECTION,
    ErrorKind.TIMEOUT: NO_CONNECTION,
    ErrorKind.CANCELLED: CANCELLED,
    ErrorKind.EXISTS: EXISTS,
    ErrorKind.BUSY: SVN_BUSY,
    ErrorKind.LOCAL_HISTORY_MISSING: LOCAL_HISTORY_MISSING,
}


class PublishError(Exception):
    """Отказ или сбой отправки. stage: check | source | upload | bind | recover.

    job: журнал, если проект уже на сервере и отправку можно завершить; create_error: сбой загрузки
    (CreateError.rolled_back: удалось ли вернуть сервер как было).
    """

    def __init__(self, code: str, stage: str, message: str = "", *, error: Optional[BaseException] = None,
                 job: Optional["PublishJob"] = None, create_error: Optional[importer.CreateError] = None):
        super().__init__(message or MESSAGES.get(code, MESSAGES[FAILED]))
        self.code = code
        self.stage = stage
        self.message = str(self)
        self.error = error
        self.job = job
        self.create_error = create_error


def _svn_code(exc: SvnError) -> str:
    return _SVN_KIND_CODES.get(exc.kind, FAILED)


# --------------------------------------------------------------------------
# Занятые папки: окно и фоновая проверка их не трогают
# --------------------------------------------------------------------------

_busy: set[str] = set()
_busy_lock = threading.Lock()


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_busy(path: str) -> bool:
    """Идёт ли сейчас отправка этой папки."""
    with _busy_lock:
        return _key(path) in _busy


def busy_paths() -> frozenset:
    """Снимок папок, которые сейчас отправляются (ключи normcase+normpath)."""
    with _busy_lock:
        return frozenset(_busy)


@contextmanager
def mark_busy(folder: str) -> Iterator[None]:
    key = _key(folder)
    with _busy_lock:
        if key in _busy:
            raise PublishError(IN_PROGRESS, "check")
        _busy.add(key)
    try:
        yield
    finally:
        with _busy_lock:
            _busy.discard(key)


_mark_busy = mark_busy      # прежнее имя


# --------------------------------------------------------------------------
# Журнал задачи
# --------------------------------------------------------------------------

def default_jobs_dir() -> Path:
    return jobs.default_dir()


@dataclass
class PublishJob:
    id: str
    folder: str
    project_url: str
    work_dir: str = ""              # временная копия для загрузки
    svn_old: str = ""               # куда переносится .svn на время привязки
    stage: str = STAGE_STARTED
    author: str = ""                # логин, под которым создавались папки на сервере (для отката)
    had_svn: Optional[bool] = None  # была ли в папке .svn до привязки (None: привязка ещё не начиналась)
    revision: Optional[int] = None  # в какой ревизии загружены файлы
    updated: str = ""
    kind: str = JOB_KIND
    path: str = field(default="", compare=False)     # файл журнала (в сам журнал не пишется)
    layout_revision: Optional[int] = None   # правка, которой созданы три папки проекта (None: ещё нет или старый журнал)

    @property
    def trunk_url(self) -> str:
        return f"{self.project_url}/{server.TRUNK}"


def _save_job(job: PublishJob) -> None:
    jobs.save(job)
    log.info("Отправка %s: стадия %s", job.folder, job.stage)


def _drop_job(job: PublishJob) -> None:
    jobs.remove(job.path)


def pending_jobs(jobs_dir: Optional[os.PathLike] = None, include_active: bool = False) -> list[PublishJob]:
    """Незаконченные отправки (для разбора при запуске программы). Битые файлы пропускаются с записью в журнал;
    задачи, которые сейчас идут (в этом или другом процессе), пропускаются, если не include_active."""
    found = []
    for job in jobs.load_jobs(PublishJob, jobs_dir, JOB_KIND, include_active):
        if not _job_paths_ok(job):
            log.warning("Журнал задачи %s пропущен: служебные папки не рядом с проектом (%s, %s)",
                        job.path, job.svn_old, job.work_dir)
            continue
        found.append(job)
    return found


def _job_paths_ok(job: PublishJob) -> bool:
    """Журнал указывает только на наши соседние папки: иначе по нему можно удалить чужое."""
    if not os.path.isabs(job.folder):
        return False
    folder = os.path.normpath(job.folder)
    if not job.svn_old or not jobs.same_path(job.svn_old, svn_old_path(folder)):
        return False
    pattern = r"\." + re.escape(os.path.basename(folder)) + r"\.publish\d*"
    return not job.work_dir or jobs.is_child(job.work_dir, os.path.dirname(folder), pattern)


def find_job(folder: str, project_url: str = "", jobs_dir: Optional[os.PathLike] = None) -> Optional[PublishJob]:
    for job in pending_jobs(jobs_dir, include_active=True):
        if _key(job.folder) == _key(folder) and (not project_url or job.project_url == project_url):
            return job
    return None


# --------------------------------------------------------------------------
# Проверки до начала
# --------------------------------------------------------------------------

@dataclass
class PublishPlan:
    folder: str
    name: str                      # имя проекта на сервере
    project_url: str               # закодированный адрес (server.build_url)
    local_url: Optional[str]       # адрес локальной истории (file://), если папка к ней привязана, иначе None

    @property
    def base(self) -> str:
        return os.path.dirname(self.folder)


def svn_old_path(folder: str) -> str:
    """Скрытая соседняя папка на том же диске: туда переносится .svn на время привязки."""
    return os.path.join(os.path.dirname(folder), f".{os.path.basename(folder)}.svn-old")


def check_publish(client: SvnClient, folder: str, parent_url: str, name: Optional[str] = None,
                  rules: Optional[FilterRules] = None,
                  jobs_dir: Optional[os.PathLike] = None) -> PublishPlan:
    """Проверки шага 1. parent_url закодирован (build_url). Бросает PublishError(stage="check")."""
    folder = os.path.normpath(os.path.abspath(folder))
    if folder.startswith(("\\\\", "//")) or checks.drive_type(folder) == checks.DRIVE_REMOTE:
        raise PublishError(NETWORK_DRIVE, "check")
    if not os.path.isdir(folder):
        raise PublishError(NO_FOLDER, "check", f"Папка проекта не найдена: {folder}")
    try:
        name = server.validate_line_name(name if name is not None else os.path.basename(folder))
    except ValueError as exc:
        raise PublishError(BAD_NAME, "check", str(exc)) from exc
    if os.path.lexists(svn_old_path(folder)):
        raise PublishError(UNFINISHED, "check", job=find_job(folder, jobs_dir=jobs_dir))
    project_url = server.build_url(parent_url, [name])
    try:
        local_url = _check_local_copy(client, folder, parent_url, rules)
        if not client.url_exists(parent_url):
            raise PublishError(NO_PARENT, "check")
        if client.url_exists(project_url):
            job = find_job(folder, project_url, jobs_dir)
            if job is None:
                raise PublishError(EXISTS, "check")
            raise PublishError(UNFINISHED if job.stage in _BEFORE_UPLOAD else RESUMABLE, "check", job=job)
    except SvnError as exc:
        raise PublishError(_svn_code(exc), "check", error=exc) from exc
    return PublishPlan(folder=folder, name=name, project_url=project_url, local_url=local_url)


def _check_local_copy(client: SvnClient, folder: str, parent_url: str,
                      rules: Optional[FilterRules]) -> Optional[str]:
    """Папка без .svn -> None. С локальной историей (file://) -> её адрес, если всё сохранено. История пропала -> None.

    На сервере: адрес не file:// или из того же хранилища, что и выбранное место (тесты, file:// сервер)."""
    if not os.path.isdir(os.path.join(folder, ".svn")):
        try:
            client.info(folder)
        except SvnError as exc:
            if "E155007" in exc.codes:          # не рабочая копия: обычная папка
                return None
            if "W155010" not in exc.codes:      # W155010: папка внутри копии, но не сохранена в ней
                raise
        raise PublishError(INSIDE_PROJECT, "check")
    try:
        info = client.info(folder)
    except SvnError as exc:
        log.warning("В %s есть .svn, но данные о ней не читаются: %s", folder, exc.raw)
        raise PublishError(BROKEN_COPY, "check", error=exc) from exc
    same_repo = bool(info.root) and (unquote(parent_url).rstrip("/") + "/").startswith(
        unquote(info.root).rstrip("/") + "/")
    if not info.url.lower().startswith("file://") or same_repo:
        raise PublishError(ALREADY_ON_SERVER, "check")
    from . import localrepo          # отложенно: localrepo сам импортирует publish
    if not localrepo.repo_exists(info.root or info.url):
        # история пропала: сохранять некуда и «несохранённых правок» нет смысла требовать; уходит то, что лежит
        # в папке (как у обычной папки), а прежняя .svn при привязке уйдёт в .svn-old и будет удалена
        return None
    st = client.status(folder, rules=rules)      # локально: связь с историей проекта не нужна
    if st.local_changes or st.conflicts:
        raise PublishError(LOCAL_CHANGES, "check")
    return info.url


# --------------------------------------------------------------------------
# Отправка
# --------------------------------------------------------------------------

@dataclass
class PublishResult:
    project_url: str
    revision: Optional[int]
    entries: int
    read_only: list[str] = field(default_factory=list)      # файлы, которым поставлено «только для чтения»
    not_uploaded: list[str] = field(default_factory=list)   # остались только на компьютере (мусор, ссылки)
    changed: list[str] = field(default_factory=list)        # после привязки отличаются от сервера (должно быть пусто)
    leftover: str = ""                                       # .svn-old, который не удалось удалить


def publish(client: SvnClient, folder: str, parent_url: str, message: str, name: Optional[str] = None,
            rules: Optional[FilterRules] = None, progress: Optional[CheckoutProgress] = None,
            cancel: Optional[CancelToken] = None, jobs_dir: Optional[os.PathLike] = None,
            retry_delay: float = RETRY_DELAY) -> PublishResult:
    """Отправляет последнее состояние папки на сервер в <parent_url>/<name> и привязывает её на месте.

    Бросает PublishError. Сбой до конца загрузки: сервер откатывается (create_project), папка не тронута,
    журнал удаляется. Сбой привязки: проект на сервере остаётся, .svn возвращается, журнал остаётся
    (PublishError.job -> «Завершить отправку»).
    """
    rules = rules or FilterRules()
    progress = progress or CheckoutProgress()
    plan = check_publish(client, folder, parent_url, name, rules, jobs_dir)
    creds = client.credentials
    job_id = jobs.new_id()
    job = PublishJob(id=job_id, folder=plan.folder, project_url=plan.project_url,
                     work_dir=_free_work_dir(plan.folder), svn_old=svn_old_path(plan.folder),
                     author=creds.username if creds else "", path=str(jobs.job_file(jobs_dir, job_id)))
    with mark_busy(plan.folder), jobs.active(job.id):
        _save_job(job)
        patterns = importer.name_patterns(rules)
        try:
            _prepare_source(client, plan, job.work_dir, patterns, progress, cancel)
            if cancel and cancel.cancelled:
                raise SvnError(ErrorKind.CANCELLED)
        except Exception as exc:
            _remove_work_dir(job)
            _drop_job(job)
            if isinstance(exc, PublishError):
                raise
            code = _svn_code(exc) if isinstance(exc, SvnError) else FAILED
            raise PublishError(code, "source", error=exc) from exc
        job.stage = STAGE_SOURCE
        _save_job(job)

        job.stage = STAGE_UPLOADING
        _save_job(job)
        progress.count, progress.last_path = 0, ""
        try:
            created = importer.create_project(client, plan.project_url, job.work_dir, message,
                                              importer.svn_ignores(patterns), progress, cancel,
                                              on_layout=jobs.layout_recorder(job))
        except importer.CreateError as exc:
            _remove_work_dir(job)
            text = MESSAGES.get(_svn_code(exc.error), MESSAGES[FAILED])
            if exc.rolled_back is False:
                # журнал остаётся на стадии uploading: при следующем запуске recover_job уберёт каркас
                log.warning("Каркас проекта %s не откачен (%s): журнал %s остаётся", plan.project_url,
                            exc.rollback_error, job.path)
                text += " " + ROLLBACK_LATER_TEXT
            else:
                _drop_job(job)
            raise PublishError(_svn_code(exc.error), "upload", text, error=exc.error, create_error=exc) from exc
        job.stage, job.revision = STAGE_UPLOADED, created.revision
        _save_job(job)
        _remove_work_dir(job)
        log.info("Проект %s загружен (ревизия %s, записей %s)", plan.project_url, created.revision, created.entries)
        result = PublishResult(project_url=plan.project_url, revision=created.revision, entries=created.entries)
        return _bind(client, job, result, rules, progress, cancel, retry_delay)


def resume_publish(client: SvnClient, job: PublishJob, rules: Optional[FilterRules] = None,
                   progress: Optional[CheckoutProgress] = None, cancel: Optional[CancelToken] = None,
                   retry_delay: float = RETRY_DELAY) -> PublishResult:
    """«Завершить отправку»: проект уже на сервере, папку осталось привязать. Бросает PublishError."""
    rules = rules or FilterRules()
    progress = progress or CheckoutProgress()
    with mark_busy(job.folder), jobs.active(job.id):
        try:
            if job.stage == STAGE_BOUND:
                return _finish(client, job, PublishResult(job.project_url, job.revision, 0), rules)
            if _bound_to(client, job):
                # привязка начата, но могла оборваться посреди скачивания: докачиваем (повтор --force
                # идемпотентен, файлы пользователя не перезаписывает); _finish удалит .svn-old и журнал
                _complete_checkout(client, job, progress, cancel)
                return _finish(client, job, PublishResult(job.project_url, job.revision, 0), rules)
            _restore_svn(job, retry_delay)
            if not client.url_exists(job.trunk_url):
                _drop_job(job)
                raise PublishError(FAILED, "recover", "Проекта на сервере нет: отправьте папку заново.")
        except SvnError as exc:
            raise PublishError(_svn_code(exc), "recover", error=exc, job=job) from exc
        except OSError as exc:
            raise PublishError(SVN_BUSY, "recover", error=exc, job=job) from exc
        job.stage = STAGE_UPLOADED
        _save_job(job)
        return _bind(client, job, PublishResult(job.project_url, job.revision, 0), rules, progress, cancel,
                     retry_delay)


def _complete_checkout(client: SvnClient, job: PublishJob, progress: CheckoutProgress,
                       cancel: Optional[CancelToken]) -> None:
    """Доделывает оборванную привязку: cleanup снимает блокировку прерванного svn, повтор checkout --force
    докачивает недостающее (и расширяет неполную глубину). Сбой -> PublishError, журнал остаётся."""
    job.stage = STAGE_BINDING
    _save_job(job)
    try:
        client.cleanup(job.folder)
        progress.count, progress.last_path = 0, ""
        client.checkout(job.trunk_url, job.folder, progress, cancel, force=True)
    except SvnError as exc:
        code = _svn_code(exc)
        text = None if code in (SVN_BUSY, NO_CONNECTION, NEED_LOGIN) else (
            "Не удалось докачать файлы проекта с сервера. Нажмите «Завершить отправку» ещё раз; "
            "если не поможет, обратитесь к администратору.")
        raise PublishError(code, "recover", text or "", error=exc, job=job) from exc
    job.stage = STAGE_BOUND
    _save_job(job)


def _bound_to(client: SvnClient, job: PublishJob) -> bool:
    if not os.path.isdir(os.path.join(job.folder, ".svn")):
        return False
    try:
        return unquote(client.info(job.folder).url).rstrip("/") == unquote(job.trunk_url).rstrip("/")
    except SvnError:
        return False


def _free_work_dir(folder: str) -> str:
    base, name = os.path.dirname(folder), os.path.basename(folder)
    for n in range(1, 1000):
        path = os.path.join(base, f".{name}.publish{n if n > 1 else ''}")
        if not os.path.lexists(path):
            return path
    raise PublishError(FAILED, "source", "Не удалось подобрать временную папку рядом с проектом.")


def _prepare_source(client: SvnClient, plan: PublishPlan, work_dir: str, patterns: list[str],
                    progress: CheckoutProgress, cancel: Optional[CancelToken]) -> None:
    """Временная копия последнего состояния: export из самой копии (сохранённые файлы в том виде, в каком
    они лежат в папке; хранилище истории не нужно) или чистая копия обычной папки."""
    if plan.local_url:
        client.export(plan.folder, work_dir, progress, cancel)
    else:
        _copy_clean(plan.folder, work_dir, patterns, progress, cancel)
    meta = projectmeta.read(work_dir)
    if meta is not None:
        meta.svn = {"url": plan.project_url, "published": projectmeta.now_iso()}
        projectmeta.write(work_dir, meta)


def _copy_clean(src: str, dst: str, patterns: list[str], progress: CheckoutProgress,
                cancel: Optional[CancelToken]) -> None:
    """Копия папки без служебных папок (.svn, .git), мусора и ссылок на другие папки. Пустые папки сохраняются."""
    os.makedirs(dst)
    stack = [(src, dst)]
    while stack:
        if cancel and cancel.cancelled:
            raise SvnError(ErrorKind.CANCELLED)
        from_dir, to_dir = stack.pop()
        try:
            entries = list(os.scandir(from_dir))
        except OSError as exc:
            raise _copy_error(exc, os.path.relpath(from_dir, src)) from exc
        for entry in entries:
            if entry.name.casefold() in importer.ALWAYS_SKIP or importer.is_link(entry) \
                    or importer.is_junk(entry.name, patterns):
                continue
            target = os.path.join(to_dir, entry.name)
            rel = os.path.relpath(entry.path, src)
            try:
                if entry.is_dir(follow_symlinks=False):
                    os.mkdir(target)
                    stack.append((entry.path, target))
                else:
                    shutil.copy2(entry.path, target)
                    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)   # «только для чтения» мешал бы удалить копию
            except OSError as exc:
                raise _copy_error(exc, rel) from exc
            progress.count += 1
            progress.last_path = rel


def _copy_error(exc: OSError, rel: str) -> PublishError:
    log.warning("Копия для отправки: не удалось прочитать %s: %s", rel, exc)
    if getattr(exc, "winerror", None) == 206 or exc.errno == errno.ENAMETOOLONG:
        text = (f"Слишком длинный путь к файлу «{rel}». Сократите названия папок или перенесите проект "
                "ближе к корню диска и повторите.")
    else:
        text = f"Не удалось прочитать файл «{rel}»: закройте программу, в которой он открыт, и повторите."
    return PublishError(FAILED, "source", text, error=exc)


def _remove_work_dir(job: PublishJob) -> None:
    if not job.work_dir or not os.path.lexists(job.work_dir):
        return
    try:
        fsutil.remove_partial_dir(job.work_dir, os.path.dirname(job.folder))
    except (OSError, ValueError):
        log.exception("Не удалось удалить временную папку %s", job.work_dir)


# --------------------------------------------------------------------------
# Привязка на месте
# --------------------------------------------------------------------------

def _rename_with_retries(src: str, dst: str, retry_delay: float) -> None:
    """Перенос папки; занята другой программой (WinError 5/32) -> ещё попытки, затем PublishError(SVN_BUSY)."""
    for attempt in range(1, RENAME_ATTEMPTS + 1):
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            # WinError 5 (доступ запрещён) и 32 (файл занят): папку держит Проводник, TSVNCache, антивирус
            busy = (isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32)
                    or exc.errno in (errno.EACCES, errno.EBUSY))
            if not busy:
                raise
            log.warning("Перенос %s -> %s, попытка %d из %d: %s", src, dst, attempt, RENAME_ATTEMPTS, exc)
            if attempt == RENAME_ATTEMPTS:
                raise PublishError(SVN_BUSY, "bind", error=exc) from exc
            time.sleep(retry_delay)


def _restore_svn(job: PublishJob, retry_delay: float = RETRY_DELAY,
                 client: Optional[SvnClient] = None) -> None:
    """Возвращает папку к виду до привязки: прежняя .svn из .svn-old, у папки без истории недоделанная .svn
    удаляется. .svn удаляется только на стадии binding (её создала наша привязка) и только если папка ещё не
    привязана к проекту на сервере (проверяется, если передан client). Привязка не начиналась -> ничего."""
    svn_dir = os.path.join(job.folder, ".svn")
    if client is not None and _bound_to(client, job):
        log.info("%s уже привязана к %s: .svn не трогаю", job.folder, job.trunk_url)
        return
    ours = job.stage == STAGE_BINDING and os.path.lexists(svn_dir)
    if os.path.isdir(job.svn_old):
        if os.path.lexists(svn_dir):
            if not ours:
                log.warning("В %s есть и .svn, и %s (стадия %s): ничего не трогаю", job.folder, job.svn_old,
                            job.stage)
                return
            fsutil.remove_partial_dir(svn_dir, job.folder)
        _rename_with_retries(job.svn_old, svn_dir, retry_delay)
        log.info("Прежняя .svn возвращена в %s", job.folder)
    elif job.had_svn is False and ours:
        fsutil.remove_partial_dir(svn_dir, job.folder)
        log.info("Недоделанная .svn удалена из %s", job.folder)


def _hide(path: str) -> None:
    """Атрибут «скрытый» (Windows): .svn-old не мешает в Проводнике. Не вышло - не страшно."""
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        attrs = kernel.GetFileAttributesW(path)
        if attrs not in (-1, 0xFFFFFFFF):
            kernel.SetFileAttributesW(path, attrs | 0x2)   # FILE_ATTRIBUTE_HIDDEN
    except (AttributeError, OSError):
        pass


def _bind(client: SvnClient, job: PublishJob, result: PublishResult, rules: FilterRules,
          progress: CheckoutProgress, cancel: Optional[CancelToken], retry_delay: float) -> PublishResult:
    svn_dir = os.path.join(job.folder, ".svn")
    if job.had_svn is None:
        job.had_svn = os.path.isdir(svn_dir)
    job.stage = STAGE_BINDING
    _save_job(job)
    try:
        if cancel and cancel.cancelled:
            raise SvnError(ErrorKind.CANCELLED)
        if os.path.isdir(svn_dir):
            _rename_with_retries(svn_dir, job.svn_old, retry_delay)
            _hide(job.svn_old)
        progress.count, progress.last_path = 0, ""
        client.checkout(job.trunk_url, job.folder, progress, cancel, force=True)
    except Exception as exc:
        try:
            _restore_svn(job, retry_delay)
        except (OSError, ValueError, PublishError):
            log.exception("Не удалось вернуть .svn в %s", job.folder)
        job.stage = STAGE_UPLOADED
        _save_job(job)
        if isinstance(exc, PublishError):
            exc.job = job
            raise
        code = _svn_code(exc) if isinstance(exc, SvnError) else FAILED
        text = None if code == SVN_BUSY else (
            MESSAGES[CANCELLED] + " Проект уже на сервере: нажмите «Завершить отправку», когда будете готовы."
            if code == CANCELLED else
            "Проект отправлен на сервер, но папку на компьютере не удалось связать с ним. "
            "Нажмите «Завершить отправку», чтобы повторить.")
        raise PublishError(code, "bind", text or "", error=exc, job=job) from exc
    job.stage = STAGE_BOUND
    try:
        _save_job(job)
    except OSError:
        # привязка уже удалась: откатывать её из-за занятого журнала нельзя, _finish всё равно закроет журнал
        log.exception("Стадия bound не записана в журнал %s", job.path)
    return _finish(client, job, result, rules)


def _finish(client: SvnClient, job: PublishJob, result: PublishResult, rules: FilterRules) -> PublishResult:
    """После привязки: «только для чтения» для svn:needs-lock, project.json с сервера, проверка, уборка."""
    folder = job.folder
    try:
        for path in client.propget_needs_lock(folder):
            full = path if os.path.isabs(path) else os.path.join(folder, path)
            if os.path.isfile(full):
                os.chmod(full, stat.S_IREAD)
                result.read_only.append(full)
        meta_file = str(projectmeta.meta_path(folder))
        if os.path.isfile(meta_file):
            client.run(["revert", meta_file + "@"], timeout=120)
        st = client.status(folder, rules=rules)
        result.changed = [c.path for c in st.local_changes if c.kind != "unversioned"]
        result.not_uploaded = [c.path for c in st.local_changes if c.kind == "unversioned"]
        if result.changed:
            log.warning("После привязки %s отличаются от сервера: %s", folder, result.changed)
        if result.revision is None:
            result.revision = client.info(folder).revision
    except SvnError as exc:
        # привязка уже есть: проект рабочий, недоделанное (атрибуты, project.json) покажет обычная проверка
        log.error("После привязки %s не всё доделано: %s", folder, exc.raw)
    if os.path.lexists(job.svn_old):
        try:
            fsutil.remove_partial_dir(job.svn_old, os.path.dirname(folder))
        except (OSError, ValueError):
            log.exception("Не удалось удалить %s", job.svn_old)
            result.leftover = job.svn_old
    _drop_job(job)
    log.info("Проект %s привязан к %s", folder, job.trunk_url)
    return result


# --------------------------------------------------------------------------
# Разбор после сбоя или падения программы
# --------------------------------------------------------------------------

def recover_job(client: SvnClient, job: PublishJob, retry_delay: float = RETRY_DELAY) -> Optional[PublishJob]:
    """Разбирает незаконченную отправку (при запуске программы). Нужна связь с сервером.

    Папка возвращается к исходному виду (.svn из .svn-old), временная копия удаляется. Возвращает журнал,
    если проект уже на сервере и отправку можно завершить (`resume_publish`), иначе None: недоделанный
    нами проект на сервере откатывается, журнал удаляется. Бросает PublishError (связи нет, папка занята,
    каркас пока не откатить, чей каркас - неизвестно); журнал тогда остаётся. PublishError.job есть, только если
    загрузка уже позади (иначе «Завершить отправку» привязала бы папку к пустому каркасу).
    """
    with mark_busy(job.folder), jobs.active(job.id):
        after_upload = job if job.stage not in _BEFORE_UPLOAD else None
        try:
            if job.stage == STAGE_BOUND:
                result = _finish(client, job, PublishResult(job.project_url, job.revision, 0), FilterRules())
                log.info("Отправка %s была завершена до сбоя (%s)", job.folder, result.project_url)
                return None
            if job.stage in (STAGE_STARTED, STAGE_SOURCE):   # до сервера дело не дошло
                _remove_work_dir(job)
                _drop_job(job)
                return None
            _restore_svn(job, retry_delay, client)
            _remove_work_dir(job)
            if job.stage == STAGE_UPLOADING:
                outcome = upload_outcome(client, job.project_url, job.trunk_url, job.author, job.layout_revision)
                if outcome == OUTCOME_RETRY:
                    raise PublishError(FAILED, "recover", RECOVER_RETRY_TEXT)
                if outcome == OUTCOME_UNKNOWN:
                    raise PublishError(FAILED, "recover", unknown_owner_text(job.project_url) + " Папку на "
                                       "компьютере можно вернуть к прежнему виду кнопкой «Убрать недоделанное».")
                if outcome == OUTCOME_CLEAR:
                    _drop_job(job)
                    return None
        except SvnError as exc:
            raise PublishError(_svn_code(exc), "recover", error=exc, job=after_upload) from exc
        except OSError as exc:
            raise PublishError(SVN_BUSY, "recover", error=exc, job=after_upload) from exc
        job.stage = STAGE_UPLOADED
        _save_job(job)
        return job


def upload_outcome(client: SvnClient, project_url: str, trunk_url: str, author: str,
                   layout_revision: Optional[int] = None) -> str:
    """Что на сервере после прерванной загрузки (OUTCOME_*). Только каркас, созданный нами, откатывается здесь же.

    Есть номер правки каркаса (layout_revision): после неё у проекта есть правки - загрузка дошла, нет - на сервере
    только каркас. Номера нет (журнал прежней версии или сбой до его записи): каркас наш, если все правки проекта от
    author; author неизвестен - OUTCOME_UNKNOWN, сервер не трогаем. Бросает SvnError (нет связи).
    """
    if not client.url_exists(project_url):
        return OUTCOME_CLEAR
    entries = client.last_log_entry(project_url, limit=2)
    if not entries:
        log.warning("Журнал проекта %s на сервере пуст: не трогаю", project_url)
        return OUTCOME_CLEAR
    newest = entries[0]
    if layout_revision is not None:
        if newest.revision > layout_revision:          # после каркаса есть правки: загрузка дошла
            return OUTCOME_LANDED if client.url_exists(trunk_url) else OUTCOME_CLEAR
        if newest.revision < layout_revision:
            log.warning("Проект %s пересоздан после правки %s: не наш, не трогаю", project_url, layout_revision)
            return OUTCOME_CLEAR
        return _rollback_scaffold(client, project_url, trunk_url, newest)
    if not author:
        log.warning("Автор проекта %s неизвестен, номера правки нет: проект на сервере не откатываю", project_url)
        return OUTCOME_UNKNOWN
    if any(e.author != author for e in entries):
        log.warning("Проект %s создан не нами (%s): не трогаю", project_url, [e.author for e in entries])
        return OUTCOME_CLEAR
    if len(entries) >= 2:          # папки и загрузка: загрузка дошла
        return OUTCOME_LANDED if client.url_exists(trunk_url) else OUTCOME_CLEAR
    return _rollback_scaffold(client, project_url, trunk_url, newest)


def _rollback_scaffold(client: SvnClient, project_url: str, trunk_url: str, entry) -> str:
    """На сервере одна наша правка - три папки: откатываем. Не вышло - OUTCOME_RETRY (журнал остаётся)."""
    if client.list_entries(trunk_url + "@"):
        log.warning("Проект %s: одна правка, но с файлами - не наша загрузка, не трогаю", project_url)
        return OUTCOME_CLEAR
    ok, reason = importer.rollback(client, project_url, entry.revision, entry.author)
    if not ok:
        log.warning("Недоделанный проект %s не удалён: %s; повторю при следующем запуске", project_url, reason)
        return OUTCOME_RETRY
    return OUTCOME_CLEAR


def discard_job(job: PublishJob, retry_delay: float = RETRY_DELAY, client: Optional[SvnClient] = None) -> None:
    """«Убрать недоделанное» без сервера: папка к исходному виду, временная копия и журнал удаляются.
    Проект, уже загруженный на сервер, остаётся там. client (если есть) только читает .svn папки: уже
    привязанную папку не трогаем."""
    with mark_busy(job.folder):
        try:
            if job.stage != STAGE_BOUND:
                _restore_svn(job, retry_delay, client)
            elif os.path.lexists(job.svn_old):
                fsutil.remove_partial_dir(job.svn_old, os.path.dirname(job.folder))
        except OSError as exc:
            raise PublishError(SVN_BUSY, "recover", error=exc, job=job) from exc
        _remove_work_dir(job)
        _drop_job(job)
