"""Главное окно: шапка, меню, фоновые задачи, вход, тема. Вкладки подмешаны из соседних модулей."""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import webbrowser
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .. import (__version__, autostart, docs, installer, messages, publish, selfupdate, server, templates, theme, tortoise,
                tray, winutil)
from ..config import (app_dir, apply_notify, build_config, Config, DEFAULT_SHARED_CONFIG, effective_credentials,
                      fetch_shared, find_svn, is_newer_version, load_shared_cache, parse_mirrors_text, protect_text,
                      READ_WAIT_WINDOW, save_shared_cache, Settings, SettingsStore, SettingsUnreadable, setup_logging,
                      SharedConfigError)
from ..notifier import discover_projects
from ..dialogs import AboutDialog, center_window, ErrorDialog, FONT_FAMILY
from ..svn_core import CancelToken, CheckoutProgress, ErrorKind, SvnClient, SvnError
from .models import (CLOSE_POLL_MS, CLOSE_WAIT_SEC, DownloadJob, JobStamp, log, MineItem, Node, POLL_MS,
                     REFRESH_SEC, UpdateJob)
from .widgets import ButtonRow, Lamp, SettingsDialog, SideButtons, Tooltip
from .login import connection_error_text, LoginCoordinator
from .download_tab import DownloadTabMixin
from .projects_tab import ProjectsTabMixin
from .lines_tab import LinesTabMixin
from .new_project_dialog import auth_error, publish_summary, recover_create_jobs, report_publish_error
from .notify_ui import NotifyMixin
from .agent_dialog import AgentDialog
from .project_panel import TabLoads


# через сколько миллисекунд после открытия окна начинаются фоновые задачи запуска
STARTUP_MESSAGES_MS = 300
RECOVER_NEW_PROJECTS_MS = 500       # разбор журналов создания проектов и локальной истории
RECOVER_PUBLISH_MS = 900            # разбор незаконченных отправок
WARM_TEMPLATES_MS = 2500            # шаблоны в кэш


def enable_dpi_awareness() -> None:
    """Должно вызываться до создания Tk."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class App(DownloadTabMixin, ProjectsTabMixin, LinesTabMixin, NotifyMixin):
    def __init__(self, root: tk.Tk, config: Config, settings_store: SettingsStore,
                 client: Optional[SvnClient], log_path: str, startup_jobs: bool = True):
        """startup_jobs: разбирать журналы прерванных задач и готовить шаблоны при запуске (тесты отключают, чтобы
        разбор не шёл одновременно с их собственными задачами)."""
        self.root = root
        self.config = config
        self.settings_store = settings_store
        self.settings = settings_store.settings
        self.client = client
        self.log_path = str(log_path)

        self.project: Optional[server.ProjectRef] = None
        self.line: server.Line = server.TRUNK_LINE          # что выбрано в «Что скачать»
        self._line_choices: list[server.Line] = [server.TRUNK_LINE]
        self._lines_cache: dict[str, dict] = {}            # проект → {"branches", "tags", ...}, на сеанс
        self._lines_loading: set[str] = set()
        self._lines_error = ""                             # почему список веток не загрузился
        self._lines_after: Optional[str] = None
        self._tree_pick: Optional[tuple] = None            # (текст ссылки, ProjectRef) проекта, выбранного в дереве
        self._mirror_note: Optional[tuple] = None          # (текст ссылки, пояснение): проект найден по пути на диске
        self._create_target: Optional[tuple] = None        # (путь, адрес сервера, сегменты): проекта ещё нет
        self._create_dialog = None                         # открытое окно «Новый проект из папки»
        self._new_project_dialog = None                    # открытое окно «Новый проект» (из шаблона)
        self._publish_dialog = None                        # открытое окно «Отправить на сервер»
        self._extra_trees: set = set()                     # ServerTree в открытых окнах: перекрашиваются со сменой темы
        self._created_notes: list[str] = []                # проекты, созданные на сервере, но не скачанные (после сбоя)
        self._publish_pending: list[list] = []             # незаконченные отправки: [PublishJob, разобрана ли при запуске]
        self._publish_busy = False                         # идёт «Завершить отправку» или «Убрать недоделанное»
        self._publish_cancel: Optional[CancelToken] = None
        self._pending_select = ""                          # проект (normcase пути), который выделить после поиска проектов
        self._local_gen = 0                                # номер проверки пути на диске; старые ответы отбрасываются
        self._local_after: Optional[str] = None
        self._info_gen = 0                                 # номер запроса «Создал… Работали…»; старые ответы отбрасываются
        self._info_text = ""
        self._dest_gen = 0
        self.shared_data: Optional[dict] = None
        self.upd: Optional[UpdateJob] = None
        self.mine: dict[str, MineItem] = {}
        self._mine_dirty = True
        self._start_tab_chosen = False             # первый поиск проектов уже решил, с какой вкладки начинать
        self._offline = False                      # последняя проверка не достучалась до сервера (полоса «Нет связи»)
        self._auto_selected = ""                   # строка, выделенная программой после поиска проектов
        self._conn_checking = False                # идёт «Проверить подключение» по щелчку на индикаторе
        self._auto_auth_failed = False             # автообновление остановлено: вход на сервер отменили
        self._mine_scanned = False                 # список скачанных папок хотя бы раз прочитан
        self._scan_gen = 0                         # номер поиска проектов; ответы прежних поисков отбрасываются
        # колонка 3: вкладки выбранного проекта
        self._tab_loads = TabLoads()               # что из вкладок Новое/История уже прочитано; смена проекта отменяет
        self._panel_key = ""                       # проект (normcase пути), для которого показана колонка 3
        self._mine_hint_text = messages.PANEL_NO_PROJECT   # что сказать в шапке, пока проект не выбран
        self._commit_item: Optional[MineItem] = None       # проект, чьи правки показаны в панели «Изменения»
        self._shown_changes: Optional[tuple] = None        # что сейчас в панели «Изменения» (чтобы не сбрасывать галочки)
        self._shown_locks: Optional[tuple] = None          # что сейчас на вкладке «Занятые»
        # вкладка «Ветки» колонки 3
        self._lines_path: Optional[str] = None     # проект, для которого прочитан список линий (normcase)
        self._lines_stale = False                  # список линий проекта надо прочитать, когда вкладка будет на виду
        self._lines_cancel: Optional[CancelToken] = None   # чтение списка линий; смена проекта его обрывает
        self._lines_gen = 0                        # номер запроса списка линий; старые ответы отбрасываются
        self._lines_view_loading = False
        self._lines_view_error = ""
        self._lines_pending_select: Optional[server.Line] = None   # какую линию выбрать после перечитывания списка
        self._lines_scroll_pending = False
        self._diff_gen = 0                         # номер запроса «Отличия»; старые ответы отбрасываются
        self._diff_cancel: Optional[CancelToken] = None   # чтение отличий; выбор другой ветки его обрывает
        self._diff_url: Optional[str] = None       # адрес ветки, для которой показаны (или читаются) отличия
        self._diff_loading = False
        self._line_rows: dict[str, tuple] = {}     # строка таблицы → (линия, запись списка или None)
        self._line_history: dict = {}              # адрес линии → BranchHistory или False (не вышло); на сеанс
        self._line_locks: dict = {}                # адрес ветки → занятые файлы (по требованию)
        self._history_loading: set[str] = set()
        self._locks_loading: set[str] = set()
        self._checking = False
        self._check_batch: list[MineItem] = []     # что проверяется сейчас
        self._check_queue: list[MineItem] = []     # что ждёт окончания текущей проверки
        self._closing = False
        self._close_deadline = 0.0
        self._tray: Optional[tray.Tray] = None     # значок в трее (создаёт main; None - окно закрывается по-настоящему)
        # папки, которые сейчас заняты внешней программой или починкой (окно TortoiseSVN, cleanup):
        # пока путь здесь, Update, Get/Release lock и повторный запуск для него запрещены
        self._tortoise_open: set[str] = set()
        self._locking = False                      # идёт занятие/освобождение файлов
        self._committing = False                   # идёт сохранение на сервер
        self._action_notes: dict[str, str] = {}    # путь проекта -> сообщение о результате последнего действия
        self._mine_spinning = False                # полоса прогресса вкладки «Мои проекты» сейчас крутится
        self._removing_partial = False             # идёт удаление недокачанной папки
        self.error_open = False                    # открыто окно ошибки (поверх него окно входа не показываем)
        self._pending_errors: list[tuple] = []     # ошибки, дожидающиеся закрытия окна входа или другой ошибки
        self._login_confirmed: Optional[tuple] = None   # (клиент, поколение данных), с которыми сервер ответил успешно
        self.tortoise_exe: Optional[str] = self._find_tortoise()
        self._tools_checked = False                # svn.exe и TortoiseSVN уже проверены при запуске
        self._pending_rebuild = False              # настройки изменились во время задачи: применим после неё
        self._partial_job: Optional[DownloadJob] = None   # недокачанная папка, которую можно удалить
        self._clip_used = ""                       # что из буфера обмена уже подставляли в поле ссылки
        self._cache_lock = threading.Lock()
        self._cache_save_lock = threading.Lock()
        self.tree_cache: dict = {}
        self._search_gen = 0
        self._search_after: Optional[str] = None
        self._search_hits: dict[str, list[str]] = {}
        self._load_cache()
        self.tree_visible = False
        self.job: Optional[DownloadJob] = None
        self._queue: queue.Queue = queue.Queue()
        self._probe_pool = ThreadPoolExecutor(max_workers=server.PROBE_THREADS,
                                              thread_name_prefix="probe")
        self.login = LoginCoordinator(self)
        self._apply_store_auth()

        self._setup_style()
        self._build_ui()
        self._fill_roots()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.report_callback_exception = self._on_tk_exception
        root.after(POLL_MS, self._poll)
        root.after(STARTUP_MESSAGES_MS, self._show_startup_messages)
        if startup_jobs:
            root.after(RECOVER_NEW_PROJECTS_MS, self._recover_new_projects)
            root.after(RECOVER_PUBLISH_MS, self._recover_publish_jobs)
            root.after(WARM_TEMPLATES_MS, self._warm_templates)
        root.after(REFRESH_SEC * 1000, self._auto_refresh_tick)
        self._notice_setup()

    # -- инфраструктура ------------------------------------------------------

    def post(self, fn: Callable, *args) -> None:
        """Передать вызов в главный поток (безопасно из любого потока)."""
        self._queue.put((fn, args))

    def _poll(self) -> None:
        self.root.after(POLL_MS, self._poll)  # планируем заранее: вложенные диалоги не остановят опрос
        while True:
            try:
                fn, args = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:
                log.exception("Ошибка в обработчике результата")

    def run_bg(self, work: Callable, done: Callable) -> None:
        """Выполняет work() в потоке; done(result, error, stamp) вызывается в главном потоке."""
        gen = self.login.stamp()

        def runner() -> None:
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            self.post(done, result, error, gen)

        threading.Thread(target=runner, daemon=True).start()

    def _recover_new_projects(self) -> None:
        """При запуске убирает остатки прерванного создания проекта из шаблона (папки `.<имя>.creating`)."""
        client = self.client

        def work() -> tuple[list, list]:
            landed: list = []
            unsure: list = []
            recover_create_jobs(client=client, landed=landed, store=self.settings_store, unsure=unsure)
            return landed, unsure

        def done(res, err, _stamp) -> None:
            if err is not None:
                log.error("Не удалось убрать остатки создания проектов", exc_info=err)
                return
            landed, unsure = res
            for url in landed:              # проект уже на сервере, а скачать его программа не успела
                name = unquote(url).rstrip("/").rsplit("/", 1)[-1]
                self._created_notes.append(f"Проект «{name}» создан на сервере, но не скачан — скачайте на вкладке "
                                           "«Скачать проект».")
            for url in unsure:              # каркас на сервере, но чей - по журналу не понять
                self._created_notes.append(publish.unknown_owner_text(url))
            self._update_publish_banner()
        self.run_bg(work, done)

    # -- незаконченная отправка проекта на сервер ---------------------------------------------

    def _recover_publish_jobs(self) -> None:
        """При запуске: разбирает незаконченные отправки (publish.recover_job). Папка возвращается к прежнему виду;
        то, что уже лежит на сервере, можно завершить кнопкой «Завершить отправку»."""
        client = self.client

        def work() -> list[list]:
            pending: list[list] = []
            for job in publish.pending_jobs():
                if client is None:
                    pending.append([job, False])
                    continue
                try:
                    resumable = publish.recover_job(client, job)
                except publish.PublishError as exc:
                    log.warning("Отправка %s не разобрана при запуске (%s): %s", job.folder, exc.code, exc.message)
                    pending.append([job, False])
                    continue
                if resumable is not None:
                    pending.append([resumable, True])
            return pending

        def done(res, err, _stamp) -> None:
            if err is not None:
                log.error("Не удалось разобрать незаконченные отправки проектов", exc_info=err)
                return
            self._publish_pending = res
            self._update_publish_banner()

        self.run_bg(work, done)

    def _update_publish_banner(self) -> None:
        pending = self._publish_pending
        if not pending and self._created_notes:       # справка без кнопок выбора: «Понятно» её убирает
            self.publish_warn_var.set("\n".join(self._created_notes))
            self.publish_finish_btn.pack_forget()
            self.publish_discard_btn.pack_forget()
            self.publish_note_btn.pack(side="right", padx=(8, 0))
            self.publish_warn.grid()
            return
        self.publish_note_btn.pack_forget()
        if not self.publish_discard_btn.winfo_manager():
            self.publish_discard_btn.pack(side="right", padx=(8, 0))
            self.publish_finish_btn.pack(side="right", padx=(8, 0))
        if not pending:
            self.publish_warn.grid_remove()
            return
        name = os.path.basename(os.path.normpath(pending[0][0].folder))
        more = f" (и ещё {len(pending) - 1})" if len(pending) > 1 else ""
        self.publish_warn_var.set(f"Не закончена отправка проекта «{name}» на сервер{more}. "
                                  "Завершите её или уберите недоделанное.")
        state = "disabled" if self._publish_busy else "normal"
        self.publish_finish_btn.configure(state=state)
        self.publish_discard_btn.configure(state=state)
        self.publish_warn.grid()

    def _on_dismiss_created_notes(self) -> None:
        self._created_notes = []
        self._update_publish_banner()

    def _set_publish_busy(self, busy: bool, status: str = "") -> None:
        self._publish_busy = busy
        if status:
            self.mine_status.set(status)
        self._update_publish_banner()

    def _on_finish_publish(self) -> None:
        """«Завершить отправку»: проект уже на сервере, папку остаётся привязать (publish.resume_publish)."""
        if not self._publish_pending or self._publish_busy or not self.client:
            return
        job, recovered = self._publish_pending[0]
        if self._path_busy(job.folder):
            messagebox.showinfo("Папка занята", publish.MESSAGES[publish.SVN_BUSY], parent=self.root)
            return
        client, rules = self.client, self.config.filter
        self._publish_cancel = cancel = CancelToken()
        progress = CheckoutProgress()
        self._set_publish_busy(True, "Завершаю отправку проекта…")

        def work() -> Optional[publish.PublishResult]:
            current = job
            if not recovered:                       # при запуске связи не было: сначала разбор, затем привязка
                current = publish.recover_job(client, job)
                if current is None:
                    return None
            return publish.resume_publish(client, current, rules, progress, cancel)

        def done(res, err, stamp) -> None:
            self._set_publish_busy(False)
            auth = auth_error(err)
            if auth is not None and not self._closing:
                self.login.request(retry=self._on_finish_publish, give_up=lambda: self._publish_declined(),
                                   job_gen=stamp)
                return
            if err is not None:
                self._publish_failed(job, err)
                return
            self._publish_resolved(job, publish_summary(res) if res is not None
                                   else "Отправка этого проекта уже была закончена, делать ничего не нужно.")

        self.run_bg(work, done)

    def _on_discard_publish(self) -> None:
        """«Убрать недоделанное»: папка к прежнему виду, временные файлы и журнал удаляются (publish.discard_job).
        Проект, уже загруженный на сервер, остаётся там."""
        if not self._publish_pending or self._publish_busy:
            return
        job = self._publish_pending[0][0]
        name = os.path.basename(os.path.normpath(job.folder))
        if self._path_busy(job.folder):
            messagebox.showinfo("Папка занята", publish.MESSAGES[publish.SVN_BUSY], parent=self.root)
            return
        if not messagebox.askyesno(
                "Убрать недоделанное?",
                f"Папка проекта «{name}» вернётся к прежнему виду, временные файлы отправки будут удалены. "
                "Если проект уже загружен на сервер, он там останется.\n\nУбрать недоделанное?", parent=self.root):
            return
        client = self.client
        self._set_publish_busy(True, "Убираю недоделанное…")

        def done(res, err, _stamp) -> None:
            self._set_publish_busy(False)
            if err is not None:
                self._publish_failed(job, err)
                return
            self._publish_resolved(job, f"Недоделанная отправка проекта «{name}» убрана. Папка на компьютере "
                                        "осталась как была.")

        self.run_bg(lambda: publish.discard_job(job, client=client), done)

    def _publish_declined(self) -> None:
        self.mine_status.set("Отправка не завершена: нужен вход на сервер")

    def _publish_failed(self, job: publish.PublishJob, error: BaseException) -> None:
        if isinstance(error, publish.PublishError):
            report_publish_error(self, error, job.folder, self.root)
            if error.job is not None:                   # привязка не вышла, но проект на сервере: можно повторить
                for entry in self._publish_pending:
                    if entry[0].id == job.id:
                        entry[0], entry[1] = error.job, True
            return
        log.error("Непредвиденная ошибка при разборе отправки %s", job.folder, exc_info=error)
        self.show_error("Проект не отправлен", "Произошла непредвиденная ошибка. Подробности записаны в журнал "
                        "программы.", action="Отправка проекта на сервер", raw=str(error))

    def _publish_resolved(self, job: publish.PublishJob, text: str) -> None:
        self._publish_pending = [entry for entry in self._publish_pending if entry[0].id != job.id]
        self._update_publish_banner()
        self._mine_dirty = True
        self._scan_projects()
        if not self._closing:
            messagebox.showinfo("Готово", text, parent=self.root)

    def _warm_templates(self) -> None:
        """Скачивает шаблоны проектов на компьютер заранее, чтобы без связи проект создавался из кэша."""
        root_url = self.config.templates_root
        if not root_url.strip() or self.client is None:
            return
        client = self.client

        def done(res, err, _stamp) -> None:
            if err is not None:
                log.error("Не удалось подготовить шаблоны проектов", exc_info=err)

        self.run_bg(lambda: templates.warm_cache(client, root_url), done)

    def _on_tk_exception(self, exc_type, exc, tb) -> None:
        log.error("Необработанная ошибка окна", exc_info=(exc_type, exc, tb))
        self.show_error("Ошибка", "Произошла непредвиденная ошибка. Программа продолжит работать; "
                        "если ошибка повторяется, отправьте отчёт администратору.",
                        action="Непредвиденная ошибка окна",
                        raw="".join(traceback.format_exception(exc_type, exc, tb)))

    @staticmethod
    def _error_kind(error: Exception) -> ErrorKind:
        if isinstance(error, SvnError):
            return error.kind
        log.error("Непредвиденная ошибка в фоновой задаче", exc_info=error)
        return ErrorKind.OTHER

    @staticmethod
    def _error_raw(error: Optional[Exception]) -> str:
        """Подробности ошибки для отчёта администратору (для ошибок svn это его вывод)."""
        if isinstance(error, SvnError):
            return error.raw
        return str(error) if error is not None else ""

    def show_error(self, title: str, text: str, *, action: str, kind: ErrorKind = ErrorKind.OTHER,
                   raw: str = "", extra: Optional[dict] = None) -> None:
        """Окно ошибки для всего, что связано с svn и сервером: понятный текст, контакт, отчёт для администратора.

        action пишется по-русски («Скачивание проекта»), raw — вывод svn (пароли вырезаются в admin_report).
        """
        if self._closing:  # окно закрывается: диалоги не нужны, ошибка остаётся в журнале
            log.warning("Ошибка при закрытии программы (%s): %s", action, text)
            return
        if self.login.dialog_open or self.error_open:  # два модальных окна друг на друга не ставим
            self._pending_errors.append((title, text, action, kind, raw, extra))
            return
        report = messages.admin_report(action, kind, raw, self.log_path, extra)
        self.reveal_window()
        self.error_open = True
        try:
            dialog = ErrorDialog(self.root, title, text, report, support=self.config.support,
                                 log_path=self.log_path, on_open_log=self._open_log)
            self.root.wait_window(dialog)
        finally:
            self.error_open = False
            self.post(self.after_modal_closed)

    def reveal_window(self) -> None:
        """Окно спрятано в трей, а из фона надо показать окно входа или ошибки: сначала показываем программу,
        иначе окно появилось бы без хозяина и его легко не заметить."""
        if self.root.state() == "withdrawn":
            self.show_window()

    def after_modal_closed(self) -> None:
        """Закрылось окно входа или ошибки: показываем отложенное (сначала ошибки, потом ждущие входа задачи)."""
        if self._pending_errors and not (self.login.dialog_open or self.error_open):
            title, text, action, kind, raw, extra = self._pending_errors.pop(0)
            self.show_error(title, text, action=action, kind=kind, raw=raw, extra=extra)
        self.login.flush()

    def px(self, value: int) -> int:
        """Масштабирует размер в пикселях под DPI экрана (за 100% принят 96 dpi)."""
        return int(value * float(self.root.tk.call("tk", "scaling")) / (96 / 72))

    def _find_tortoise(self) -> Optional[str]:
        svn_path = self.client.svn_path if self.client else ""
        return tortoise.find_tortoiseproc(svn_path if isinstance(svn_path, str) else "")

    # -- «Запомнить пароль» ------------------------------------------------------

    def remember_default(self) -> bool:
        """Значение флажка по умолчанию: что выбрал пользователь, а если не выбирал, то «запомнить»."""
        value = self.settings.remember_password
        return True if value is None else value

    def _apply_store_auth(self) -> None:
        """Не запоминать пароль в кэше самого svn, если пользователь снял флажок."""
        if self.client:
            self.client.store_auth = self.remember_default()

    def apply_remember(self, password: str, remember: bool, reset_saved: bool = False) -> None:
        """Применяет выбор «Запомнить пароль» и сохраняет настройки.

        Флажок снят: сохранённый пароль стирается. Флажок стоит: непустой пароль шифруется и заменяет прежний,
        пустое поле сохранённый пароль не трогает. reset_saved=True стирает его при смене логина:
        пароль прежнего логина новому не подходит.
        """
        st = self.settings
        st.remember_password = remember
        if not remember or reset_saved:
            st.password_enc = ""
        if remember and password:
            try:
                st.password_enc = protect_text(password)
            except OSError:
                log.exception("Не удалось зашифровать пароль")
                messagebox.showwarning("Пароль не сохранён", "Не удалось сохранить пароль. "
                                       "Он будет действовать только до закрытия программы.", parent=self.root)
        self._apply_store_auth()
        self.settings_store.save()

    # -- оформление ----------------------------------------------------------

    def _setup_style(self) -> None:
        self.palette = theme.apply(self.root, self.px, self.settings.theme)
        self.font = tkfont.Font(family=FONT_FAMILY, size=10)
        self.bold_font = tkfont.Font(family=FONT_FAMILY, size=10, weight="bold")
        ttk.Style(self.root).configure("Todo.TLabel", font=self.bold_font)

    def _add_menu_button(self, title: str, menu: tk.Menu) -> None:
        """Пункт строки меню: кнопка слева, список выпадает под ней."""
        ttk.Menubutton(self.menu_bar, text=title, style="MenuBar.TMenubutton", menu=menu,
                       direction="below").pack(side="left")

    def set_theme(self, mode: str) -> None:
        """Светлая, тёмная или как в Windows: перекрашивает открытое окно сразу и запоминает выбор."""
        if mode not in theme.MODES:
            return
        old = self.palette
        self.palette = theme.apply(self.root, self.px, mode)
        ttk.Style(self.root).configure("Todo.TLabel", font=self.bold_font)
        theme.recolor(self.root, old, self.palette)
        for tree in (self.tree, self.results):   # деревья проектов на сервере: свои теги поверх общих
            self._tag_project_rows(tree)
        for server_tree in list(self._extra_trees):      # то же в открытых окнах выбора места
            server_tree.recolor()
        self.theme_var.set(mode)
        self.lamp.configure(background=self.palette.bg)
        self.lamp._draw()
        for column in (self.mine_col2, self.mine_col3):   # подпись свёрнутой колонки рисуется на Canvas
            column.redraw()
        self._refresh_project_panel()                      # цвет состояния в шапке колонки 3 берётся из палитры
        self._refresh_lamp()
        if self.settings.theme != mode:
            self.settings.theme = mode
            self.settings_store.save()

    @staticmethod
    def _show(widget: tk.Misc, visible: bool) -> None:
        """Показывает или прячет виджет из grid: спрятанный не занимает места (пустых рядов не остаётся)."""
        if isinstance(widget.master, (ButtonRow, SideButtons)):
            widget.master.set_visible(widget, visible)
        elif visible:
            widget.grid()
        else:
            widget.grid_remove()

    def _hide_when_empty(self, label: tk.Misc, var: tk.StringVar) -> None:
        """Подпись видна, только пока в её тексте что-то есть."""
        def sync(*_args) -> None:
            self._show(label, bool(var.get().strip()))

        var.trace_add("write", sync)
        sync()

    def _autowrap(self, label: ttk.Label, container: tk.Misc, margin: int = 0) -> None:
        """Перенос строк подпись делает по ширине контейнера (окно можно растянуть)."""
        def fit(event) -> None:
            width = max(self.px(240), event.width - margin)
            if str(label.cget("wraplength")) != str(width):
                label.configure(wraplength=width)

        container.bind("<Configure>", fit, add="+")

    TABLE_COL_MAX = 300      # столбцы сами раскрываются по содержимому, но не шире этого (px до масштаба экрана)

    def _table_extras(self, tree: ttk.Treeview, frame: tk.Misc) -> None:
        """Горизонтальная полоса прокрутки под таблицей и ширина столбцов по содержимому (не больше TABLE_COL_MAX).
        Клик по названию столбца сворачивает его до узкого, повторный клик раскрывает на всю ширину текста."""
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(xscrollcommand=hscroll.set)
        hscroll.grid(row=1, column=0, sticky="ew")
        tree._col_default = {col: int(tree.column(col, "width")) for col in tree["columns"]}   # type: ignore[attr-defined]
        tree._col_state = {col: "auto" for col in tree["columns"]}                             # type: ignore[attr-defined]
        for col in tree["columns"]:
            tree.heading(col, command=lambda c=col: self._toggle_column(tree, c))

    def _column_fit(self, tree: ttk.Treeview, col: str) -> int:
        font = tkfont.Font(root=self.root, font=ttk.Style(self.root).lookup("Treeview", "font") or "TkDefaultFont")
        texts = [str(tree.heading(col, "text"))] + [str(tree.set(iid, col)) for iid in tree.get_children()]
        return max(font.measure(t) for t in texts) + self.px(28)

    def _autofit_columns(self, tree: ttk.Treeview) -> None:
        """Подгоняет ширину столбцов под текст; свёрнутые вручную и узкие служебные столбцы не трогает."""
        state = getattr(tree, "_col_state", None)
        if state is None:
            return
        for col, mode in state.items():
            default = tree._col_default[col]                   # type: ignore[attr-defined]
            if mode == "collapsed" or default <= self.px(30):
                continue
            fit = self._column_fit(tree, col)
            width = max(default, fit if mode == "full" else min(fit, self.px(self.TABLE_COL_MAX)))
            if int(tree.column(col, "width")) != width:
                tree.column(col, width=width)

    def _toggle_column(self, tree: ttk.Treeview, col: str) -> None:
        state = tree._col_state                                  # type: ignore[attr-defined]
        if state[col] == "collapsed":
            state[col] = "full"
        elif int(tree.column(col, "width")) > tree._col_default[col]:   # type: ignore[attr-defined]
            state[col] = "collapsed"
            tree.column(col, width=tree._col_default[col])       # type: ignore[attr-defined]
            return
        else:
            state[col] = "full"
        self._autofit_columns(tree)

    def _menu_button(self, parent: tk.Misc, text: str, menu: tk.Menu, style: str = "TButton") -> ttk.Button:
        """Кнопка, открывающая меню под собой («▾» в подписи рисуем сами)."""
        button = ttk.Button(parent, text=text, style=style)
        button.configure(command=lambda: self._popup_menu(button, menu))
        return button

    @staticmethod
    def _popup_menu(button: ttk.Button, menu: tk.Menu) -> None:
        if str(button.cget("state")) == "disabled":
            return
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def _build_ui(self) -> None:
        root = self.root
        p = self.palette
        px = self.px
        root.title(f"{winutil.WINDOW_TITLE_PREFIX} {__version__}")
        # полоса «Доступна новая версия»: показывается только когда нужно
        self.update_bar = tk.Frame(root, background=p.warn_bg, padx=px(16), pady=px(6), highlightthickness=1,
                                   highlightbackground=p.warn_border, highlightcolor=p.warn_border)
        self.update_var = tk.StringVar()
        tk.Label(self.update_bar, textvariable=self.update_var, background=p.warn_bg, foreground=p.text,
                 anchor="w", justify="left", font=(FONT_FAMILY, 10)).pack(side="left")
        self.update_open_btn = ttk.Button(self.update_bar, text="Открыть папку с новой версией",
                                          command=self._open_update_folder)
        self.update_open_btn.pack(side="right", padx=(px(12), 0))
        self.update_now_btn = ttk.Button(self.update_bar, text="Обновить сейчас", style="Accent.TButton",
                                         command=self._start_self_update)
        self._offered_version = ""   # о какой версии уже спрашивали: по каждой новой версии окно один раз
        # полоса «Нет связи с сервером»: одна на окно, прячется при первом успешном ответе сервера
        self.offline_bar = tk.Frame(root, background=p.warn_bg, padx=px(16), pady=px(6), highlightthickness=1,
                                    highlightbackground=p.warn_border, highlightcolor=p.warn_border)
        offline_label = tk.Label(self.offline_bar, text=messages.OFFLINE_BAR, background=p.warn_bg,
                                 foreground=p.text, anchor="w", justify="left", wraplength=px(900),
                                 font=(FONT_FAMILY, 10))
        offline_label.pack(side="left", fill="x", expand=True)
        self._autowrap(offline_label, self.offline_bar, px(40))
        self._schedule_update_poll()
        self._update_check_pending = False   # ждём общие настройки после «Проверить обновление программы»

        # шапка: название слева, «Вы вошли как …» и смена пользователя справа
        self.header = header = ttk.Frame(root, style="App.TFrame", padding=(px(20), px(12), px(20), px(2)))
        header.pack(fill="x")
        ttk.Label(header, text="SVN-lite", style="AppTitle.TLabel").pack(side="left")
        # меню рядом с названием, в строке шапки (высоту окна не отнимает); родное меню Windows не принимает
        # тёмную тему и остаётся белым
        self.menu_bar = ttk.Frame(header, style="App.TFrame")
        self.menu_bar.pack(side="left", padx=(px(20), 0))
        # справа: индикатор состояния (по нажатию обновляет), левее «Логин: имя» (по нажатию окно смены пользователя)
        self.lamp = Lamp(header, px(20), p.bg, command=self._on_lamp_click)
        self.lamp.pack(side="right", padx=(px(10), 0))
        self.lamp_tip = Tooltip(self.lamp, lambda: (self.lamp_text + "\n" if self.lamp_text else "")
                                + "Нажмите, чтобы проверить подключение")
        self.lamp_text = ""
        self.login_var = tk.StringVar()        # полный текст («Вы вошли как alice»); на экране он разбит на две части
        self.login_prefix_var, self.login_name_var = tk.StringVar(), tk.StringVar()
        # справа налево: имя (основной цвет), перед ним приглушённое «Вы вошли как»
        self.login_name_label = ttk.Label(header, textvariable=self.login_name_var, style="AppUser.TLabel")
        self.login_name_label.pack(side="right")
        self.login_label = ttk.Label(header, textvariable=self.login_prefix_var, style="AppUserHint.TLabel")
        self.login_label.pack(side="right", padx=(0, px(4)))
        for label in (self.login_name_label, self.login_label):
            label.configure(cursor="hand2")
            label.bind("<Button-1>", lambda _e: self._open_settings())
        Tooltip(self.login_name_label, lambda: "Сменить пользователя")

        self.notebook = notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=px(12), pady=(0, px(12)))
        self._build_download_tab(notebook)
        self._update_login_label()
        self._build_projects_tab(notebook)   # строится после «Скачать проект» (берёт его папку), но стоит первой
        notebook.insert(0, self.mine_tab)
        notebook.select(self.mine_tab)
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        menu = tk.Menu(root, tearoff=False)
        menu.add_command(label="Параметры…\tF12", command=self._open_settings)
        menu.add_command(label="Обновить общие настройки", command=lambda: self.reload_shared_config(manual=True))
        menu.add_separator()
        menu.add_command(label="Открыть журнал", command=self._open_log)
        menu.add_separator()
        menu.add_command(label="Выйти из программы", command=self._request_exit)
        self._add_menu_button("Настройки", menu)
        self.theme_var = tk.StringVar(value=self.settings.theme)
        view_menu = tk.Menu(root, tearoff=False)
        for mode in theme.MODES:
            view_menu.add_radiobutton(label=theme.MODE_LABELS[mode], value=mode, variable=self.theme_var,
                                      command=lambda m=mode: self.set_theme(m))
        self._add_menu_button("Вид", view_menu)
        self.help_menu = help_menu = tk.Menu(root, tearoff=False)
        help_menu.add_command(label="Шпаргалка", command=self._open_help)
        self.docs_menu = tk.Menu(help_menu, tearoff=False)
        help_menu.add_cascade(label="Документы", menu=self.docs_menu, state="disabled")
        self.docs: list[docs.DocEntry] = []
        help_menu.add_command(label="Проверить обновление программы", command=self._check_for_update)
        help_menu.add_command(label="Создать ярлык на рабочем столе", command=self._create_shortcut,
                              state="normal" if getattr(sys, "frozen", False) else "disabled")
        help_menu.add_command(label="ИИ-помощник…", command=self._open_agent_dialog)
        help_menu.add_separator()
        help_menu.add_command(label="О программе", command=self._on_about)
        self._add_menu_button("Справка", help_menu)
        root.bind("<F12>", lambda _e: self._open_settings())
        root.bind("<Control-comma>", lambda _e: self._open_settings())
        root.bind("<FocusIn>", self._on_focus_in)

        self._update_version_bar(resize=False)
        self._rescan_docs()
        root.update_idletasks()
        # при запуске ширины хватает на три колонки с колонкой кнопок справа (минимум окна прежний)
        width = min(px(1200), root.winfo_screenwidth() - 40)
        self._base_height = min(max(px(720), root.winfo_reqheight() + px(24)), root.winfo_screenheight() - 100)
        self._min_width, self._min_height = min(px(900), width), min(px(640), self._base_height)
        root.minsize(self._min_width, self._min_height)
        center_window(root, width, self._base_height)
        self._update_buttons()
        self._refresh_lamp()
        root.after(600, self._paste_from_clipboard)

    def _set_offline(self, offline: bool) -> None:
        """Полоса «Нет связи с сервером» под шапкой: показывается один раз, прячется при первом ответе сервера."""
        if offline == self._offline:
            return
        self._offline = offline
        gap = self.px(6)
        if offline:
            self.offline_bar.pack(fill="x", padx=self.px(12), pady=(0, gap), before=self.notebook)
        else:
            self.offline_bar.pack_forget()
            self._tab_loads.retry_failed()      # связь вернулась: вкладки, которые не прочитались, читаются заново
        self._fit_height_for_bar(self.offline_bar, offline, margin=gap)   # как полоса «Доступна новая версия»
        self._refresh_lamp()
        if hasattr(self, "mine_tree"):
            self._on_offline_changed()

    def _on_lamp_click(self) -> None:
        """Щелчок по индикатору: «Проверить подключение» (как в параметрах, но с текущими настройками)."""
        if self._conn_checking:
            return
        self._conn_checking = True
        self.mine_status.set("Проверяю подключение…")

        def show(text: str, ok: bool) -> None:
            self._conn_checking = False
            self.mine_status.set("")
            if ok:
                self._set_offline(False)
            (messagebox.showinfo if ok else messagebox.showwarning)("Проверка подключения", text, parent=self.root)

        self._check_connection_async({}, show)

    def _show_startup_messages(self) -> None:
        if self.client is None:
            self.status_var.set("Не найдена программа svn.exe (входит в TortoiseSVN).")
        # общий конфиг (в нём путь к msi) ещё может читаться: тогда решим, когда он придёт
        self._check_tools(shared_settled=not self.settings.shared_source or self.shared_data is not None)
        if self.config.warnings:
            messagebox.showwarning("Предупреждение", "\n\n".join(self.config.warnings),
                                   parent=self.root)

    # -- установка TortoiseSVN ---------------------------------------------------

    def _check_tools(self, shared_settled: bool) -> None:
        """Нет svn.exe или TortoiseProc: предлагаем установить из msi на сервере, иначе объясняем, что делать."""
        if self._tools_checked or (self.client is not None and self.tortoise_exe is not None):
            return
        if self.config.tortoise_installer_path:
            self._tools_checked = True
            what = "программа svn.exe" if self.client is None else "окна TortoiseSVN"
            if messagebox.askyesno("Нужен TortoiseSVN", messages.INSTALL_OFFER.format(what=what),
                                   parent=self.root):
                self._install_tortoise(self.config.tortoise_installer_path)
            elif self.client is None:
                self._show_svn_not_found()
        elif shared_settled:
            self._tools_checked = True
            if self.client is None:
                self._show_svn_not_found()

    def _show_svn_not_found(self) -> None:
        self.show_error("Не найден svn.exe", messages.SVN_NOT_FOUND, action="Запуск программы: svn.exe не найден")

    def _install_tortoise(self, source: str) -> None:
        self.status_var.set("Устанавливается TortoiseSVN, подождите…")
        self.run_bg(lambda: installer.install_tortoise(source), self._on_tortoise_installed)

    def _on_tortoise_installed(self, result, error, _stamp: JobStamp) -> None:
        if error is not None:
            log.error("Установка TortoiseSVN", exc_info=error)
            result = installer.InstallResult(False, "Непредвиденная ошибка установки TortoiseSVN.")
        if not result.ok:
            self.status_var.set("TortoiseSVN не установлен.")
            self.show_error("Установка TortoiseSVN", result.message + "\n\n" + messages.SVN_NOT_FOUND,
                            action="Установка TortoiseSVN", raw=result.message)
            return
        self._rebuild_config()   # заново ищет svn.exe и TortoiseProc, включает кнопки
        if self.client is None:
            self.status_var.set("TortoiseSVN установлен, но svn.exe не найден.")
            self.show_error("Не найден svn.exe", messages.SVN_NOT_FOUND_AFTER_INSTALL,
                            action="Установка TortoiseSVN: svn.exe не найден после установки")
            return
        self.status_var.set("TortoiseSVN установлен.")
        messagebox.showinfo("TortoiseSVN", result.message or "TortoiseSVN установлен. Можно работать.",
                            parent=self.root)

    # -- новая версия программы --------------------------------------------------

    def _update_version_bar(self, resize: bool = True) -> None:
        """Жёлтая полоса вверху окна, если администратор объявил более новую версию."""
        cfg = self.config
        show = is_newer_version(cfg.latest_version, __version__)
        visible = bool(self.update_bar.winfo_manager())
        if show:
            text = f"Доступна новая версия {cfg.latest_version.strip()} (у вас {__version__})."
            if self.settings.update_attempt == cfg.latest_version.strip():
                text += " Обновление уже запускалось, но версия не изменилась: на сервере, похоже, лежит старый файл."
            self.update_var.set(text)
            can_self_update = selfupdate.is_supported() and bool(cfg.update_path.strip())
            if can_self_update:
                self.update_open_btn.pack_forget()
                self.update_now_btn.pack(side="right", padx=(self.px(12), 0))
                if (self._offered_version != cfg.latest_version.strip() and not self._update_check_pending
                        and self.settings.update_attempt != cfg.latest_version.strip()):
                    self.root.after(700, self._offer_self_update)
            else:
                self.update_now_btn.pack_forget()
                if cfg.update_path.strip():
                    self.update_open_btn.pack(side="right", padx=(self.px(12), 0))
                else:
                    self.update_open_btn.pack_forget()
        if show == visible:
            return
        if show:
            self.update_bar.pack(fill="x", before=self.header)
        else:
            self.update_bar.pack_forget()
        if resize:
            self._fit_height_for_bar(self.update_bar, show)

    def _fit_height_for_bar(self, bar: tk.Misc, shown: bool, margin: int = 0) -> None:
        """Полоса над вкладками появилась или исчезла: окно и его наименьшая высота меняются на её высоту
        (плюс отступ margin), чтобы кнопки внизу не обрезались. До первого показа окна ничего не делает."""
        if not hasattr(self, "_base_height"):
            return
        added = getattr(self, "_bar_heights", None)
        if added is None:
            added = self._bar_heights = {}     # полоса → на сколько выросло окно при её показе
        if shown:
            if bar in added:
                return
            self.root.update_idletasks()
            delta = added[bar] = bar.winfo_reqheight() + margin
        else:
            if bar not in added:
                return
            delta = -added.pop(bar)            # убираем ровно столько, сколько добавили (перенос строк мог измениться)
        self._base_height += delta
        self._min_height += delta
        self.root.minsize(self._min_width, self._min_height)
        if self.root.winfo_ismapped():      # у скрытого окна размера ещё нет: хватит наименьшей высоты
            self.root.geometry(f"{self.root.winfo_width()}x{max(self._base_height, self.root.winfo_height() + delta)}")

    UPDATE_POLL_MS = 60 * 60 * 1000    # как часто программа сама проверяет общие настройки (и новую версию)

    def _schedule_update_poll(self) -> None:
        self.root.after(self.UPDATE_POLL_MS, self._update_poll)

    def _update_poll(self) -> None:
        """Раз в час тихо перечитывает общие настройки: новая версия появится без перезапуска программы."""
        self._schedule_update_poll()
        source = self.settings.shared_source
        if self._closing or not source or self.job or self.upd:
            return
        if "://" in source and not (self.client and self.client.credentials):
            return    # ссылка svn:// без входа: не выскакиваем с окном логина раз в час
        selfupdate.apply_pending()      # окно работает днями: подмену svnlite.exe доводим и без ручной проверки
        self.reload_shared_config()

    def _check_for_update(self) -> None:
        """Справка → «Проверить обновление программы»: перечитывает общие настройки и говорит, есть ли новая версия."""
        selfupdate.apply_pending()      # помощник мог закрыться: доводим подмену svnlite.exe, которую задержал занятый файл
        if not self.settings.shared_source:
            self._report_update_check()
            return
        self._update_check_pending = True
        self.status_var.set("Проверяю, есть ли новая версия программы…")
        self.reload_shared_config()

    def _report_update_check(self) -> None:
        self._update_check_pending = False
        selfupdate.apply_pending()      # и после проверки: файл мог освободиться, пока она шла
        latest = self.config.latest_version.strip()
        if not is_newer_version(latest, __version__):
            messagebox.showinfo("Обновление программы", f"У вас последняя версия: {__version__}.", parent=self.root)
        elif selfupdate.is_supported() and self.config.update_path.strip():
            self._offered_version = ""     # просили проверить сами: спрашиваем, даже если уже отвечали «нет»
            self._offer_self_update()
        else:
            messagebox.showinfo("Обновление программы", f"Доступна новая версия {latest} (у вас {__version__}). "
                                "Получите её у администратора.", parent=self.root)

    def _offer_self_update(self) -> None:
        """Новая версия есть: предлагаем заменить программу и открыть её заново (по каждой версии один раз)."""
        latest = self.config.latest_version.strip()
        if (self._closing or not is_newer_version(latest, __version__) or self.job or self.upd
                or self.login.dialog_open):
            return    # занято или не время: полоса вверху остаётся, спросим при следующей проверке
        if self.root.state() == "withdrawn":
            return    # окно в трее: вопрос задастся, когда окно откроют (show_window), о версии скажет уведомление
        self._offered_version = latest
        if messagebox.askyesno("Новая версия", f"Доступна новая версия {self.config.latest_version.strip()} "
                               f"(у вас {__version__}). Обновить сейчас? Программа перезапустится.",
                               parent=self.root):
            self._start_self_update()

    def _start_self_update(self) -> None:
        if self.job or self.upd or self._committing or self._locking or self._create_running():
            messagebox.showinfo("Обновление программы", "Сейчас идёт операция с проектом. Дождитесь её окончания "
                                "и обновите программу ещё раз.", parent=self.root)
            return
        self.update_now_btn.configure(state="disabled")
        self.status_var.set("Скачиваю новую версию программы…")
        update_path = self.config.update_path
        self.run_bg(lambda: selfupdate.prepare(update_path), self._on_self_update_ready)

    def _on_self_update_ready(self, prepared, error, _stamp: JobStamp) -> None:
        self.update_now_btn.configure(state="normal")
        if error is not None:
            if not isinstance(error, selfupdate.UpdateError):
                log.error("Обновление программы", exc_info=error)
            self.status_var.set("Программа не обновлена.")
            self.show_error("Обновление программы", str(error) if isinstance(error, selfupdate.UpdateError)
                            else "Не удалось обновить программу. Сообщите администратору.",
                            action="Обновление программы", raw=self._error_raw(error))
            return
        self.settings.update_attempt = self.config.latest_version.strip()
        self.settings_store.save()
        try:
            selfupdate.launch_swap(prepared)
        except OSError as exc:
            log.error("Запуск подмены exe", exc_info=exc)
            self.status_var.set("Программа не обновлена.")
            self.show_error("Обновление программы", f"Не удалось запустить обновление: {exc.strerror or exc}",
                            action="Запуск подмены exe", raw=str(exc))
            return
        self._closing = True
        self._finish_close()

    def _open_update_folder(self) -> None:
        path = os.path.expandvars(self.config.update_path.strip())
        if not path:
            return
        self._open_path(path, "Открыть папку с новой версией", self._open_update_target)

    @staticmethod
    def _open_update_target(path: str) -> None:
        """Папку с новой версией открываем; файл не запускаем (путь пришёл из общего конфига), а показываем в проводнике."""
        if path.lower().startswith(("http://", "https://")):
            webbrowser.open(path)
        elif "://" in path:
            raise OSError(f"неподдерживаемая ссылка: {path}")
        elif os.path.isdir(path):
            os.startfile(path)  # type: ignore[attr-defined]
        elif os.path.isfile(path):
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            raise OSError(f"путь не найден: {path}")

    @staticmethod
    def _open_any(path: str) -> None:
        """Открывает файл или ссылку программой по умолчанию (для проверенных путей)."""
        if "://" in path:
            webbrowser.open(path)
        else:
            os.startfile(path)  # type: ignore[attr-defined]

    def _open_path(self, path: str, title: str, opener: Optional[Callable[[str], None]] = None) -> None:
        """Открывает путь через opener. Путь может быть на сетевом диске, поэтому открываем в фоне."""
        def work() -> None:
            (opener or self._open_any)(path)

        def done(_res, err, _stamp) -> None:
            if err is not None:
                log.error("Не удалось открыть %s", path, exc_info=err)
                messagebox.showerror(title, f"Не удалось открыть:\n{path}"
                                     + (f"\n\nЕсли не получается: {self.config.support}" if self.config.support else ""),
                                     parent=self.root)

        self.run_bg(work, done)

    # -- справка ---------------------------------------------------------------

    def _help_target(self) -> str:
        configured = os.path.expandvars(self.config.help_path.strip())
        return configured or docs.find_help(self.docs) or str(app_dir() / "ШПАРГАЛКА.html")

    def _rescan_docs(self) -> None:
        """Читает папку info_path в фоне (она на сервере) и перестраивает меню «Справка → Документы»."""
        folder = self.config.info_path.strip()
        if not folder:
            self._fill_docs_menu([])
            return
        self.run_bg(lambda: docs.scan_info_folder(folder), lambda found, err, _stamp:
                    self._fill_docs_menu(found or [], failed=err is not None))

    def _fill_docs_menu(self, found: list, failed: bool = False) -> None:
        self.docs = found
        menu = self.docs_menu
        menu.delete(0, "end")
        for entry in found:
            menu.add_command(label=entry.title, command=lambda e=entry: self._open_path(e.path, e.title))
        if self.config.info_path.strip():
            if found:
                menu.add_separator()
            elif not failed:
                menu.add_command(label="В папке нет документов", state="disabled")
            menu.add_command(label="Обновить список", command=self._rescan_docs)
        self.help_menu.entryconfigure("Документы", state="normal" if menu.index("end") is not None else "disabled")

    @staticmethod
    def help_target_allowed(target: str) -> bool:
        """Шпаргалка: только страница или документ (.html/.htm/.pdf) либо ссылка http(s). Запускать программы нельзя."""
        low = target.lower()
        if low.startswith(("http://", "https://")):
            return True
        return "://" not in low and os.path.splitext(low)[1] in (".html", ".htm", ".pdf")

    def _open_help(self) -> None:
        target = self._help_target()
        if not self.help_target_allowed(target):
            messagebox.showinfo("Шпаргалка", "Шпаргалку можно открыть только как страницу (.html, .htm), документ "
                                f"(.pdf) или ссылку http(s):\n{target}\n\nОбратитесь к администратору."
                                + (f"\n\nКонтакт: {self.config.support}" if self.config.support else ""),
                                parent=self.root)
            return

        def work() -> bool:
            return "://" in target or os.path.exists(target)  # проверка пути на сервере может быть долгой: в фоне

        def done(found, err, _stamp) -> None:
            if err is None and found:
                self._open_path(target, "Шпаргалка")
                return
            messagebox.showinfo("Шпаргалка", f"Шпаргалка не найдена:\n{target}\n\nОбратитесь к администратору."
                                + (f"\n\nКонтакт: {self.config.support}" if self.config.support else ""),
                                parent=self.root)

        self.run_bg(work, done)

    def _create_shortcut(self) -> None:
        """Ярлык на рабочем столе. Работает только у собранной программы (exe)."""
        if not getattr(sys, "frozen", False):
            return

        def done(path, err, _stamp) -> None:
            if err is not None:
                log.error("Не удалось создать ярлык", exc_info=err)
                messagebox.showerror("Ярлык", "Не удалось создать ярлык на рабочем столе.\n\n"
                                     f"Подробности записаны в файл:\n{self.log_path}", parent=self.root)
            else:
                messagebox.showinfo("Ярлык", f"Ярлык создан на рабочем столе:\n{path}", parent=self.root)

        self.run_bg(winutil.create_desktop_shortcut, done)

    def _on_about(self) -> None:
        client = self.client

        def work() -> Optional[tuple[int, int]]:
            return client.version() if client else None  # первый вызов запускает svn: в фоне

        self.run_bg(work, lambda ver, _err, _stamp: self._show_about(ver))

    def _show_about(self, svn_version: Optional[tuple[int, int]]) -> None:
        svn_path = (self.client.svn_path if self.client else "") or "—"
        built = ""
        if getattr(sys, "frozen", False):   # у собранной программы показываем, когда собран exe: сразу видно, свежая ли сборка
            try:
                built = time.strftime("Сборка от %d.%m.%Y %H:%M", time.localtime(os.path.getmtime(sys.executable)))
            except OSError:
                pass
        engine = (f"Движок svn.exe (отдельная программа, не версия SVN-lite): {svn_version[0]}.{svn_version[1]}"
                  if svn_version else "Движок svn.exe не найден")
        details = ([built] if built else []) + [engine, f"Сервер: {self.config.server}"]
        if svn_version and svn_path != "—":
            details.append(f"Путь к svn.exe: {svn_path}")
        details.append(f"Журнал: {self.log_path}")
        dialog = AboutDialog(self.root, __version__, details, support=self.config.support,
                             on_open_log=self._open_log)
        self.root.wait_window(dialog)

    # -- настройки -------------------------------------------------------------

    def mark_login_confirmed(self) -> None:
        """Сервер ответил успешно с текущими логином и паролем: теперь можно писать «Вы вошли как»."""
        client = self.client
        if client is None or client.credentials is None:
            return
        mark = (client, client.cred_generation)
        if (self._login_confirmed is not None and self._login_confirmed[0] is mark[0]
                and self._login_confirmed[1] == mark[1]):
            return
        self._login_confirmed = mark
        self._update_login_label()

    def mark_login_failed(self) -> None:
        """Сервер отверг логин или пароль: возвращаем подпись «Логин: …»."""
        if self._login_confirmed is not None:
            self._login_confirmed = None
            self._update_login_label()

    def _update_login_label(self) -> None:
        client = self.client
        creds = client.credentials if client else None
        confirmed = (creds is not None and self._login_confirmed is not None
                     and self._login_confirmed[0] is client and self._login_confirmed[1] == client.cred_generation)
        if creds is None:
            text, prefix, name = "Вход не выполнен: логин запросится сам", "Вход не выполнен: логин запросится сам", ""
            self.login_label.configure(style="AppHint.TLabel")
        else:
            prefix = "Логин:"
            name = creds.username
            text = f"{prefix} {name}"
            self.login_label.configure(style="AppUserHint.TLabel")
        self.login_var.set(text)
        self.login_prefix_var.set(prefix)
        self.login_name_var.set(name)

    def _open_settings(self) -> None:
        if self.job or self.login.dialog_open:
            return
        dialog = SettingsDialog(self.root, self.config, self.settings,
                                has_password=bool(self.client and self.client.credentials),
                                check_cb=self._check_connection_async, autostart=autostart.is_enabled(),
                                notify_projects=discover_projects(self._notice_dirs()),
                                test_cb=self.show_test_notice, popup_open=self.popups_open)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self._apply_settings(dialog.result)

    # «Проверить подключение» в окне параметров

    def _check_connection_async(self, values: dict, show: Callable[[str, bool], None]) -> None:
        def done(res, err, _stamp) -> None:
            if err is not None:
                log.error("Проверка подключения не удалась", exc_info=err)
                show("Не удалось проверить подключение. Подробности записаны в журнал.", False)
            else:
                show(*res)

        self.run_bg(lambda: self._connection_check(values), done)

    def _connection_check(self, values: dict) -> tuple[str, bool]:
        """(текст для человека, всё ли в порядке). Использует значения из окна, ничего не сохраняет. Фоновый поток."""
        svn = find_svn(values.get("svn_path") or self.config.svn_path)
        if not svn:
            return ("Не найдена программа svn.exe (входит в TortoiseSVN). Установите TortoiseSVN "
                    "или укажите путь к svn.exe.", False)
        client = SvnClient(svn)
        client.store_auth = False  # проверка не должна оставлять пароль в кэше svn
        version = client.version()
        head = f"Программа svn найдена (версия {version[0]}.{version[1]}). "
        user, password = values.get("username") or "", values.get("password") or ""
        current = self.client.credentials if self.client else None
        if not password and current and (not user or user == current.username):
            user, password = current.username, current.password  # пароль уже введён раньше, поле пустое
        if not (user and password):
            saved = effective_credentials(self.settings, self.config)
            if saved:
                user, password = saved
        if user and password:
            client.set_credentials(user, password)
        repos = [r.strip().strip("/") for r in (values.get("repos") or "").split(",") if r.strip().strip("/")]
        repo = (repos or self.config.repos or [""])[0]
        address = values.get("server") or self.config.server
        try:
            client.list_dirs(server.build_url(address, [repo] if repo else []))
        except SvnError as exc:
            log.warning("Проверка подключения: %s %s", exc.kind.value, exc.raw.strip()[-300:])
            return head + connection_error_text(exc.kind, repo, bool(user and password)), False
        who = f"вы вошли как {user}" if user and password else "сервер отвечает"
        return head + f"Всё в порядке, {who}.", True

    def _apply_settings(self, values: dict) -> None:
        st = self.settings
        base = build_config(Settings(), self.shared_data)  # что получилось бы без личных правок
        repos = [r.strip().strip("/") for r in values["repos"].split(",") if r.strip().strip("/")]
        server_value = values["server"].rstrip("/")
        old_source = st.shared_source
        old = self.client.credentials if self.client else None
        previous_user = old.username if old else (st.username or self.config.username)
        st.config_path = "" if values["config_path"] in ("", DEFAULT_SHARED_CONFIG) else values["config_path"]
        st.username = values["username"]
        st.server = "" if server_value == base.server else server_value
        st.repos = [] if repos == base.repos else repos
        st.svn_path = "" if values["svn_path"] == base.svn_path else values["svn_path"]
        st.skip_junk = None if values["skip_junk"] == base.filter.skip_junk else values["skip_junk"]
        st.show_report = None if values["show_report"] == base.filter.show_report else values["show_report"]
        if "mirrors" in values:
            st.mirrors = parse_mirrors_text(values["mirrors"])
        if values["last_dir"]:
            self._remember_project_dir(values["last_dir"])    # и в общий список папок для проектов
            self.dir_var.set(st.last_dir)
        if "tray_on_close" in values:
            st.tray_on_close = bool(values["tray_on_close"])
        if "autostart" in values:
            self._apply_autostart(bool(values["autostart"]))
        for problem in apply_notify(st, values):      # из окна приходят уже проверенные значения; это страховка
            log.warning("Уведомления: %s", problem)

        password = values["password"]
        if not password and old and old.username == values["username"]:
            password = old.password  # прежний пароль подходит, только если логин не менялся
        login_changed = bool(values["username"]) and values["username"] != previous_user
        # пустое поле пароля сохранённый пароль не стирает; при смене логина стирает: он был от другого логина
        self.apply_remember(password, bool(values["remember"]),
                            reset_saved=login_changed and not values["password"])  # заодно сохраняет настройки

        self._rebuild_config()
        if self.client and values["username"] and password:
            self.client.set_credentials(values["username"], password)
        self._update_login_label()
        if st.shared_source != old_source:
            self.reload_shared_config()

    def _apply_autostart(self, wanted: bool) -> None:
        """Включает или выключает запуск вместе с Windows (только если значение изменилось).
        Уже включённый автозапуск с устаревшим путём (программу перенесли) получает верный путь."""
        if wanted == autostart.is_enabled():
            if wanted:
                self._refresh_autostart()
            return
        try:
            autostart.enable() if wanted else autostart.disable()
        except OSError:
            log.exception("Не удалось изменить автозапуск (нужно: %s)", wanted)
            messagebox.showerror("Запуск вместе с Windows", messages.AUTOSTART_FAILED, parent=self.root)

    @staticmethod
    def _refresh_autostart() -> None:
        """Автозапуск включён, но указывает на другой путь: переписываем его на текущий."""
        try:
            if autostart.refresh():
                log.info("Автозапуск: записан текущий путь программы")
        except OSError:
            log.exception("Не удалось обновить путь автозапуска")

    def _rebuild_config(self) -> None:
        """Пересобирает итоговый конфиг (по умолчанию → файл рядом → общий → личное) и обновляет окно."""
        if self.job or self.upd:  # клиент и адрес сервера нельзя менять под работающей задачей: применим после неё
            self._pending_rebuild = True
            return
        new = build_config(self.settings, self.shared_data)
        for warning in new.warnings:
            log.warning("Настройки: %s", warning)
        self.config.__dict__.update(new.__dict__)
        svn = find_svn(self.config.svn_path)
        old = self.client.credentials if self.client else None
        if svn and (self.client is None or svn != self.client.svn_path):
            self.client = SvnClient(svn)
            if old:
                self.client.set_credentials(old.username, old.password)
        elif not svn:
            self.client = None
            self.status_var.set("Не найдена программа svn.exe (входит в TortoiseSVN).")
        self._apply_store_auth()
        self.tortoise_exe = self._find_tortoise()
        self._apply_tortoise()
        if self.client and not self.client.credentials:
            creds = effective_credentials(self.settings, self.config)
            if creds:
                self.client.set_credentials(*creds)
        if not self.settings.last_dir:
            self.dir_var.set(self.config.work_dir)
        self._commit_panel.set_templates(self.config.commit_templates)
        self._update_version_bar()
        self._rescan_docs()
        self._on_refresh()
        self._on_url_changed()

    def _apply_pending_rebuild(self) -> None:
        """Настройки менялись во время скачивания или обновления: применяем, когда задач больше нет."""
        if self._pending_rebuild and not (self.job or self.upd):
            self._pending_rebuild = False
            self._rebuild_config()

    # -- общий svn_helper.json на сервере ------------------------------------------

    def reload_shared_config(self, manual: bool = False) -> None:
        source = self.settings.shared_source
        if not source:
            if manual:
                messagebox.showinfo("Общие настройки", "Путь к общему svn_helper.json не задан "
                                    "(Настройки → Параметры).", parent=self.root)
            return
        client = self.client

        def svn_cat(url: str) -> bytes:
            if client is None:
                raise OSError("не найдена программа svn.exe")
            return client.run(["cat", server.normalize_url(url)], timeout=20)

        self.run_bg(lambda: fetch_shared(source, svn_cat),
                    lambda res, err, stamp: self._on_shared_loaded(source, manual, res, err, stamp))

    def _on_shared_loaded(self, source: str, manual: bool, data, error, stamp: JobStamp) -> None:
        if source != self.settings.shared_source:
            return  # путь успели поменять
        if error is None:
            if "://" in source:  # с диска/UNC читается без входа: успех ничего не говорит о пароле
                self.login.reset_attempts()
            log.info("Общие настройки прочитаны: %s", source)
            if data != self.shared_data:
                save_shared_cache(data)
                self.shared_data = data
                self._rebuild_config()
            self._check_tools(shared_settled=True)
            if self._update_check_pending:
                self._report_update_check()
            elif manual:
                messagebox.showinfo("Общие настройки", "Общие настройки обновлены.", parent=self.root)
            return
        kind = error.kind if isinstance(error, SvnError) else None
        if kind is ErrorKind.AUTH:
            self.login.request(retry=lambda: self.reload_shared_config(manual),
                               give_up=lambda: self._shared_failed(source, False, messages.short_message(kind),
                                                                   kind, self._error_raw(error)),
                               job_gen=stamp)
            return
        text = (messages.short_message(kind) if kind else
                str(error) if isinstance(error, SharedConfigError) else "непредвиденная ошибка")
        if not isinstance(error, (SharedConfigError, SvnError)):
            log.error("Ошибка чтения общих настроек", exc_info=error)
        self._shared_failed(source, manual, text, kind, self._error_raw(error))

    def _shared_failed(self, source: str, manual: bool, text: str,
                       kind: Optional[ErrorKind] = None, raw: str = "") -> None:
        log.warning("Общие настройки не прочитаны (%s): %s", source, text)
        self._check_tools(shared_settled=True)
        if self._update_check_pending:   # проверка обновления: без общих настроек узнать версию нельзя
            self._update_check_pending = False
            manual = True
        if manual:
            self.show_error("Общие настройки", f"Не удалось прочитать общие настройки:\n{source}\n\n{text}\n\n"
                            "Используется последняя сохранённая копия.",
                            action="Чтение общих настроек", kind=kind or ErrorKind.OTHER, raw=raw,
                            extra={"Источник": source})
        elif self.settings.config_path.strip():  # путь задан явно: напоминаем, но без окон
            self.status_var.set(f"Общие настройки не прочитаны ({text}). Используется сохранённая копия.")

    def _open_agent_dialog(self) -> None:
        """Справка → «ИИ-помощник…»: состояние, папки, подключение. Окно одно: повторный вызов выводит его вперёд."""
        dialog = getattr(self, "_agent_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.deiconify()
            dialog.lift()
            dialog.focus_set()
            return
        self._agent_dialog = AgentDialog(self)

    def _open_log(self) -> None:
        try:
            os.startfile(self.log_path)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            messagebox.showinfo("Журнал", f"Журнал находится здесь:\n{self.log_path}", parent=self.root)

    def _on_tab_changed(self, _event) -> None:
        if self.notebook.select() != str(self.mine_tab):
            return
        if self._mine_dirty:
            self._scan_projects()
        else:
            self._auto_auth_failed = False
            self._load_project_tab()   # колонка 3 показывает открытую вкладку проекта
            self._on_check_all()       # при открытии вкладки состояние всегда свежее

    def run_auth(self, work: Callable, ok: Callable, fail: Callable) -> None:
        """Фоновая задача с общим окном входа: ok(результат) или fail(ErrorKind)."""
        def done(res, err, stamp: JobStamp) -> None:
            if err is None:
                self.login.reset_attempts()
                ok(res)
                return
            kind = self._error_kind(err)
            if kind is ErrorKind.AUTH:
                self.login.request(retry=lambda: self.run_auth(work, ok, fail),
                                   give_up=lambda: fail(ErrorKind.AUTH), job_gen=stamp)
            else:
                fail(kind)
        self.run_bg(work, done)

    # -- закрытие ------------------------------------------------------------------

    # -- трей ------------------------------------------------------------------------

    def attach_tray(self, icon: tray.Tray) -> None:
        """Подключает значок в трее: с этого момента крестик (если так настроено) прячет окно, а не закрывает."""
        self._tray = icon

    def tray_event(self, event: str) -> None:
        """Событие значка. Вызывать из любого потока: выполнится в главном."""
        self.post(self._handle_tray_event, event)

    def _handle_tray_event(self, event: str) -> None:
        if event == tray.EVENT_OPEN:
            self.show_window()
        elif event == tray.EVENT_CHECK:
            self.request_notice_check()
        elif event == tray.EVENT_SNOOZE:
            self.toggle_snooze()
        elif event == tray.EVENT_QUIT:
            self.show_window()        # вопросы при выходе должны быть видны
            self._request_exit()

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if not self.settings.tray_hint_shown:       # первый раз: объясняем, куда делось окно
            self.settings.tray_hint_shown = True
            self.settings_store.save()
            self._notice_info(messages.TRAY_HINT, messages.TRAY_HINT_TITLE)

    def show_window(self) -> None:
        """Показывает окно из трея (или просто выводит вперёд, если оно уже на экране)."""
        if self._closing:
            return
        reopened = self.root.state() == "withdrawn"
        self.root.deiconify()
        if reopened and self._offered_version != self.config.latest_version.strip():
            self.root.after(700, self._offer_self_update)       # вопрос о новой версии, отложенный на время трея
        if self.root.state() == "iconic":
            self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        winutil.activate_window(winutil.toplevel_handle(self.root.winfo_id()))

    def _on_close(self) -> None:
        """Крестик окна: в трей, если он подключён и так настроено; иначе выход."""
        if self._closing:
            return
        if self._tray is not None and self._tray.active and self.settings.tray_on_close:
            self.hide_to_tray()
            return
        self._request_exit()

    def _request_exit(self) -> None:
        """Настоящий выход (меню трея, кнопка выхода, крестик без трея) с вопросами о несохранённом и фоновых задачах."""
        if self._closing:
            return
        if self._committing:
            messagebox.showinfo(messages.COMMIT_TITLE, messages.COMMIT_RUNNING_ON_CLOSE, parent=self.root)
            return
        unsaved = self._unsaved_project_names()
        if unsaved and not messagebox.askyesno(
                "Несохранённые правки", messages.COMMIT_UNSAVED_ON_CLOSE.format(names=", ".join(unsaved)),
                parent=self.root, default="no"):
            return
        if self.job and not messagebox.askyesno(
                "Скачивание не закончено",
                "Проект ещё скачивается. Остановить скачивание и закрыть программу?",
                parent=self.root):
            return
        if self.upd and not messagebox.askyesno(
                "Получение нового не закончено",
                "Из проекта ещё получается новое. Остановить и закрыть программу?",
                parent=self.root):
            return
        if self._create_running() and not messagebox.askyesno(
                "Проект ещё создаётся",
                "Проект ещё загружается на сервер. Остановить (загруженное будет удалено) и закрыть программу?",
                parent=self.root):
            return
        if self._new_project_running() and not messagebox.askyesno(
                "Проект ещё создаётся", self._new_project_dialog.exit_question(), parent=self.root):
            return
        if self._publish_running() and not messagebox.askyesno(
                "Проект ещё отправляется",
                "Проект ещё отправляется на сервер. Остановить и закрыть программу? Папка на компьютере "
                "останется как была.", parent=self.root):
            return
        self._closing = True
        if self.job:
            self.job.cancel.cancel()
        if self.upd:
            self.upd.cancel.cancel()
        if self._create_dialog is not None:
            self._create_dialog.stop_for_exit()
        if self._new_project_dialog is not None:
            self._new_project_dialog.stop_for_exit()
        if self._publish_dialog is not None:
            self._publish_dialog.stop_for_exit()
        if self._publish_cancel is not None:
            self._publish_cancel.cancel()
        if (self.job or self.upd or self._removing_partial or self._create_running() or self._new_project_running()
                or self._publish_running()):
            # после отмены svn ещё чистит рабочую копию: дожидаемся, иначе она останется заблокированной
            self.status_var.set("Останавливаю…")
            self.mine_status.set("Останавливаю…")
            self._close_deadline = time.monotonic() + CLOSE_WAIT_SEC
            self._wait_close()
        else:
            self._finish_close()

    def _wait_close(self) -> None:
        # создание проекта после отмены ещё откатывается на сервере: ждём его так же, как скачивание
        busy = (self.job is not None or self.upd is not None or self._removing_partial or self._create_running()
                or self._new_project_running() or self._publish_running())
        if busy and time.monotonic() < self._close_deadline:
            self.root.after(CLOSE_POLL_MS, self._wait_close)
            return
        if busy:
            log.warning("Фоновые задачи не завершились за %d с, окно закрывается принудительно", CLOSE_WAIT_SEC)
        self._finish_close()

    def _finish_close(self) -> None:
        if self._create_running():
            log.warning("Окно закрывается, а создание проекта ещё не закончило откат")
        self._probe_pool.shutdown(wait=False, cancel_futures=True)
        self.popups.close_all()
        if self._tray is not None:
            self._tray.stop()
        winutil.release_single_instance()   # новый запуск не должен упираться в закрывающуюся программу
        self.root.destroy()


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

ALREADY_RUNNING_TEXT = ("Программа уже запущена или закрывается. Подождите несколько секунд и запустите её ещё раз.")
SETTINGS_UNREADABLE_TEXT = ("Не удалось прочитать файл настроек: его, возможно, занимает другая программа. "
                            "Подождите несколько секунд и запустите программу ещё раз. Настройки не тронуты.")


def exit_if_threads_alive() -> None:
    """После закрытия окна ждать фоновые потоки (например, пул проверки папок) до минуты незачем: выходим сразу.

    Потоки-демоны завершаются вместе с процессом; не-демоны (пул потоков) интерпретатор иначе дожидался бы.
    """
    alive = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive() and not t.daemon]
    if alive:
        log.warning("Выход, не дожидаясь потоков: %s", ", ".join(t.name for t in alive))
        logging.shutdown()   # журнал сбрасываем до жёсткого выхода
        os._exit(0)


def main() -> None:
    start_in_tray = autostart.TRAY_ARG in sys.argv[1:]
    if not winutil.acquire_single_instance():
        if not tray.notify_running_instance() and not winutil.activate_existing_window():
            # окно не нашлось: первый экземпляр ещё закрывается (ждёт фоновые задачи). Молча выходить нельзя
            hidden = tk.Tk()
            hidden.withdraw()
            messagebox.showinfo(winutil.WINDOW_TITLE_PREFIX, ALREADY_RUNNING_TEXT, parent=hidden)
            hidden.destroy()
        return
    enable_dpi_awareness()
    log_path = setup_logging()
    selfupdate.apply_pending()           # svnlite.exe, который прошлое обновление не смогло подменить (был занят)
    selfupdate.record_window_version()   # по этой записи doctor в svnlite.exe видит, что версии разошлись
    try:
        store = SettingsStore(load_wait=READ_WAIT_WINDOW)
    except SettingsUnreadable:        # запуск с пустыми настройками затёр бы настоящие при первом сохранении
        hidden = tk.Tk()
        hidden.withdraw()
        messagebox.showerror(winutil.WINDOW_TITLE_PREFIX, SETTINGS_UNREADABLE_TEXT, parent=hidden)
        hidden.destroy()
        winutil.release_single_instance()
        return
    cached = load_shared_cache()  # последняя копия общего конфига: окно открывается сразу, без ожидания сети
    config = build_config(store.settings, cached)
    svn = find_svn(config.svn_path)
    log.info("Запуск SVN-lite %s, svn=%s, сервер=%s", __version__, svn, config.server)
    client = SvnClient(svn) if svn else None
    creds = effective_credentials(store.settings, config)
    if client and creds:
        client.set_credentials(*creds)
    App._refresh_autostart()      # автозапуск включён, но путь устарел (программу перенесли): исправляем сразу
    root = tk.Tk()
    if start_in_tray:
        root.withdraw()       # запуск из автозапуска: окно не показываем, пока не откроют из трея
    app = App(root, config, store, client, str(log_path))
    app.shared_data = cached
    icon = tray.Tray(app.tray_event, labels=app.tray_labels)
    if icon.start():
        app.attach_tray(icon)
    else:
        log.warning("Значок в трее не создан")
        root.deiconify()      # без значка скрытое окно не вернуть
        if start_in_tray:
            messagebox.showwarning(winutil.WINDOW_TITLE_PREFIX, messages.TRAY_UNAVAILABLE, parent=root)
    root.after(400, app.reload_shared_config)
    root.mainloop()
    exit_if_threads_alive()
