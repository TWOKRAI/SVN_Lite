"""Окно «Новый проект»: проект из шаблона на компьютере или сразу на сервере; окно «Отправить на сервер»
(Tasks T.3 и T.5 из docs/PLAN_templates.md).

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

«Отправить позже» (`PublishDialog`) вызывает `publish.publish`: уже существующая папка привязывается к серверу
на месте. Окно отказывает заранее, если папку держит TortoiseSVN (`app._path_busy`).

Сборка проекта, загрузка и журнал вынесены в `svnhelper/newproject.py` (без tkinter); здесь они импортируются под прежними
именами. Тексты без терминов SVN.
"""
from __future__ import annotations

import logging
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .. import localrepo, messages, projectmeta, publish, server, templates, theme
from .. import config as config_module
from ..config import effective_credentials
from ..dialogs import center_window, FONT_FAMILY
from ..newproject import (  # noqa: F401  (часть имён здесь не нужна, но их берут из этого модуля окно, app и тесты)
    ACTION, ACTION_PUBLISH, CREATING_SUFFIX, EXISTS_TEXT, JOB_KIND, NO_HISTORY_TEXT, NO_LINK_KEEP_TEXT, NO_LINK_KINDS,
    NO_LINK_SERVER_HINT, NO_LINK_TEXT, NO_PLACE_TEXT, NO_RIGHTS_TEXT, NOT_CONFIGURED, PUBLISH_COMMENT, PUBLISH_INFO,
    STAGE_BUILDING, STAGE_DONE, STAGE_DOWNLOAD, STAGE_UPLOADING, STORAGE_LOCAL, STORAGE_SERVER,
    CreateCancelled, CreateJob, LoadedTemplate, LocalCreateResult, ServerCreateFailed, ServerCreatedNotDownloaded,
    ServerCreateResult, _save_job, check_base_dir, create_local_project, create_project_folder, create_server_project,
    creating_path, download_project, load_template, publish_summary, recover_create_jobs, undo_text)
from ..svn_core import CancelToken, CheckoutProgress, ErrorKind, SvnError
from .models import PROGRESS_MS
from .server_tree import ServerPlaceDialog
from .widgets import Tooltip

log = logging.getLogger("svnhelper.new_project")

SERVER_CHECK_DELAY_MS = 400


# --------------------------------------------------------------------------
# Окно
# --------------------------------------------------------------------------

class _ScrollFrame(ttk.Frame):
    """Тело окна с вертикальной прокруткой: окно не выше экрана (малый экран, масштаб 125-150%), лишнее прокручивается."""

    def __init__(self, master, max_height: int):
        super().__init__(master)
        self.max_height = max_height
        self.canvas = tk.Canvas(self, highlightthickness=0, background=theme.PALETTE.surface)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.winfo_toplevel().bind("<MouseWheel>", self._on_wheel, add="+")

    def _on_inner(self, _event=None) -> None:
        height = self.inner.winfo_reqheight()
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), height), width=self.inner.winfo_reqwidth(),
                              height=min(height, self.max_height))
        if height > self.max_height:
            if not self.vbar.winfo_manager():
                self.vbar.pack(side="right", fill="y")
        else:
            self.vbar.pack_forget()

    def _on_wheel(self, event) -> None:
        if self.vbar.winfo_manager():
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


class _BackgroundRun:
    """Общее для окон: `_run` выполняет work() в фоне, окно входа на сервер открывается само и повторяет задачу."""

    def _run(self, work: Callable, on_ok: Callable, on_error: Callable) -> None:
        """work() в фоне; on_ok(результат) или on_error(ошибка) в главном потоке."""
        def done(res, err, stamp) -> None:
            if not self.winfo_exists():
                return
            if not (self.app.login.dialog_open or self.app.error_open):
                current = self.grab_current()      # окно входа или ошибки могло забрать захват себе;
                if current is None or str(current) == str(self):   # окно «Место на сервере» своё не отдаёт
                    self.grab_set()
            auth = auth_error(err)
            if err is None:
                on_ok(res)
            elif auth is not None and not self.app._closing:
                self.status_var.set("Нужен вход на сервер…")
                self.app.login.request(retry=lambda: self._run(work, on_ok, on_error),
                                       give_up=lambda: on_error(auth), job_gen=stamp)
            else:
                on_error(err)
        self.app.run_bg(work, done)


def auth_error(error: Optional[BaseException]) -> Optional[SvnError]:
    """Нужен вход на сервер: сам SvnError или отказ отправки до начала работы с папкой (повтор безопасен)."""
    if isinstance(error, SvnError) and error.kind is ErrorKind.AUTH:
        return error
    if (isinstance(error, publish.PublishError) and error.code == publish.NEED_LOGIN and error.job is None
            and isinstance(error.error, SvnError)):
        return error.error
    return None


class NewProjectDialog(_BackgroundRun, tk.Toplevel):
    """result — путь созданной папки проекта (или уже существующей, если выбрали «Открыть его»), иначе None.

    app — главное окно (run_bg, show_error, login, client, config, settings); модуль app.py не импортируется.
    """

    def __init__(self, app, base_dir: str = "", cache_dir: Optional[Path] = None,
                 jobs_dir: Optional[os.PathLike] = None):
        super().__init__(app.root, background=theme.PALETTE.surface)
        self.app = app
        self.client = app.client             # свой клиент на всё время окна: смена настроек его не подменит
        self.cache_dir = cache_dir
        self.jobs_dir = jobs_dir
        self.result: Optional[str] = None
        self.state = "loading"               # loading | ready | running | done
        self.cancel = CancelToken()
        self.template_list: list[templates.TemplateInfo] = []
        self.list_offline = False
        self.loaded: Optional[LoadedTemplate] = None
        self.plan: Optional[templates.Plan] = None
        self.field_vars: dict[str, tk.StringVar] = {}
        self.existing: str = ""              # путь уже существующего проекта с таким названием
        self._touched = False                # пользователь уже что-то менял: ошибки показываем красным
        self._gen = 0                        # номер выбора шаблона; ответы прежних выборов отбрасываются
        self._details_open = False
        self.server_segments: list[str] = []     # место на сервере: части пути, например ["konstruktor", "projects"]
        self.server_state: dict[str, Optional[tuple[bool, bool]]] = {}   # адрес проекта -> (место есть, имя занято)
        self._server_pending: set[str] = set()
        self._server_after: Optional[str] = None
        self._tick_after: Optional[str] = None
        self.retry: Optional[tuple[str, str, str]] = None   # (адрес проекта, итоговая папка, база): «Скачать ещё раз»
        self.phase = ""                          # build | upload | download: что делает сборка на сервер
        self.progress = CheckoutProgress()
        self.title("Новый проект")
        self.transient(app.root)
        px = app.px
        self.px = px

        screen_h = self.winfo_screenheight()
        self._scroll = _ScrollFrame(self, max_height=max(px(300), screen_h - 120))
        self._scroll.pack(fill="both", expand=True)
        body = self._scroll.inner
        body.configure(padding=px(16))
        body.columnconfigure(1, weight=1)
        self.minsize(px(560), min(px(420), screen_h - 120))
        self.maxsize(self.winfo_screenwidth(), screen_h - 80)
        ttk.Label(body, text="Новый проект из шаблона", font=(FONT_FAMILY, 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, px(10)))

        ttk.Label(body, text="Шаблон:").grid(row=1, column=0, sticky="w")
        self.template_combo = ttk.Combobox(body, state="disabled", width=48)
        self.template_combo.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(px(10), 0))
        self.template_combo.bind("<<ComboboxSelected>>", lambda _e: self._select_template(self.template_combo.current()))
        self.desc_var = tk.StringVar()
        ttk.Label(body, textvariable=self.desc_var, style="Hint.TLabel", wraplength=px(520), justify="left").grid(
            row=2, column=1, columnspan=2, sticky="w", padx=(px(10), 0))
        self.source_var = tk.StringVar()
        self.source_label = ttk.Label(body, textvariable=self.source_var, style="Warn.TLabel", wraplength=px(520),
                                      justify="left")
        self.source_label.grid(row=3, column=1, columnspan=2, sticky="w", padx=(px(10), 0))

        self.fields_frame = ttk.Frame(body)
        self.fields_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(px(8), 0))
        self.fields_frame.columnconfigure(1, weight=1)

        where = ttk.Frame(body)
        where.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(px(8), 0))
        where.columnconfigure(1, weight=1)
        ttk.Label(where, text="Куда создать:").grid(row=0, column=0, sticky="w")
        dirs = list(app.settings.project_dirs)
        start = (base_dir or (dirs[0] if dirs else "")).strip()
        if start and start not in dirs:
            dirs.append(start)
        self.base_var = tk.StringVar(value=start)
        self.base_combo = ttk.Combobox(where, textvariable=self.base_var, values=dirs, width=48)
        self.base_combo.grid(row=0, column=1, sticky="ew", padx=(px(10), px(6)))
        self.browse_btn = ttk.Button(where, text="Выбрать…", command=self._on_browse)
        self.browse_btn.grid(row=0, column=2)
        self.base_var.trace_add("write", lambda *_: self._on_input())

        store = ttk.LabelFrame(body, text="Где хранить проект", padding=px(8))
        store.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(px(10), 0))
        self.storage_var = tk.StringVar(value=STORAGE_LOCAL)
        self.server_radio = ttk.Radiobutton(store, text="На общем сервере: коллеги сразу увидят",
                                            variable=self.storage_var, value=STORAGE_SERVER)
        self.server_radio.grid(row=0, column=0, sticky="w")
        self.server_hint = ttk.Label(store, text="", style="Hint.TLabel", wraplength=px(500), justify="left")
        self.server_hint.grid(row=1, column=0, sticky="w", padx=(px(22), 0))
        # место на сервере: «Будет на сервере: …» и «Изменить…» (видны, пока выбран сервер)
        self.place_row = ttk.Frame(store)
        self.place_row.grid(row=2, column=0, sticky="ew", padx=(px(22), 0))
        self.place_row.columnconfigure(0, weight=1)
        self.place_var = tk.StringVar()
        ttk.Label(self.place_row, textvariable=self.place_var, wraplength=px(400), justify="left").grid(
            row=0, column=0, sticky="w")
        self.place_btn = ttk.Button(self.place_row, text="Изменить…", command=self._on_change_place)
        self.place_btn.grid(row=0, column=1, padx=(px(8), 0))
        self.local_radio = ttk.Radiobutton(store, text="Пока только на этом компьютере: отправлю позже",
                                           variable=self.storage_var, value=STORAGE_LOCAL)
        self.local_radio.grid(row=3, column=0, sticky="w", pady=(px(4), 0))
        self.local_hint = ttk.Label(store, text="", style="Hint.TLabel", wraplength=px(500), justify="left")
        self.local_hint.grid(row=4, column=0, sticky="w", padx=(px(22), 0))
        Tooltip(self.server_radio, lambda: NO_LINK_SERVER_HINT if self.list_offline else "")
        self.storage_var.trace_add("write", lambda *_: self._on_storage_changed())

        self.preview_var = tk.StringVar()
        ttk.Label(body, textvariable=self.preview_var, wraplength=px(560), justify="left",
                  font=(FONT_FAMILY, 10, "bold")).grid(row=7, column=0, columnspan=3, sticky="w", pady=(px(10), 0))
        problem_row = ttk.Frame(body)
        problem_row.grid(row=8, column=0, columnspan=3, sticky="ew")
        self.problem_label = ttk.Label(problem_row, text="", style="Error.TLabel", wraplength=px(480), justify="left")
        self.problem_label.pack(side="left", anchor="w")
        self.open_existing_btn = ttk.Button(problem_row, text="Открыть", command=self._on_open_existing)
        self.warn_label = ttk.Label(body, text="", style="Warn.TLabel", wraplength=px(560), justify="left")
        self.warn_label.grid(row=9, column=0, columnspan=3, sticky="w")

        self.details_btn = ttk.Button(body, text="Подробнее ▸", style="Ghost.TButton", command=self._toggle_details)
        self.details_btn.grid(row=10, column=0, columnspan=3, sticky="w", pady=(px(4), 0))
        self.details = ttk.Treeview(body, columns=("was", "now"), show="headings", height=6)
        self.details.heading("was", text="Было", anchor="w")
        self.details.heading("now", text="Стало", anchor="w")
        self.details.column("was", width=px(260), anchor="w")
        self.details.column("now", width=px(260), anchor="w")

        self.bar = ttk.Progressbar(body, mode="indeterminate", maximum=100)
        self.bar.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(px(10), px(4)))
        self.status_var = tk.StringVar(value="Ищу шаблоны…")
        ttk.Label(body, textvariable=self.status_var, wraplength=px(560), justify="left").grid(
            row=13, column=0, columnspan=3, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=14, column=0, columnspan=3, sticky="e", pady=(px(12), 0))
        self.create_btn = ttk.Button(buttons, text="Создать", style="Accent.TButton", command=self._on_create,
                                     state="disabled")
        self.create_btn.pack(side="left", padx=(0, px(8)))
        self.retry_btn = ttk.Button(buttons, text="Скачать ещё раз", command=self._on_retry_download)
        self.close_btn = ttk.Button(buttons, text="Отмена", command=self._on_close)
        self.close_btn.pack(side="left")
        self.bind("<Escape>", lambda _e: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_storage_defaults()
        center_window(self, parent=app.root)
        self.grab_set()
        self._start_list()

    @staticmethod
    def _error_text(error: BaseException) -> str:
        if isinstance(error, templates.ManifestError):
            return f"Шаблон настроен неверно. Покажите это администратору: {error}"
        if isinstance(error, templates.TemplateError):
            return str(error)
        if isinstance(error, SvnError):
            return messages.short_message(error.kind)
        log.error("Непредвиденная ошибка при работе с шаблоном", exc_info=error)
        return "Произошла непредвиденная ошибка. Подробности записаны в журнал программы."

    # -- список шаблонов -----------------------------------------------------------------

    def _start_list(self) -> None:
        root_url = self.app.config.templates_root.strip()
        if not root_url:
            self._show_not_configured()
            return
        self.bar.start(12)
        client, cache_dir, cancel = self.client, self.cache_dir, self.cancel

        def work() -> templates.TemplateList:
            if client is None:
                return templates.TemplateList(templates.cached_templates(cache_dir or templates.default_cache_dir()),
                                              offline=True)
            return templates.load_templates(client, root_url, cache_dir, cancel)

        self._run(work, self._on_listed, self._on_list_failed)

    def _show_not_configured(self) -> None:
        support = self.app.config.support.strip()
        self.status_var.set(NOT_CONFIGURED + (f" ({support})" if support else "") + ".")
        self.state = "ready"

    def _on_listed(self, result: templates.TemplateList) -> None:
        self.bar.stop()
        self.template_list = result.templates
        self.list_offline = result.offline
        self._update_storage_defaults()
        if not result.templates:
            if result.offline:
                self.status_var.set(templates.NO_CACHE_MESSAGE)
                self.state = "ready"
            else:
                self._show_not_configured()
            return
        self.template_combo.configure(values=[t.title for t in result.templates], state="readonly")
        self.template_combo.current(0)
        self._select_template(0)

    def _on_list_failed(self, error: BaseException) -> None:
        self.bar.stop()
        if isinstance(error, SvnError) and error.kind is ErrorKind.CANCELLED:
            return
        self.state = "ready"
        self.status_var.set(self._error_text(error))

    # -- выбор шаблона и поля ----------------------------------------------------------------

    def _select_template(self, index: int) -> None:
        if not 0 <= index < len(self.template_list):
            return
        info = self.template_list[index]
        self._gen += 1
        gen = self._gen
        self.loaded = None
        self.state = "loading"
        self.desc_var.set(info.description)
        self.source_var.set("")
        self.status_var.set("Загружаю шаблон…")
        self.bar.start(12)
        self._clear_fields()
        self._refresh()
        client, cache_dir, cancel = self.client, self.cache_dir, self.cancel
        cfg = self.app.config

        def work() -> LoadedTemplate:
            return load_template(client, info, cache_dir, cancel, list(cfg.repos), cfg.project_folder_format)

        self._run(work, lambda res: gen == self._gen and self._on_loaded(res),
                  lambda err: gen == self._gen and self._on_load_failed(err))

    def _on_loaded(self, loaded: LoadedTemplate) -> None:
        self.bar.stop()
        self.loaded = loaded
        self.state = "ready"
        self.status_var.set("")
        if self.list_offline or loaded.fetched.offline:
            when = loaded.fetched.fetched
            date_text = f" от {when:%d.%m.%Y}" if when else ""
            self.source_var.set(f"Шаблон{date_text} (нет связи: взят сохранённый на компьютере)")
        self._build_fields(loaded.manifest)
        self.server_segments = [part for part in loaded.manifest.parent.split("/") if part]
        self._touched = False
        self._refresh()

    def _on_load_failed(self, error: BaseException) -> None:
        self.bar.stop()
        if isinstance(error, SvnError) and error.kind is ErrorKind.CANCELLED:
            return
        self.state = "ready"
        self.status_var.set(self._error_text(error))
        self._refresh()

    def _clear_fields(self) -> None:
        for child in self.fields_frame.winfo_children():
            child.destroy()
        self.field_vars = {}

    def _build_fields(self, manifest: templates.Manifest) -> None:
        self._clear_fields()
        px = self.px
        author = projectmeta.default_author(self._login())
        defaults = templates.default_values(manifest, author)
        entries = []
        for row, field in enumerate(manifest.fields):
            label = (field.label or field.id) + (" *" if field.required else "")
            ttk.Label(self.fields_frame, text=label + ":").grid(row=row * 2, column=0, sticky="w", pady=(px(4), 0))
            var = tk.StringVar(value=defaults.get(field.id, ""))
            var.trace_add("write", lambda *_: self._on_input())
            entry = ttk.Entry(self.fields_frame, textvariable=var, width=50)
            entry.grid(row=row * 2, column=1, sticky="ew", padx=(px(10), 0), pady=(px(4), 0))
            if field.hint:
                ttk.Label(self.fields_frame, text=field.hint, style="Hint.TLabel").grid(
                    row=row * 2 + 1, column=1, sticky="w", padx=(px(10), 0))
            self.field_vars[field.id] = var
            entries.append((entry, var))
        target = next((e for e, v in entries if not v.get()), entries[0][0] if entries else None)
        if target is not None:
            target.focus_set()

    def _login(self) -> str:
        creds = effective_credentials(self.app.settings, self.app.config)
        return creds[0] if creds else (self.app.settings.username or self.app.config.username)

    # -- предпросмотр и проверки ---------------------------------------------------------------

    def _on_input(self) -> None:
        # «проверить не удалось» не запоминается: при следующем вводе проверим заново
        self.server_state = {url: state for url, state in self.server_state.items() if state is not None}
        self._touched = True
        self._refresh()

    def _on_browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self, initialdir=self.base_var.get() or None,
                                         title="Куда создать проект")
        if chosen:
            self.base_var.set(os.path.normpath(chosen))
        self.grab_set()

    def _update_storage_defaults(self) -> None:
        """Сервер выбран сам, когда связь есть; без связи он недоступен и выбран компьютер."""
        use_server = not self.list_offline
        self.server_radio.configure(state="normal" if use_server else "disabled")
        self.server_hint.configure(text="" if use_server else NO_LINK_SERVER_HINT)
        self.storage_var.set(STORAGE_SERVER if use_server else STORAGE_LOCAL)
        self.local_hint.configure(text=NO_LINK_TEXT if self.list_offline else "")
        self._on_storage_changed()

    def _on_storage_changed(self) -> None:
        if not hasattr(self, "preview_var"):
            return
        if self.storage_var.get() == STORAGE_SERVER:
            self.place_row.grid()
        else:
            self.place_row.grid_remove()
        self._refresh()

    def _server_selected(self) -> bool:
        return self.storage_var.get() == STORAGE_SERVER and not self.list_offline

    def _on_change_place(self) -> None:
        """«Изменить…»: выбор папки на сервере в дереве (тот же виджет, что на вкладке «Скачать проект»)."""
        if self.state != "ready":
            return
        dialog = ServerPlaceDialog(self.app, self.server_segments, parent=self)
        self.wait_window(dialog)
        if self.winfo_exists():
            self.grab_set()
            if dialog.result:
                self.server_segments = list(dialog.result)
                self._touched = True
                self._refresh()

    def _server_urls(self, folder: str) -> Optional[tuple[str, str]]:
        """(адрес места, адрес проекта) для выбранного места и имени папки; None, если места нет или имя не годится."""
        if not self.server_segments or not folder:
            return None
        try:
            name = server.validate_line_name(folder)
        except ValueError:
            return None
        parent_url = server.build_url(self.app.config.server, self.server_segments)
        return parent_url, server.build_url(parent_url, [name])

    def _schedule_server_check(self, parent_url: str, project_url: str) -> None:
        """Проверка «место есть, имя свободно» идёт после паузы ввода: не на каждую букву."""
        if project_url in self.server_state or project_url in self._server_pending:
            return
        if self._server_after is not None:
            self.after_cancel(self._server_after)
        self._server_after = self.after(SERVER_CHECK_DELAY_MS, lambda: self._start_server_check(parent_url, project_url))

    def _start_server_check(self, parent_url: str, project_url: str) -> None:
        self._server_after = None
        if project_url in self.server_state or project_url in self._server_pending or self.client is None:
            return
        self._server_pending.add(project_url)
        client = self.client

        def work() -> tuple[bool, bool]:
            if not client.url_exists(parent_url):
                return False, False
            return True, client.url_exists(project_url)

        def done(result, error, _stamp) -> None:
            self._server_pending.discard(project_url)
            if not self.winfo_exists():
                return
            if error is not None:         # нет связи, нужен вход и т.п.: не проверено, создание проверит само
                log.warning("Не удалось проверить место на сервере %s: %s", project_url, error)
                result = None
            self.server_state[project_url] = result
            self._refresh()
        self.app.run_bg(work, done)

    def _refresh(self) -> None:
        """Пересчитывает имена, предпросмотр и доступность кнопки «Создать»."""
        self.plan = None
        self.existing = ""
        problems: list[str] = []
        warnings: list[str] = []
        preview = ""
        details: list[tuple[str, str]] = []
        loaded = self.loaded
        if loaded is not None:
            base = self.base_var.get().strip().strip('"')
            values = {fid: var.get() for fid, var in self.field_vars.items()}
            plan = templates.build_plan(loaded.manifest, values, projectmeta.default_author(self._login()),
                                        template_paths=loaded.paths, base_dir=base,
                                        warn_length=self.app.config.filter.path_warn_length,
                                        staging_extra=len(".") + len(CREATING_SUFFIX))
            self.plan = plan
            problems.extend(plan.errors)
            warnings.extend(plan.warnings)
            base_problem = check_base_dir(base)
            if base_problem:
                problems.append(base_problem)
            if plan.folder and not base_problem:
                dest = os.path.join(os.path.normpath(base), plan.folder)
                preview = f"Будет создано: {dest}"
                if plan.ok and os.path.lexists(dest):
                    self.existing = dest
            details = [(loaded.info.title, plan.folder)] + [(path, new) for path, new in plan.renames]
        if self.existing:
            problems = [EXISTS_TEXT]
        place_text = ""
        if self._server_selected() and loaded is not None:
            server_problems, place_text = self._server_checks()
            problems.extend(server_problems)
        self.place_var.set(place_text)
        self.preview_var.set(preview)
        self.problem_label.configure(text="\n".join(problems),
                                     style="Error.TLabel" if self._touched else "Hint.TLabel")
        self.warn_label.configure(text="\n".join("⚠ " + w for w in warnings))
        if self.existing:
            self.open_existing_btn.pack(side="left", padx=(self.px(8), 0))
        else:
            self.open_existing_btn.pack_forget()
        self.details.delete(*self.details.get_children())
        for was, now in details:
            self.details.insert("", "end", values=(was, now))
        ok = self.state == "ready" and self.loaded is not None and self.plan is not None and not problems
        self.create_btn.configure(state="normal" if ok else "disabled")

    def _server_checks(self) -> tuple[list[str], str]:
        """Проблемы выбранного места на сервере и строка «Будет на сервере: …»."""
        plan = self.plan
        if not self.server_segments:
            return [NO_PLACE_TEXT], "Место на сервере не выбрано."
        place = "/".join(self.server_segments)
        folder = plan.folder if plan is not None else ""
        if not folder:
            return [], f"Будет на сервере: {place}"
        text = f"Будет на сервере: {place}/{folder}"
        try:
            server.validate_line_name(folder)
        except ValueError as exc:
            return [str(exc)], text
        urls = self._server_urls(folder)
        if urls is None or plan is None or not plan.ok:
            return [], text
        parent_url, project_url = urls
        if project_url not in self.server_state:
            self._schedule_server_check(parent_url, project_url)
            return [], text + " (проверяю…)"
        state = self.server_state[project_url]
        if state is None:
            return [], text
        place_exists, taken = state
        if not place_exists:
            return [publish.MESSAGES[publish.NO_PARENT]], text
        if taken:
            return [publish.MESSAGES[publish.EXISTS]], text
        return [], text

    def _toggle_details(self) -> None:
        self._details_open = not self._details_open
        if self._details_open:
            self.details.grid(row=11, column=0, columnspan=3, sticky="ew")
            self.details_btn.configure(text="Подробнее ▾")
        else:
            self.details.grid_remove()
            self.details_btn.configure(text="Подробнее ▸")

    def _on_open_existing(self) -> None:
        if self.existing and self.state == "ready":
            self.result = self.existing
            self.state = "done"
            self.cancel.cancel()
            self.destroy()

    # -- создание -----------------------------------------------------------------------

    def _set_inputs(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for entry in self.fields_frame.winfo_children():
            if isinstance(entry, ttk.Entry):
                entry.configure(state=state)
        self.base_combo.configure(state=state)
        self.browse_btn.configure(state=state)
        self.place_btn.configure(state=state)
        self.local_radio.configure(state=state)
        self.server_radio.configure(state="normal" if enabled and not self.list_offline else "disabled")
        self.template_combo.configure(state="readonly" if enabled and self.template_list else "disabled")

    def _on_create(self) -> None:
        self._refresh()
        if self.state != "ready" or self.loaded is None or self.plan is None or str(self.create_btn.cget("state")) == "disabled":
            return
        loaded, plan = self.loaded, self.plan
        base = os.path.normpath(self.base_var.get().strip().strip('"'))
        values = {fid: var.get().strip() for fid, var in self.field_vars.items()}
        meta = projectmeta.new_meta(
            values.get("name", ""), plan.folder, login=self._login(),
            template={"id": loaded.info.id, "title": loaded.manifest.title or loaded.info.title,
                      "revision": loaded.fetched.revision},
            fields=values)
        self.state = "running"
        self.cancel = CancelToken()
        self.retry = None
        self.retry_btn.pack_forget()
        self._set_inputs(False)
        self.create_btn.configure(state="disabled")
        self.close_btn.configure(text="Остановить", state="normal")
        self.status_var.set("Создаю проект…")
        self.bar.start(12)
        cancel, jobs_dir, template_dir = self.cancel, self.jobs_dir, loaded.fetched.path
        if self._server_selected():
            self._create_on_server(base, plan, template_dir, meta, loaded)
            return
        client, rules, store = self.client, self.app.config.filter, self.app.settings_store
        svnadmin = self._svnadmin()
        self._run(lambda: create_local_project(client, base, plan, template_dir, meta, cancel, jobs_dir, svnadmin,
                                               rules, store),
                  self._on_created, self._on_create_failed)

    def _svnadmin(self) -> Optional[str]:
        """svnadmin.exe рядом с svn.exe: с ним проект на компьютере получает историю."""
        client = self.client
        return config_module.find_svnadmin(getattr(client, "svn_path", "")) if client is not None else None

    def _create_on_server(self, base: str, plan: templates.Plan, template_dir: Path, meta: projectmeta.ProjectMeta,
                          loaded: LoadedTemplate) -> None:
        """Сразу на сервер: сборка, загрузка, обычное скачивание. Всё в фоне, ход виден по счётчику."""
        client, cancel, jobs_dir = self.client, self.cancel, self.jobs_dir
        parent_url = server.build_url(self.app.config.server, self.server_segments)
        rules, store, svnadmin = self.app.config.filter, self.app.settings_store, self._svnadmin()
        comment = f"Новый проект из шаблона «{loaded.manifest.title or loaded.info.title}»"
        self.progress = progress = CheckoutProgress()
        self.phase = "build"
        self.bar.configure(mode="indeterminate")

        def work() -> ServerCreateResult:
            return create_server_project(client, base, plan, template_dir, meta, parent_url, comment, rules, cancel,
                                         progress, self._set_phase, jobs_dir, svnadmin=svnadmin, store=store)

        self._run(work, self._on_server_created, self._on_create_failed)
        self._tick_after = self.after(PROGRESS_MS, self._tick_progress, progress)

    def _on_retry_download(self) -> None:
        """«Скачать ещё раз»: проект уже на сервере, повторяется только скачивание в итоговую папку."""
        if self.retry is None or self.state != "ready":
            return
        project_url, final, base = self.retry
        client, cancel, jobs_dir = self.client, CancelToken(), self.jobs_dir
        self.cancel = cancel
        self.state = "running"
        self.retry_btn.pack_forget()
        self._set_inputs(False)
        self.create_btn.configure(state="disabled")
        self.close_btn.configure(text="Остановить", state="normal")
        self.bar.start(12)
        self.progress = progress = CheckoutProgress()
        self.phase = "download"

        def work() -> ServerCreateResult:
            return download_project(client, project_url, final, base, cancel, progress, self._set_phase, jobs_dir)

        self._run(work, self._on_server_created, self._on_create_failed)
        self._tick_after = self.after(PROGRESS_MS, self._tick_progress, progress)

    def _set_phase(self, phase: str) -> None:
        self.phase = phase

    def _tick_progress(self, progress: CheckoutProgress) -> None:
        """Раз в долю секунды: что сейчас делается и сколько файлов обработано. Только чтение, svn не трогаем."""
        if self.state != "running" or progress is not self.progress or not self.winfo_exists():
            return
        count = progress.count
        counted = messages.format_number(count)
        if self.phase == "upload":
            self.status_var.set(f"Загружаю на сервер… Обработано файлов и папок: {counted}" if count
                                else "Загружаю на сервер…")
        elif self.phase == "download":
            self.status_var.set(f"Скачиваю проект на компьютер… Файлов и папок: {counted}" if count
                                else "Скачиваю проект на компьютер…")
        else:
            self.status_var.set("Собираю проект…")
        self._tick_after = self.after(PROGRESS_MS, self._tick_progress, progress)

    def _back_to_ready(self, status: str) -> None:
        self.bar.stop()
        self.state = "ready"
        self._set_inputs(True)
        self.close_btn.configure(text="Закрыть", state="normal")
        self.status_var.set(status)
        self._refresh()

    def _on_created(self, res: LocalCreateResult) -> None:
        self.bar.stop()
        log.info("Проект создан из шаблона: %s (история на компьютере: %s)", res.path, res.history)
        if res.note and not self.app._closing:
            messagebox.showinfo("Проект создан", res.note, parent=self)
        self.result = res.path
        self.state = "done"
        self.destroy()

    def _on_server_created(self, res: ServerCreateResult) -> None:
        self.bar.stop()
        log.info("Проект создан из шаблона: %s (на сервере: %s)", res.path, res.on_server)
        if res.note and not self.app._closing:
            messagebox.showinfo("Проект создан на компьютере", res.note, parent=self)
        self.result = res.path
        self.state = "done"
        self.destroy()

    def exit_question(self) -> str:
        """Что спросить при закрытии программы, пока идёт создание (текст зависит от этапа)."""
        if self.phase == "download":
            return ("Проект уже создан на сервере, скачивание остановится. "
                    "Остановить скачивание и закрыть программу?")
        return "Новый проект ещё создаётся. Остановить (созданное будет удалено) и закрыть программу?"

    def _on_create_failed(self, error: BaseException) -> None:
        if self.app._closing:                  # программа закрывается: окон с вопросами и ошибками не показываем
            log.info("Создание проекта прервано закрытием программы: %s", error)
            self._back_to_ready("")
            return
        if isinstance(error, CreateCancelled):
            undo = undo_text(error.rolled_back, error.rollback_error)
            self._back_to_ready("Создание остановлено. " + (undo or "Ничего не создано."))
            if error.rolled_back is False:
                messagebox.showwarning("Создание остановлено", "Создание проекта остановлено. " + undo, parent=self)
            return
        if isinstance(error, FileExistsError):
            self._back_to_ready("")
            return
        if isinstance(error, ServerCreateFailed):
            self._back_to_ready("Проект не создан." + ("" if error.rolled_back is False
                                                       else " На сервере ничего не осталось."))
            self.app.show_error("Проект не создан", error.text, action=ACTION, kind=error.kind, raw=error.raw)
            if self.winfo_exists():
                self.grab_set()
            return
        if isinstance(error, ServerCreatedNotDownloaded):
            log.error("Проект %s создан на сервере, но не скачан", error.project_url, exc_info=error.error)
            self.server_state[error.project_url] = (True, True)         # имя теперь занято
            self._back_to_ready("Проект создан на сервере, но не скачан.")
            if self.loaded is not None and self.plan is not None:
                self.retry = (error.project_url, os.path.join(os.path.normpath(self.base_var.get().strip().strip('"')),
                                                              self.plan.folder),
                              os.path.normpath(self.base_var.get().strip().strip('"')))
                self.retry_btn.pack(side="left", padx=(0, self.px(8)), before=self.close_btn)
            messagebox.showinfo("Проект создан на сервере", error.text, parent=self)
            return
        if isinstance(error, SvnError):                         # вход на сервер не удался или отменён
            self._back_to_ready("Проект не создан.")
            messagebox.showwarning("Проект не создан", messages.short_message(error.kind), parent=self)
            return
        if isinstance(error, templates.TemplateError):
            self._back_to_ready("Проект не создан.")
            self.app.show_error("Проект не создан", str(error), action=ACTION, raw=str(error))
        else:
            log.error("Не удалось создать проект из шаблона", exc_info=error)
            reason = error.strerror if isinstance(error, OSError) and error.strerror else ""
            self._back_to_ready("Проект не создан. Ничего лишнего не осталось.")
            self.app.show_error(
                "Проект не создан",
                "Не удалось создать папку проекта" + (f" ({reason})" if reason else "") + ". Недоделанное убрано. "
                "Проверьте, что в выбранной папке можно создавать файлы и на диске есть место.",
                action=ACTION, raw=str(error))
        if self.winfo_exists():
            self.grab_set()

    # -- закрытие ------------------------------------------------------------------------

    def _on_close(self) -> None:
        if self.state == "running":
            text = ("Остановить создание проекта? Уже созданное будет удалено." if self.phase != "download" else
                    "Проект уже создан на сервере. Остановить скачивание? Скачать его можно будет позже "
                    "на вкладке «Скачать проект» или кнопкой «Скачать ещё раз».")
            if messagebox.askyesno("Остановить создание?", text, parent=self):
                self.close_btn.configure(state="disabled")
                self.status_var.set("Останавливаю…")
                self.cancel.cancel()
            return
        self.cancel.cancel()      # загрузка шаблона ещё идёт: останавливаем
        self.destroy()

    def stop_for_exit(self) -> None:
        """Главное окно закрывается: останавливаем без вопросов."""
        self.cancel.cancel()

    def destroy(self) -> None:
        for name in ("_server_after", "_tick_after"):
            after_id = getattr(self, name, None)
            if after_id:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
                setattr(self, name, None)
        super().destroy()


# --------------------------------------------------------------------------
# «Отправить на сервер…» (отправить позже)
# --------------------------------------------------------------------------

# отказы, о которых достаточно сказать человеку: администратору отчёт не нужен
_PLAIN_REFUSALS = (publish.NO_PARENT, publish.EXISTS, publish.SVN_BUSY, publish.NO_RIGHTS, publish.NO_CONNECTION,
                   publish.CANCELLED, publish.NEED_LOGIN)


def publish_error_text(error: publish.PublishError) -> str:
    if error.code != publish.NO_RIGHTS:
        return error.message
    scaffold_left = error.create_error is not None and error.create_error.rolled_back is False
    return NO_RIGHTS_TEXT + (" " + publish.ROLLBACK_LATER_TEXT if scaffold_left else "")


def report_publish_error(app, error: publish.PublishError, folder: str, parent) -> None:
    """Сообщает о сбое отправки: понятный отказ — обычным окном, сбой — окном с отчётом для администратора."""
    text = publish_error_text(error)
    if error.code in _PLAIN_REFUSALS or error.stage == "check":
        messagebox.showwarning("Проект не отправлен", text, parent=parent)
        return
    kind = error.error.kind if isinstance(error.error, SvnError) else ErrorKind.OTHER
    raw = error.error.raw if isinstance(error.error, SvnError) else str(error.error or "")
    app.show_error("Проект не отправлен", text, action=ACTION_PUBLISH, kind=kind, raw=raw, extra={"Папка": folder})


def place_from_template(app, folder: str) -> list[str]:
    """Место на сервере из шаблона, по которому создан проект (если он есть в кэше на компьютере). Фоновый поток."""
    meta = projectmeta.read(folder)
    template_id = (meta.template or {}).get("id") if meta else None
    if not template_id:
        return []
    try:
        fetched = templates.cached_template(templates.default_cache_dir(), str(template_id))
        if fetched is None:
            return []
        manifest = templates.load_manifest(fetched.path, list(app.config.repos), app.config.project_folder_format,
                                           str((meta.template or {}).get("title", "")))
    except (templates.TemplateError, OSError, ValueError):
        log.warning("Место на сервере из шаблона %s не прочитано", template_id, exc_info=True)
        return []
    return [part for part in manifest.parent.split("/") if part]


class PublishDialog(_BackgroundRun, tk.Toplevel):
    """Окно «Отправить на сервер»: место, название и комментарий; отправка идёт в `publish.publish`.

    result — PublishResult после успешной отправки, иначе None. app — главное окно (run_bg, show_error, login,
    client, config, _path_busy).
    """

    def __init__(self, app, folder: str, jobs_dir: Optional[os.PathLike] = None):
        super().__init__(app.root, background=theme.PALETTE.surface)
        self.app = app
        self.client = app.client
        self.folder = os.path.normpath(folder)
        self.jobs_dir = jobs_dir
        self.result: Optional[publish.PublishResult] = None
        self.state = "ready"                 # ready | running | done
        self.cancel = CancelToken()
        self.progress = CheckoutProgress()
        self.segments: list[str] = []
        self.pending_job: Optional[publish.PublishJob] = None   # проект уже на сервере: можно завершить отправку
        self._tick_after: Optional[str] = None
        self.title("Отправить на сервер")
        self.transient(app.root)
        px = app.px
        self.px = px

        body = ttk.Frame(self, padding=px(16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Отправить проект на сервер", font=(FONT_FAMILY, 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, px(6)))
        ttk.Label(body, text=f"Папка: {self.folder}", wraplength=px(520), justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text=PUBLISH_INFO, style="Hint.TLabel", wraplength=px(520), justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(px(4), px(8)))

        ttk.Label(body, text="Название на сервере:").grid(row=3, column=0, sticky="w")
        self.name_var = tk.StringVar(value=os.path.basename(self.folder))
        self.name_entry = ttk.Entry(body, textvariable=self.name_var, width=50)
        self.name_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(px(10), 0))
        self.place_var = tk.StringVar()
        ttk.Label(body, textvariable=self.place_var, wraplength=px(430), justify="left",
                  font=(FONT_FAMILY, 10, "bold")).grid(row=4, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
        self.place_btn = ttk.Button(body, text="Изменить…", command=self._on_change_place)
        self.place_btn.grid(row=4, column=2, sticky="e", pady=(px(8), 0))
        ttk.Label(body, text="Комментарий:").grid(row=5, column=0, sticky="w", pady=(px(8), 0))
        self.comment_var = tk.StringVar(value=PUBLISH_COMMENT)
        self.comment_entry = ttk.Entry(body, textvariable=self.comment_var, width=50)
        self.comment_entry.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(px(10), 0), pady=(px(8), 0))
        self.problem_label = ttk.Label(body, text="", style="Error.TLabel", wraplength=px(520), justify="left")
        self.problem_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(px(6), 0))

        self.bar = ttk.Progressbar(body, mode="indeterminate", maximum=100)
        self.bar.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(px(10), px(4)))
        self.status_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.status_var, wraplength=px(520), justify="left").grid(
            row=8, column=0, columnspan=3, sticky="w")
        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=3, sticky="e", pady=(px(12), 0))
        self.send_btn = ttk.Button(buttons, text="Отправить", style="Accent.TButton", command=self._on_send,
                                   state="disabled")
        self.send_btn.pack(side="left", padx=(0, px(8)))
        self.resume_btn = ttk.Button(buttons, text="Завершить отправку", command=self._on_resume)
        self.close_btn = ttk.Button(buttons, text="Отмена", command=self._on_close)
        self.close_btn.pack(side="left")
        for var in (self.name_var, self.comment_var):
            var.trace_add("write", lambda *_: self._refresh())
        self.bind("<Escape>", lambda _e: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh()
        center_window(self, parent=app.root)
        self.grab_set()
        self._run(lambda: place_from_template(app, self.folder), self._on_place_found, lambda _e: None)

    # -- ввод ----------------------------------------------------------------------------

    def _on_place_found(self, segments: list[str]) -> None:
        if segments and not self.segments:
            self.segments = list(segments)
            self._refresh()

    def _on_change_place(self) -> None:
        if self.state != "ready":
            return
        dialog = ServerPlaceDialog(self.app, self.segments, parent=self)
        self.wait_window(dialog)
        if self.winfo_exists():
            self.grab_set()
            if dialog.result:
                self.segments = list(dialog.result)
                self._refresh()

    def _problem(self) -> str:
        if not self.segments:
            return NO_PLACE_TEXT
        try:
            server.validate_line_name(self.name_var.get())
        except ValueError as exc:
            return str(exc)
        return ""

    def _refresh(self) -> None:
        name = self.name_var.get().strip()
        self.place_var.set("Будет на сервере: " + "/".join([*self.segments, name]) if self.segments
                           else "Место на сервере не выбрано.")
        problem = self._problem()
        self.problem_label.configure(text=problem)
        ready = self.state == "ready" and not problem and self.client is not None
        self.send_btn.configure(state="normal" if ready else "disabled")

    def _set_inputs(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.name_entry.configure(state=state)
        self.comment_entry.configure(state=state)
        self.place_btn.configure(state=state)

    # -- отправка ------------------------------------------------------------------------

    def _on_send(self) -> None:
        self._refresh()
        if self.state != "ready" or str(self.send_btn.cget("state")) == "disabled":
            return
        if self.app._path_busy(self.folder):                  # открыто окно TortoiseSVN или идёт другая работа
            messagebox.showinfo("Папка занята", publish.MESSAGES[publish.SVN_BUSY], parent=self)
            return
        client, folder, jobs_dir = self.client, self.folder, self.jobs_dir
        name = server.validate_line_name(self.name_var.get())
        parent_url = server.build_url(self.app.config.server, self.segments)
        comment = self.comment_var.get().strip() or PUBLISH_COMMENT
        rules = localrepo.local_rules(self.app.config.filter)     # .git и подобное в папке - не несохранённые правки
        self._start_running("Отправляю проект на сервер…")
        cancel, progress = self.cancel, self.progress
        self._run(lambda: publish.publish(client, folder, parent_url, comment, name, rules, progress, cancel, jobs_dir),
                  self._on_published, self._on_publish_failed)

    def _save_and_send(self) -> None:
        """«Сохранить у себя и отправить» одним шагом: правки сохраняются в историю на компьютере, затем отправка."""
        client, folder, rules = self.client, self.folder, self.app.config.filter
        self._start_running(localrepo.SAVING_TEXT)
        cancel = self.cancel
        self._run(lambda: localrepo.save(client, folder, rules, "", cancel), self._on_saved_then_send,
                  self._on_publish_failed)

    def _on_saved_then_send(self, _saved) -> None:
        self._back_to_ready("")
        if not self.app._closing:
            self._on_send()

    def _on_resume(self) -> None:
        job = self.pending_job
        if job is None or self.state != "ready":
            return
        client, rules = self.client, self.app.config.filter
        self._start_running("Завершаю отправку…")
        cancel, progress = self.cancel, self.progress
        self._run(lambda: publish.resume_publish(client, job, rules, progress, cancel),
                  self._on_published, self._on_publish_failed)

    def _start_running(self, status: str) -> None:
        self.state = "running"
        self.cancel = CancelToken()
        self.progress = CheckoutProgress()
        self.pending_job = None
        self.resume_btn.pack_forget()
        self._set_inputs(False)
        self.send_btn.configure(state="disabled")
        self.close_btn.configure(text="Остановить", state="normal")
        self.status_var.set(status)
        self.bar.start(12)
        self._tick_after = self.after(PROGRESS_MS, self._tick_progress, self.progress, status)

    def _tick_progress(self, progress: CheckoutProgress, status: str) -> None:
        if self.state != "running" or progress is not self.progress or not self.winfo_exists():
            return
        count = progress.count
        self.status_var.set(f"{status} Обработано файлов и папок: {messages.format_number(count)}" if count else status)
        self._tick_after = self.after(PROGRESS_MS, self._tick_progress, progress, status)

    def _back_to_ready(self, status: str) -> None:
        self.bar.stop()
        self.state = "ready"
        self._set_inputs(True)
        self.close_btn.configure(text="Закрыть", state="normal")
        self.status_var.set(status)
        self._refresh()

    def _on_published(self, result: publish.PublishResult) -> None:
        self.bar.stop()
        log.info("Проект %s отправлен на сервер: %s", self.folder, result.project_url)
        self.result = result
        self.state = "done"
        if not self.app._closing:
            messagebox.showinfo("Проект отправлен", publish_summary(result), parent=self)
        self.destroy()

    def exit_question(self) -> str:
        return "Проект ещё отправляется на сервер. Остановить и закрыть программу? Папка на компьютере останется как была."

    def _on_publish_failed(self, error: BaseException) -> None:
        if self.app._closing:                  # программа закрывается: окон с вопросами и ошибками не показываем
            log.info("Отправка проекта прервана закрытием программы: %s", error)
            self._back_to_ready("")
            return
        if not isinstance(error, publish.PublishError):
            if isinstance(error, SvnError):                    # вход на сервер не удался или отменён
                self._back_to_ready("Проект не отправлен.")
                messagebox.showwarning("Проект не отправлен", messages.short_message(error.kind), parent=self)
                return
            log.error("Непредвиденная ошибка при отправке проекта", exc_info=error)
            self._back_to_ready("Проект не отправлен.")
            self.app.show_error("Проект не отправлен", "Произошла непредвиденная ошибка. Подробности записаны "
                                "в журнал программы.", action=ACTION_PUBLISH, raw=str(error))
            return
        text = publish_error_text(error)
        self.pending_job = error.job if error.job is not None and error.code != publish.UNFINISHED else None
        if error.code == publish.LOCAL_CHANGES:            # предложить сохранить у себя и сразу отправить
            self._back_to_ready("Проект не отправлен.")
            if messagebox.askyesno("Проект не отправлен", text + "\n\n" + localrepo.SAVE_NOW_ASK, parent=self):
                self._save_and_send()
            elif self.winfo_exists():
                self.grab_set()
            return
        if error.code == publish.CANCELLED:
            self._back_to_ready(text)
        else:
            self._back_to_ready("Проект не отправлен.")
            report_publish_error(self.app, error, self.folder, self)
        if self.pending_job is not None:
            self.resume_btn.pack(side="left", padx=(0, self.px(8)), before=self.close_btn)
        if self.winfo_exists():
            self.grab_set()

    # -- закрытие ------------------------------------------------------------------------

    def _on_close(self) -> None:
        if self.state == "running":
            if messagebox.askyesno("Остановить отправку?", "Остановить отправку проекта? Папка на компьютере "
                                   "останется как была.", parent=self):
                self.close_btn.configure(state="disabled")
                self.status_var.set("Останавливаю…")
                self.cancel.cancel()
            return
        self.destroy()

    def stop_for_exit(self) -> None:
        """Главное окно закрывается: останавливаем без вопросов."""
        self.cancel.cancel()

    def destroy(self) -> None:
        after_id = getattr(self, "_tick_after", None)
        if after_id:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
            self._tick_after = None
        super().destroy()
