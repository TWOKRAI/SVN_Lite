"""Ядро «Нового проекта» без tkinter: сборка проекта из шаблона, загрузка на сервер, журнал и разбор остатков
(Tasks T.3, T.5 из docs/PLAN_templates.md; вынесено из окна в Task AG.0c из docs/PLAN_agent_access.md).

Путь «На компьютере». Сборка идёт в скрытой соседней папке `<папка>\\.<имя>.creating`, в конце она переименовывается в итоговое имя:
итоговая папка либо готова целиком, либо её нет. О каждой сборке пишется журнал `data_dir()/create_jobs/<id>.json`
(`jobs.py`); при запуске программы `recover_create_jobs` убирает остатки прерванной сборки (закрыли окно, убили
процесс), не трогая задачи, которые ещё идут.
Если найден svnadmin.exe, готовая папка получает историю на этом компьютере (`localrepo.create_local_repo`, Task T.6);
не вышло или программы нет - проект остаётся без истории, это не ошибка.

Путь «На общем сервере»: сборка в той же скрытой папке `.<имя>.creating`, загрузка (`importer.create_project`,
с откатом при сбое), затем обычное скачивание в итоговую папку. Если на создание проектов в этом месте сервера
нет прав или пропала связь, проект остаётся на компьютере (как в первом пути) и об этом говорится по-русски.
Откат каркаса на сервере не удался - журнал остаётся, каркас уберёт разбор при следующем запуске.

Модуль не импортирует tkinter и svnhelper.gui.*. Окно (`gui/new_project_dialog.py`) берёт отсюда всё нужное.
Тексты без терминов SVN.
"""
from __future__ import annotations

import dataclasses
import functools
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote

from . import checks, fsutil, importer, jobs, localrepo, messages, projectmeta, publish, server, templates
from .svn_core import CancelToken, CheckoutProgress, ErrorKind, SvnError

log = logging.getLogger("svnhelper.new_project")

ACTION = "Создание проекта из шаблона"
ACTION_PUBLISH = "Отправка проекта на сервер"
JOB_KIND = "create_local"
CREATING_SUFFIX = ".creating"
STAGE_BUILDING = "building"
STAGE_UPLOADING = "uploading"      # идёт загрузка на сервер: на сервере может быть недоделанный проект
STAGE_DOWNLOAD = "download"        # проект на сервере, идёт скачивание в итоговую папку
STAGE_DONE = "done"

STORAGE_SERVER = "server"
STORAGE_LOCAL = "local"

NOT_CONFIGURED = "Шаблоны не настроены, обратитесь к администратору"
NO_LINK_TEXT = "Нет связи с сервером."
NO_LINK_SERVER_HINT = "Нет связи с сервером. Проект можно создать на компьютере и отправить на сервер позже."
EXISTS_TEXT = "Проект с таким названием уже есть в этой папке. Открыть его?"
NO_PLACE_TEXT = "Выберите место на сервере: нажмите «Изменить…»."
NO_HISTORY_TEXT = ("Проект создан, но историю на этом компьютере включить не удалось: прежние варианты файлов "
                   "сохраняться не будут. Файлы проекта в порядке.")
NO_RIGHTS_TEXT = ("Нет прав создавать проекты в этой папке сервера. Проект создан на компьютере: "
                  "отправить можно позже или попросить администратора.")
NO_LINK_KEEP_TEXT = ("Нет связи с сервером: проект создан на компьютере. Отправьте его на сервер позже: "
                     "«Отправить на сервер…».")
NO_LINK_KINDS = (ErrorKind.NO_CONNECTION, ErrorKind.TIMEOUT)     # связь пропала: собранный проект не стираем
PUBLISH_INFO = ("На сервер уйдёт последний сохранённый вариант. "
                "Прежние сохранения останутся только на этом компьютере.")
PUBLISH_COMMENT = "Проект отправлен на сервер"


# --------------------------------------------------------------------------
# Сборка проекта и журнал (вызывать из фонового потока)
# --------------------------------------------------------------------------

class CreateCancelled(Exception):
    """Сборку остановили; созданное убрано. rolled_back: что с сервером (True — возвращён как был, False — откат не
    удался, None — на сервер ещё не заходили); rollback_error — причина."""

    def __init__(self, rolled_back: Optional[bool] = None, rollback_error: str = ""):
        super().__init__("cancelled")
        self.rolled_back = rolled_back
        self.rollback_error = rollback_error


@dataclass
class CreateJob:
    id: str
    base: str            # папка, в которой собирается проект
    creating: str        # временная папка `.<имя>.creating`
    final: str           # итоговая папка
    stage: str = STAGE_BUILDING
    updated: str = ""
    kind: str = JOB_KIND
    path: str = ""       # файл журнала (в сам журнал не пишется)
    project_url: str = ""    # адрес проекта на сервере (путь «сразу на сервер»)
    author: str = ""         # логин, под которым создавались папки на сервере (для отката)
    layout_revision: Optional[int] = None    # правка, которой созданы три папки на сервере (jobs.layout_recorder)


def creating_path(base: str, folder: str) -> str:
    return os.path.join(base, f".{folder}{CREATING_SUFFIX}")


def _save_job(job: CreateJob) -> None:
    jobs.save(job)


def _drop_job(job: CreateJob) -> None:
    jobs.remove(job.path)


def _remove_creating(job: CreateJob) -> bool:
    """Убирает временную папку сборки. Только папку с именем `.<имя>.creating` внутри base. True: её больше нет."""
    if not jobs.is_child(job.creating, job.base, r"\..+" + re.escape(CREATING_SUFFIX)):
        log.warning("Журнал создания %s указывает на неожиданный путь %s: не трогаю", job.path, job.creating)
        return True
    try:
        fsutil.remove_partial_dir(job.creating, job.base)
    except (OSError, ValueError) as exc:
        log.warning("Не удалось убрать недоделанную папку %s: %s", job.creating, exc)
        return not os.path.lexists(job.creating)
    return True


def recover_create_jobs(jobs_dir: Optional[os.PathLike] = None, client=None,
                        landed: Optional[list] = None, store=None, unsure: Optional[list] = None) -> int:
    """При запуске: убирает остатки прерванных сборок и их журналы. Возвращает число убранных папок.

    Задачи, которые сейчас идут (в этом процессе или в другом живом), пропускаются (`jobs.load_jobs`). Журнал без
    pid (прежняя версия) разбирается как брошенный. Итоговая папка (после переименования) готова целиком, её не
    трогаем: журнал с ней просто закрывается. Если папку убрать не удалось (её держит другая программа), журнал
    остаётся до следующего запуска.

    «Сразу на сервер»: стадия uploading — `.creating` убирается, а что на сервере, решает `publish.upload_outcome`
    (нужен client; без него или без связи журнал остаётся до следующего запуска): на сервере только каркас — он
    откатывается (не вышло — журнал остаётся, откат повторится), загрузка дошла — адрес добавляется в landed (окно
    подскажет скачать его), чей каркас неизвестно (ни номера правки, ни логина) — адрес в unsure (окно скажет
    о нём; без unsure журнал остаётся). Стадия download — недокачанная итоговая папка удаляется, адрес проекта тоже
    попадает в landed.

    Заодно разбираются прерванные задачи локальной истории (`localrepo.recover_jobs`; store - куда записать путь к
    истории, если привязка папки успела закончиться).
    """
    removed = localrepo.recover_jobs(jobs_dir, store)
    for job in jobs.load_jobs(CreateJob, jobs_dir, JOB_KIND):
        if job.stage != STAGE_DONE and os.path.lexists(job.creating):
            log.info("Убираю недоделанный проект %s", job.creating)
            if not _remove_creating(job):
                continue
            removed += 1
        if job.stage == STAGE_UPLOADING:
            if not _settle_upload(job, client, landed, unsure):
                continue                 # решить пока нельзя: журнал остаётся до следующего запуска
        elif job.stage == STAGE_DOWNLOAD:
            _remove_unfinished_download(job)
            if landed is not None and job.project_url:
                landed.append(job.project_url)
        _drop_job(job)
    return removed


def _settle_upload(job: CreateJob, client, landed: Optional[list], unsure: Optional[list]) -> bool:
    """Что осталось на сервере после прерванной загрузки. False: решить пока нельзя (нет клиента или связи, откат
    каркаса не удался, чей каркас неизвестно и сказать об этом некому)."""
    if client is None or not job.project_url:
        return client is not None
    try:
        outcome = publish.upload_outcome(client, job.project_url, f"{job.project_url}/{server.TRUNK}", job.author,
                                         job.layout_revision)
    except SvnError as exc:
        log.warning("Прерванная загрузка %s: сервер не проверен (%s)", job.project_url, exc.kind.value)
        return False
    if outcome == publish.OUTCOME_RETRY:
        return False
    if outcome == publish.OUTCOME_UNKNOWN:
        if unsure is None:
            return False
        unsure.append(job.project_url)
    if outcome == publish.OUTCOME_LANDED and landed is not None:
        landed.append(job.project_url)
    return True


def _remove_unfinished_download(job: CreateJob) -> None:
    """Недокачанная итоговая папка: убирается, только если она пустая или это начатая копия (есть .svn)."""
    final = job.final
    if not final or not os.path.lexists(final):
        return
    try:
        if os.listdir(final) and not os.path.isdir(os.path.join(final, ".svn")):
            log.warning("В %s после прерванного скачивания лежит что-то своё: не трогаю", final)
            return
        fsutil.remove_partial_dir(final, job.base)
        log.info("Недокачанная папка %s убрана", final)
    except (OSError, ValueError):
        log.exception("Не удалось убрать недокачанную папку %s", final)


def create_project_folder(base: str, plan: templates.Plan, template_dir: Path, meta: projectmeta.ProjectMeta,
                          cancel: CancelToken, jobs_dir: Optional[os.PathLike] = None) -> str:
    """Собирает проект в `.<имя>.creating`, пишет project.json и переименовывает в итоговое имя. Возвращает путь.

    Сбой или остановка убирают только созданное здесь (CreateCancelled при остановке). Итоговая папка,
    которая уже есть, не затрагивается (FileExistsError). Пока идёт сборка, задача в реестре `jobs.active`:
    разбор журналов в это время её `.creating` не тронет.
    """
    base = os.path.normpath(base)
    final = os.path.join(base, plan.folder)
    creating = creating_path(base, plan.folder)
    _check_free(final, creating)
    job = _new_job(base, creating, final, jobs_dir)
    with jobs.active(job.id):
        _save_job(job)                       # журнал раньше папки: следа без записи не остаётся
        try:
            _raise_if_cancelled(cancel)
            try:
                templates.apply_plan(plan, template_dir, Path(creating), cancel)
            except SvnError as exc:
                if exc.kind == ErrorKind.CANCELLED:
                    raise CreateCancelled() from exc
                raise
            _raise_if_cancelled(cancel)
            projectmeta.write(creating, meta)
            _raise_if_cancelled(cancel)
            if os.path.lexists(final):
                raise FileExistsError(final)
            os.rename(creating, final)
        except BaseException:
            if _remove_creating(job):
                _drop_job(job)               # убрать не вышло: журнал остаётся, программа повторит при запуске
            raise
        job.stage = STAGE_DONE
        _save_job(job)
        _drop_job(job)
    return final


def _raise_if_cancelled(cancel: CancelToken) -> None:
    if cancel.cancelled:
        raise CreateCancelled()


@dataclass
class LocalCreateResult:
    path: str                   # готовая папка проекта
    history: bool               # включена ли история на этом компьютере
    note: str = ""              # что сказать пользователю (например, почему истории нет)


def _add_history(client, path: str, svnadmin: Optional[str], rules, store, cancel: Optional[CancelToken],
                 jobs_dir: Optional[os.PathLike]) -> Optional[bool]:
    """История на этом компьютере для готовой папки проекта. None — не включалась (нет svnadmin.exe или клиента),
    True — включена, False — не вышло (созданное для истории убрано, проект целый)."""
    if not svnadmin or client is None:
        return None
    try:
        localrepo.create_local_repo(client, path, svnadmin, rules, store, cancel=cancel, jobs_dir=jobs_dir)
    except localrepo.LocalRepoError as exc:
        log.warning("История проекта %s не включена (%s): %s", path, exc.code, exc.message, exc_info=exc.error)
        return False
    except Exception:
        log.exception("История проекта %s не включена из-за непредвиденной ошибки", path)
        return False
    return True


def create_local_project(client, base: str, plan: templates.Plan, template_dir: Path, meta: projectmeta.ProjectMeta,
                         cancel: CancelToken, jobs_dir: Optional[os.PathLike] = None, svnadmin: Optional[str] = None,
                         rules=None, store=None) -> LocalCreateResult:
    """Проект на компьютере: `create_project_folder`, затем (если есть svnadmin.exe и svn) история на этом компьютере.

    Нет svnadmin.exe - проект без истории и без ошибки. Не вышло включить историю (или остановили именно на этом
    шаге) - созданное для истории убрано, проект остаётся целым без истории (note объясняет). Вызывать из фона."""
    path = create_project_folder(base, plan, template_dir, meta, cancel, jobs_dir)
    history = _add_history(client, path, svnadmin, rules, store, cancel, jobs_dir)
    return LocalCreateResult(path, bool(history), NO_HISTORY_TEXT if history is False else "")


def _check_free(final: str, creating: str) -> None:
    if os.path.lexists(final):
        raise FileExistsError(final)
    if os.path.lexists(creating):
        raise templates.TemplateError(
            f"Рядом осталась недоделанная папка «{os.path.basename(creating)}» от прошлой попытки. "
            "Перезапустите программу: она уберёт её сама. Если не уберёт, удалите её вручную.")


def undo_text(rolled_back: Optional[bool], rollback_error: str = "") -> str:
    """Что стало с сервером после сбоя или остановки загрузки. Откат не удался — его повторит разбор при запуске."""
    if rolled_back is False:
        return publish.ROLLBACK_LATER_TEXT
    return messages.create_undo_text(rolled_back, rollback_error)


# --------------------------------------------------------------------------
# Сразу на сервер: сборка, загрузка, обычное скачивание
# --------------------------------------------------------------------------

@dataclass
class ServerCreateResult:
    path: str                   # итоговая папка проекта на компьютере
    on_server: bool             # False: прав на сервере или связи нет, проект остался на компьютере
    note: str = ""              # что сказать пользователю
    history: bool = False       # проект остался на компьютере и получил историю на этом компьютере


class ServerCreateFailed(Exception):
    """Проект на сервере не создан (откат сделан, если было что откатывать). text — по-русски, raw — для отчёта."""

    def __init__(self, text: str, kind: ErrorKind = ErrorKind.OTHER, raw: str = "",
                 rolled_back: Optional[bool] = None, rollback_error: str = ""):
        super().__init__(text)
        self.text = text
        self.kind = kind
        self.raw = raw
        self.rolled_back = rolled_back
        self.rollback_error = rollback_error

    def to_dict(self) -> dict:
        """Для ответа агенту: код причины, откат сервера и текст по-русски; сырого ответа svn нет."""
        return {"kind": self.kind.value, "rolled_back": self.rolled_back, "message": self.text}


class ServerCreatedNotDownloaded(Exception):
    """Проект уже создан на сервере (он полный и виден коллегам), но скачать его в итоговую папку не вышло."""

    def __init__(self, project_url: str, cancelled: bool, error: Optional[BaseException] = None):
        super().__init__(project_url)
        self.project_url = project_url
        self.cancelled = cancelled
        self.error = error

    def to_dict(self) -> dict:
        """Для ответа агенту: проект на сервере уже есть (откатывать нечего), скачать не вышло."""
        return {"kind": "created_not_downloaded", "rolled_back": None, "message": self.text}

    @property
    def text(self) -> str:
        where = unquote(self.project_url)
        if self.cancelled:
            return ("Скачивание остановлено. Проект уже создан на сервере: "
                    f"скачайте его: «Скачать проект» в колонке «Проекты». Место на сервере: {where}")
        return ("Проект создан на сервере, но скачать его на компьютер не удалось. "
                f"Скачайте его позже: «Скачать проект» в колонке «Проекты». Место на сервере: {where}")


def _failure_from_svn(exc: SvnError) -> ServerCreateFailed:
    return ServerCreateFailed(messages.short_message(exc.kind), exc.kind, exc.raw)


def create_server_project(client, base: str, plan: templates.Plan, template_dir: Path, meta: projectmeta.ProjectMeta,
                          parent_url: str, comment: str, rules, cancel: CancelToken,
                          progress: Optional[CheckoutProgress] = None, on_phase: Optional[Callable[[str], None]] = None,
                          jobs_dir: Optional[os.PathLike] = None, svnadmin: Optional[str] = None,
                          store=None) -> ServerCreateResult:
    """Создаёт проект сразу на сервере: сборка в `.<имя>.creating`, загрузка, обычное скачивание в итоговую папку.

    Вызывать из фонового потока. parent_url закодирован (server.build_url). Бросает: CreateCancelled (остановка,
    сервер возвращён как был), FileExistsError, templates.TemplateError, ServerCreateFailed (сервер не тронут или
    откатан), ServerCreatedNotDownloaded (проект на сервере готов, скачать не вышло), SvnError AUTH (нужен вход:
    окно запросит пароль и повторит; к повтору папки сборки уже нет).
    Нет прав на создание или нет связи с сервером: проект остаётся на компьютере (результат on_server=False), с
    историей на этом компьютере, если есть svnadmin (как в пути «на компьютере»). Откат каркаса на сервере не удался
    (при любом исходе): журнал остаётся на стадии uploading, каркас уберёт разбор при следующем запуске.
    """
    progress = progress or CheckoutProgress()
    phase = on_phase or (lambda _name: None)
    base = os.path.normpath(base)
    final = os.path.join(base, plan.folder)
    creating = creating_path(base, plan.folder)
    _check_free(final, creating)
    try:
        name = server.validate_line_name(plan.folder)
    except ValueError as exc:
        raise templates.TemplateError(str(exc)) from exc
    project_url = server.build_url(parent_url, [name])
    credentials = getattr(client, "credentials", None)
    job = _new_job(base, creating, final, jobs_dir, project_url=project_url,
                   author=credentials.username if credentials else "")
    add_history = functools.partial(_add_history, client, svnadmin=svnadmin, rules=rules, store=store,
                                    cancel=cancel, jobs_dir=jobs_dir)
    with jobs.active(job.id):
        _save_job(job)                       # журнал раньше папки: следа без записи не остаётся
        kept = _build_and_upload(client, job, plan, template_dir, meta, parent_url, comment, rules, cancel, progress,
                                 phase, add_history)
        if kept is not None:
            return kept
        _remove_creating(job)                # загруженное лежит на сервере: сборка больше не нужна
        job.stage = STAGE_DOWNLOAD           # журнал живёт до конца скачивания: убитое скачивание программа уберёт сама
        _save_job(job)
        return _download_and_close(client, job, progress, cancel, phase)


def _build_and_upload(client, job: CreateJob, plan: templates.Plan, template_dir: Path, meta: projectmeta.ProjectMeta,
                      parent_url: str, comment: str, rules, cancel: CancelToken, progress: CheckoutProgress,
                      phase: Callable[[str], None], add_history: Callable) -> Optional[ServerCreateResult]:
    """Сборка и загрузка. None — проект на сервере, дальше скачивание; ServerCreateResult — проект остался на
    компьютере (нет прав или связи). Сбой: `.creating` убирается; журнал закрывается, если на сервере не остался
    неоткаченный каркас."""
    scaffold_left = False                # откат каркаса не удался: журнал остаётся на стадии uploading
    try:
        phase("build")
        _raise_if_cancelled(cancel)
        try:
            templates.apply_plan(plan, template_dir, Path(job.creating), cancel)
            _raise_if_cancelled(cancel)
            if not client.url_exists(parent_url):          # create_project создал бы недостающие папки сам
                raise ServerCreateFailed(publish.MESSAGES[publish.NO_PARENT])
        except SvnError as exc:
            if exc.kind == ErrorKind.CANCELLED:
                raise CreateCancelled() from exc
            if exc.kind == ErrorKind.AUTH:
                raise
            if exc.kind in NO_LINK_KINDS:                   # на сервер ещё не заходили: собранное оставляем у себя
                return _keep_local(job, meta, add_history, NO_LINK_KEEP_TEXT)
            raise _failure_from_svn(exc) from exc
        on_server_meta = dataclasses.replace(meta, svn={"url": job.project_url, "published": projectmeta.now_iso()})
        projectmeta.write(job.creating, on_server_meta)
        phase("upload")
        progress.count, progress.last_path = 0, ""
        job.stage = STAGE_UPLOADING          # с этой минуты на сервере может появиться недоделанный проект
        _save_job(job)
        try:
            importer.create_project(client, job.project_url, job.creating, comment,
                                    importer.svn_ignores(importer.name_patterns(rules)), progress, cancel,
                                    on_layout=jobs.layout_recorder(job))
        except importer.CreateError as exc:
            scaffold_left = exc.rolled_back is False
            if scaffold_left:
                log.warning("Каркас проекта %s не откачен (%s): журнал %s остаётся до следующего запуска",
                            job.project_url, exc.rollback_error, job.path)
            if exc.kind == ErrorKind.CANCELLED:
                raise CreateCancelled(exc.rolled_back, exc.rollback_error) from exc
            # вход нужен до того, как на сервере что-то появилось: после ввода пароля начинаем заново
            if exc.kind == ErrorKind.AUTH and exc.stage in ("check", "mkdir"):
                raise exc.error from exc
            if exc.kind == ErrorKind.AUTHZ:
                return _keep_local(job, meta, add_history, NO_RIGHTS_TEXT, scaffold_left)
            if exc.kind in NO_LINK_KINDS:
                return _keep_local(job, meta, add_history, NO_LINK_KEEP_TEXT, scaffold_left)
            text = messages.short_message(exc.kind)
            if exc.kind == ErrorKind.EXISTS:
                text = publish.MESSAGES[publish.EXISTS]
            raise ServerCreateFailed((text + " " + undo_text(exc.rolled_back, exc.rollback_error)).strip(),
                                     exc.kind, exc.error.raw, exc.rolled_back, exc.rollback_error) from exc
        log.info("Проект %s создан на сервере из шаблона", job.project_url)
    except BaseException:
        if _remove_creating(job) and not scaffold_left:
            _drop_job(job)               # убрать не вышло: журнал остаётся, программа повторит при запуске
        raise
    return None


def _new_job(base: str, creating: str, final: str, jobs_dir: Optional[os.PathLike], **fields) -> CreateJob:
    job_id = jobs.new_id()
    return CreateJob(job_id, base, creating, final, path=str(jobs.job_file(jobs_dir, job_id)), **fields)


def download_project(client, project_url: str, final: str, base: str, cancel: CancelToken,
                     progress: Optional[CheckoutProgress] = None, on_phase: Optional[Callable[[str], None]] = None,
                     jobs_dir: Optional[os.PathLike] = None) -> ServerCreateResult:
    """Только скачивание готового проекта в итоговую папку («Скачать ещё раз»). Пишет свой журнал."""
    job = _new_job(base, creating_path(base, os.path.basename(final)), final, jobs_dir, project_url=project_url,
                   stage=STAGE_DOWNLOAD)
    with jobs.active(job.id):
        _save_job(job)
        return _download_and_close(client, job, progress or CheckoutProgress(), cancel, on_phase or (lambda _p: None))


def _download_and_close(client, job: CreateJob, progress: CheckoutProgress, cancel: CancelToken,
                        phase: Callable[[str], None]) -> ServerCreateResult:
    """Скачивание под отметкой «папка занята»; журнал закрывается, только если процесс дожил до конца."""
    try:
        with publish.mark_busy(job.final):
            return _download(client, job.project_url, job.final, job.base, progress, cancel, phase)
    finally:
        _drop_job(job)


def _keep_local(job: CreateJob, meta: projectmeta.ProjectMeta, add_history: Callable, reason: str,
                scaffold_left: bool = False) -> ServerCreateResult:
    """Нет прав или связи: сборка становится обычным проектом на компьютере (с историей, если есть svnadmin).

    scaffold_left: на сервере остался неоткаченный каркас — журнал не закрывается (стадия uploading), каркас уберёт
    разбор при следующем запуске; итоговую папку этот разбор не трогает."""
    projectmeta.write(job.creating, meta)            # без адреса на сервере
    if os.path.lexists(job.final):
        raise FileExistsError(job.final)
    os.rename(job.creating, job.final)
    if not scaffold_left:
        job.stage = STAGE_DONE
        _save_job(job)
        _drop_job(job)
    log.info("Проект %s остался на компьютере: %s", job.final, reason)
    history = add_history(job.final)
    notes = [reason]
    if scaffold_left:
        notes.append(publish.ROLLBACK_LATER_TEXT)
    if history is False:
        notes.append(NO_HISTORY_TEXT)
    return ServerCreateResult(job.final, on_server=False, note=" ".join(notes), history=bool(history))


def _download(client, project_url: str, final: str, base: str, progress: CheckoutProgress, cancel: CancelToken,
              phase: Callable[[str], None]) -> ServerCreateResult:
    """Обычное скачивание готового проекта в итоговую папку. Сбой: частично скачанное убирается, проект остаётся
    на сервере."""
    phase("download")
    progress.count, progress.last_path = 0, ""
    if os.path.lexists(final):             # папка появилась, пока шла загрузка: чужое не трогаем и не затираем
        raise ServerCreatedNotDownloaded(project_url, False, FileExistsError(final))
    try:
        if cancel.cancelled:
            raise SvnError(ErrorKind.CANCELLED)
        client.checkout(f"{project_url}/{server.TRUNK}", final, progress, cancel)
    except SvnError as exc:
        _remove_partial(final, base)       # папки до скачивания не было: всё, что в ней есть, принесли мы
        raise ServerCreatedNotDownloaded(project_url, exc.kind == ErrorKind.CANCELLED, exc) from exc
    except OSError as exc:
        _remove_partial(final, base)
        raise ServerCreatedNotDownloaded(project_url, False, exc) from exc
    return ServerCreateResult(final, on_server=True)


def _remove_partial(path: str, base: str) -> None:
    try:
        if os.path.lexists(path):
            fsutil.remove_partial_dir(path, base)
    except (OSError, ValueError):
        log.exception("Не удалось убрать недокачанную папку %s", path)


def publish_summary(result: publish.PublishResult) -> str:
    """Что сказать после отправки: готово и (если есть) что требует внимания. Без терминов svn."""
    lines = ["Проект отправлен на сервер и связан с папкой на компьютере.",
             f"Место на сервере: {unquote(result.project_url)}"]

    def names(items: list[str]) -> str:
        shown = ", ".join(os.path.basename(p) or p for p in items[:5])
        return shown + (f" и ещё {len(items) - 5}" if len(items) > 5 else "")

    if result.changed:
        lines.append("Эти файлы в папке отличаются от отправленных на сервер: " + names(result.changed)
                     + ". Проверьте их на вкладке «Мои проекты».")
    if result.not_uploaded:
        lines.append("Остались только на компьютере (на сервер не отправлялись): " + names(result.not_uploaded) + ".")
    if result.leftover:
        lines.append(f"Служебную папку {result.leftover} убрать не удалось: удалите её вручную, когда папку "
                     "проекта ничто не держит.")
    return "\n\n".join(lines)


def check_base_dir(base: str) -> Optional[str]:
    """Текст ошибки, если в этой папке проект создавать нельзя (сетевой диск, нет папки), иначе None."""
    base = base.strip().strip('"')
    if not base:
        return "Выберите папку, куда создать проект."
    if base.startswith(("\\\\", "//")):
        return checks.NETWORK_MESSAGE
    if not os.path.splitdrive(base)[0]:
        return "Укажите полный путь к папке, начиная с буквы диска, например C:\\Проекты."
    base = os.path.normpath(base)
    dtype = checks.drive_type(base)
    if dtype == checks.DRIVE_REMOTE:
        return checks.NETWORK_MESSAGE
    if dtype == checks.DRIVE_NO_ROOT_DIR:
        return f"Диск {base[:2]} не найден. Выберите другую папку."
    if not os.path.isdir(base):
        return "Папка не найдена. Выберите другую папку."
    return None


@dataclass
class LoadedTemplate:
    info: templates.TemplateInfo
    fetched: templates.FetchedTemplate
    manifest: templates.Manifest
    paths: list


def load_template(client, info: templates.TemplateInfo, cache_dir: Optional[Path], cancel: CancelToken,
                  repos: list, folder_format: str) -> LoadedTemplate:
    """Копия шаблона на компьютере (из кэша, если связи нет) и его проверенный манифест."""
    if client is None:
        fetched = templates.cached_template(cache_dir or templates.default_cache_dir(), info.id)
        if fetched is None:
            raise templates.TemplateError(templates.NO_CACHE_MESSAGE)
        fetched.offline = True
    else:
        fetched = templates.fetch_template(client, info, cache_dir, cancel)
    manifest = templates.load_manifest(fetched.path, repos, folder_format, info.title)
    return LoadedTemplate(info, fetched, manifest, templates.list_template_paths(fetched.path))
