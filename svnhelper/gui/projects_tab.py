"""Вкладка «Мои проекты»: проекты, их состояние, получение нового, сохранение на сервер, занятые файлы."""
from __future__ import annotations

import os
import re
import threading
import time
import tkinter as tk
from dataclasses import replace
from datetime import datetime
from urllib.parse import unquote
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .. import __version__, audit, localrepo, messages, mirrors, next_step, publish, server, theme, tortoise
from ..config import add_project_dir, is_newer_version, same_dir
from ..dialogs import ConflictDialog, FONT_FAMILY
from ..svn_core import CommitResult, ErrorKind, SvnClient, SvnError, WcStatus
from .commit_panel import CommitPanel
from .new_project_dialog import NewProjectDialog, PublishDialog
from .project_panel import ProjectPanel, PROJECT_TABS
from .models import (AUTO_CHECK_SEC, JobStamp, REFRESH_SEC, line_cell, lock_holder_text, LOCK_WARN_DAYS, LockResult, log,
                     MineItem, PROGRESS_MS, shorten_path, UpdateJob)
from . import hints
from .widgets import ButtonRow, CollapsibleColumn, PickDialog, SideButtons, Tooltip, ViewToggle


# Кнопка next_step (B_*) -> главное действие окна. Кнопки входа и папки проектов main_button не выбирает (это дела
# окна целиком, а не проекта). Файл изменён, но не занят - подсвечена «Занять файл…» на вкладке «Занятые» ("lock").
MAIN_ACTIONS = {
    next_step.B_RESOLVE: "resolve",      # «Разобрать конфликт» (особая кнопка слева)
    next_step.B_REPAIR: "repair",        # «Починить папку» (особая кнопка слева)
    next_step.B_RESTORE: "restore",      # «Восстановить историю» (особая кнопка слева)
    next_step.B_UPDATE: "update",        # «Получить новое» (слева)
    next_step.B_PUBLISH: "publish",      # «Отправить на сервер…» (слева)
    next_step.B_COMMIT: "commit",        # сохранение справа, на «Изменениях»
    next_step.B_LOCK: "lock",            # «Занять файл…» справа, на «Занятых»
    next_step.B_LOGIN: None,
    next_step.B_CHOOSE_FOLDER: None,
}


def main_action(state: Optional[str], status: Optional[WcStatus], read_only: bool = False,
                local: bool = False, history_missing: bool = False, history_found: bool = False) -> Optional[str]:
    """Главное действие по состоянию проекта: "resolve" | "repair" | "commit" | "lock" | "update" | "publish" |
    "restore" | None (проект не выбран или подсвечивать нечего).

    Обёртка над `next_step.main_button` (docs/PLAN_ui_v4.md, раздел 6): подсветка, строка «Что сделать» и
    `next_action` помощника считаются одной функцией. state - MineItem.state ("svn" | "plain" | "broken") или None.
    Порядок: конфликт, повреждённая папка, незаконченный переход, новое на сервере (и при своих правках: сначала
    «Получить новое»), свои правки (файл изменён, но не занят - "lock"). Делать нечего или проверка ещё не пришла - «Получить новое». Версия (read_only)
    правки не принимает. Обычная папка - "publish". Проект с историей на компьютере (local): история пропала -
    "restore", если она найдена на диске, иначе "publish"; есть правки (или проверка ещё не пришла) - "commit"
    («Сохранить у себя»); папка чистая - "publish". «Продолжить без истории» главным не бывает: это второе действие
    с подтверждением.
    """
    button = next_step.main_button(next_step.Facts(state=state, status=status, read_only=read_only, local=local,
                                                    history_missing=history_missing, history_found=history_found))
    return MAIN_ACTIONS[button] if button is not None else None


class ProjectsTabMixin:
    """Вкладка «Мои проекты»: проекты, их состояние, получение нового, сохранение на сервер, занятые файлы."""

    # -- вкладка «Мои проекты» ---------------------------------------------------------

    # Раскладка окна (self.mine_tab, лежит прямо в окне; вкладок окна нет) - три колонки (docs/PLAN_v3_ui.md,
    # «Целевой вид»; docs/PLAN_ui_v4.md, раздел 3, вариант A):
    #   1. self.actions_row   - кнопки по выбранному проекту, фиксированной ширины; прячется и показывается вместе
    #                           с колонкой «Проекты» (_layout_mine_columns), на подвкладке «Скачать проект» скрыта;
    #   2. self.mine_col2     - CollapsibleColumn «Проекты» с подвкладками self.list_tabs (_list_mode):
    #                           «Мои проекты» - папка для проектов, поиск, таблица mine_tree (или пустое состояние);
    #                           «Скачать проект» - поиск и дерево сервера (DownloadTabMixin._build_server_browser);
    #   3. self.mine_col3     - CollapsibleColumn «Состояние проекта»: ProjectPanel (self.project_panel) - шапка и
    #                           внутренние вкладки self.project_tabs (рамки в self.project_tab_frames, открывает
    #                           open_project_tab): Изменения (CommitPanel), Новое, История, Занятые, Ветки.
    #                           Справа в панели - вертикальная колонка кнопок выбранной вкладки (ProjectPanel.side),
    #                           она прячется и показывается вместе с панелью.
    #                           На «Скачать проект» в той же ячейке вместо панели форма «Скачивание»
    #                           (DownloadTabMixin._build_download_form) со своей колонкой кнопок.
    # Колонка 3 свёрнута - колонка 2 занимает её место (вес 1). Свёрнутость хранится в settings.

    MINE_COL2_WIDTH = 260     # ширина колонки «Проекты», пока колонка 3 развёрнута (px до масштаба экрана)
    MINE_COL2_DOWNLOAD_WIDTH = 340    # она же на «Скачать проект»: в 260 px дерево сервера обрезает длинные имена
    # подвкладки колонки «Проекты»: ключ для select_list_mode → подпись на ярлыке
    LIST_MODES = (("mine", "Мои проекты"), ("download", "Скачать проект"))
    MINE_EMPTY_TEXT = "В этой папке пока нет проектов."
    # неразрывные пробелы: название кнопки не разрывается переносом строки
    MINE_EMPTY_HINT = "Скачайте проект с сервера или создайте новый кнопкой «＋\u00a0Новый\u00a0проект…»."
    MINE_EMPTY_BUTTON = "Скачать проект с сервера"
    # папки нет (отключился сетевой диск, папку переименовали): скачивать рано, нужна другая папка
    MINE_MISSING_TEXT = "Папка «{path}» не найдена."
    MINE_MISSING_HINT = "Проверьте, подключён ли сетевой диск, или выберите другую папку."
    MINE_MISSING_BUTTON = "Выбрать…"
    MINE_COL3_WIDTH = 360     # запрошенная ширина колонки 3 (дальше растягивается весом)
    MINE_STRIP_WIDTH = 28     # ширина свёрнутой колонки
    MINE_SASH_WIDTH = 8       # разделитель колонок 2 и 3 (он же промежуток между ними)
    # внутренние вкладки колонки 3: ключ для open_project_tab → подпись на ярлыке
    PROJECT_TABS = PROJECT_TABS
    HISTORY_LIMIT = 50        # сколько последних сохранений показывает вкладка «История»
    # столбцы mine_tree: узкий вид - один, широкий - пять; щелчок по заголовку сортирует (столбец → режим сортировки)
    MINE_NARROW_COLUMNS = ("brief",)
    MINE_WIDE_COLUMNS = ("name", "status", "line", "changed", "todo")
    MINE_SORT_HEADINGS = {"name": "name", "changed": "changed"}
    MINE_HEADINGS = {"name": "Проект", "changed": "Изменён"}
    MINE_SORT_MODES = ("name", "opened", "created", "changed")   # прежние значения settings.mine_sort остаются рабочими
    PROJECT_DIR_REMOVE = "Убрать эту папку из списка"     # последний пункт выпадающего списка папок
    # двойной щелчок по проекту разворачивает «Состояние проекта» на вкладке главного действия (main_action);
    # у остальных действий (починить, отправить, восстановить) вкладки своей нет: остаётся открытая
    MAIN_ACTION_TAB = {"commit": "changes", "resolve": "changes", "update": "incoming", "lock": "locks"}
    _auto_tab_key = ""        # проект, для которого решение «открыть Изменения» ещё не принято (ждём проверку)
    _folder_missing_path = ""   # папка проектов не найдена (отключён сетевой диск): её путь, иначе пусто
    _hint_other_iid = ""        # строка таблицы проекта, о котором говорит подсказка «В проекте «…»: …»

    def _build_projects_tab(self, parent: tk.Misc) -> None:
        """Строит рабочую область окна self.mine_tab в parent; разместить её (pack) должен вызывающий."""
        px = self.px
        p = self.palette
        self.mine_tab = tab = ttk.Frame(parent, style="Card.TFrame", padding=(px(16), px(12)))
        tab.rowconfigure(1, weight=1)

        # предупреждение о давно занятых файлах (скрыто, пока нечего напоминать)
        self.lock_warn = tk.Frame(tab, background=p.warn_bg, padx=px(10), pady=px(6), highlightthickness=1,
                                  highlightbackground=p.warn_border, highlightcolor=p.warn_border)
        self.lock_warn.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, px(8)))
        self.lock_warn_var = tk.StringVar()
        warn_label = tk.Label(self.lock_warn, textvariable=self.lock_warn_var, background=p.warn_bg,
                              foreground=p.text, anchor="w", justify="left", wraplength=px(560),
                              font=(FONT_FAMILY, 10))
        warn_label.pack(side="left", fill="x", expand=True)
        self._autowrap(warn_label, self.lock_warn, px(130))
        ttk.Button(self.lock_warn, text="Показать", command=self._show_old_locks).pack(side="right", padx=(px(8), 0))
        self.lock_warn.grid_remove()

        # незаконченная отправка проекта на сервер (скрыто, пока нечего завершать); кнопки обрабатывает App
        self.publish_warn = tk.Frame(tab, background=p.warn_bg, padx=px(10), pady=px(6), highlightthickness=1,
                                     highlightbackground=p.warn_border, highlightcolor=p.warn_border)
        self.publish_warn.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(px(8), 0))
        self.publish_warn_var = tk.StringVar()
        publish_label = tk.Label(self.publish_warn, textvariable=self.publish_warn_var, background=p.warn_bg,
                                 foreground=p.text, anchor="w", justify="left", wraplength=px(560),
                                 font=(FONT_FAMILY, 10))
        publish_label.pack(side="left", fill="x", expand=True)
        self._autowrap(publish_label, self.publish_warn, px(330))
        self.publish_discard_btn = ttk.Button(self.publish_warn, text="Убрать недоделанное",
                                              command=self._on_discard_publish)
        self.publish_discard_btn.pack(side="right", padx=(px(8), 0))
        self.publish_finish_btn = ttk.Button(self.publish_warn, text="Завершить отправку",
                                             command=self._on_finish_publish)
        self.publish_finish_btn.pack(side="right", padx=(px(8), 0))
        self.publish_note_btn = ttk.Button(self.publish_warn, text="Понятно", command=self._on_dismiss_created_notes)
        self.publish_warn.grid_remove()

        # колонка 1: кнопки по частоте использования; главное действие по состоянию проекта - Accent.TButton
        # (_update_mine_buttons). Относится к панели «Проекты»: сворачивается и разворачивается вместе с ней.
        # Кнопки со значками; вид «только значки» переключает стрелка вверху колонки или меню «Вид → Кнопки»
        self.actions_row = row = SideButtons(tab, px(6), px=px, side="left",
                                             compact=self.settings.buttons_compact_left)
        row.on_toggle = lambda: self.set_buttons_compact("left", not self.actions_row.compact)
        row.grid(row=1, column=0, sticky="nw", padx=(0, px(12)))
        # имя выбранного проекта не дублируется над кнопками: оно в шапке колонки «Состояние проекта»
        self.check_btn = ttk.Button(row, text="⟳", command=self._on_refresh_tab)      # пояснение - в панели подсказок
        self.special_btn = ttk.Button(row, text="", state="disabled")  # «Починить папку» / «Разобрать конфликт»
        self.update_btn = ttk.Button(row, text="Получить новое", command=self._on_update_selected, state="disabled")
        self.publish_btn = ttk.Button(row, text=localrepo.PUBLISH_BUTTON, command=self._on_publish_project,
                                      state="disabled")       # у проекта без сервера: с историей на компьютере или без неё
        self.continue_btn = ttk.Button(row, text=localrepo.CONTINUE_BUTTON, command=self._on_continue_without_history)
        self.open_btn = ttk.Button(row, text="Открыть папку", command=self._on_open_mine, state="disabled")
        self.server_btn = ttk.Button(row, text="Открыть на сервере", command=self._on_open_server, state="disabled")
        self.more_menu = tk.Menu(tab, tearoff=False)
        self.expert_menu = tk.Menu(self.more_menu, tearoff=False)      # подменю «Для опытных»
        self.more_btn = self._menu_button(row, "Ещё ▾", self.more_menu)
        self.more_btn.configure(state="disabled")
        # «Новый проект» стоит в колонке 1, а не в колонке 2: там при любом масштабе экрана ей не хватало места.
        # Обычного стиля: подсвечена на экране только одна кнопка - главное действие (_update_mine_buttons)
        self.new_project_btn = ttk.Button(row, text="＋ Новый проект…", style="TButton", command=self._on_new_project)
        # порядок сверху вниз: действия над выбранным проектом целиком по частоте, потом «Открыть…» и «Ещё»;
        # сохранение, занятые файлы и ветки - справа, на вкладках «Состояния проекта»
        # hint - пояснение в панели подсказок внизу окна (hints.HINTS); у особой кнопки ключ меняется вместе с ней
        row.add(self.check_btn, hint="project.check")
        row.add(self.special_btn, visible=False, hint="project.resolve")
        row.add(self.update_btn, hint="project.get_new")
        row.add(self.publish_btn, visible=False, hint="project.publish")
        row.add(self.continue_btn, visible=False, hint="project.continue")    # второе действие при потерянной истории
        row.add(self.open_btn, separator=True, hint="project.open")
        row.add(self.server_btn, hint="project.server")
        row.add(self.more_btn, hint="project.more")
        row.add(self.new_project_btn, separator=True, hint="project.new")
        self._more_idx: dict[str, int] = {}
        self._expert_idx: dict[str, int] = {}

        # колонки 2 и 3
        self.mine_col2 = CollapsibleColumn(tab, "Проекты", side="left", width=px(self._col2_width("mine")),
                                           strip_width=px(self.MINE_STRIP_WIDTH),
                                           collapsed=self.settings.mine_col2_collapsed)
        self.mine_col3 = CollapsibleColumn(tab, "Состояние проекта", side="right", width=px(self.MINE_COL3_WIDTH),
                                           strip_width=px(self.MINE_STRIP_WIDTH), collapsed_title="Состояние",
                                           collapsed=self.settings.mine_col3_collapsed)
        for column in (self.mine_col2, self.mine_col3):
            column.bind(CollapsibleColumn.EVENT, self._on_mine_column_toggled)
        # вид кнопок справа («Состояние проекта» и форма «Скачивание»): стрелка в заголовке колонки, а не над кнопками -
        # так она не отнимает строку у шести кнопок «Веток» в самом низком окне
        self.buttons_right_toggle = ViewToggle(
            self.mine_col3.header, px, "right", compact=self.settings.buttons_compact_right,
            command=lambda: self.set_buttons_compact("right", not self.settings.buttons_compact_right))
        self.mine_col3.add_header_widget(self.buttons_right_toggle)
        # форма скачивания первой: в ней папка для проектов (dir_var), её берёт и список своих проектов
        self._build_download_form(self.mine_col3.body)
        if not self.settings.project_dirs and self.dir_var.get().strip():   # первый запуск: папка по умолчанию
            self.settings.project_dirs = add_project_dir([], self.dir_var.get())
        self._build_list_tabs(self.mine_col2.body)
        self._build_mine_state_column(self.mine_col3.body)
        self.download_form.grid(row=0, column=0, sticky="nsew")     # та же ячейка, что у ProjectPanel
        self.download_form.grid_remove()
        self._build_mine_sash(tab)
        self._layout_mine_columns()

        # ход и итог операций с проектами (получение нового, сохранение, занятие файлов, окна TortoiseSVN): пока
        # операция идёт - справа в панели подсказок, после неё - итог слева (_on_mine_status)
        self.mine_status = tk.StringVar()
        self._mine_status_kind = ""            # "error" - следующая запись в mine_status - совет о сбое (_status_error)
        self.mine_status.trace_add("write", lambda *_: self._on_mine_status())
        self._mine_hint("Выберите проект в списке, чтобы увидеть подробности.")
        self.dir_var.trace_add("write", lambda *_: self._on_dir_var_changed())
        self._apply_tortoise()

    def _build_list_tabs(self, body: ttk.Frame) -> None:
        """Колонка 2: подвкладки «Мои проекты» (свои проекты) и «Скачать проект» (дерево сервера)."""
        px = self.px
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.list_tabs = tabs = ttk.Notebook(body, style="Inner.TNotebook")
        tabs.grid(row=0, column=0, sticky="nsew")
        self.list_frames: dict[str, ttk.Frame] = {}
        for key, title in self.LIST_MODES:
            frame = ttk.Frame(tabs, padding=(px(6), px(8), px(6), px(6)))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            tabs.add(frame, text=title)
            self.list_frames[key] = frame
        hints.hint_tabs(tabs, [f"tab.{key}" for key, _title in self.LIST_MODES])
        mine = ttk.Frame(self.list_frames["mine"])
        mine.grid(row=0, column=0, sticky="nsew")
        self._build_mine_projects_column(mine)
        self._build_server_browser(self.list_frames["download"])
        self.server_browser.grid(row=0, column=0, sticky="nsew")
        # первое событие придёт само (ярлык выбран при добавлении): с него начинается поиск проектов при запуске
        tabs.bind("<<NotebookTabChanged>>", self._on_list_tab_changed)

    def _build_mine_projects_column(self, body: ttk.Frame) -> None:
        """Подвкладка «Мои проекты»: папка для проектов (список сохранённых папок), поиск и таблица проектов;
        если в папке проектов нет, вместо поиска и таблицы - пустое состояние с путём к скачиванию."""
        px = self.px
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        folder = ttk.Frame(body)
        folder.grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
        folder.columnconfigure(1, weight=1)
        ttk.Label(folder, text="Папка:").grid(row=0, column=0, sticky="w", padx=(0, px(6)))
        self.mine_dir_var = tk.StringVar(value=self.dir_var.get())
        self.mine_dir_combo = ttk.Combobox(folder, textvariable=self.mine_dir_var, state="readonly")
        self.mine_dir_combo.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.mine_dir_combo.bind("<<ComboboxSelected>>", self._on_mine_dir_selected)
        Tooltip(self.mine_dir_combo, lambda: self.dir_var.get().strip())    # длинный путь целиком (данные)
        hints.hint(self.mine_dir_combo, "mine.folder")
        buttons = ButtonRow(folder, px(4), px(4))       # не помещаются в ширину колонки - вторая строка, а не обрезка
        buttons.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(px(4), 0))
        self.mine_browse_btn = ttk.Button(buttons, text="Выбрать…", command=self._on_browse_mine)
        self.rescan_btn = ttk.Button(buttons, text="Найти проекты", style="Ghost.TButton", command=self._scan_projects)
        buttons.add(self.mine_browse_btn, hint="mine.browse")
        buttons.add(self.rescan_btn, hint="mine.rescan")
        self._fill_project_dir_lists()

        # поиск по названию проекта (без учёта регистра); порядок задаёт щелчок по заголовку в широком виде
        self.mine_find_row = find_row = ttk.Frame(body)
        find_row.grid(row=1, column=0, sticky="ew", pady=(0, px(6)))
        find_row.columnconfigure(1, weight=1)
        ttk.Label(find_row, text="Найти проект:").grid(row=0, column=0, sticky="w", padx=(0, px(6)))
        self.mine_filter_var = tk.StringVar()
        self.mine_filter_entry = ttk.Entry(find_row, textvariable=self.mine_filter_var)
        self.mine_filter_entry.grid(row=0, column=1, sticky="ew")
        hints.hint(self.mine_filter_entry, "mine.filter")
        self.mine_filter_var.trace_add("write", lambda *_: self._apply_mine_sort())

        self.mine_table = frame = ttk.Frame(body, style="Card.TFrame", padding=1)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._build_mine_empty(body)
        # "brief" - узкий вид: название и под ним строка состояния; остальные столбцы - в широком виде и в подробностях
        self.mine_tree = ttk.Treeview(frame, columns=("name", "line", "status", "todo", "desc", "by", "created", "mark", "path",
                                               "opened", "changed", "brief"),
                                      displaycolumns=self.MINE_NARROW_COLUMNS,
                                      show="headings", selectmode="browse", height=5)
        self._mine_wide: Optional[bool] = None   # какой вид сейчас показан (None - ещё не выбран)
        # растягивается «Описание» (комментарий к ветке или версии); полный текст подсказок виден в шапке колонки 3
        for col, title, width, minwidth in (("name", "Проект", 85, 70), ("line", "Ветка", 85, 70),
                                            ("by", "Создал", 55, 50), ("created", "Создана", 70, 60),
                                            ("status", "Состояние", 100, 90), ("todo", "Что сделать", 105, 80),
                                            ("desc", "Описание", 55, 50), ("mark", "", 26, 26),
                                            ("path", "Путь на сервере", 100, 60), ("opened", "Открыт", 80, 70),
                                            ("changed", "Изменён", 80, 70), ("brief", "Проект", 100, 60)):
            self.mine_tree.heading(col, text=title, anchor="w")
            self.mine_tree.column(col, width=px(width), minwidth=px(minwidth), anchor="w",
                                  stretch=col in ("desc", "brief"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.mine_tree.yview)
        self.mine_tree.configure(yscrollcommand=scroll.set)
        self.mine_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self._table_extras(self.mine_tree, frame)
        self.mine_tree._col_state.pop("brief", None)   # type: ignore[attr-defined]  # узкий столбец не подгоняется по тексту
        theme.configure_tree_tags(self.mine_tree)
        self.mine_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_mine_select())
        self.mine_tree.bind("<Double-1>", self._on_mine_double_click)
        hints.hint_rows(self.mine_tree, self._mine_row_hint, "mine.table")

    def _build_mine_empty(self, body: ttk.Frame) -> None:
        """Пустое состояние «Мои проекты»: в папке нет проектов - текст и одна подсвеченная кнопка к скачиванию.
        Лежит на месте поиска и таблицы (ряды 1-2), видно только после поиска, не нашедшего ни одного проекта."""
        px = self.px
        self.mine_empty = empty = ttk.Frame(body, style="Card.TFrame", padding=(px(12), px(12)))
        empty.grid(row=1, column=0, rowspan=2, sticky="nsew")
        empty.columnconfigure(0, weight=1)
        self.mine_empty_label = ttk.Label(empty, text=self.MINE_EMPTY_TEXT, style="Section.TLabel", justify="left")
        self.mine_empty_label.grid(row=0, column=0, sticky="w")
        self.mine_empty_hint = hint = ttk.Label(empty, text=self.MINE_EMPTY_HINT, style="Hint.TLabel", justify="left")
        hint.grid(row=1, column=0, sticky="w", pady=(px(4), px(12)))
        self.mine_empty_btn = ttk.Button(empty, text=self.MINE_EMPTY_BUTTON, style="Accent.TButton",
                                         command=lambda: self.select_list_mode("download"))
        self.mine_empty_btn.grid(row=2, column=0, sticky="ew")
        hints.hint(self.mine_empty_btn, "mine.empty_download")

        def wrap(event) -> None:      # колонка узкая: _autowrap с его наименьшей шириной здесь не подходит
            width = max(px(80), event.width - px(28))
            for label in (self.mine_empty_label, hint):
                if str(label.cget("wraplength")) != str(width):
                    label.configure(wraplength=width)

        empty.bind("<Configure>", wrap, add="+")
        empty.grid_remove()

    def _set_mine_empty(self, empty: bool, missing_path: str = "") -> None:
        """Пустое состояние вместо поиска и таблицы (empty=True) или обратно.

        missing_path - папки проектов нет: вместо «нет проектов» и «Скачать проект с сервера» текст про пропавшую папку
        и подсвеченная «Выбрать…» (скачивать в несуществующую папку не предлагаем)."""
        for widget in (self.mine_find_row, self.mine_table):
            self._show(widget, not empty)
        self._show(self.mine_empty, empty)
        self._folder_missing_path = missing_path if empty else ""      # для подсказки «Папка … не найдена»
        if not empty:
            return
        if missing_path:
            texts = (self.MINE_MISSING_TEXT.format(path=missing_path), self.MINE_MISSING_HINT,
                     self.MINE_MISSING_BUTTON, self._on_browse_mine)
        else:
            texts = (self.MINE_EMPTY_TEXT, self.MINE_EMPTY_HINT, self.MINE_EMPTY_BUTTON,
                     lambda: self.select_list_mode("download"))
        label, hint, button, command = texts
        self.mine_empty_label.configure(text=label)
        self.mine_empty_hint.configure(text=hint)
        self.mine_empty_btn.configure(text=button, command=command)
        hints.hint(self.mine_empty_btn, "mine.empty_browse" if missing_path else "mine.empty_download")

    def _build_mine_state_column(self, body: ttk.Frame) -> None:
        """Колонка 3: ProjectPanel (шапка и вкладки выбранного проекта). Вкладку «Ветки» наполняет LinesTabMixin."""
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.project_panel = panel = ProjectPanel(
            body, self.px, templates=self.config.commit_templates, recent_comments=self.settings.recent_comments,
            on_save=self._on_panel_save, on_release=self._on_panel_release, on_lock=lambda: self._on_get_lock(),
            on_save_local=self._on_save_local_clicked, compact=self.settings.buttons_compact_right)
        panel.grid(row=0, column=0, sticky="nsew")
        self.project_tabs = panel.tabs
        self.project_tab_frames: dict[str, ttk.Frame] = panel.frames
        self._commit_panel: CommitPanel = panel.commit_panel
        self._build_lines_view(self.project_tab_frames["lines"])
        self.project_tabs.bind("<<NotebookTabChanged>>", lambda _e: self._load_project_tab(), add="+")
        # человек сам выбрал вкладку: автоматически «Изменения» после проверки уже не открываем
        self.project_tabs.bind("<ButtonPress-1>", lambda _e: setattr(self, "_auto_tab_key", ""), add="+")

    # -- колонки: сворачивание и раскладка -------------------------------------------------

    MINE_COL2_MIN = 180       # границы ширины колонки «Проекты» при перетаскивании разделителя (px до масштаба)
    MINE_COL3_MIN = 300       # столько остаётся колонке «Состояние проекта»

    def _build_mine_sash(self, tab: ttk.Frame) -> None:
        """Разделитель между колонками 2 и 3: тянется мышью, пока развёрнуты обе колонки."""
        px = self.px
        p = self.palette
        self.mine_sash = sash = tk.Frame(tab, width=px(self.MINE_SASH_WIDTH), background=p.bg, cursor="sb_h_double_arrow")
        drag: dict[str, int] = {}

        def press(event) -> None:
            drag["x"], drag["width"] = event.x_root, self.mine_col2.winfo_width()

        def move(event) -> None:
            if "x" not in drag:
                return
            left = self.actions_row.winfo_width() if self.actions_row.winfo_ismapped() else 0
            room = tab.winfo_width() - left - sash.winfo_width() - px(self.MINE_COL3_MIN + 40)
            width = drag["width"] + event.x_root - drag["x"]
            self.mine_col2.set_width(max(px(self.MINE_COL2_MIN), min(width, max(px(self.MINE_COL2_MIN), room))))

        def release(_event) -> None:
            if drag.pop("x", None) is None:
                return
            scale = px(1000) / 1000
            width = max(self.MINE_COL2_MIN, round(self.mine_col2.width / scale))
            # у каждой подвкладки своя ширина: дереву сервера нужно больше места, чем списку своих проектов
            key = "mine_col2_download_width" if self._list_mode() == "download" else "mine_col2_width"
            if getattr(self.settings, key) != width:
                setattr(self.settings, key, width)
                self.settings_store.save()

        sash.bind("<ButtonPress-1>", press)
        sash.bind("<B1-Motion>", move)
        sash.bind("<ButtonRelease-1>", release)
        sash.bind("<Enter>", lambda _e: sash.configure(background=self.palette.border_strong))
        sash.bind("<Leave>", lambda _e: sash.configure(background=self.palette.bg))

    def _layout_mine_columns(self) -> None:
        """Веса колонок по их состоянию: колонка 3 растягивается; свёрнута - растягивается колонка 2.
        Свёрнуты обе - полоса «Состояние» прижата к правому краю. Пока развёрнуты обе, между ними разделитель."""
        tab, col2, col3 = self.mine_tab, self.mine_col2, self.mine_col3
        px = self.px
        # кнопки левой колонки уходят вместе с панелью «Проекты»; на «Скачать проект» они не нужны (в них только
        # действия над выбранным своим проектом), там их место отдано дереву и форме
        if col2.collapsed or self._list_mode() == "download":
            self.actions_row.grid_remove()
        else:
            self.actions_row.grid(row=1, column=0, sticky="nw", padx=(0, px(12)))
        wide2 = col3.collapsed and not col2.collapsed
        both = not col2.collapsed and not col3.collapsed
        tab.columnconfigure(1, weight=1 if wide2 else 0)
        tab.columnconfigure(3, weight=0 if wide2 else 1)
        col2.grid(row=1, column=1, sticky="nsw" if col2.collapsed else "nsew", padx=(0, 0 if both else px(8)))
        col3.grid(row=1, column=3, sticky="nse" if col3.collapsed else "nsew")
        if both:
            self.mine_sash.grid(row=1, column=2, sticky="ns")
        else:
            self.mine_sash.grid_remove()
        self._apply_mine_view(wide2)

    def _apply_mine_view(self, wide: bool) -> None:
        """Узкий вид (колонка 3 развёрнута): «Проект» + строка состояния. Широкий (колонка 3 свёрнута): таблица.
        Выделение и прокрутка при смене вида сохраняются; сортировка по заголовку - только в широком виде."""
        if wide == self._mine_wide:
            return
        tree = self.mine_tree
        self._mine_wide = wide
        top = tree.yview()[0]
        tree.configure(displaycolumns=self.MINE_WIDE_COLUMNS if wide else self.MINE_NARROW_COLUMNS)
        for col, mode in self.MINE_SORT_HEADINGS.items():
            if wide:
                tree.heading(col, command=lambda m=mode: self._on_heading_sort(m))
            else:
                tree.heading(col, command=lambda c=col: self._toggle_column(tree, c))
        tree.heading("brief", command="")
        self._update_sort_headings()
        tree.yview_moveto(top)

    def _on_heading_sort(self, mode: str) -> None:
        """Щелчок по заголовку «Проект» (по названию) или «Изменён» (сначала свежие)."""
        self.settings.mine_sort = mode
        self.settings_store.save()
        self._update_sort_headings()
        self._apply_mine_sort()

    def _update_sort_headings(self) -> None:
        """Стрелка у заголовка, по которому сейчас отсортирован список."""
        mode = self._sort_mode()
        for col, col_mode in self.MINE_SORT_HEADINGS.items():
            arrow = (" ▲" if col_mode == "name" else " ▼") if col_mode == mode else ""
            self.mine_tree.heading(col, text=self.MINE_HEADINGS[col] + arrow)

    def _on_mine_column_toggled(self, _event=None) -> None:
        """Свернули или развернули колонку: раскладка и запоминание в settings."""
        self._layout_mine_columns()
        self._sync_hint_background()          # карточка спряталась или появилась: панель говорит больше или меньше
        st = self.settings
        state = (self.mine_col2.collapsed, self.mine_col3.collapsed)
        if (st.mine_col2_collapsed, st.mine_col3_collapsed) != state:
            st.mine_col2_collapsed, st.mine_col3_collapsed = state
            self.settings_store.save()
        self._load_project_tab()      # колонка 3 развернулась: открытая вкладка догружается

    def open_project_tab(self, name: str) -> None:
        """Разворачивает колонку 3 и открывает её внутреннюю вкладку name (ключ из PROJECT_TABS:
        changes, incoming, history, locks, lines). Вызывают кнопки колонки 1 и уведомления."""
        frame = self.project_tab_frames.get(name)
        if frame is None:
            raise ValueError(f"нет вкладки «{name}» в колонке «Состояние проекта»")
        if self._list_mode() != "mine":
            self.select_list_mode("mine")
        self.mine_col3.expand()            # событие <<ColumnToggled>> само поправит раскладку и settings
        self.project_tabs.select(frame)
        self._load_project_tab()

    # -- подвкладки колонки «Проекты»: «Мои проекты» | «Скачать проект» ----------------------

    def _list_mode(self) -> str:
        """Какая подвкладка колонки «Проекты» выбрана: "mine" | "download"."""
        tabs = getattr(self, "list_tabs", None)
        if tabs is None:
            return "mine"
        return "download" if tabs.select() == str(self.list_frames["download"]) else "mine"

    def select_list_mode(self, mode: str) -> None:
        """Открывает подвкладку колонки «Проекты» (ключ из LIST_MODES). Вызывают кнопки, уведомления, первый запуск.
        Для «Скачать проект» колонка «Проекты» разворачивается: подвкладки видны только в ней."""
        frame = self.list_frames.get(mode)
        if frame is None:
            raise ValueError(f"нет подвкладки «{mode}» в колонке «Проекты»")
        if mode == "download":
            self.mine_col2.expand()
        self.list_tabs.select(frame)
        self._on_list_tab_changed()      # раскладка сразу; событие от ярлыков придёт позже и ничего не повторит

    def _col2_width(self, mode: str) -> int:
        """Ширина колонки «Проекты» на подвкладке mode (px до масштаба экрана): запомненная или по умолчанию."""
        if mode == "download":
            return self.settings.mine_col2_download_width or self.MINE_COL2_DOWNLOAD_WIDTH
        return self.settings.mine_col2_width or self.MINE_COL2_WIDTH

    def _apply_list_mode(self, mode: str) -> None:
        """Раскладка под подвкладку: в колонке 3 «Состояние проекта» или форма «Скачивание», ширина колонки 2,
        левая колонка (на «Скачать проект» скрыта). Можно вызывать повторно."""
        download = mode == "download"
        self._show(self.project_panel, not download)
        self._show(self.download_form, download)
        title = ("Скачивание", "Скачивание") if download else ("Состояние проекта", "Состояние")
        if (self.mine_col3.title, self.mine_col3.collapsed_title) != title:
            self.mine_col3.set_title(*title)
        self.mine_col2.set_width(self.px(self._col2_width(mode)))
        self._layout_mine_columns()
        self._sync_hint_background()

    def _on_list_tab_changed(self, _event=None) -> None:
        """Выбрана подвкладка колонки «Проекты» (щелчком или select_list_mode). Вход в «Скачать проект» разворачивает
        колонку 3 (форма скачивания); вход в «Мои проекты» освежает список, как прежде открытие вкладки окна."""
        mode = self._list_mode()
        entered = mode != self._list_mode_shown
        self._list_mode_shown = mode
        self._apply_list_mode(mode)
        if not entered:
            return
        if mode == "download":
            self.mine_col3.expand()            # событие <<ColumnToggled>> само поправит раскладку и settings
        else:
            self._on_mine_shown()

    def _on_mine_shown(self) -> None:
        """Видна подвкладка «Мои проекты» (и при запуске): список устарел - ищем проекты, иначе состояние свежее.
        Поиск уже идёт (например, только что скачан проект) - проверяем все проекты один раз, когда он закончится:
        проверка старых строк сейчас повторилась бы после поиска (ревью v4, находка 6)."""
        if self._mine_dirty:
            self._scan_projects()
        elif self._scan_running:
            self._auto_auth_failed = False
            self._check_all_after_scan = True
        else:
            self._auto_auth_failed = False
            self._load_project_tab()   # колонка 3 показывает открытую вкладку проекта
            self._on_check_all()       # при открытии подвкладки состояние всегда свежее

    def _open_download_on_first_scan(self) -> None:
        """Первый поиск после запуска не нашёл проектов: новичок сразу попадает на «Скачать проект». Повторные
        поиски подвкладку не переключают."""
        first_scan, self._start_tab_chosen = not self._start_tab_chosen, True
        if first_scan and self._list_mode() == "mine":
            self.select_list_mode("download")

    # -- папки для проектов (общий список «Мои проекты» и «Скачать проект») ----------------------

    def _project_dir_values(self) -> list[str]:
        return list(self.settings.project_dirs)

    def _fill_project_dir_lists(self) -> None:
        """Список папок в выпадающих списках обеих подвкладок; на «Мои проекты» последний пункт - «Убрать…»."""
        dirs = self._project_dir_values()
        if hasattr(self, "mine_dir_combo"):
            self.mine_dir_combo.configure(values=dirs + [self.PROJECT_DIR_REMOVE] if dirs else [])
        if hasattr(self, "dir_entry"):
            self.dir_entry.configure(values=dirs)

    def _remember_project_dir(self, path: str) -> None:
        """Папка выбрана текущей: в список (если её там нет), в last_dir, в настройки на диск."""
        path = path.strip()
        if not path:
            return
        path = os.path.normpath(path)
        self.settings.project_dirs = add_project_dir(self.settings.project_dirs, path)
        self.settings.last_dir = path
        self.settings_store.save()
        self._fill_project_dir_lists()

    def _on_dir_var_changed(self) -> None:
        """Текущая папка поменялась (выбор, ввод на «Скачать проект», параметры): список проектов устарел."""
        self._mine_dirty = True
        if hasattr(self, "mine_dir_var") and self.mine_dir_var.get() != self.dir_var.get():
            self.mine_dir_var.set(self.dir_var.get())

    def _on_mine_dir_selected(self, _event=None) -> None:
        """Выбор в списке папок над проектами: другая папка - сразу ищем в ней проекты; «Убрать…» - убираем текущую."""
        chosen = self.mine_dir_var.get()
        if chosen == self.PROJECT_DIR_REMOVE:
            self._forget_current_project_dir()
            return
        self._switch_project_dir(chosen)

    def _switch_project_dir(self, path: str) -> None:
        self._remember_project_dir(path)
        self.dir_var.set(os.path.normpath(path))
        self._scan_projects()

    def _forget_current_project_dir(self) -> None:
        """Убирает текущую папку из списка (с диска ничего не удаляется) и переходит на первую оставшуюся."""
        current = self.dir_var.get()
        dirs = [d for d in self.settings.project_dirs if not same_dir(d, current)]
        self.settings.project_dirs = dirs
        self.settings_store.save()
        self._fill_project_dir_lists()
        if dirs:
            self._switch_project_dir(dirs[0])
        else:   # больше папок нет: показываем прежнюю как есть, список проектов не трогаем
            self.mine_dir_var.set(current)

    def _on_browse_mine(self) -> None:
        self._on_browse()
        if self._mine_dirty:
            self._scan_projects()

    def _apply_tortoise(self) -> None:
        """Пункты TortoiseSVN в «Ещё ▾» есть, только если TortoiseProc найден. «Сохранить отмеченное» от него не зависит."""
        if not hasattr(self, "update_btn"):
            return
        menu, experts = self.more_menu, self.expert_menu
        menu.delete(0, "end")
        experts.delete(0, "end")
        self._more_idx = {}        # пункты «Ещё ▾»: commit, experts
        self._archive_idx: Optional[int] = None    # пункт «Удалить прежние сохранения…»: есть, только если у проекта они остались
        self._expert_idx = {}      # пункты подменю «Для опытных»: log, revert, resolve, cleanup

        def add(target: tk.Menu, index: dict, key: str, label: str, command: Callable) -> None:
            target.add_command(label=label, command=command)
            index[key] = target.index("end")

        if self.tortoise_exe:
            add(menu, self._more_idx, "commit", "Сохранить через TortoiseSVN…", self._on_commit_tortoise)
            add(experts, self._expert_idx, "log", "История в TortoiseSVN", lambda: self._on_tortoise(
                "log", "В истории видно, кто и когда сохранял версии. Старую версию файла можно вернуть: "
                       "правый щелчок по записи → пункт про возврат к этой версии."))
            add(experts, self._expert_idx, "revert", "Отменить мои правки (TortoiseSVN)…", lambda: self._on_tortoise(
                "revert", "Отметьте файлы, в которых нужно отменить ваши несохранённые правки."))
            add(experts, self._expert_idx, "resolve", "Разобрать конфликт в TortoiseSVN", self._on_resolve_tortoise)
        add(experts, self._expert_idx, "cleanup", "Починить папку", self._on_cleanup)
        add(menu, self._more_idx, "publish", "Отправить на сервер…", self._on_publish_project)
        menu.add_cascade(label="Для опытных", menu=experts)
        self._more_idx["experts"] = menu.index("end")
        self._update_mine_buttons()

    def _probe_item(self, client: Optional[SvnClient], path: str, name: str) -> MineItem:
        """Что за папка: обычная, проект SVN или повреждённая копия. Выполняется в фоновом потоке."""
        archive = localrepo.archive_path(self.settings_store, path)       # прежние сохранения после отправки на сервер
        if not os.path.isdir(os.path.join(path, ".svn")):
            return MineItem(path, name, "plain", archive=archive)
        if client is None:
            return MineItem(path, name, "svn", archive=archive)
        try:
            info = client.info(path)
            item = MineItem(path, name, "svn", url=unquote(info.url), revision=info.revision, raw_url=info.url,
                            archive=archive)
            if localrepo.is_local_url(info.url, self.config.server):
                # история на этом компьютере: ни веток, ни версий, ни адреса на сервере
                item.local = True
                item.history_missing = not localrepo.repo_exists(info.root or info.url)
                if item.history_missing:        # папку проектов переименовали или перенесли: хранилище могло переехать с ней
                    item.history_found = localrepo.find_history(self.settings_store, path, info.uuid)
                return item
            found = server.line_from_url(info.url, info.root)   # адрес нужен закодированный, как его напечатал svn
            prefix = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*", info.url.strip())
            if found and prefix:
                item.project_segments, item.line = found
                item.project_prefix = prefix.group(0)
                item.line_url = item.line_url_of(item.line)
            return item
        except SvnError as exc:
            return MineItem(path, name, "broken", error=exc.raw.strip().splitlines()[-1] if exc.raw else "")

    def _scan_projects(self) -> None:
        base = os.path.normpath(self.dir_var.get().strip() or ".")
        self._mine_dirty = False
        self._mine_hint("Поиск проектов…")
        client = self.client
        # папку можно сменить, пока идёт поиск в прежней: ответ прежнего поиска опоздал и не нужен
        self._scan_gen += 1
        gen = self._scan_gen
        self._scan_running = True

        def work() -> list[MineItem]:
            names = sorted((n for n in os.listdir(base)
                            if not n.startswith(".") and os.path.isdir(os.path.join(base, n))), key=str.casefold)
            futures = [self._probe_pool.submit(self._probe_item, client, os.path.join(base, n), n) for n in names]
            return [f.result() for f in futures]

        def done(res, err, _stamp) -> None:
            if gen == self._scan_gen:
                self._scan_running = False
                self._on_scanned(base, res, err)

        self.run_bg(work, done)

    # -- новый проект из шаблона ---------------------------------------------------------

    def _on_offline_changed(self) -> None:
        """Связь пропала или вернулась: у папок без сервера подсказка и кнопка «Отправить на сервер…» зависят от неё."""
        for item in self.mine.values():
            if item.state == "plain" or item.local:
                self._refresh_row(item)
        self._update_mine_buttons()

    def _on_new_project(self) -> None:
        """Окно «Новый проект»; созданный проект появляется в списке и выделяется."""
        if self._new_project_dialog is not None or self._closing:
            return
        dialog = NewProjectDialog(self, self.dir_var.get().strip())
        self._new_project_dialog = dialog
        try:
            self.root.wait_window(dialog)
        finally:
            self._new_project_dialog = None
        if dialog.result is None or self._closing:
            return
        self._show_new_project(dialog.result)

    def _show_new_project(self, path: str) -> None:
        """Папка проекта в списке: ищем проекты в её родительской папке и выделяем строку."""
        path = os.path.normpath(path)
        base = os.path.dirname(path)
        self._pending_select = os.path.normcase(path)
        if same_dir(base, self.dir_var.get()):
            self._scan_projects()
        else:
            self._switch_project_dir(base)

    def _new_project_running(self) -> bool:
        dialog = self._new_project_dialog
        return dialog is not None and dialog.state == "running"

    # -- отправка на сервер («Отправить позже») ----------------------------------------------

    @staticmethod
    def _can_publish(item: MineItem) -> bool:
        """Папка без привязки к серверу: обычная или с локальной историей (адрес file://)."""
        return item.state == "plain" or (item.state == "svn" and item.url.lower().startswith("file://"))

    def _on_publish_project(self) -> None:
        """«Ещё → Отправить на сервер…»: окно места и комментария, затем отправка (publish.publish)."""
        item = self._selected_item()
        if (item is None or not self.client or self._publish_dialog is not None or self._closing
                or self.upd or self.job or self._locking or self._committing or not self._can_publish(item)):
            return
        if self._path_busy(item.path):      # открыто окно TortoiseSVN или идёт другая работа с этой папкой
            messagebox.showinfo("Папка занята", publish.MESSAGES[publish.SVN_BUSY], parent=self.root)
            return
        dialog = PublishDialog(self, item.path)
        self._publish_dialog = dialog
        try:
            self.root.wait_window(dialog)
        finally:
            self._publish_dialog = None
        if dialog.result is None or self._closing:
            return
        self._show_new_project(item.path)       # папка теперь привязана к серверу: список обновляется

    def _on_restore_history(self) -> None:
        """«Восстановить историю»: хранилище переехало вместе с папкой проектов - папка перенацеливается на него
        (localrepo.restore_history), файлы не трогаются."""
        item = self._selected_item()
        client = self.client
        if (item is None or not client or not item.local or not item.history_missing or self._closing
                or self._path_busy(item.path)):
            return
        store = self.settings_store

        def done(_res, error, _stamp) -> None:
            if self._closing:
                return
            current = self._current(item) or item
            if error is not None:
                self._history_action_failed(item, error, localrepo.RESTORE_BUTTON)
                if isinstance(error, localrepo.LocalRepoError):
                    self._reprobe(current)          # история пропала насовсем или уже на месте: кнопки обновятся
                return
            self._action_notes[os.path.normcase(item.path)] = localrepo.RESTORE_DONE
            self.mine_status.set(localrepo.RESTORE_DONE)
            self._reprobe(current)

        self.mine_status.set(localrepo.RESTORING_TEXT)
        self.run_bg(lambda: localrepo.restore_history(client, item.path, store), done)

    def _on_continue_without_history(self) -> None:
        """«Продолжить без истории»: `.svn` убирается, файлы остаются (localrepo.detach_history). Второе действие
        при потерянной истории, с подтверждением: прежние сохранения после этого из программы не достать."""
        item = self._selected_item()
        client = self.client
        if (item is None or not client or not item.local or not item.history_missing or self._closing
                or self._path_busy(item.path)):
            return
        ask = localrepo.CONTINUE_ASK_FOUND if item.history_found else localrepo.CONTINUE_ASK
        if not messagebox.askyesno(localrepo.CONTINUE_BUTTON, ask, parent=self.root, icon="warning"):
            return
        store = self.settings_store

        def done(_res, error, _stamp) -> None:
            if self._closing:
                return
            if error is not None:
                self._history_action_failed(item, error, localrepo.CONTINUE_BUTTON)
                return
            self._action_notes[os.path.normcase(item.path)] = localrepo.CONTINUE_DONE
            self.mine_status.set(localrepo.CONTINUE_DONE)
            self._show_new_project(item.path)       # теперь это обычная папка: список перечитывается

        self.mine_status.set("Продолжаю без истории…")
        self.run_bg(lambda: localrepo.detach_history(client, item.path, store), done)

    def _on_delete_archive(self) -> None:
        """«Ещё → Удалить прежние сохранения…»: после отправки на сервер история остаётся на диске, пока её не удалят явно."""
        item = self._selected_item()
        if (item is None or not item.archive or item.local or self._closing or self._path_busy(item.path)
                or self._publish_running()):
            return
        if not messagebox.askyesno(localrepo.DELETE_ARCHIVE_MENU.rstrip("…"),
                                   localrepo.DELETE_ARCHIVE_ASK.format(name=item.display_name), parent=self.root):
            return
        store, client = self.settings_store, self.client

        def done(_res, error, _stamp) -> None:
            if self._closing:
                return
            if error is not None:
                self._history_action_failed(item, error, localrepo.DELETE_ARCHIVE_MENU.rstrip("…"))
                return
            current = self._current(item) or item
            current.archive = ""
            self.mine_status.set(localrepo.DELETE_ARCHIVE_DONE)
            self._update_mine_buttons()
            messagebox.showinfo(localrepo.DELETE_ARCHIVE_MENU.rstrip("…"), localrepo.DELETE_ARCHIVE_DONE,
                                parent=self.root)

        self.run_bg(lambda: localrepo.delete_history(store, item.path, client), done)

    def _history_action_failed(self, item: MineItem, error: Exception, title: str) -> None:
        self.mine_status.set("")
        if isinstance(error, localrepo.LocalRepoError):
            messagebox.showwarning(title, error.message, parent=self.root)
            return
        log.error("Действие с историей проекта %s не удалось", item.path, exc_info=error)
        self.show_error(title, localrepo.MESSAGES[localrepo.FAILED], action=f"{title}: {item.name}",
                        raw=str(error), extra={"Папка": item.path})

    def _publish_running(self) -> bool:
        dialog = self._publish_dialog
        return (dialog is not None and dialog.state == "running") or self._publish_busy

    def _on_scanned(self, base: str, items, error) -> None:
        previous = {os.path.normcase(i.path): i for i in self.mine.values()}
        selected = self._selected_item()
        previous_selected = os.path.normcase(selected.path) if selected else ""
        if self._pending_select:        # только что создан проект: выделяем его, а не прежний
            previous_selected, self._pending_select = self._pending_select, ""
        self.mine_tree.delete(*self.mine)     # и спрятанные поиском строки тоже
        self.mine.clear()
        self._mine_scanned = True
        check_all, self._check_all_after_scan = self._check_all_after_scan, False
        if error is not None:
            self._mine_hint(f"Папка {base} не найдена или недоступна. Выберите другую папку в списке «Папка» выше.")
            # папки нет. Первый запуск (папку ещё никто не выбирал, её создаст скачивание) - тот же путь к скачиванию,
            # что у пустой папки. Иначе папка пропала (отключён сетевой диск): предлагаем выбрать другую, а не скачивать.
            # Подвкладку сами не переключаем: папка могла пропасть на миг, а человек работает
            missing = isinstance(error, FileNotFoundError)
            self._set_mine_empty(missing, missing_path=base if missing and self.settings.last_dir else "")
            self._update_mine_buttons()
            self._update_lock_warning()
            self._refresh_project_panel()
            return
        interrupted: list[MineItem] = []
        for item in items:
            old = previous.get(os.path.normcase(item.path))
            if old and old.state == item.state == "svn" and old.revision == item.revision:
                # та же копия, что была: не теряем уже полученный статус и время проверки
                item.status, item.checked, item.error, item.last_check = (
                    old.status, old.checked, old.error, old.last_check)
            if old and old.checking and item.state == "svn":
                interrupted.append(item)  # проверка шла для старой строки: её результат придёт, но проверим и новую
            self.mine[self._insert_mine_row(item)] = item
        self._set_mine_empty(not items)
        if not items:
            self._mine_hint(f"В папке {base} пока нет проектов. Скачайте проект: «Скачать проект» в колонке "
                            "«Проекты», или создайте новый: ＋ Новый проект.")
            self._open_download_on_first_scan()      # первый запуск: проектов ещё нет, начинаем со скачивания
        else:
            self._start_tab_chosen = True
            if self._offline:
                self._mine_hint("Нет связи с сервером. Выберите проект, чтобы увидеть подробности.")
            else:
                self._mine_hint(f"Найдено проектов: {len(items)}. Выберите проект, чтобы увидеть подробности.")
        self._apply_mine_sort()
        self._autofit_columns(self.mine_tree)
        self._select_after_scan(previous_selected)
        self._refresh_project_panel()
        self._update_mine_buttons()
        self._update_lock_warning()
        if check_all:                     # подвкладку открыли, пока шёл поиск: проверяем всё один раз
            self._on_check_all()
            return
        self._check_status(interrupted)   # встанут в очередь, если проверка ещё идёт
        self._auto_check()

    def _select_after_scan(self, previous_path: str) -> None:
        """После поиска проектов выделяет прежний проект (если он есть) или первую строку: кнопки не серые."""
        rows = self.mine_tree.get_children()
        if not rows:
            return
        iid = next((i for i in rows if os.path.normcase(self.mine[i].path) == previous_path), rows[0])
        self._auto_selected = iid     # выбор сделан программой: «Открыт» от него не меняется
        self.mine_tree.selection_set(iid)
        self.mine_tree.focus(iid)
        self.mine_tree.see(iid)
        self._refresh_project_panel()

    @staticmethod
    def _is_offline_error(error: str) -> bool:
        return error == messages.short_message(ErrorKind.NO_CONNECTION)

    def _facts_of(self, item: MineItem) -> next_step.Facts:
        """Факты о проекте для next_step: состояние, проверка, версия (только для просмотра), история на компьютере,
        нет связи (у обычной папки - общий флаг окна, у проекта - ошибка проверки «нет связи»), нужный файл занят
        другим (_needed_locked)."""
        is_svn = item.state == "svn"
        missing = bool(item.local and item.history_missing)
        return next_step.Facts(
            state=item.state, status=item.status, checking=item.checking, error=item.error if is_svn else "",
            read_only=bool(is_svn and not item.local and item.line is not None and item.line.read_only),
            local=item.local, history_missing=missing, history_found=bool(missing and item.history_found),
            offline=self._offline if item.state == "plain" else self._is_offline_error(item.error),
            needed_locked=self._needed_locked(item))

    # «нужный мне файл занят другим»: файлы, которые человек пытался занять, а их держал другой. Пока такой файл занят
    # другим, панель подсказок говорит, кем и с какого времени (просьба освободить - PM.3, здесь только подсказка)
    _wanted_locks: dict = {}         # папка проекта (normcase) -> множество файлов (normcase, полный путь)

    @staticmethod
    def _lock_key(base: str, path: str) -> str:
        full = path if os.path.isabs(path) else os.path.join(base, path)
        return os.path.normcase(os.path.abspath(full))

    def _remember_wanted_locks(self, item: MineItem, results: list[LockResult]) -> None:
        """Итог «Занять файл…»: занятые другим файлы запоминаются как нужные человеку, занятые им - забываются."""
        project = os.path.normcase(item.path)
        wanted = set(self._wanted_locks.get(project, ()))
        for r in results:
            key = self._lock_key(item.path, r.path)
            if r.error is not None and r.error.kind is ErrorKind.LOCKED:
                wanted.add(key)
            elif r.error is None:
                wanted.discard(key)
        self._wanted_locks = {**self._wanted_locks, project: wanted}

    def _forget_released_wanted(self, item: MineItem, status: WcStatus) -> None:
        """Нужный файл больше не занят другим (освободили): о нём больше не напоминаем."""
        project = os.path.normcase(item.path)
        wanted = self._wanted_locks.get(project)
        if wanted:
            held = {self._lock_key(item.path, lock.path) for lock in status.other_locks}
            self._wanted_locks = {**self._wanted_locks, project: wanted & held}

    def _needed_locked(self, item: MineItem) -> Optional[tuple]:
        """(имя файла, кто занял, «с 10:20») первого нужного человеку файла, который всё ещё занят другим; None - нет."""
        wanted = self._wanted_locks.get(os.path.normcase(item.path))
        if not wanted or item.state != "svn" or item.status is None:
            return None
        for lock in item.status.other_locks:
            if self._lock_key(item.path, lock.path) in wanted:
                since = ""
                if lock.created is not None:
                    created = lock.created.astimezone()
                    since = f"с {created:%H:%M}" if created.date() == datetime.now().date() else f"с {created:%d.%m}"
                return os.path.basename(lock.path.rstrip("\\/")), lock.owner, since
        return None

    def _status_for(self, item: MineItem, short: bool = False) -> tuple[str, str, str]:
        """(состояние, тег цвета, что сделать) по коду next_step - тому же, что выбирает подсвеченную кнопку.
        Поправки окна учтены в фактах: версия (только для просмотра, тег «frozen»), незаконченный переход, нет связи
        и ошибка проверки, обычная папка без связи."""
        return messages.state_line(self._facts_of(item), short=short)

    def _row_values(self, item: MineItem) -> tuple[tuple, str]:
        """Значения строки таблицы («Проект», «Ветка», «● Состояние», «Что сделать») и тег цвета."""
        text, tag, hint = self._state_cells(item, short=True)
        line = line_cell(item.line) if item.state == "svn" else ""
        desc = self._description_of(item) if item.state == "svn" else ""
        status_cell = theme.dot(tag) + messages.table_text(text)
        return (item.display_name, line, status_cell, messages.table_text(hint),
                desc, self._creator_of(item) if item.state == "svn" else "",
                self._created_on(item) if item.state == "svn" else "",
                "✔" if item.state == "svn" and tag in ("ok", "frozen") else "",
                (localrepo.WHERE_TEXT if item.local else self._server_dir(item) or "—") if item.state == "svn" else "",
                self._opened_text(item), self._changed_text(item),
                f"{item.display_name}\n{status_cell}"), tag

    def _sort_mode(self) -> str:
        """Текущая сортировка; старое значение settings.mine_sort работает как начальная."""
        mode = self.settings.mine_sort
        return mode if mode in self.MINE_SORT_MODES else "name"

    def _touch_opened(self, item: Optional[MineItem]) -> None:
        """Запоминает, когда проект последний раз выбирали или открывали (для сортировки «по дате открытия»)."""
        if item is None or item.state == "plain":
            return
        key, now = os.path.normcase(item.path), time.time()
        if now - self.settings.opened.get(key, 0) < 30:      # не пишем файл настроек на каждый клик
            return
        self.settings.opened[key] = now
        self.settings_store.save()
        self._refresh_row(item)

    def _opened_at(self, item: MineItem) -> Optional[float]:
        return self.settings.opened.get(os.path.normcase(item.path))

    def _line_history_of(self, item: MineItem):
        if item.state != "svn" or item.line is None:
            return None
        self._ensure_line_history(item, item.line)
        return self._line_history.get(item.line_url_of(item.line)) or None

    def _created_at(self, item: MineItem):
        history = self._line_history_of(item) if item.line is not None and item.line.kind != "trunk" else None
        return history.created if history else None

    def _changed_at(self, item: MineItem):
        history = self._line_history_of(item)
        return messages.last_change(history)[1] if history else None

    def _opened_text(self, item: MineItem) -> str:
        opened = self._opened_at(item)
        return datetime.fromtimestamp(opened).strftime("%d.%m %H:%M") if opened else "—"

    def _changed_text(self, item: MineItem) -> str:
        if item.state != "svn" or item.line is None:
            return ""
        when = self._changed_at(item)
        return f"{when.astimezone():%d.%m.%Y}" if when else "—"

    def _apply_mine_sort(self) -> None:
        """Переставляет строки таблицы по выбранной сортировке и прячет не подходящие под поиск «Найти проект»
        (без учёта регистра); даты берутся те, что уже загружены."""
        mode = self._sort_mode()
        items = list(self.mine.items())
        stamp = {"opened": lambda i: self._opened_at(i),
                 "created": lambda i: (self._created_at(i).timestamp() if self._created_at(i) else None),
                 "changed": lambda i: (self._changed_at(i).timestamp() if self._changed_at(i) else None)}.get(mode)
        if stamp is None:
            ordered = sorted(items, key=lambda p: p[1].display_name.casefold())
        else:   # даты по убыванию, проекты без даты внизу (по названию)
            ordered = sorted(items, key=lambda p: (stamp(p[1]) is None, -(stamp(p[1]) or 0),
                                                   p[1].display_name.casefold()))
        query = self.mine_filter_var.get().strip().casefold()
        visible = [iid for iid, item in ordered if query in item.display_name.casefold()]
        if visible != list(self.mine_tree.get_children()):
            self.mine_tree.detach(*[iid for iid, _item in ordered if iid not in visible])
            for index, iid in enumerate(visible):      # move возвращает и спрятанные ранее строки
                self.mine_tree.move(iid, "", index)
        for index, iid in enumerate(visible):
            tags = [t for t in self.mine_tree.item(iid, "tags") if t != "odd"]
            self.mine_tree.item(iid, tags=tuple(tags + (["odd"] if index % 2 else [])))

    def _creator_of(self, item: MineItem) -> str:
        """Кто создал ветку или версию, в которой сейчас папка; для «Основной» «—»."""
        if item.line is None:
            return ""
        if item.line.kind == "trunk":
            return "—"
        history = self._line_history.get(item.line_url_of(item.line))
        return messages.created_cells(history)[0] if history else ("…" if history is None else "—")

    def _created_on(self, item: MineItem) -> str:
        """Дата создания ветки или версии, в которой сейчас папка; для «Основной» «—»."""
        if item.line is None:
            return ""
        if item.line.kind == "trunk":
            return "—"
        history = self._line_history.get(item.line_url_of(item.line))
        return messages.created_cells(history)[1] if history else ("…" if history is None else "—")

    def _description_of(self, item: MineItem) -> str:
        """Описание ветки или версии, в которой сейчас папка (комментарий при создании). Для «Основной» — «—»."""
        if item.line is None:
            return ""
        if item.line.kind == "trunk":
            return "—"
        self._ensure_line_history(item, item.line)   # подгрузится в фоне, строка перерисуется сама
        return messages.description_cell(self._line_history.get(item.line_url_of(item.line)))

    def _insert_mine_row(self, item: MineItem, iid: Optional[str] = None) -> str:
        values, tag = self._row_values(item)
        if iid is None:
            tags = (tag, "odd") if len(self.mine_tree.get_children()) % 2 else (tag,)
            return self.mine_tree.insert("", "end", values=values, tags=tags)
        shown = list(self.mine_tree.get_children())     # строка может быть спрятана поиском
        tags = (tag, "odd") if iid in shown and shown.index(iid) % 2 else (tag,)
        self.mine_tree.item(iid, values=values, tags=tags)
        return iid

    def _refresh_row(self, item: MineItem) -> None:
        """Перерисовывает строку проекта (и колонку 3, если проект выбран)."""
        iid = self._iid_of(item)
        if iid:
            self._insert_mine_row(item, iid)
            self._apply_mine_sort()
            self._autofit_columns(self.mine_tree)
            self._refresh_lamp()
            if self.mine_tree.selection() == (iid,):
                self._refresh_project_panel()
                self._update_mine_buttons()
            else:
                self._sync_hint_background()      # подсказка может говорить о другом проекте («В проекте «…»: …»)

    def _iid_of(self, item: MineItem) -> Optional[str]:
        return next((iid for iid, it in self.mine.items() if it is item), None)

    def _current(self, item: MineItem) -> Optional[MineItem]:
        """Актуальная строка того же проекта в self.mine (после «Найти проекты» объекты заменяются). None, если проекта нет."""
        key = os.path.normcase(item.path)
        return next((it for it in self.mine.values() if os.path.normcase(it.path) == key), None)

    def _path_busy(self, path: str) -> bool:
        """Папку сейчас занимает TortoiseSVN, починка или отправка на сервер: трогать её нельзя."""
        key = os.path.normcase(path)
        return any(os.path.normcase(p) == key for p in self._tortoise_open) or publish.is_busy(path)

    def _selected_item(self) -> Optional[MineItem]:
        sel = self.mine_tree.selection()
        return self.mine.get(sel[0]) if sel else None

    def _rules_for(self, item: MineItem):
        """Правила мусора для проверки правок проекта: у проекта с историей на компьютере ещё и служебные папки."""
        return localrepo.local_rules(self.config.filter) if item.local else self.config.filter

    @staticmethod
    def _sentence(text: str) -> str:
        """Текст как предложение: точку ставим, только если фраза ещё не закончена («…», «.», «!», «?»)."""
        return text if text.endswith(("…", ".", "!", "?")) else text + "."

    @staticmethod
    def _revision_text(item: MineItem) -> str:
        """«сохранение № 7» (и «на сервере уже № 9», если там новее)."""
        if item.revision is None:
            return ""
        text = f"сохранение № {item.revision}"
        head = item.status.head if item.status else None
        if head and head > item.revision:
            text += f" (на сервере уже № {head})"
        return text

    def _state_cells(self, item: MineItem, short: bool = False) -> tuple[str, str, str]:
        """(состояние, тег цвета, что сделать): общий текст для строки таблицы и шапки колонки 3."""
        return self._status_for(item, short=short)

    def _header_fields(self, item: MineItem) -> tuple[str, str, str, str, str, str]:
        """Шапка колонки 3: (название, состояние, тег цвета, ветка, что сделать, результат последнего действия)."""
        text, tag, hint = self._state_cells(item)
        branch = ""
        if item.local:
            branch = " · ".join(p for p in (localrepo.WHERE_TEXT, self._revision_text(item)) if p)
        elif item.state == "svn":
            head = line_cell(item.line) if item.line is not None else "не определена"
            branch = " · ".join(p for p in (head, self._revision_text(item)) if p)
        note = self._action_notes.get(os.path.normcase(item.path), "")
        return item.display_name, text, tag, branch, self._sentence(hint), note

    def _mine_hint(self, text: str) -> None:
        """Подсказка колонки 3, пока проект не выбран (проектов нет, идёт поиск, выберите проект)."""
        self._mine_hint_text = text
        if self._selected_item() is None:
            self.project_panel.clear_header(text)
            self._sync_hint_background()

    # панель подсказок внизу окна: фоновый текст и строки таблицы проектов

    def _card_visible(self) -> bool:
        """Карточка выбранного проекта (шапка «Состояния проекта») сейчас на экране."""
        return not self.mine_col3.collapsed and self._list_mode() == "mine"

    # коды, которые говорят не о выбранном проекте: панель не добавляет к ним имя и состояние проекта
    _WINDOW_CODES = (next_step.LOGIN, next_step.NO_FOLDER, next_step.OTHER)

    def _hint_step(self, item: Optional[MineItem]) -> next_step.NextStep:
        """Следующий шаг для панели подсказок (docs/PLAN_ui_v4.md, раздел 6): выбранный проект по тем же фактам, что
        у карточки и подсветки, плюс дела окна целиком - вход не выполнен, папка проектов не найдена, идёт операция,
        самая важная проблема другого проекта (её строка в таблице - self._hint_other_iid)."""
        facts = self._facts_of(item) if item is not None else next_step.Facts()
        other_iid, other = self._other_problem(item)
        self._hint_other_iid = other_iid
        return next_step.next_step(replace(
            facts, login_failed=bool(self._auto_auth_failed and self.client),
            folder_missing=bool(self._folder_missing_path), busy=self._operation_running(), other=other))

    def _other_problem(self, selected: Optional[MineItem]) -> tuple[str, Optional[tuple]]:
        """Самая важная проблема другого проекта: (строка таблицы, (имя проекта, ключ текста её шага)) или ("", None).
        Проекты, спрятанные поиском «Найти проект», тоже считаются: их строку подсказка покажет, сбросив поиск."""
        best = next_step.most_important(
            (iid, next_step.next_step(self._facts_of(it))) for iid, it in self.mine.items() if it is not selected)
        if best is None:
            return "", None
        iid, step = best
        return iid, (self.mine[iid].display_name, step.text_key)

    @staticmethod
    def _step_signature(step: next_step.NextStep) -> tuple:
        """Подпись подсказки (как Notice.signature у уведомлений): код, ключ текста, числа и имена из data."""
        data = tuple(sorted((k, v) for k, v in step.data.items() if isinstance(v, (str, int, bool))))
        return step.code, step.text_key, data

    def _sync_hint_background(self) -> None:
        """Фоновый текст панели подсказок - проактивная подсказка: что сделать и как (какая кнопка и где), слова из
        hints.NEXT_STEP_TEXTS - те же, что у строки «Что сделать» карточки. Карточки не видно (свёрнута,
        «Скачать проект») - панель говорит ещё и имя и состояние. Щелчок по тексту - _on_hint_click."""
        bar = getattr(self, "hint_bar", None)
        if bar is None or not hasattr(self, "project_panel"):
            return
        if self._list_mode() == "download":
            bar.set_background(self.DOWNLOAD_BACKGROUND)
            return
        item = self._selected_item()
        step = self._hint_step(item)
        if item is None and step.code not in (next_step.LOGIN, next_step.NO_FOLDER):
            bar.set_background(self._mine_hint_text)
            return
        detailed, short = hints.step_texts(step, where=self._where_text(item), folder=self._folder_missing_path)
        if item is not None and not self._card_visible() and step.code not in self._WINDOW_CODES:
            name, state = item.display_name, self._state_cells(item)[0]
            prefix = f"«{name}»: {self._sentence(state)}" if state else f"«{name}»."
            detailed, short = (" ".join(p for p in (prefix, text) if p) for text in (detailed, short))
        click = None
        if not step.quiet and step.click != next_step.CLICK_NONE:
            click = lambda shown=step: self._on_hint_click(shown)      # noqa: E731
        signature = self._step_signature(step)
        about = self._hint_about(item, step)
        hide_key = (about, signature) if detailed and step.code not in self._UNHIDDEN_CODES else None
        bar.set_background(detailed, short, click=click, subject=os.path.normcase(item.path) if item else "",
                           signature=signature, hide_key=hide_key)

    # «всё в порядке» скрывать незачем: это не совет
    _UNHIDDEN_CODES = (next_step.NOTHING, next_step.READ_ONLY)

    def _hint_about(self, item: Optional[MineItem], step: next_step.NextStep) -> str:
        """О чём подсказка (для «Скрыть»): папка проекта, о котором она говорит (у «В проекте «…»» - другой проект);
        вход и папка проектов - дела окна (пустая строка)."""
        if step.code == next_step.OTHER:
            other = self.mine.get(self._hint_other_iid)
            return os.path.normcase(other.path) if other is not None else ""
        if step.code in (next_step.LOGIN, next_step.NO_FOLDER) or item is None:
            return ""
        return os.path.normcase(item.path)

    def _step_button(self, key: Optional[str]) -> Optional[tk.Misc]:
        """Кнопка окна по ключу next_step (B_*). Вход - «⟳»: проверка откроет окно входа (у имени в шапке кнопки нет)."""
        item = self._selected_item()
        panel = self.project_panel
        buttons = {
            next_step.B_LOGIN: self.check_btn,
            next_step.B_CHOOSE_FOLDER: (self.mine_empty_btn if self._folder_missing_path else self.mine_browse_btn),
            next_step.B_RESOLVE: self.special_btn, next_step.B_REPAIR: self.special_btn,
            next_step.B_RESTORE: self.special_btn, next_step.B_UPDATE: self.update_btn,
            next_step.B_PUBLISH: self.publish_btn, next_step.B_LOCK: panel.lock_btn,
            next_step.B_COMMIT: (panel.save_local_btn if item is not None and item.local
                                 else self._commit_panel.save_button),
        }
        return buttons.get(key) if key is not None else None

    def _reveal_button(self, button: tk.Misc) -> None:
        """Кнопка слева спрятана (колонка «Проекты» свёрнута или открыта «Скачать проект»): показываем её."""
        if button.master is not self.actions_row:
            return
        if self._list_mode() != "mine":
            self.select_list_mode("mine")
        if self.mine_col2.collapsed:
            self.mine_col2.expand()

    def _on_hint_click(self, shown: next_step.NextStep) -> None:
        """Щелчок по проактивной подсказке (docs/PLAN_ui_v4.md, раздел 6.3, правило безопасности).

        Безопасный шаг (у обработчика своё окно: разобрать конфликт, отправить на сервер, занять файл, вход, выбор
        папки) - invoke() той же кнопки: тот же обработчик с теми же проверками и окнами; серая кнопка не срабатывает.
        Остальное - только фокус на кнопку (получить новое, починить, восстановить: пишут на диск без окна) или
        открыть её вкладку (сохранение - курсор в поле «Что вы сделали?»): действие делает человек. Другой проект -
        выбрать его. Состояние уже сменилось (текст вот-вот обновится) - ничего не делаем."""
        step = self._hint_step(self._selected_item())
        if self._step_signature(step) != self._step_signature(shown) or step.quiet:
            return
        click = step.click
        if click == next_step.CLICK_SELECT_PROJECT:
            self._select_mine_row(self._hint_other_iid)
            return
        if click == next_step.CLICK_NONE:
            return
        if click in (next_step.CLICK_OPEN_TAB, next_step.CLICK_OPEN_TAB_INVOKE) and step.tab:
            self.open_project_tab(step.tab)
        if click == next_step.CLICK_OPEN_TAB:
            entry = self._commit_panel.entry
            if step.tab == "changes" and not entry.instate(["disabled"]):
                entry.focus_set()                 # сохранение: напишите, что сделали, и нажмите кнопку сами
            return
        button = self._step_button(step.button)
        if button is None or not button.winfo_exists():
            return
        self._reveal_button(button)
        if click == next_step.CLICK_FOCUS:
            button.focus_set()
            return
        if not button.instate(["disabled"]):     # CLICK_INVOKE, CLICK_OPEN_TAB_INVOKE: серая кнопка не срабатывает
            button.invoke()

    def _select_mine_row(self, iid: str) -> None:
        """Выбирает проект в таблице «Мои проекты» (подсказка о другом проекте); поиск, который его прячет, сбрасывается."""
        if not iid or iid not in self.mine:
            return
        if self._list_mode() != "mine":
            self.select_list_mode("mine")
        if iid not in self.mine_tree.get_children():
            self.mine_filter_var.set("")
        self.mine_tree.selection_set(iid)
        self.mine_tree.focus(iid)
        self.mine_tree.see(iid)

    @staticmethod
    def _where_text(item: Optional[MineItem]) -> str:
        """Поле {where} текстов next_step: откуда проект («Хранится на этом компьютере» / «Проект с сервера»)."""
        return messages.LOCAL_TEXT if item is not None and item.local else messages.SERVER_TEXT

    UPDATE_RUNNING_TEXT = "Получаю новое…"     # подпись серой «Получить новое», пока оно идёт

    def _operation_running(self) -> bool:
        """Идёт долгая операция, ход которой показывает правая часть панели подсказок. Фоновая проверка проектов
        (раз в несколько секунд) операцией не считается: о ней панель молчит."""
        return (self.upd is not None or self.job is not None or self._committing or self._locking
                or self._publish_busy or self._conn_checking or bool(self._tortoise_open))

    def _sync_hint_progress(self) -> None:
        """Правая часть панели подсказок: ход получения нового или скачивания (с «Отменой»), ход сохранения, занятия
        файлов, окна TortoiseSVN (без отмены). Операции нет - правая часть пуста; в режиме «только ход работы» панель
        видна, только пока что-то идёт."""
        bar = getattr(self, "hint_bar", None)
        if bar is None or not hasattr(self, "mine_status"):
            return
        status = self.mine_status.get().strip()
        if self.upd is not None:
            bar.set_progress(status or self.UPDATE_RUNNING_TEXT, cancel=self._on_cancel_update,
                             cancel_enabled=not self.upd.cancel.cancelled)
        elif self.job is not None:
            done = self.status_var.get().strip()
            bar.set_progress(f"«{self.job.name}»: {done}" if done else f"Скачиваю «{self.job.name}»…",
                             cancel=self._on_cancel, cancel_enabled=not self.job.cancel.cancelled)
        elif self._operation_running():
            bar.set_progress(status)
        else:
            bar.clear_progress()
        self._place_hint_bar()

    def _status_error(self, text: str) -> None:
        """Тихий сбой без окна («Нужен вход на сервер»): совет в панели подсказок на ERROR_MS.

        Пока идёт операция (например, открыто окно TortoiseSVN, а из уведомления нажали «Получить»), mine_status - это
        её ход справа в панели: сбой туда не пишем (он выглядел бы ходом работы, и совета не было бы), а сразу
        показываем советом слева. Ход операции остаётся на месте."""
        bar = getattr(self, "hint_bar", None)
        if bar is not None and self._operation_running():
            bar.show_error(text)
            return
        self._mine_status_kind = "error"
        self.mine_status.set(text)

    def _on_mine_status(self) -> None:
        """Записан ход или итог операции (mine_status). Пока операция идёт - это ход справа в панели; после неё -
        итог слева на RESULT_MS (тихий сбой - на ERROR_MS). Итог по проекту, который уже написан в видимой карточке
        (note_label), панель не повторяет."""
        kind, self._mine_status_kind = self._mine_status_kind, ""
        self._refresh_lamp()
        self._sync_hint_progress()
        self._sync_hint_background()              # операция началась или кончилась, вход не выполнен
        text = self.mine_status.get().strip()
        bar = getattr(self, "hint_bar", None)
        if bar is None or not text or self._operation_running():
            return
        if kind == "error":
            bar.show_error(text)
            return
        item = self._selected_item()
        if (item is not None and self._card_visible()
                and self._action_notes.get(os.path.normcase(item.path), "").strip() == text):
            return
        bar.show_result(text)

    def _mine_row_hint(self, iid: str) -> str:
        """Пояснение строки таблицы проектов: имя, состояние и что сделать."""
        item = self.mine.get(iid)
        if item is None:
            return ""
        state, _tag, todo = self._state_cells(item)
        text = f"«{item.display_name}»: {self._sentence(state)}"
        if todo.strip(" ."):
            text += f" Что сделать: {self._sentence(todo)}"
        return text + " Двойной щелчок откроет «Состояние проекта»."

    def _refresh_project_panel(self) -> None:
        """Шапка и вкладки колонки 3 по выбранному проекту. Другой проект: незаконченные загрузки отменяются."""
        panel, item = self.project_panel, self._selected_item()
        key = os.path.normcase(item.path) if item is not None else ""
        if key != self._panel_key:
            self._panel_key = key
            self._tab_loads.reset()
            panel.incoming_view.clear()
            panel.history_view.clear()
            panel.incoming_view.set_message("")
            panel.history_view.set_message("")
            self._commit_panel.entry.delete(0, "end")        # комментарий относится к прежнему проекту
            self._shown_changes = self._shown_locks = None
            self._auto_tab_key = key                         # открыть ли «Изменения», решит первая проверка проекта
        if item is None:
            panel.clear_header(self._mine_hint_text)
        else:
            panel.set_header(*self._header_fields(item), local=item.local)
        panel.set_local_view(item is not None and item.local)
        panel.set_incoming_count(item.status.incoming if item is not None and item.state == "svn" and not item.local
                                 and item.status is not None else 0)
        self._sync_commit_view(item)
        self._sync_locks_view(item)
        self._sync_lines_view()
        self._open_changes_if_needed(item)
        self._load_project_tab()
        self._sync_hint_background()

    def _typing_comment(self) -> bool:
        """Курсор в поле комментария на «Изменениях»: человек печатает, вкладки у него под руками не меняются."""
        return str(self.root.tk.call("focus")) == str(self._commit_panel.entry)

    def _open_changes_if_needed(self, item: Optional[MineItem]) -> None:
        """Выбрали другой проект, и у него конфликт или несохранённые правки: открывается вкладка «Изменения».
        Решение принимается один раз на выбор проекта, по первой проверке (автопроверка раз в несколько секунд вкладку
        не трогает), и не мешает вводу: пока курсор в поле комментария или человек сам выбрал вкладку, ничего не меняется."""
        if item is None or not self._auto_tab_key or self._auto_tab_key != os.path.normcase(item.path):
            return
        if item.state != "svn":
            self._auto_tab_key = ""
            return
        status = item.status
        if status is None:
            if item.error and not item.checking:      # проверка не удалась: больше ждать нечего
                self._auto_tab_key = ""
            return
        self._auto_tab_key = ""
        if not (status.conflicts or status.local_changes):
            return
        if self._typing_comment():
            return
        # файл изменён, но не занят: подсвечена «Занять файл…» - открываем её вкладку, а не «Изменения»
        tab = "locks" if self._main_action_of(item) == "lock" else "changes"
        self.project_tabs.select(self.project_tab_frames[tab])

    # вкладка «Изменения»: панель постоянная, список правок берётся из последней проверки проекта

    def _sync_commit_view(self, item: Optional[MineItem]) -> None:
        panel, commit = self.project_panel, self._commit_panel
        self._commit_item = item
        changes = list(item.status.local_changes) if item is not None and item.state == "svn" and item.status else []
        panel.set_changes_count(len(changes))
        key = (os.path.normcase(item.path) if item is not None else "",
               tuple((c.path, c.kind, c.locked_by_me, c.needs_lock) for c in changes))
        if key != self._shown_changes and not self._committing:      # во время сохранения список не трогаем
            commit.set_changes(changes)
            self._shown_changes = key
        self._sync_commit_enabled()

    def _sync_commit_enabled(self) -> None:
        """«Сохранить отмеченное» недоступно, пока идёт сохранение, получение нового, занятие файлов или работает
        TortoiseSVN в этой папке, и когда проект - версия (в неё сохранять нельзя). Других кнопок сохранения нет."""
        item = self._commit_item
        if self._committing:
            self._commit_panel.set_enabled(False, messages.COMMIT_SAVING)
        elif item is not None and item.line is not None and item.line.read_only:
            self._commit_panel.set_enabled(False, messages.COMMIT_TAG_REFUSED)
        elif item is not None and (self._path_busy(item.path) or self.upd or self.job or self._locking):
            self._commit_panel.set_enabled(False, messages.COMMIT_BUSY)
        else:
            self._commit_panel.set_enabled(True)

    # вкладка «Занятые»: данные уже есть в последней проверке проекта

    def _sync_locks_view(self, item: Optional[MineItem]) -> None:
        panel = self.project_panel
        if item is None or item.state != "svn" or item.local:
            panel.clear_locks(messages.PANEL_NOT_SVN if item is not None and not item.local else "")
            self._shown_locks = None
            return
        st = item.status
        if st is None:
            panel.clear_locks(messages.LOCKS_UNCHECKED)
            self._shown_locks = None
            return
        key = (os.path.normcase(item.path),
               tuple((lk.path, lk.owner, lk.created) for lk in (*st.my_locks, *st.other_locks)), len(st.my_locks))
        if key != self._shown_locks:                  # таблицу не пересобираем без нужды: сбросилось бы выделение
            panel.set_locks(item.path, st.my_locks, st.other_locks)
            self._shown_locks = key

    def _on_panel_release(self, paths: list[str]) -> None:
        """«Освободить…» на вкладке «Занятые»: выбранные свои файлы, а без выбора - окно выбора своих файлов.
        Среди выбранных есть файлы с несохранёнными правками - сначала вопрос (окно выбора такие файлы не отмечает:
        освобождённый файл займёт коллега, и правки разойдутся)."""
        if not paths:
            self._on_release_lock()
            return
        item = self._selected_item()
        if item is None or item.state != "svn" or not self.client or self._path_busy(item.path):
            return
        unsaved = self._unsaved_of(item, paths)
        if unsaved and not messagebox.askyesno(
                messages.UNLOCK_UNSAVED_TITLE, messages.unlock_unsaved_question([os.path.basename(p) for p in unsaved]),
                icon="warning", default="no", parent=self.root):
            return
        self._run_locks(item, paths, lock=False)

    @staticmethod
    def _unsaved_of(item: MineItem, paths: list[str]) -> list[str]:
        """Те из paths, в которых по последней проверке проекта есть несохранённые правки."""
        def key(path: str) -> str:
            return os.path.normcase(os.path.abspath(path if os.path.isabs(path) else os.path.join(item.path, path)))

        changed = {key(c.path) for c in item.status.local_changes} if item.status is not None else set()
        return [p for p in paths if key(p) in changed]

    # вкладки «Новое» и «История»: читаются, когда их открывают, и заново, когда у проекта что-то поменялось

    def _project_tab_shown(self, key: str) -> bool:
        """Вкладка колонки 3 сейчас на виду: выбрана подвкладка «Мои проекты», колонка развёрнута, выбрана эта вкладка."""
        return (not self.mine_col3.collapsed and self._list_mode() == "mine"
                and self.project_panel.current_tab() == key)

    def _load_project_tab(self) -> None:
        """Догружает открытую вкладку выбранного проекта (идемпотентно: ту же версию заново не читает)."""
        if not hasattr(self, "project_panel"):
            return
        if self._project_tab_shown("incoming"):
            self._load_log_tab("incoming", self.project_panel.incoming_view)
        elif self._project_tab_shown("history"):
            self._load_log_tab("history", self.project_panel.history_view)
        elif self._project_tab_shown("lines"):
            self._sync_lines_view()

    def _log_tab_blocker(self, item: Optional[MineItem]) -> str:
        """Почему вкладку не прочитать сейчас (текст для человека) или пустая строка."""
        if item is None:
            return messages.PANEL_NO_PROJECT
        if item.state == "broken":
            return messages.PANEL_BROKEN
        if item.state != "svn":
            return messages.PANEL_NOT_SVN
        if item.local:      # история на компьютере читается без сервера
            return localrepo.MISSING_TEXT if item.history_missing else ("" if self.client else messages.PANEL_OFFLINE)
        return messages.PANEL_OFFLINE if self._offline or not self.client else ""

    def _load_log_tab(self, tab: str, view) -> None:
        item = self._selected_item()
        blocked = self._log_tab_blocker(item)
        if blocked:
            view.clear()
            view.set_message(blocked)
            self._tab_loads.invalidate(tab)
            return
        head = item.status.head if item.status else None
        marker = (os.path.normcase(item.path), item.revision, head if tab == "incoming" else None)
        if not self._tab_loads.needed(tab, marker):
            return
        ticket = self._tab_loads.start(tab, marker)
        client, path = self.client, item.path
        view.clear()
        if tab == "incoming":
            view.set_message(messages.INCOMING_LOADING)
            work = lambda: client.incoming_log(path, ticket.cancel)[::-1]     # noqa: E731  новые сверху
        else:
            view.set_message(messages.HISTORY_LOADING)
            work = lambda: client.recent_log(path, self.HISTORY_LIMIT, ticket.cancel)     # noqa: E731
        empty = messages.INCOMING_EMPTY if tab == "incoming" else messages.HISTORY_EMPTY
        self.run_auth(work, lambda entries: self._on_log_tab_loaded(ticket, view, entries, empty),
                      lambda kind: self._on_log_tab_failed(ticket, view, kind))

    def _on_log_tab_loaded(self, ticket, view, entries, empty: str) -> None:
        if self._tab_loads.finish(ticket, True):
            view.set_entries(entries)
            view.set_message("" if entries else empty)

    def _on_log_tab_failed(self, ticket, view, kind: ErrorKind) -> None:
        """Сбой чтения вкладки: «Нет связи» или короткое объяснение в самой вкладке, без окна ошибки."""
        if kind is ErrorKind.CANCELLED or not self._tab_loads.finish(ticket, False):
            return
        if kind in (ErrorKind.NO_CONNECTION, ErrorKind.TIMEOUT):
            text = messages.PANEL_OFFLINE
        elif kind is ErrorKind.AUTH:
            text = messages.PANEL_AUTH
        else:
            text = messages.PANEL_LOAD_FAILED.format(reason=messages.short_message(kind))
        view.set_message(text)

    def _on_mine_select(self) -> None:
        if self.mine_tree.selection() == (self._auto_selected,):
            self._auto_selected = ""    # автовыбор после поиска не считается «открытием» проекта
        else:
            self._touch_opened(self._selected_item())
        self._refresh_project_panel()
        self._update_mine_buttons()

    def _update_mine_buttons(self) -> None:
        if not hasattr(self, "update_btn"):
            return
        busy = self.upd is not None or self.job is not None
        item = self._selected_item()
        ready = bool(self.client) and not busy
        is_svn = bool(item and item.state == "svn")
        local = bool(item and item.local)                        # история на этом компьютере: упрощённая карточка
        plain = bool(item and item.state == "plain")             # папка без истории и без сервера
        missing = bool(local and item.history_missing)
        on = lambda flag: "normal" if flag else "disabled"  # noqa: E731
        free = bool(item) and not self._path_busy(item.path)   # папку не держит TortoiseSVN или починка
        # получение нового, занятие и освобождение файлов меняют рабочую копию: при открытом TortoiseSVN
        # и во время другой такой операции нельзя
        can_change = ready and is_svn and free and not self._locking and not self._committing
        read_only = bool(is_svn and item.line is not None and item.line.read_only)   # версия (тег): только чтение
        self.update_btn.configure(state=on(can_change))
        # у проекта без сервера (история на компьютере или обычная папка) нет нового на сервере и адреса сервера
        for button in (self.update_btn, self.server_btn):
            self._show(button, not (local or plain))
        # «Сохранить у себя» живёт справа, на «Изменениях» (сохраняются все правки, а не отмеченные)
        self.project_panel.set_local_save(local and not missing, can_change and not missing)
        self._show(self.publish_btn, local or plain)
        self._show(self.continue_btn, missing)
        can_send = (ready and free and not self._locking and not self._committing and not self._publish_running()
                    and item is not None and self._can_publish(item))
        self.publish_btn.configure(state=on(can_send and not self._offline))      # без связи отправить нельзя
        self.continue_btn.configure(state=on(missing and ready and free and not self._locking and not self._committing))
        self.check_btn.configure(state=on(bool(self.client) and not busy))
        self.rescan_btn.configure(state="disabled" if busy else "normal")

        has_conflicts = bool(is_svn and item.status and item.status.conflicts)
        # главное действие по состоянию: одна кнопка в стиле Accent.TButton, остальные обычные
        main = self._main_action_of(item)
        # на экране подсвечена одна кнопка: слева (разобрать, починить, восстановить, получить, отправить) или
        # справа, где сохранение (на «Изменениях»)
        main_buttons = {"resolve": self.special_btn, "repair": self.special_btn, "restore": self.special_btn,
                        "update": self.update_btn, "publish": self.publish_btn}
        for button in (self.special_btn, self.update_btn, self.publish_btn):
            button.configure(style="Accent.TButton" if main_buttons.get(main) is button else "TButton")
        self.project_panel.set_save_accent(main == "commit")
        self.project_panel.set_lock_accent(main == "lock")
        # особая кнопка видна, только когда она и есть главное действие (конфликт, повреждённая папка, пропавшая история)
        if main == "restore":
            self.special_btn.configure(text=localrepo.RESTORE_BUTTON, command=self._on_restore_history,
                                       state=on(ready and free and not self._locking and not self._committing))
        elif main == "repair":
            self.special_btn.configure(text="Починить папку", command=self._on_cleanup, state=on(ready and free))
        elif main == "resolve":
            self.special_btn.configure(text="Разобрать конфликт", command=self._on_resolve,
                                       state=on(ready and free and not self._locking and not self._committing))
        self._show(self.special_btn, main in ("resolve", "repair", "restore"))
        if main in ("resolve", "repair", "restore"):
            self.actions_row.set_hint(self.special_btn, f"project.{main}")     # пояснение и значок
        self.new_project_btn.configure(state=on(not busy and not self._new_project_running()))

        incoming = item.status.incoming if is_svn and item.status else 0
        if self.upd is not None and not self.upd.url:      # ход и «Отмена» - в панели подсказок внизу окна
            self.update_btn.configure(text=self.UPDATE_RUNNING_TEXT)
        else:
            self.update_btn.configure(text=f"Получить новое ({incoming})" if incoming else "Получить новое")
        self.more_btn.configure(state=on(item))
        self.open_btn.configure(state=on(item))
        self.server_btn.configure(state=on(item and self._server_dir(item)))
        if "commit" in self._more_idx:
            self.more_menu.entryconfigure(self._more_idx["commit"], state=on(can_change and not read_only))
        if "publish" in self._more_idx:
            self.more_menu.entryconfigure(self._more_idx["publish"], state=on(can_send and not self._offline))
        self._sync_archive_entry(item, on(ready and free and not self._locking and not self._committing
                                          and not self._publish_running()))
        if "experts" in self._more_idx:
            self.more_menu.entryconfigure(self._more_idx["experts"], state=on(item))
        entries = {"log": is_svn and free, "revert": is_svn and free, "resolve": has_conflicts and free,
                   "cleanup": bool(item and item.state in ("svn", "broken") and ready and free)}
        for key, idx in self._expert_idx.items():
            self.expert_menu.entryconfigure(idx, state=on(entries[key]))
        self.project_panel.set_locks_enabled(can_change and not read_only, can_change)
        self._sync_commit_enabled()
        if hasattr(self, "switch_here_btn"):
            self._update_lines_buttons()
        self._sync_hint_progress()
        self._sync_hint_background()

    def _main_action_of(self, item: Optional[MineItem]) -> Optional[str]:
        """Главное действие выбранного проекта (main_action по его состоянию) или None, если проект не выбран.
        Факты те же, что у строки «Что сделать» и панели подсказок (_facts_of, в том числе «нужный файл занят
        другим» - тогда подсвечивать нечего): подсветка и подсказка не расходятся."""
        if item is None:
            return None
        button = next_step.main_button(self._facts_of(item))
        return MAIN_ACTIONS[button] if button is not None else None

    def _on_mine_double_click(self, event: tk.Event) -> None:
        """Двойной щелчок по проекту: разворачивается «Состояние проекта» на вкладке главного действия. Это путь
        к сохранению, когда «Состояние» свёрнуто: кнопка сохранения живёт в нём."""
        if self.mine_tree.identify_row(event.y) == "" or self.mine_tree.identify_region(event.x, event.y) != "cell":
            return
        item = self._selected_item()
        if item is None:
            return
        tab = self.MAIN_ACTION_TAB.get(self._main_action_of(item) or "")
        if tab is not None:
            self.open_project_tab(tab)
        else:
            self.mine_col3.expand()

    def _sync_archive_entry(self, item: Optional[MineItem], state: str) -> None:
        """Пункт «Ещё → Удалить прежние сохранения…» есть, только когда у проекта на сервере остались прежние
        сохранения на этом компьютере (архив после отправки): удаляются они только по этой явной команде."""
        want = bool(item and item.archive and not item.local)
        if want and self._archive_idx is None:
            self.more_menu.add_command(label=localrepo.DELETE_ARCHIVE_MENU, command=self._on_delete_archive)
            self._archive_idx = self.more_menu.index("end")
        elif not want and self._archive_idx is not None:
            self.more_menu.delete(self._archive_idx)
            self._archive_idx = None
        if self._archive_idx is not None:
            self.more_menu.entryconfigure(self._archive_idx, state=state)

    def _on_save_local_clicked(self) -> None:
        """«Сохранить у себя» на «Изменениях»: проект, чей список правок сейчас показан."""
        item = self._commit_item
        if item is not None and item.local:
            self._save_locally(item)

    def _on_open_mine(self) -> None:
        self._touch_opened(self._selected_item())
        self._open_folder(self._selected_item())

    def _server_dir(self, item: Optional[MineItem]) -> str:
        """Папка проекта на диске-хранилище (Z:\\…) по соответствиям «mirrors»; пусто, если не определить."""
        if item is None or item.state != "svn" or not item.project_prefix or not item.project_segments:
            return ""
        return mirrors.disk_path(item.project_prefix, item.project_segments, self.config.mirrors, self.config.server)

    def _on_open_server(self) -> None:
        self._touch_opened(self._selected_item())
        self._open_server_folder(self._selected_item())

    def _open_server_folder(self, item: Optional[MineItem]) -> None:
        """Открывает папку проекта на сервере (диск Z) в Проводнике."""
        path = self._server_dir(item)
        if not path:
            return
        if not os.path.isdir(path):
            messagebox.showinfo("Открыть на сервере", f"Папка недоступна:\n{path}\n\nПроверьте, подключён ли диск.",
                                parent=self.root)
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            log.exception("Не удалось открыть папку на сервере")

    @staticmethod
    def _open_folder(item: Optional[MineItem]) -> None:
        """Открывает папку проекта в Проводнике."""
        if item:
            try:
                os.startfile(item.path)  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                log.exception("Не удалось открыть папку")

    # окна TortoiseSVN (журнал, разбор конфликта, починка папки, дополнительный путь сохранения)

    def _on_commit_tortoise(self) -> None:
        """«Ещё → Сохранить через TortoiseSVN…»: дополнительный путь, основной - _on_commit."""
        self._on_tortoise("commit")

    def _on_resolve_tortoise(self) -> None:
        """«Ещё → Для опытных → Разобрать конфликт в TortoiseSVN»: дополнительный путь, основной - _on_resolve."""
        self._on_tortoise("resolve")

    def _on_tortoise(self, command: str, note: str = "", item: Optional[MineItem] = None) -> None:
        """Открывает окно TortoiseSVN для проекта (по умолчанию выбранного в «Мои проекты»); ждёт его закрытия
        в фоне и перепроверяет проект.

        note — короткая подсказка, что делать в окне (показывается в строке состояния).
        """
        item = item or self._selected_item()
        if not item or not self.tortoise_exe or self._path_busy(item.path):
            return
        try:
            args = tortoise.build_args(self.tortoise_exe, command, [item.path])
        except ValueError:
            log.exception("Не удалось собрать команду TortoiseSVN")
            return
        self._tortoise_open.add(item.path)
        self.mine_status.set((note + " " if note else "Открыто окно TortoiseSVN. ")
                             + "Когда закончите, состояние проекта обновится само.")
        self._update_mine_buttons()
        self.run_bg(lambda: tortoise.launch(args).wait(),
                    lambda _res, err, _stamp: self._on_tortoise_closed(item, command, err))

    def _on_tortoise_closed(self, item: MineItem, command: str, error: Optional[Exception]) -> None:
        self._tortoise_open.discard(item.path)
        self.mine_status.set("")
        self._update_mine_buttons()
        item = self._current(item) or item   # за время работы окна список проектов могли обновить
        if error is not None:
            log.error("TortoiseSVN (%s) не запустился", command, exc_info=error)
            self.show_error("Не удалось открыть TortoiseSVN",
                            "Не удалось открыть окно TortoiseSVN. Проверьте, что программа установлена.",
                            action=f"Запуск TortoiseSVN ({command}) для {item.name}", raw=str(error))
            return
        if command == "cleanup":
            self._reprobe(item)
        elif command != "log":
            self._check_status([item])

    # конфликт: свой выбор «Оставить мою версию / Взять версию с сервера»

    def _on_resolve(self) -> None:
        """«Разобрать конфликт»: для каждого конфликтного файла выбор версии (без TortoiseSVN)."""
        item = self._selected_item()
        if item is not None:
            self._start_resolve(item)

    def _start_resolve(self, item: MineItem) -> None:
        """Читает список конфликтных файлов проекта и спрашивает, какую версию оставить в каждом."""
        client = self.client
        if not client or item.state != "svn":
            return
        if self._path_busy(item.path) or self._committing or self.upd or self.job or self._locking:
            messagebox.showinfo(messages.CONFLICT_TITLE, messages.COMMIT_BUSY, parent=self.root)
            return
        rules = self.config.filter

        def conflicted() -> list[str]:
            return [c.path for c in client.status(item.path, rules=rules).local_changes if c.kind == "conflicted"]

        self.run_auth(conflicted, lambda files: self._ask_resolve(item, files),
                      lambda kind: self.show_error(messages.CONFLICT_FAILED_TITLE, messages.short_message(kind),
                                                   action=f"Поиск конфликтов проекта {item.name}", kind=kind,
                                                   extra={"Папка": item.path}))

    def _ask_resolve(self, item: MineItem, files: list[str]) -> None:
        if self._closing:
            return
        if not files:
            messagebox.showinfo(messages.CONFLICT_TITLE, messages.CONFLICT_NONE, parent=self.root)
            self._check_status([item])
            return
        choices: list[tuple[str, str]] = []
        for index, path in enumerate(files, 1):
            name = os.path.relpath(path, item.path) if os.path.isabs(path) else path
            dialog = ConflictDialog(self.root, name, index, len(files))
            self.root.wait_window(dialog)
            if dialog.result is None:
                return                  # «Отмена»: ничего не меняем, в том числе в уже просмотренных файлах
            choices.append((path, dialog.result))
        self._run_resolve(item, choices)

    def _run_resolve(self, item: MineItem, choices: list[tuple[str, str]]) -> None:
        client = self.client
        if not client or self._locking or self._committing:
            return
        self._locking = True            # пока решается конфликт, копию трогать нельзя, как при занятии файлов
        self._refresh_mine_progress()
        self._update_mine_buttons()
        self.mine_status.set(messages.CONFLICT_RESOLVING)

        def work() -> None:
            for path, accept in choices:
                client.resolve(path, accept)

        def done(_res, error, _stamp: JobStamp) -> None:
            self._locking = False
            self._refresh_mine_progress()
            self._update_mine_buttons()
            self._drain_check_queue()
            if self._closing:
                return
            current = self._current(item) or item
            if error is not None:
                self._resolve_failed(current, error)
            else:
                self.mine_status.set(messages.CONFLICT_DONE)
                self._action_notes[os.path.normcase(current.path)] = messages.CONFLICT_DONE
            current.status, current.checked = None, False
            self._refresh_row(current)
            self._check_status([current])

        self.run_bg(work, done)

    def _resolve_failed(self, item: MineItem, error: Exception) -> None:
        self.mine_status.set("")
        kind = self._error_kind(error)
        raw = self._error_raw(error)
        if "E155027" in raw or "W195024" in raw:      # конфликт другого вида (например, файл удалили на сервере)
            text = (messages.CONFLICT_UNSUPPORTED_TORTOISE if self.tortoise_exe
                    else messages.CONFLICT_UNSUPPORTED_ADMIN)
        else:
            text = messages.short_message(kind)
        self.show_error(messages.CONFLICT_FAILED_TITLE, text, action=f"Решение конфликта проекта {item.name}",
                        kind=kind, raw=raw, extra={"Папка": item.path})

    def _on_cleanup(self) -> None:
        """Починить папку: через TortoiseSVN, а если его нет, самим svn cleanup."""
        item = self._selected_item()
        if not item or item.state not in ("svn", "broken"):
            return
        if self.tortoise_exe:
            self._on_tortoise("cleanup")
            return
        client = self.client
        if not client or self._path_busy(item.path):
            return
        self._tortoise_open.add(item.path)   # на время починки папка занята: второй запуск и получение нового не нужны
        self.mine_status.set("Чиню папку…")
        self._update_mine_buttons()

        def done(_res, err, _stamp) -> None:
            self._tortoise_open.discard(item.path)
            self.mine_status.set("")
            self._update_mine_buttons()
            if err is not None:
                kind = self._error_kind(err)
                self.show_error("Не удалось починить папку", messages.short_message(kind),
                                action=f"Починка папки проекта {item.name}", kind=kind,
                                raw=self._error_raw(err), extra={"Папка": item.path})
                return
            self._reprobe(item)

        self.run_bg(lambda: client.cleanup(item.path), done)

    def _reprobe(self, item: MineItem) -> None:
        """Заново определяет, что за папка (после починки), и проверяет её на сервере."""
        client = self.client
        self.run_bg(lambda: self._probe_item(client, item.path, item.name),
                    lambda res, err, _stamp: self._on_reprobed(item, res, err))

    def _on_reprobed(self, item: MineItem, fresh: Optional[MineItem], error) -> None:
        if error is not None or fresh is None:
            log.error("Не удалось заново прочитать папку %s", item.path, exc_info=error)
            return
        item = self._current(item)
        if item is None:   # проект исчез из списка, пока шла проверка
            return
        item.take_probe(fresh)
        item.status, item.checked, item.last_check = None, False, None
        self._refresh_row(item)
        self._check_status([item])

    # проверка состояния на сервере

    def _on_check_all(self) -> None:
        self._check_status([i for i in self.mine.values() if i.state == "svn"])

    def _on_refresh_tab(self) -> None:
        """Кнопка «⟳»: заново читает всё, что видно на вкладке «Мои проекты», в том числе вкладки колонки 3."""
        self._auto_auth_failed = False
        if self._mine_dirty:
            self._scan_projects()
            return
        self._line_history.clear()
        self._line_locks.clear()
        for tab in ("incoming", "history"):
            self._tab_loads.invalidate(tab)
        self._load_project_tab()
        if self._lines_tab_active():
            self._on_lines_refresh()
        if self._scan_running:
            self._check_all_after_scan = True      # поиск уже идёт и заменит строки: одна проверка после него
            return
        for item in self.mine.values():
            if item.state == "svn":
                self._refresh_row(item)    # описания перечитаются
        self._on_check_all()

    def _auto_refresh_tick(self) -> None:
        """Раз в REFRESH_SEC секунд сама обновляет открытую вкладку «Мои проекты» (и ветки в колонке 3)."""
        if self._closing:
            return
        self.root.after(REFRESH_SEC * 1000, self._auto_refresh_tick)
        if self.root.state() == "withdrawn":
            return                # окно спрятано в трей: смотреть на него некому, фоновые проверки делает уведомлятор
        self._refresh_lamp()
        if (not self.client or self.upd or self.job or self._auto_auth_failed or self._mine_dirty
                or self._scan_running):
            return
        if self.login.dialog_open or self.login.waiters or self.error_open:
            return
        if self._list_mode() != "mine":
            return
        self._check_status([i for i in self.mine.values()
                            if i.state == "svn" and not i.checking and not self._path_busy(i.path)])
        if self._lines_tab_active():
            self._reload_lines_quietly()

    def _auto_check(self) -> None:
        """Сама проверяет проекты, которые не проверялись последние AUTO_CHECK_SEC секунд."""
        if not self.client or self._closing:
            return
        now = time.monotonic()
        due = [i for i in self.mine.values() if i.state == "svn" and not i.checking
               and not self._path_busy(i.path)
               and (i.last_check is None or now - i.last_check >= AUTO_CHECK_SEC)]
        self._check_status(due)

    def _check_status(self, items: list[MineItem]) -> None:
        """Проверка статуса с сервера. Если проверка уже идёт, проекты встают в очередь (параллельно не запускаем)."""
        current = [self._current(i) for i in items if i.state == "svn"]   # строки могли быть заменены поиском проектов
        items = []
        for item in current:
            if item is not None and not any(item is i for i in items):
                items.append(item)
        if not items or not self.client:
            return
        if self.upd or self._committing or self._locking:
            # во время получения нового, сохранения и занятия файлов не проверяем: очередь разберёт конец
            # операции (checking не ставим: проверки ещё нет)
            self._queue_checks(items)
            return
        for item in items:
            item.checking = True
            item.last_check = time.monotonic()
            self._refresh_row(item)
        if self._checking:
            self._queue_checks(items)
            return
        self._checking = True
        self._check_batch = list(items)
        self._refresh_mine_progress()
        self._update_mine_buttons()
        stamp = self.login.stamp()
        rules = {id(i): self._rules_for(i) for i in items}   # мусор среди новых файлов не считается несохранённой правкой
        client = self.client           # клиент берётся здесь, в главном потоке: поток его не подменит

        def worker() -> None:
            for idx, item in enumerate(items):
                try:
                    status = client.status(item.path, check_server=True, rules=rules[id(item)])
                except SvnError as exc:
                    if exc.kind is ErrorKind.AUTH:
                        self.post(self._status_auth, items[idx:], stamp)
                        return
                    self.post(self._on_item_status, item, None, messages.short_message(exc.kind))
                    continue
                except Exception:
                    log.exception("Ошибка проверки %s", item.path)
                    self.post(self._on_item_status, item, None, messages.short_message(ErrorKind.OTHER))
                    continue
                self.post(self._on_item_status, item, status, "")
            self.post(self._status_done)

        threading.Thread(target=worker, daemon=True).start()

    def _queue_checks(self, items: list[MineItem]) -> None:
        self._check_queue.extend(i for i in items if not any(q is i for q in self._check_queue))

    def _drain_check_queue(self) -> None:
        """Запускает проверки, дожидавшиеся конца получения нового, сохранения, занятия файлов или прошлой проверки."""
        queued, self._check_queue = self._check_queue, []
        if queued:
            self._check_status(queued)

    def _refresh_mine_progress(self) -> None:
        """Индикатор в шапке крутится, пока идёт хоть что-то из: проверка, получение нового, сохранение, занятие файлов."""
        self._mine_spinning = self._checking or self._locking or self._committing or self.upd is not None
        self._refresh_lamp()

    def _lamp_state(self) -> tuple[str, bool, str]:
        """(цвет, крутится ли, подсказка) для индикатора в шапке: серый — нечего показывать, зелёный — всё
        актуально, жёлтый — нужно получить новое или сохранить, красный — ошибка или конфликт."""
        p = self.palette
        busy = self._mine_spinning or self._lines_view_loading or self.job is not None
        if self.client is None:
            return p.danger, False, "Не найдена программа svn.exe (TortoiseSVN)"
        items = [i for i in self.mine.values() if i.state in ("svn", "broken")]
        offline = self._offline
        bad = [i for i in items if i.state == "broken" or i.history_missing or (i.status is None and i.error
                                                           and not self._is_offline_error(i.error))
               or (i.status is not None and i.status.conflicts)]
        warn = [i for i in items if i.status is not None and (i.status.incoming or i.status.tracked_changes
                                                              or i.status.switched)]
        if is_newer_version(self.config.latest_version, __version__):
            warn_text = f"Доступна новая версия программы {self.config.latest_version.strip()}"
        else:
            warn_text = ""
        if bad:
            color, text = p.danger, "Есть ошибка или конфликт: " + ", ".join(i.display_name for i in bad[:3])
        elif offline:
            color, text = p.warning, "Нет связи с сервером"
        elif warn or warn_text:
            names = ", ".join(i.display_name for i in warn[:3])
            color, text = p.warning, (f"Нужно получить новое или сохранить: {names}" if warn else warn_text)
        elif any(i.checked for i in items):
            color, text = p.success, "Всё актуально"
        else:
            color, text = p.muted, "Проекты ещё не проверены" if items else "Скачанных проектов пока нет"
        if busy:
            text = "Идёт работа…"           # что именно идёт - справа в панели подсказок внизу окна
        return color, busy, text

    def _refresh_lamp(self) -> None:
        if not hasattr(self, "lamp"):
            return
        color, busy, text = self._lamp_state()
        self.lamp_text = text
        self.lamp.set_state(color, busy)

    def _on_item_status(self, item: MineItem, status: Optional[WcStatus], error: str) -> None:
        item = self._current(item)    # результат мог прийти для строки, которую успели заменить
        if item is None:
            return
        item.status, item.error, item.checked = status, error, status is not None
        item.checking = False
        if item.local:                   # история на компьютере с сервером не связана: ни входа, ни полосы «Нет связи»
            if status is not None:
                item.history_missing = False
            elif error == messages.short_message(ErrorKind.LOCAL_HISTORY_MISSING):
                was_missing, item.history_missing = item.history_missing, True
                if not was_missing:
                    self._reprobe(item)         # где история теперь, выяснит фоновая проверка папки
        elif status is not None:
            self.login.reset_attempts()  # статус с -u ходил на сервер с учётными данными
            self._set_offline(False)     # сервер ответил: полосу «Нет связи» прячем
            self._forget_released_wanted(item, status)
        elif self._is_offline_error(error):
            self._set_offline(True)
        self._refresh_row(item)
        self._update_lock_warning()

    def _status_auth(self, items: list[MineItem], stamp: JobStamp) -> None:
        if self._closing:   # окно закрывается: входить незачем
            self._status_done()
            return
        self.login.request(retry=lambda: (self._end_check(), self._check_status(items)),
                           give_up=lambda: (setattr(self, "_auto_auth_failed", True), self._status_done(),
                                            self._status_error("Нужен вход на сервер.")),
                           job_gen=stamp)

    def _end_check(self) -> None:
        self._checking = False
        self._refresh_mine_progress()

    def _status_done(self) -> None:
        self._end_check()
        for batch_item in self._check_batch:
            item = self._current(batch_item)
            if item is not None and item.checking and not any(item is q for q in self._check_queue):
                item.checking = False   # проверка прервалась (например, отказ от входа): не оставляем «Идёт проверка…»
                self._refresh_row(item)
        self._check_batch = []
        self._update_mine_buttons()
        self._drain_check_queue()

    # напоминание о давно занятых файлах

    def _old_locks(self) -> list[tuple[MineItem, int]]:
        """(проект, возраст в днях) для каждого вашего файла, занятого дольше LOCK_WARN_DAYS."""
        found = []
        for item in self.mine.values():
            if item.status is None:
                continue
            for lock in item.status.my_locks:
                age = lock.age_days
                if age is not None and age > LOCK_WARN_DAYS:
                    found.append((item, age))
        return found

    def _update_lock_warning(self) -> None:
        old = self._old_locks()
        if not old:
            self.lock_warn.grid_remove()
            return
        count = len(old)
        oldest = max(age for _, age in old)
        self.lock_warn_var.set(
            f"Вы заняли {count} {messages.plural(count, ('файл', 'файла', 'файлов'))}, "
            f"самый давний — {messages.format_age(oldest)}. Освободите, если уже не правите.")
        self.lock_warn.grid()

    def _show_old_locks(self) -> None:
        """Выбирает проект с самым давним занятым файлом и открывает вкладку «Занятые»."""
        old = self._old_locks()
        if not old:
            return
        item = max(old, key=lambda pair: pair[1])[0]
        iid = self._iid_of(item)
        if iid:
            self.mine_tree.selection_set(iid)
            self.mine_tree.focus(iid)
            self.mine_tree.see(iid)
            self._on_mine_select()
            self.open_project_tab("locks")

    # сохранение на сервер средствами программы (svn add / rm / commit)

    def _commit_refusal(self, item: MineItem) -> str:
        """Почему сохранять сейчас нельзя (текст для человека) или пустая строка."""
        if item.line is not None and item.line.read_only:
            return messages.COMMIT_TAG_REFUSED
        if self._path_busy(item.path) or self._committing or self.upd or self.job or self._locking:
            return messages.COMMIT_BUSY
        return ""

    def _on_commit(self) -> None:
        """Находит правки выбранного проекта и показывает панель «Изменения»."""
        item = self._selected_item()
        client = self.client
        if not item or item.state != "svn" or not client:
            return
        refusal = self._commit_refusal(item)
        if refusal:
            messagebox.showinfo(messages.COMMIT_TITLE, refusal, parent=self.root)
            return
        self._action_notes.pop(os.path.normcase(item.path), None)
        self.mine_status.set(messages.COMMIT_LOADING)
        rules = self._rules_for(item)
        self.run_auth(lambda: client.status(item.path, rules=rules).local_changes,
                      lambda changes: self._show_found_changes(item, changes),
                      lambda kind: self._commit_load_failed(item, kind))

    def _save_locally(self, item: MineItem) -> None:
        """«Сохранить у себя»: все правки проекта с историей на компьютере одним сохранением, в фоне. Комментарий
        берётся из панели «Изменения», если человек его там написал, иначе подставляется сам."""
        client = self.client
        if not client or item.state != "svn" or not item.local or item.history_missing or self._committing:
            return
        refusal = self._commit_refusal(item)
        if refusal:
            messagebox.showinfo(localrepo.SAVE_TITLE, refusal, parent=self.root)
            return
        typed = self._commit_panel.entry.get().strip() if self._commit_item is item else ""
        comment = typed or localrepo.auto_comment()
        rules = self.config.filter
        self._action_notes.pop(os.path.normcase(item.path), None)
        self._committing = True
        self._refresh_mine_progress()
        self._update_mine_buttons()
        self._sync_commit_enabled()
        self.mine_status.set(localrepo.SAVING_TEXT)

        def done(result, error, _stamp: JobStamp) -> None:
            self._committing = False
            self._refresh_mine_progress()
            self._update_mine_buttons()
            self._drain_check_queue()
            if self._closing:
                return
            if error is None:
                self._commit_succeeded(item, result.committed if result else [], comment,
                                       result or CommitResult(revision=None), remember=bool(typed))
            else:
                self._commit_failed(item, [], comment, frozenset({"update", "lock"}), error, self._error_kind(error))

        self.run_bg(lambda: localrepo.save(client, item.path, rules, comment), done)

    def _commit_load_failed(self, item: MineItem, kind: ErrorKind) -> None:
        if kind is ErrorKind.AUTH:
            self._status_error("Нужен вход на сервер.")
        else:
            self.mine_status.set("")
        if kind is not ErrorKind.AUTH:
            self.show_error(messages.COMMIT_TITLE, messages.short_message(kind),
                            action=f"Поиск правок проекта {item.name}", kind=kind, extra={"Папка": item.path})

    def _show_found_changes(self, item: MineItem, changes: list) -> None:
        """Показывает найденные правки в панели «Изменения» колонки 3 (отдельного окна нет)."""
        if self._selected_item() is not item:       # пока искали правки, выбрали другой проект: ответ не нужен
            self.mine_status.set("")
            return
        self.mine_status.set("")
        self._commit_item = item
        key = (os.path.normcase(item.path), tuple((c.path, c.kind, c.locked_by_me, c.needs_lock) for c in changes))
        if key != self._shown_changes:                # тот же список: отмеченные галочки не сбрасываем
            self._commit_panel.set_changes(changes)
            self._shown_changes = key
        self.project_panel.set_changes_count(len(changes))
        self.open_project_tab("changes")
        self._sync_commit_enabled()

    def _reset_changes_panel(self, item: MineItem) -> None:
        """После сохранения или конфликта проекта item: если в панели «Изменения» именно он, комментарий очищается,
        а список правок ждёт новой проверки. Выбрали другой проект - его панель не трогаем, а только перерисовываем."""
        if self._commit_item is not None and same_dir(self._commit_item.path, item.path):
            self._commit_panel.entry.delete(0, "end")
            self._commit_panel.set_changes([])
            self._shown_changes = None
            self.project_panel.set_changes_count(0)
        else:
            self._sync_commit_view(self._selected_item())

    def _on_panel_save(self, paths: list[str], comment: str) -> None:
        """«Сохранить отмеченное» в панели «Изменения»: сохраняет правки проекта, чей список сейчас показан."""
        item = self._commit_item
        if item is not None and item.state == "svn":
            self._run_commit(item, paths, comment)

    def _run_commit(self, item: MineItem, paths: list[str], comment: str, prelude: Optional[Callable] = None,
                    tried: frozenset = frozenset()) -> None:
        """Сохраняет paths с комментарием в фоне. prelude(client) выполняется до сохранения в том же потоке и
        возвращает список конфликтов (получить новое) или пусто (занять файлы). tried: что уже пробовали
        ("update", "lock"), чтобы не ходить по кругу."""
        client = self.client
        if not client or self._committing:
            return
        self._committing = True
        self._refresh_mine_progress()
        self._update_mine_buttons()
        self._sync_commit_enabled()
        self.mine_status.set(messages.COMMIT_UPDATING if "update" in tried and prelude
                             else localrepo.SAVING_TEXT if item.local else messages.COMMIT_SAVING)

        def work():
            if prelude is not None:
                conflicts = prelude(client)
                if conflicts:
                    return None, conflicts
            result = client.commit(item.path, paths, comment)
            if item.local:      # корень папки догоняет сохранение: список прежних сохранений покажет и его
                client.update(item.path)
            return result, []

        def done(outcome, error, stamp: JobStamp) -> None:
            self._committing = False
            self._refresh_mine_progress()
            self._update_mine_buttons()
            self._drain_check_queue()
            if self._closing:
                return
            if error is None:
                result, conflicts = outcome
                if conflicts:
                    self._commit_conflict(item)
                else:
                    self._commit_succeeded(item, paths, comment, result)
                return
            kind = self._error_kind(error)
            if kind is ErrorKind.AUTH:
                self.login.request(retry=lambda: self._run_commit(item, paths, comment, prelude, tried), job_gen=stamp,
                                   give_up=lambda: self._status_error("Нужен вход на сервер."))
                return
            self._commit_failed(item, paths, comment, tried, error, kind)

        self.run_bg(work, done)

    def _commit_failed(self, item: MineItem, paths: list[str], comment: str, tried: frozenset,
                       error: Exception, kind: ErrorKind) -> None:
        self.mine_status.set("")
        self._sync_commit_enabled()
        parent = self.root
        title = localrepo.SAVE_TITLE if item.local else messages.COMMIT_TITLE
        if kind is ErrorKind.OUT_OF_DATE and "update" not in tried:
            if messagebox.askyesno(title, messages.COMMIT_OUT_OF_DATE, parent=parent):
                self._run_commit(item, paths, comment, lambda c: c.update(item.path).conflicts, tried | {"update"})
            return
        if kind is ErrorKind.LOCK_REQUIRED and "lock" not in tried:
            files = [line.strip() for line in self._error_raw(error).splitlines() if line.strip()]
            names = ", ".join(os.path.basename(f) for f in files[:5]) + (" и др." if len(files) > 5 else "")
            text = messages.COMMIT_LOCK_REQUIRED if len(files) == 1 else messages.COMMIT_LOCK_REQUIRED_MANY
            if messagebox.askyesno(title, text.format(names=names), parent=parent):
                def lock_files(client: SvnClient) -> list:
                    for f in files:
                        client.lock(f)
                    return []
                self._run_commit(item, paths, comment, lock_files, tried | {"lock"})
            return
        text = messages.COMMIT_STILL_OUT_OF_DATE if kind is ErrorKind.OUT_OF_DATE else messages.short_message(kind)
        self.show_error(title, text,
                        action=f"Сохранение {'у себя' if item.local else 'на сервер'} проекта {item.name}", kind=kind,
                        raw=self._error_raw(error),
                        extra={"Папка": item.path, "Файлов": len(paths)})

    def _commit_conflict(self, item: MineItem) -> None:
        """После получения нового появился конфликт: правки не сохранены, дальше только разбор конфликта."""
        item = self._current(item) or item
        self._reset_changes_panel(item)
        self.mine_status.set("Есть конфликт: сначала разберите его.")
        item.status, item.checked = None, False
        self._refresh_row(item)
        self._check_status([item])
        if messagebox.askyesno(messages.COMMIT_TITLE, messages.COMMIT_CONFLICT_ASK, parent=self.root):
            self._start_resolve(item)

    def _commit_succeeded(self, item: MineItem, paths: list[str], comment: str, result, remember: bool = True) -> None:
        if not item.local:                      # сервер ответил; история на компьютере с ним не связана
            self.login.reset_attempts()
            self._set_offline(False)
        if remember:                            # комментарий, подставленный самой программой, не запоминается
            recent = [comment] + [c for c in self.settings.recent_comments if c != comment]
            self.settings.recent_comments = recent[:CommitPanel.RECENT_LIMIT]
            self.settings_store.save()
        self._reset_changes_panel(self._current(item) or item)
        self._commit_panel.set_recent_comments(self.settings.recent_comments)
        for tab in ("incoming", "history"):       # свои же сохранения появились в журнале
            self._tab_loads.invalidate(tab)
        if result.revision is None:
            text = messages.COMMIT_NOTHING
        else:
            text = localrepo.saved_text(len(paths)) if item.local else messages.commit_done(len(paths))
        item = self._current(item) or item
        if item.local and result.revision is not None:
            item.revision = result.revision
        self._action_notes[os.path.normcase(item.path)] = text
        self.mine_status.set(text)
        item.status, item.checked = None, False
        self._refresh_row(item)
        client, rules = self.client, self._rules_for(item)
        self.run_bg(lambda: client.status(item.path, check_server=True, rules=rules),
                    lambda status, err, _stamp: self._on_commit_rechecked(item, status, err))

    def _on_commit_rechecked(self, item: MineItem, status: Optional[WcStatus], error: Optional[Exception]) -> None:
        """Повторная проверка после сохранения; занятые мной файлы, которых я не менял, предлагаем освободить."""
        if error is not None:
            self._check_status([item])    # ошибка (нет связи, вход) разбирается обычным путём
            return
        self._on_item_status(item, status, "")
        changed = {os.path.normcase(os.path.abspath(c.path)) for c in status.local_changes}
        idle = [lock.path if os.path.isabs(lock.path) else os.path.join(item.path, lock.path)
                for lock in status.my_locks]
        idle = [f for f in idle if os.path.normcase(os.path.abspath(f)) not in changed]
        if not idle:
            return
        names = ", ".join(os.path.basename(f) for f in idle[:5]) + (" и др." if len(idle) > 5 else "")
        if messagebox.askyesno(messages.COMMIT_TITLE, messages.COMMIT_KEEP_LOCKS.format(names=names), parent=self.root):
            self._run_locks(item, idle, lock=False)

    def _unsaved_project_names(self) -> list[str]:
        """Проекты, в которых есть несохранённые правки (по последней проверке), в том числе новые файлы:
        local_changes, а не счётчик modified (он не видит файлов, которых ещё нет на сервере)."""
        return [i.display_name for i in self.mine.values()
                if i.state == "svn" and i.status is not None and i.status.local_changes]

    # занять и освободить файлы

    def _on_get_lock(self) -> None:
        item = self._selected_item()
        if not item or item.state != "svn" or not self.client or self._path_busy(item.path):
            return
        files = filedialog.askopenfilenames(
            parent=self.root, title="Какие файлы вы собираетесь править?", initialdir=item.path)
        files = [os.path.normpath(f) for f in files]
        outside = [f for f in files if not os.path.normcase(f).startswith(os.path.normcase(item.path) + os.sep)]
        if outside:
            messagebox.showerror("Не тот проект", "Выбраны файлы не из папки этого проекта:\n" + outside[0],
                                 parent=self.root)
            return
        if files:
            self._run_locks(item, files, lock=True)

    def _on_release_lock(self) -> None:
        item = self._selected_item()
        if not item or item.state != "svn" or not self.client or self._path_busy(item.path) or self._locking:
            return
        self.mine_status.set("Ищу файлы, которые вы заняли…")
        client = self.client
        self.run_auth(lambda: client.status(item.path, check_server=True),
                      lambda st: self._choose_unlock(item, st),
                      lambda kind: self._status_error(messages.short_message(kind)))

    def _choose_unlock(self, item: MineItem, status: WcStatus) -> None:
        self._on_item_status(item, status, "")
        self.mine_status.set("")
        if not status.my_locks:
            messagebox.showinfo("Нечего освобождать", "Вы сейчас не занимали ни одного файла в этом проекте.",
                                parent=self.root)
            return
        # файлы с несохранёнными правками заранее не отмечаем: освободив их, человек даст коллеге занять файл,
        # и две версии (двоичный файл не объединить) разойдутся
        changed = {os.path.normcase(os.path.abspath(c.path)) for c in status.local_changes}
        shown: dict[str, str] = {}  # подпись в списке → путь файла
        preselect: list[int] = []
        for lock in sorted(status.my_locks, key=lambda lk: lk.path):
            rel = os.path.relpath(lock.path, item.path) if os.path.isabs(lock.path) else lock.path
            full = lock.path if os.path.isabs(lock.path) else os.path.join(item.path, lock.path)
            notes = []
            if lock.age_days is not None:
                notes.append(f"занят {messages.format_age(lock.age_days)}")
            unsaved = os.path.normcase(os.path.abspath(full)) in changed
            if unsaved:
                notes.append(messages.UNLOCK_UNSAVED)
            else:
                preselect.append(len(shown))
            shown[f"{rel}   ({', '.join(notes)})" if notes else rel] = lock.path
        dialog = PickDialog(self.root, "Освободить файлы", messages.UNLOCK_PROMPT, list(shown), preselect=preselect)
        self.root.wait_window(dialog)
        if dialog.result:
            paths = [p if os.path.isabs(p) else os.path.join(item.path, p)
                     for p in (shown[label] for label in dialog.result)]
            self._run_locks(item, paths, lock=False)

    def _run_locks(self, item: MineItem, files: list[str], lock: bool) -> None:
        client = self.client
        if not client or self._locking or self._committing:
            return
        if self._path_busy(item.path):
            self._status_error(messages.UPDATE_BUSY)
            return
        self._locking = True
        self._refresh_mine_progress()
        self._update_mine_buttons()
        self.mine_status.set("Занимаю файлы…" if lock else "Освобождаю файлы…")

        def work() -> list[LockResult]:
            results = []
            for f in files:
                try:
                    client.lock(f) if lock else client.unlock(f)
                    results.append(LockResult(f))
                except SvnError as exc:
                    if exc.kind is ErrorKind.AUTH:
                        raise  # вход нужен для всех файлов: повторим всё после ввода пароля
                    holder = None
                    if lock and exc.kind is ErrorKind.LOCKED:  # узнаём, кто занял файл
                        try:
                            holder = client.remote_lock(f)
                        except SvnError:
                            log.exception("Не удалось узнать, кто занял файл %s", f)
                    results.append(LockResult(f, exc, holder))
            return results

        def done(results, error, stamp: JobStamp) -> None:
            self._locking = False
            self._refresh_mine_progress()   # полоса останавливается, только если не идёт ещё и проверка
            self._update_mine_buttons()
            self._drain_check_queue()
            if error is not None:
                if self._error_kind(error) is ErrorKind.AUTH:
                    self.login.request(retry=lambda: self._run_locks(item, files, lock), job_gen=stamp,
                                       give_up=lambda: self._status_error("Нужен вход на сервер."))
                else:
                    self.mine_status.set("")
                    kind = self._error_kind(error)
                    self.show_error("Не удалось занять файлы" if lock else "Не удалось освободить файлы",
                                    messages.short_message(kind),
                                    action=f"{'Занять' if lock else 'Освободить'} файлы проекта {item.name}",
                                    kind=kind, raw=self._error_raw(error), extra={"Папка": item.path})
                return
            self.login.reset_attempts()
            self._show_lock_results(item, results, lock)

        self.run_bg(work, done)

    def _show_lock_results(self, item: MineItem, results: list[LockResult], lock: bool) -> None:
        if lock:
            self._remember_wanted_locks(item, results)
        ok = [os.path.basename(r.path) for r in results if r.error is None]
        lines = []
        if ok:
            lines.append(("Заняты, теперь их можно править:" if lock else "Освобождены:"))
            lines += [f"  {n}" for n in ok[:15]]
            if lock:
                lines.append("\nКогда закончите, сохраните изменения (вкладка «Изменения», «Сохранить отмеченное») "
                             "и освободите файлы.")
        failed = [r for r in results if r.error is not None]
        stale = [r for r in failed if r.error.kind is ErrorKind.OUT_OF_DATE]
        for r in failed:
            if r in stale:
                continue
            reason = (lock_holder_text(r.holder) if r.error.kind is ErrorKind.LOCKED
                      else messages.short_message(r.error.kind))
            lines.append(f"\nНе удалось: {os.path.basename(r.path)}. {reason}")
        self.mine_status.set("Готово" if not failed else "Часть файлов не удалось обработать")
        title = "Занять файлы" if lock else "Освободить файлы"
        if stale:
            names = ", ".join(os.path.basename(r.path) for r in stale[:5])
            lines.append(f"\nНа сервере есть более новая версия ({names}). Получить новое сейчас?")
            if messagebox.askyesno(title, "\n".join(lines), parent=self.root) and self._begin_update(item):
                return  # после получения нового проект проверится сам; занять файл можно будет ещё раз
        elif failed:
            self.show_error(title, "\n".join(lines).strip(),
                            action=f"{'Занять' if lock else 'Освободить'} файлы проекта {item.name}",
                            kind=failed[0].error.kind, raw="\n\n".join(r.error.raw for r in failed if r.error.raw),
                            extra={"Папка": item.path, "Не обработано файлов": len(failed)})
        else:
            messagebox.showinfo(title, "\n".join(lines), parent=self.root)
        self._check_status([item])

    # получение нового с сервера

    def _on_update_selected(self) -> None:
        item = self._selected_item()
        if not item or item.state != "svn" or not self.client or self.upd or self.job or self._path_busy(item.path):
            return
        if item.status and item.status.conflicts and not messagebox.askyesno(
                "Есть конфликты", "В проекте есть неразрешённые конфликты. Получение нового может их усугубить. "
                "Всё равно продолжить?", parent=self.root):
            return
        self._begin_update(item)

    def _begin_update(self, item: MineItem) -> bool:
        """Запускает получение нового для проекта. False, если сейчас уже идёт другая задача."""
        if not self.client or self.upd or self.job or self._locking or self._committing:
            return False
        if self._path_busy(item.path):   # TortoiseSVN держит папку: получение нового при блокировке запустит починку и снимет её
            self._status_error(messages.UPDATE_BUSY)
            return False
        self._action_notes.pop(os.path.normcase(item.path), None)
        self.upd = UpdateJob(self._current(item) or item, client=self.client)
        self._start_update()
        return True

    def _start_update(self) -> None:
        job = self.upd
        self._refresh_mine_progress()
        if job.url:
            self.mine_status.set(
                f"Освобождаю занятые файлы ({len(job.unlock_paths)}) и переключаю…" if job.unlock_paths
                else f"Переключаю: {messages.line_label(job.target)}…")
        else:
            self.mine_status.set("Получаю новое с сервера…")
        self._update_buttons()
        client = job.client or self.client
        rules = self.config.filter

        def work():
            path = job.item.path

            def operation():
                if job.url:   # переключение на другую линию (ветку, версию, основную)
                    return client.switch(path, job.url, job.progress, job.cancel)
                return client.update(path, job.progress, job.cancel)

            try:
                job.stage = "unlock" if job.unlock_paths else ""
                for locked in job.unlock_paths:   # свои занятые файлы освобождаем до переключения; сбой - не переключаем
                    client.unlock(locked)
                job.stage = ""
                try:
                    result = operation()
                except SvnError as exc:
                    if exc.kind is ErrorKind.OTHER and "E155004" in exc.codes:  # копия заблокирована прошлым сбоем
                        client.cleanup(path)
                        result = operation()
                    else:
                        raise
                excluded: list[str] = []
                if rules.enabled and rules.skip_junk and result.added_paths:
                    excluded = audit.junk_among(result.added_paths, rules, path)
                    if excluded:
                        client.exclude_paths(path, excluded, job.cancel)
            except SvnError as exc:
                if exc.kind is ErrorKind.CANCELLED:
                    try:
                        client.cleanup(path)  # прерванное обновление оставляет копию заблокированной
                    except SvnError:
                        log.exception("Cleanup после отмены не удался")
                elif exc.kind is ErrorKind.BUSY:   # файл был открыт в другой программе: копию надо привести в порядок
                    try:
                        client.cleanup(path)
                    except SvnError:
                        log.exception("Cleanup после занятого файла не удался")
                raise
            return result, excluded

        self.run_bg(work, lambda res, err, stamp: self._on_updated(job, res, err, stamp))
        self.root.after(PROGRESS_MS, self._tick_update, job)

    def _tick_update(self, job: UpdateJob) -> None:
        if job is not self.upd:
            return
        self.mine_status.set("Останавливаю…" if self._closing
                             else "Отмена…" if job.cancel.cancelled       # «Отмену» в панели уже нажали
                             else f"Получено файлов: {messages.format_number(job.progress.count)}")
        self.root.after(PROGRESS_MS, self._tick_update, job)

    def _on_cancel_update(self) -> None:
        """«Отмена» в панели подсказок, пока идёт получение нового (или переключение ветки)."""
        if self.upd:
            self.upd.cancel.cancel()
            self.mine_status.set("Отмена…")     # панель покажет «Отмена…», её кнопка станет серой

    def _end_update(self) -> None:
        self.upd = None
        self._refresh_mine_progress()
        self._update_buttons()
        self._apply_pending_rebuild()
        if not self._checking and not self._closing:
            self._drain_check_queue()   # проверки, отложенные на время получения нового

    def _on_updated(self, job: UpdateJob, outcome, error, stamp: JobStamp) -> None:
        if job is not self.upd:
            return
        item = self._current(job.item) or job.item   # список проектов могли заново найти, пока шло получение нового
        switching = bool(job.url)
        if error is not None:
            kind = self._error_kind(error)
            if kind is ErrorKind.AUTH and not self._closing:
                self.login.request(retry=self._start_update, job_gen=stamp,
                                   give_up=lambda: (self._end_update(), self._status_error("Нужен вход на сервер.")))
                return
            self._end_update()
            if self._closing:
                return  # окно закрывается: диалоги не нужны
            self._notify_done(job.started, job.cancel.cancelled)
            if kind is ErrorKind.CANCELLED and switching:
                self.mine_status.set("Переключение отменено")
                messagebox.showinfo("Переключение отменено", "Переключение остановлено. Часть файлов могла уже "
                                    "перейти на новую ветку: повторите переключение, чтобы завершить его.",
                                    parent=self.root)
            elif kind is ErrorKind.CANCELLED:
                self.mine_status.set("Получение нового остановлено")
                messagebox.showinfo(messages.UPDATE_CANCELLED_TITLE, "Получение нового остановлено. Часть файлов "
                                    "могла прийти, остальные придут при следующем «Получить новое».",
                                    parent=self.root)
            elif switching:
                self.mine_status.set("Переключение не выполнено")
                unlock_failed = job.stage == "unlock"
                self.show_error("Не удалось освободить занятые файлы" if unlock_failed else "Не удалось переключить проект",
                                messages.short_message(kind),
                                action=f"Переключение проекта {item.name} на {messages.line_label(job.target)}",
                                kind=kind, raw=self._error_raw(error),
                                extra={"Папка": item.path, "Адрес": job.url,
                                       **({"Этап": "освобождение занятых файлов"} if unlock_failed else {})})
            else:
                self.mine_status.set("Новое не получено")
                self.show_error("Не удалось получить новое", messages.short_message(kind),
                                action=f"Получение нового для проекта {item.name}", kind=kind,
                                raw=self._error_raw(error), extra={"Папка": item.path})
            if switching:
                self._reprobe(item)   # при отмене или сбое копия могла остаться на прежней или на новой линии
            return
        self.login.reset_attempts()
        result, excluded = outcome
        self._end_update()
        item.revision = result.revision
        item.status, item.checked = None, False
        if switching:
            item.line, item.line_url = job.target, job.url
        self._refresh_row(item)
        if switching:
            self._reload_lines_view(item)   # на вкладке «Ветки» «● вы здесь» переезжает на новую линию
        if self._closing:
            return
        total = result.added + result.updated + result.deleted
        if switching:
            if job.created_label:   # ветка только что создана: одно короткое окно вместо двух
                lines = [f"{job.created_label} создана. Папка переключена на неё."]
            else:
                lines = [f"Папка переключена: {messages.line_label(job.target)}."]
            if job.target.read_only:
                lines.append(messages.TAG_EXPLAIN)
            if total or result.conflicts:
                lines.append(f"Изменено в папке: добавлено {result.added}, изменено {result.updated}, "
                             f"удалено {result.deleted}.")
        else:
            lines = [f"Проект «{item.name}»: получено новое, теперь в папке сохранение № {result.revision}."]
            lines.append(f"Получено: добавлено {result.added}, изменено {result.updated}, удалено {result.deleted}."
                         if total or result.conflicts else "На сервере не было новых изменений.")
        if excluded:
            lines.append(f"Лишние файлы не оставлены в папке: {len(excluded)}.")
        if result.conflicts:
            lines.append(f"\nКонфликты ({len(result.conflicts)}): вы и другой человек изменили одно и то же. "
                         "Выберите проект и нажмите «Разобрать конфликт»:")
            lines += [f"  {os.path.basename(c)}" for c in result.conflicts[:10]]
        show = messagebox.showwarning if result.conflicts else messagebox.showinfo
        self.mine_status.set("Готово")
        self._notify_done(job.started)
        title = messages.UPDATE_DONE_TITLE
        if switching:
            title = "Ветка создана" if job.created_label else "Переключение завершено"
        show(title, "\n".join(lines), parent=self.root)
        if switching:
            self._reprobe(item)   # заново читает линию и проверяет проект
        else:
            self._check_status([item])
