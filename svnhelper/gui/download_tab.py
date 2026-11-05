"""Подвкладка «Скачать проект» колонки «Проекты»: дерево сервера, поиск, ссылка, выбор ветки или версии, скачивание."""
from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .. import audit, checks, fsutil, messages, mirrors, server, theme, winutil
from ..config import add_recent, load_tree_cache, save_tree_cache
from ..progress import EtaTracker, format_eta
from ..svn_core import CheckoutProgress, ErrorKind, Estimate, SvnError
from .create_dialog import CreateProjectDialog
from .models import (CLIPBOARD_MAX_LEN, DownloadJob, FAV_ADD_TEXT, FAV_REMOVE_TEXT, JobStamp,
                     line_choice_label, LINES_DELAY_MS, log, LONG_TASK_SEC, Node, PROGRESS_MS, same_url,
                     SEARCH_DELAY_MS, search_projects, shorten_path, TREE_HINT, TRUNK_CHOICE)
from . import hints
from .server_tree import ServerTree
from .widgets import AuditDialog, button_column_width, ButtonRow, SideButtons

LOCAL_OUTSIDE = "Эта папка не входит в проекты на сервере."
LOCAL_MISSING = "Этого проекта ещё нет на сервере."


class DownloadTabMixin:
    """Подвкладка «Скачать проект»: дерево сервера, поиск, ссылка, выбор ветки или версии, скачивание."""

    # подписи кнопок колонки справа в форме; ширина колонки - не меньше, чем у «Скачать проект», и не меньше самой
    # длинной из показанных (_fit_download_actions): «Создать проект из этой папки…» и «Удалить недокачанную папку»
    # появляются редко, и пока их нет, форме остаётся больше места (в окне 900x640 при скачивании это важно)
    DOWNLOAD_BUTTONS = ("Скачать", "Создать проект из этой папки…", "Удалить недокачанную папку")
    DOWNLOAD_COLUMN_MIN_TEXT = "Скачать проект"
    # фоновый текст панели подсказок на подвкладке «Скачать проект»
    DOWNLOAD_BACKGROUND = ("Найдите проект в списке слева или вставьте ссылку на него, проверьте «Что скачать» "
                           "и «Куда» и нажмите «Скачать».")

    # -- подвкладка «Скачать проект» колонки «Проекты» -------------------------------------------
    # Две части в разных колонках окна (docs/PLAN_ui_v4.md, раздел 3, вариант A): в колонке 2 вместо списка своих
    # проектов - поиск и дерево сервера (_build_server_browser), в колонке 3 вместо «Состояния проекта» - форма
    # «Скачивание» со своей колонкой кнопок справа (_build_download_form). Переключает их ProjectsTabMixin
    # (_apply_list_mode).

    def _build_server_browser(self, parent: tk.Misc) -> None:
        """Колонка 2 на «Скачать проект»: поиск по названию, дерево папок сервера, список найденного, строка состояния."""
        px = self.px
        self.server_browser = browser = ttk.Frame(parent)
        browser.columnconfigure(0, weight=1)
        browser.rowconfigure(1, weight=1)
        search = ttk.Frame(browser)
        search.grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="Найти на сервере:").grid(row=0, column=0, sticky="w", padx=(0, px(6)))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.search_entry.bind("<Escape>", lambda _e: self.search_var.set(""))
        hints.hint(self.search_entry, "download.search")
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())

        # дерево и список результатов поиска лежат друг на друге: виден один из них
        stack = ttk.Frame(browser, style="Card.TFrame", padding=1)
        stack.grid(row=1, column=0, sticky="nsew")
        stack.columnconfigure(0, weight=1)
        stack.rowconfigure(0, weight=1)
        self.server_tree = ServerTree(stack, self, cache_get=lambda segments: self._cached_folder(segments),
                                      cache_put=lambda *args: self._cache_store(*args),
                                      on_select=self._on_tree_select, on_status=self._set_tree_status)
        self.tree_pane = self.server_tree
        self.tree_pane.grid(row=0, column=0, sticky="nsew")
        self.tree = self.server_tree.tree
        self.nodes = self.server_tree.nodes
        self.results_pane = ttk.Frame(stack)
        self.results_pane.grid(row=0, column=0, sticky="nsew")
        self.results_pane.columnconfigure(0, weight=1)
        self.results_pane.rowconfigure(0, weight=1)
        self.results = ttk.Treeview(self.results_pane, columns=("path",), show="tree", selectmode="browse",
                                    height=7)
        self.results.column("#0", width=px(180), stretch=True)
        self.results.column("path", width=px(160), stretch=True)
        rscroll = ttk.Scrollbar(self.results_pane, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=rscroll.set)
        self.results.grid(row=0, column=0, sticky="nsew")
        rscroll.grid(row=0, column=1, sticky="ns")
        self._tag_project_rows(self.results)
        self.results.bind("<<TreeviewSelect>>", self._on_result_select)
        for tree in (self.tree, self.results):
            hints.hint(tree, "download.tree")
        self.results_pane.grid_remove()
        bar = ttk.Frame(browser)
        bar.grid(row=2, column=0, sticky="ew", pady=(px(8), 0))
        bar.columnconfigure(1, weight=1)
        # «⟳», а не «Обновить»: слово «Обновить» в программе не используется для разных вещей (PLAN_v3)
        self.refresh_btn = ttk.Button(bar, text="⟳", style="Ghost.TButton", width=2, command=self._on_refresh)
        self.refresh_btn.grid(row=0, column=0, sticky="nw")
        hints.hint(self.refresh_btn, "download.refresh")         # что делает «⟳» - в панели подсказок
        self.tree_status = ttk.Label(bar, text=TREE_HINT, style="Hint.TLabel", justify="left")
        self.tree_status.grid(row=0, column=1, sticky="w", padx=(px(8), 0))
        self._autowrap(self.tree_status, bar, px(60))

    def _build_download_form(self, parent: tk.Misc) -> None:
        """Колонка 3 на «Скачать проект»: какой проект, что скачать, куда; полоса хода скачивания. Кнопки - колонкой
        справа (self.download_actions): «Скачать», «Создать проект из этой папки…» и «Удалить недокачанную папку»
        (когда они нужны). Ход скачивания словами и «Отмена» - справа в панели подсказок внизу окна."""
        px = self.px
        self.download_form = form = ttk.Frame(parent)
        form.columnconfigure(0, weight=1)
        form.rowconfigure(0, weight=1)
        steps = ttk.Frame(form)
        steps.grid(row=0, column=0, sticky="nsew")
        steps.columnconfigure(0, weight=1)
        # отступы плотнее, чем у Step.TLabelframe: форма делит высоту окна (от 640 px) с ходом скачивания
        pad = (px(12), px(8))

        # 1. Какой проект
        self.step1 = step1 = ttk.LabelFrame(steps, text="1. Какой проект", style="Step.TLabelframe", padding=pad)
        step1.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        step1.columnconfigure(0, weight=1)
        step1_hint = ttk.Label(step1, text="Выберите проект в списке слева или вставьте ссылку на него.",
                               style="Hint.TLabel", justify="left")
        step1_hint.grid(row=0, column=0, sticky="w", pady=(0, px(6)))
        self._autowrap(step1_hint, step1, px(40))
        ttk.Label(step1, text="Ссылка на проект:").grid(row=1, column=0, sticky="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(step1, textvariable=self.url_var)
        self.url_entry.grid(row=2, column=0, sticky="ew", pady=(px(2), 0))
        hints.hint(self.url_entry, "download.url")
        tools = ButtonRow(step1, px(8), px(2))     # в узкой колонке ссылки переходят на вторую строку
        tools.grid(row=3, column=0, sticky="ew", pady=(px(2), 0))
        self.recent_menu = tk.Menu(step1, tearoff=False, postcommand=self._fill_recent_menu)
        self.recent_btn = self._menu_button(tools, "Недавние ▾", self.recent_menu, style="Link.TButton")
        self.fav_btn = ttk.Button(tools, text=FAV_ADD_TEXT, style="Link.TButton", command=self._toggle_favorite,
                                  state="disabled")
        self.browse_disk_btn = ttk.Button(tools, text="Выбрать папку на диске…", style="Link.TButton",
                                          command=self._on_browse_disk)
        for widget, key in ((self.recent_btn, "download.recent"), (self.fav_btn, "download.favorite"),
                            (self.browse_disk_btn, "download.browse_disk")):
            tools.add(widget, hint=key)
        self.url_status = ttk.Label(step1, text="", style="Hint.TLabel", justify="left")
        self.url_status.grid(row=4, column=0, sticky="w", pady=(px(6), 0))
        self._autowrap(self.url_status, step1, px(40))
        self.url_var.trace_add("write", lambda *_: self._on_url_changed())
        # подсказка шага 1 нужна, пока поле пустое; дальше её место занимают «Что скачать» и сведения о ветке
        self.url_var.trace_add("write", lambda *_: self._show(step1_hint, not self.url_var.get().strip()))

        # «Что скачать»: есть только у проектов с ветками или версиями
        self.line_row = line_row = ttk.Frame(step1)
        line_row.grid(row=5, column=0, sticky="ew", pady=(px(8), 0))
        line_row.columnconfigure(1, weight=1)
        ttk.Label(line_row, text="Что скачать:").grid(row=0, column=0, sticky="w")
        self.line_var = tk.StringVar(value=TRUNK_CHOICE)
        self.line_combo = ttk.Combobox(line_row, textvariable=self.line_var, state="disabled", values=[TRUNK_CHOICE])
        self.line_combo.grid(row=0, column=1, sticky="ew", padx=(px(10), 0))
        self.line_combo.bind("<<ComboboxSelected>>", self._on_line_selected)
        hints.hint(self.line_combo, "download.line")
        line_row.grid_remove()
        self.line_info = ttk.Label(step1, text="", style="Hint.TLabel", justify="left")
        self.line_info.grid(row=6, column=0, sticky="w", pady=(px(4), 0))
        self._autowrap(self.line_info, step1, px(40))
        self.line_info.grid_remove()

        # 2. Куда скачать
        step2 = ttk.LabelFrame(steps, text="2. Куда скачать", style="Step.TLabelframe", padding=pad)
        step2.grid(row=1, column=0, sticky="ew", pady=(0, px(8)))
        step2.columnconfigure(0, weight=1)
        ttk.Label(step2, text="Папка для проектов:").grid(row=0, column=0, columnspan=2, sticky="w")
        self.dir_var = tk.StringVar(value=self.settings.last_dir or self.config.work_dir)
        # тот же список папок, что над «Мои проекты»; можно вписать новую: в список она попадёт при скачивании
        self.dir_entry = ttk.Combobox(step2, textvariable=self.dir_var, values=list(self.settings.project_dirs))
        self.dir_entry.grid(row=1, column=0, sticky="ew", pady=(px(2), 0))
        self.dir_entry.bind("<<ComboboxSelected>>", lambda _e: self._remember_project_dir(self.dir_var.get()))
        hints.hint(self.dir_entry, "download.folder")
        self.browse_btn = ttk.Button(step2, text="Выбрать…", command=self._on_browse)
        hints.hint(self.browse_btn, "download.browse")
        self.browse_btn.grid(row=1, column=1, sticky="e", padx=(px(8), 0), pady=(px(2), 0))
        self.dest_var = tk.StringVar()
        dest_label = ttk.Label(step2, textvariable=self.dest_var, style="Hint.TLabel", justify="left")
        dest_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(px(6), 0))
        self._autowrap(dest_label, step2, px(40))
        self._hide_when_empty(dest_label, self.dest_var)
        self.dir_var.trace_add("write", lambda *_: self._update_dest_label())

        # 3. Скачать: кнопка в колонке справа, здесь ход работы и итог
        step3 = ttk.LabelFrame(steps, text="3. Скачать", style="Step.TLabelframe", padding=pad)
        step3.grid(row=2, column=0, sticky="ew")
        step3.columnconfigure(0, weight=1)
        step3_hint = ttk.Label(step3, text="Нажмите «Скачать» справа, и проект скачается в выбранную папку.",
                               style="Hint.TLabel", justify="left")
        step3_hint.grid(row=0, column=0, sticky="w")
        self._autowrap(step3_hint, step3, px(40))
        self.progress = ttk.Progressbar(step3, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(px(4), px(4)))
        self.progress.grid_remove()
        self.status_var = tk.StringVar()
        self.download_status = status = ttk.Label(step3, textvariable=self.status_var, justify="left")
        status.grid(row=2, column=0, sticky="w", pady=(px(4), 0))
        self._autowrap(status, step3, px(40))
        # пока идёт скачивание, ход словами виден в панели подсказок (с «Отменой»); здесь - итог после него
        self.status_var.trace_add("write", lambda *_: self._sync_download_status())
        self._show(status, False)
        # подсказка нужна, пока скачивания не было; дальше её место занимают ход и итог (окно в 640 px невысокое)
        self.status_var.trace_add("write", lambda *_: self._show(step3_hint, not self.status_var.get().strip()))
        self.info_var = tk.StringVar()
        info = ttk.Label(step3, textvariable=self.info_var, style="Hint.TLabel", justify="left")
        info.grid(row=3, column=0, sticky="w")
        self._autowrap(info, step3, px(40))
        self._hide_when_empty(info, self.info_var)

        # колонка кнопок справа, как у «Состояния проекта»: одна главная кнопка, остальные появляются по делу
        self.download_actions = actions = SideButtons(
            form, px(6), button_column_width(form, [self.DOWNLOAD_COLUMN_MIN_TEXT], px))
        actions.grid(row=0, column=1, sticky="n", padx=(px(12), 0))
        self.download_btn = ttk.Button(actions, text="Скачать", style="Accent.TButton", command=self._on_download,
                                       state="disabled")
        # появляется, когда введён путь к папке, которой ещё нет в хранилище
        self.create_btn = ttk.Button(actions, text=self.DOWNLOAD_BUTTONS[1], command=self._on_create_project)
        # появляется только после отмены или ошибки, если на диске осталась недокачанная папка
        self.cleanup_btn = ttk.Button(actions, text=self.DOWNLOAD_BUTTONS[2], command=self._on_remove_partial)
        actions.add(self.download_btn, hint="download.start")
        actions.add(self.create_btn, visible=False, separator=True, hint="download.create")
        actions.add(self.cleanup_btn, visible=False, separator=True, hint="download.cleanup")
        actions.on_change = self._fit_download_actions

    def _sync_download_status(self) -> None:
        """Строка хода в форме видна, когда в ней есть текст и скачивание не идёт (во время скачивания ход - в панели
        подсказок); заодно обновляется сама панель."""
        self._show(self.download_status, bool(self.status_var.get().strip()) and self.job is None)
        self._sync_hint_progress()

    def _fit_download_actions(self) -> None:
        """Ширина колонки кнопок формы по самой длинной из показанных подписей: длинные не обрезаются."""
        actions = self.download_actions
        texts = [self.DOWNLOAD_COLUMN_MIN_TEXT] + [str(b.cget("text")) for b in actions._items if b not in actions._hidden]
        actions.set_width(button_column_width(self.download_form, texts, self.px))

    def _tag_project_rows(self, tree: ttk.Treeview) -> None:
        """Цвета строк дерева: проект (✔) зелёный и полужирный, служебные строки серые."""
        theme.configure_tree_tags(tree)
        tree.tag_configure("project", foreground=self.palette.success, font=self.bold_font)
        tree.tag_configure("muted", foreground=self.palette.muted)

    # -- кэш дерева сервера ----------------------------------------------------------
    # Кэш лежит в файле и нужен, чтобы папки открывались сразу, а поиск знал о проектах,
    # которые вы раньше открывали. Читается один раз при старте, пишется только из фоновых потоков.

    def _empty_cache(self) -> dict:
        return {"server": self.config.server, "folders": {}}

    def _load_cache(self) -> None:
        data = load_tree_cache()
        if data.get("server") != self.config.server or not isinstance(data.get("folders"), dict):
            data = self._empty_cache()
        self.tree_cache = data

    def _sync_cache_server(self) -> None:
        """Сменили адрес сервера: старый кэш к новому серверу не относится."""
        with self._cache_lock:
            if self.tree_cache.get("server") != self.config.server:
                self.tree_cache = self._empty_cache()

    @staticmethod
    def _cache_key(segments: list[str]) -> str:
        return "/".join(segments)

    def _cached_folder(self, segments: list[str]) -> Optional[dict]:
        with self._cache_lock:
            entry = self.tree_cache.get("folders", {}).get(self._cache_key(segments))
        if isinstance(entry, dict) and isinstance(entry.get("dirs"), list):
            return entry
        return None

    def _cache_store(self, segments: list[str], listing: server.FolderListing, server_url: str) -> None:
        """Запоминает список папок. Вызывается из фонового потока (запись файла может быть долгой).

        server_url — адрес, с которого список получен на старте загрузки: если сервер успели сменить,
        чужой список в новый кэш не попадает.
        """
        entry: dict = {"dirs": [[e.name, e.is_project] for e in listing.entries], "ts": time.time()}
        if listing.is_project:
            entry["is_project"] = True
        with self._cache_lock:
            if self.tree_cache.get("server") != server_url:
                return
            self.tree_cache.setdefault("folders", {})[self._cache_key(segments)] = entry
        with self._cache_save_lock:  # одновременно писать файл должен только один поток
            with self._cache_lock:
                snapshot = {"server": self.tree_cache.get("server"), "folders": dict(self.tree_cache["folders"])}
            save_tree_cache(snapshot)

    # -- дерево ------------------------------------------------------------
    # Само дерево (узлы, загрузка папок, вход на сервер) живёт в ServerTree (gui/server_tree.py);
    # здесь остаются только его подключение к вкладке.

    def _fill_roots(self) -> None:
        self.server_tree.fill_roots()

    def _load_node(self, iid: str) -> None:
        self.server_tree.load_node(iid)

    def _on_refresh(self) -> None:
        self._lines_cache.clear()
        self._sync_cache_server()
        self._set_tree_status(TREE_HINT)
        self.server_tree.reset()

    def _set_tree_status(self, text: str, error: bool = False) -> None:
        self.tree_status.configure(text=text, style="Error.TLabel" if error else "Hint.TLabel")

    def _on_tree_select(self, node: Node) -> None:
        if node.is_project:
            self._pick_project(node.segments)
        else:
            self._pick_folder(node.segments)
            self._set_tree_status("Это обычная папка, не проект: она скачается как есть, целиком.")

    # -- поиск проекта по названию ------------------------------------------------------

    def _on_search_changed(self) -> None:
        if self._search_after is not None:
            self.root.after_cancel(self._search_after)
            self._search_after = None
        if not self.search_var.get().strip():
            self._search_gen += 1  # запоздавший ответ предыдущего поиска не нужен
            self.results_pane.grid_remove()
            if self.server_tree.busy == 0:
                self._set_tree_status(TREE_HINT)
            return
        self._search_after = self.root.after(SEARCH_DELAY_MS, self._run_search)

    def _run_search(self) -> None:
        self._search_after = None
        query = self.search_var.get().strip().casefold()
        if not query:
            return
        self._search_gen += 1
        gen = self._search_gen
        loaded = [list(n.segments) for n in self.nodes.values() if n.is_project]
        with self._cache_lock:
            folders = dict(self.tree_cache.get("folders", {}))  # поверхностная копия: записи не меняются, а заменяются
        # на большом кэше поиск может занять время: ищем в фоне, окно не замирает
        self.run_bg(lambda: search_projects(query, loaded, folders),
                    lambda res, err, _stamp: self._on_search_done(gen, res, err))

    def _on_search_done(self, gen: int, found, error) -> None:
        if gen != self._search_gen:
            return
        self.results.delete(*self.results.get_children())
        self._search_hits.clear()
        if error is not None:
            log.error("Ошибка поиска проекта", exc_info=error)
            found = ([], False)
        hits, truncated = found
        for index, segments in enumerate(hits):
            iid = f"r{index}"
            self.results.insert("", "end", iid=iid, text=f"✔  {segments[-1]}", values=("/".join(segments[:-1]),),
                                tags=("project",))
            self._search_hits[iid] = segments
        if hits:
            note = f"Найдено проектов: {len(hits)}" + (" (показаны первые)" if truncated else "")
            self._set_tree_status(note + ". Выберите нужный, и ссылка подставится сама.")
        else:
            self.results.insert("", "end", text="Ничего не найдено", tags=("muted",))
            self._set_tree_status("Поиск идёт среди папок, которые вы уже открывали. "
                                  "Откройте нужные папки в списке и повторите поиск.")
        self.results_pane.grid()

    def _on_result_select(self, _event) -> None:
        sel = self.results.selection()
        segments = self._search_hits.get(sel[0]) if sel else None
        if segments:
            self._pick_project(segments)

    def _pick_project(self, segments: list[str]) -> None:
        """Проект выбран в дереве: ссылка подставляется в поле, а проект берётся из путей, а не из разбора строки
        (проект может лежать в папке с именем tags или branches)."""
        prefix = self.config.server.rstrip("/")
        text = prefix + "/" + "/".join(segments)
        self._tree_pick = (text, server.project_from_segments(prefix, segments))
        self.url_var.set(text)

    def _pick_folder(self, segments: list[str]) -> None:
        """Выбрана обычная папка сервера: ссылка подставляется, а скачиваться будет сама папка (без trunk)."""
        prefix = self.config.server.rstrip("/")
        text = prefix + "/" + "/".join(segments)
        self._tree_pick = (text, server.folder_from_segments(prefix, segments))
        self.url_var.set(text)

    def _on_browse_disk(self) -> None:
        """Выбор папки на сетевом диске (Z:\…): путь превращается в ссылку SVN тем же поиском, что и вставленный путь."""
        initial = next((m["path"] for m in self.config.mirrors if os.path.isdir(m["path"])), "")
        chosen = filedialog.askdirectory(parent=self.root, title="Папка на диске", initialdir=initial or None,
                                         mustexist=True)
        if chosen:
            self.url_var.set(os.path.normpath(chosen))

    # -- ссылка: буфер обмена, недавние и избранное -------------------------------------

    def _on_focus_in(self, event) -> None:
        if event.widget is self.root:  # окно получило фокус (например, пользователь вернулся из письма со ссылкой)
            self._paste_from_clipboard()

    def _paste_from_clipboard(self) -> None:
        """Подставляет ссылку на проект из буфера обмена, если поле ссылки пустое."""
        if self._closing or self.job or self.upd or str(self.url_entry.cget("state")) == "disabled":
            return
        if self.url_var.get().strip():
            return  # то, что человек уже ввёл, не трогаем
        try:
            text = self.root.clipboard_get()
        except tk.TclError:  # в буфере не текст (картинка, файлы) или он пуст
            return
        text = text.strip()
        if not text or text == self._clip_used or "\n" in text or len(text) > CLIPBOARD_MAX_LEN:
            return
        low = text.lower()
        prefix = self.config.server.rstrip("/").lower()
        if not (low.startswith("svn://") or (prefix and low.startswith(prefix))):
            return
        try:
            project = server.parse_project_url(text, self.config.server)
        except ValueError:
            return
        self._clip_used = text  # если человек сотрёт ссылку, не подставляем её снова
        self.url_var.set(text)
        self.url_status.configure(text=f"✔ Проект: {project.name}. Ссылка вставлена из буфера обмена.",
                                  style="Hint.TLabel")

    def _name_from_url(self, url: str) -> str:
        try:
            return server.parse_project_url(url, self.config.server).name
        except ValueError:
            return url.rstrip("/").rsplit("/", 1)[-1]

    def _fill_recent_menu(self) -> None:
        """Меню «Недавние»: сначала избранное, потом последние скачанные проекты."""
        menu = self.recent_menu
        menu.delete(0, "end")
        st = self.settings
        names = {r["url"]: r.get("name", "") for r in st.recent}

        def add(url: str, star: bool) -> None:
            name = names.get(url) or self._name_from_url(url)
            menu.add_command(label=f"{'★  ' if star else ''}{name}   —   {url}",
                             command=lambda u=url: self.url_var.set(u))

        for url in st.favorites:
            add(url, True)
        recent = [r["url"] for r in st.recent if r["url"] not in st.favorites]
        if st.favorites and recent:
            menu.add_separator()
        for url in recent:
            add(url, False)
        if not st.favorites and not recent:
            menu.add_command(label="(пока пусто: здесь появятся скачанные проекты)", state="disabled")

    def _toggle_favorite(self) -> None:
        if not self.project:
            return
        url = self.project.display
        favorites = self.settings.favorites
        if url in favorites:
            favorites.remove(url)
        else:
            favorites.append(url)
        self.settings_store.save()
        self._update_fav_button()

    def _update_fav_button(self) -> None:
        starred = bool(self.project) and self.project.display in self.settings.favorites
        self.fav_btn.configure(text=FAV_REMOVE_TEXT if starred else FAV_ADD_TEXT)

    def _url_hint(self) -> str:
        """Подсказка под пустой строкой ссылки: пример с адресом сервера из настроек (и диска, если он настроен)."""
        example = server.example_url(self.config.server, self.config.repos)
        disk = next((str(m["path"]).rstrip("\\") for m in self.config.mirrors if m.get("path")), "")
        return ("Вставьте ссылку на проект или путь к папке, например " + example
                + (f" или {disk}\\мой_проект." if disk else "."))

    def _on_url_changed(self) -> None:
        text = self.url_var.get()
        self.project = None
        self.line = server.TRUNK_LINE
        self._set_create_target(None)
        self._local_gen += 1                  # ответ про прежний путь на диске уже не нужен
        if self._local_after is not None:
            self.root.after_cancel(self._local_after)
            self._local_after = None
        if not text.strip():
            self.url_status.configure(text=self._url_hint(), style="Hint.TLabel")
        elif mirrors.looks_like_local_path(text):
            self._begin_local_lookup(text)
        else:
            try:
                picked = self._tree_pick
                self.project = (picked[1] if picked and picked[0] == text
                                else server.parse_project_url(text, self.config.server))
                note = self._mirror_note[1] if self._mirror_note and self._mirror_note[0] == text else ""
                what = "Папка" if self.project.line.kind == "folder" else "Проект"
                self.url_status.configure(text=f"✔ {what}: {self.project.name}" + (f" {note}" if note else ""),
                                          style="Hint.TLabel")
            except ValueError as exc:
                self.url_status.configure(text=str(exc), style="Error.TLabel")
        if self.project:
            self.line = self.project.line     # ссылка на ветку или версию выбирает её сразу
        self._lines_error = ""
        self._info_gen += 1                   # ответ про прежний выбор уже не нужен
        self._set_line_info("")
        self._fill_line_combo()
        self._schedule_lines_load()
        self._update_fav_button()
        self._update_dest_label()
        self._update_buttons()

    # -- путь к папке на диске вместо ссылки (Z:\… или \\сервер\…) ----------------------------

    def _use_project(self, ref: server.ProjectRef, note: str = "") -> None:
        """Подставляет в поле ссылку на проект (проект берётся как есть, без разбора строки)."""
        text = ref.display
        self._tree_pick = (text, ref)
        self._mirror_note = (text, note) if note else None
        self.url_var.set(text)

    def _set_create_target(self, target: Optional[tuple]) -> None:
        """(путь на диске, адрес сервера, сегменты проекта) — тогда видна кнопка «Создать проект из этой папки…»."""
        self._create_target = target
        self._show(self.create_btn, target is not None)
        self._update_buttons()

    def _local_status(self, text: str, error: bool = False) -> None:
        self.url_status.configure(text=text, style="Error.TLabel" if error else "Hint.TLabel")

    def _begin_local_lookup(self, text: str) -> None:
        """Путь на диске: сервер спрашиваем не на каждую букву, а после паузы ввода."""
        if not self.client:
            self._local_status("Не найдена программа svn.exe (TortoiseSVN): проверить папку нельзя.", error=True)
            return
        if not self.config.mirrors:
            self._local_status(LOCAL_OUTSIDE + " Соответствие папок на диске и на сервере не настроено: "
                               "обратитесь к администратору (поле mirrors в общем svn_helper.json).", error=True)
            return
        self._local_status("Проверяю папку…")
        gen = self._local_gen
        self._local_after = self.root.after(LINES_DELAY_MS, lambda: self._on_local_timer(gen, text))

    def _on_local_timer(self, gen: int, text: str) -> None:
        self._local_after = None
        if gen != self._local_gen or self._closing or not self.client:
            return
        client, found_mirrors, server_url = self.client, list(self.config.mirrors), self.config.server

        def work():
            match = mirrors.map_path(text, found_mirrors, server_url, mirrors.unc_resolver)
            if match is None:
                return None, False
            target = mirrors.find_target(client, match)
            return target, (mirrors.is_dir(text) if target.kind == mirrors.MISSING else True)

        self.run_auth(work, lambda res: self._on_local_found(gen, text, *res),
                      lambda kind: self._on_local_failed(gen, kind))

    def _on_local_found(self, gen: int, text: str, target: Optional[mirrors.Target], exists: bool) -> None:
        if gen != self._local_gen:
            return
        if target is None:
            places = ", ".join(m["path"] for m in self.config.mirrors[:3])
            self._local_status(f"{LOCAL_OUTSIDE} Проекты на сервере лежат в папках: {places}.", error=True)
        elif target.kind == mirrors.ROOT:
            self._local_status("Это папка со всеми проектами сервера. Укажите папку конкретного проекта внутри неё.", error=True)
        elif target.kind == mirrors.NO_REPO:
            self._local_status(f"Папки «{'/'.join(target.match.repo_segments)}» нет на сервере. "
                               "Сообщите администратору: неверно настроено соответствие папок.", error=True)
        elif target.kind == mirrors.FOLDER:
            self._use_project(server.folder_from_segments(target.match.prefix, tuple(target.match.segments)),
                              "(обычная папка, не проект: скачается как есть)")
        elif target.kind == mirrors.PROJECT:
            note = "(найден по пути на диске"
            note += f"; скачивается весь проект, а не только папка «{target.subfolder[-1]}»)" if target.subfolder \
                else ")"
            self._use_project(target.project, note)
        elif not exists:
            self._local_status(LOCAL_MISSING + " Папка на диске не найдена: проверьте путь.", error=True)
        else:
            self._local_status(LOCAL_MISSING + " Его можно создать из этой папки: сама папка не изменится.")
            self._set_create_target((text, target.match.prefix, target.create_segments))

    def _on_local_failed(self, gen: int, kind: ErrorKind) -> None:
        if gen == self._local_gen:
            self._local_status("Проверить папку не удалось: " + messages.short_message(kind), error=True)

    def _on_create_project(self) -> None:
        target = self._create_target
        if target is None or self.job or self.upd or not self.client:
            return
        src, prefix, segments = target
        dialog = CreateProjectDialog(self, src, prefix, segments)
        self._create_dialog = dialog
        try:
            self.root.wait_window(dialog)
        finally:
            self._create_dialog = None
        if dialog.result is None or self._closing:
            return
        ref, download = dialog.result
        self._lines_cache.pop(ref.display, None)
        self._use_project(ref, "(создан из папки)")
        if download:
            self._on_download()

    def _create_running(self) -> bool:
        dialog = self._create_dialog
        return dialog is not None and dialog.state == "running"

    # -- что скачать: основная линия, ветка или версия ---------------------------------

    @staticmethod
    def _dest_name(project: server.ProjectRef, line: server.Line) -> str:
        """Имя папки: «проект» для основной линии, «проект__ветка» для ветки и версии."""
        return checks.safe_local_name(project.name if line.kind in ("trunk", "folder") else f"{project.name}__{line.name}")

    @staticmethod
    def _display_line_url(project: server.ProjectRef, line: server.Line) -> str:
        """Ссылка в читаемом виде, включая ветку или версию (для «Недавних»)."""
        return project.display + ("" if line.kind in ("trunk", "folder") else "/" + "/".join(line.segments()))

    def _fill_line_combo(self) -> None:
        project = self.project
        lines = self._lines_cache.get(project.display) if project else None
        choices, labels = [server.TRUNK_LINE], [TRUNK_CHOICE]
        for kind, key in (("branch", "branches"), ("tag", "tags")):
            for entry in (lines or {}).get(key, []):
                choices.append(server.Line(kind, entry.name))
                labels.append(line_choice_label(kind, entry))
        if project and self.line.kind == "folder":   # обычная папка: линий нет, выбирать нечего
            choices, labels = [self.line], [messages.line_label(self.line)]
        elif project and self.line not in choices:   # ссылка указывает на линию, которой в списке (пока) нет
            choices.append(self.line)
            labels.append(messages.line_label(self.line))
        self._line_choices = choices
        self._show(self.line_row, bool(project) and len(choices) > 1)   # у проекта без веток и версий выбирать нечего
        self.line_combo.configure(values=labels)
        self.line_combo.current(choices.index(self.line) if self.line in choices else 0)
        self.line_combo.configure(state="readonly" if project and not (self.job or self.upd) else "disabled")

    def _schedule_lines_load(self) -> None:
        """Ветки и версии проекта грузятся не на каждую букву ссылки, а после короткой паузы."""
        if self._lines_after is not None:
            self.root.after_cancel(self._lines_after)
            self._lines_after = None
        if self.project and self.client:
            self._lines_after = self.root.after(LINES_DELAY_MS, self._on_lines_timer)

    def _on_lines_timer(self) -> None:
        self._lines_after = None
        project = self.project
        if not project or not self.client or self._closing:
            return
        self._load_line_info()
        if project.display not in self._lines_cache:
            self._load_lines(project)

    def _load_lines(self, project: server.ProjectRef) -> None:
        key = project.display
        if key in self._lines_loading or not self.client:
            return
        self._lines_loading.add(key)
        client = self.client
        url = server.build_url(project.prefix, project.segments)
        self.run_auth(lambda: client.list_lines(url), lambda res: self._on_lines_loaded(key, res),
                      lambda kind: self._on_lines_failed(key, kind))

    def _on_lines_loaded(self, key: str, lines: dict) -> None:
        self._lines_loading.discard(key)
        self._lines_cache[key] = lines
        if self.project and self.project.display == key:
            self._lines_error = ""
            self._fill_line_combo()
            self._set_line_info(self._info_text)

    def _on_lines_failed(self, key: str, kind: ErrorKind) -> None:
        self._lines_loading.discard(key)
        if self.project and self.project.display == key:
            self._lines_error = ("Список веток и версий получить не удалось: " + messages.short_message(kind)
                                 + " Основную скачать можно.")
            self._set_line_info(self._info_text)

    def _on_line_selected(self, _event=None) -> None:
        index = self.line_combo.current()
        if not self.project or not 0 <= index < len(self._line_choices):
            return
        self.line = self._line_choices[index]
        self._set_line_info("")
        self._load_line_info()
        self._update_dest_label()
        self._update_buttons()

    def _set_line_info(self, text: str) -> None:
        """Одна короткая строка под списком «Что скачать» (и ошибка загрузки списка, если была)."""
        self._info_text = text
        shown = "\n".join(part for part in (self._lines_error, text) if part)
        self.line_info.configure(text=shown)
        self._show(self.line_info, bool(shown))
        self._ensure_height()

    def _ensure_height(self) -> None:
        """Появилась новая строка сведений: окно подрастает, чтобы шаги 2 и 3 не обрезались."""
        if not self.root.winfo_viewable():
            return
        self.root.update_idletasks()
        need = self.root.winfo_reqheight()
        if need > self.root.winfo_height():
            self.root.geometry(f"{self.root.winfo_width()}x{need}")

    def _load_line_info(self) -> None:
        """Про ветку: кто создал и кто менял последним (в фоне). Про версию: «только для просмотра». Про основную: ничего."""
        self._info_gen += 1
        gen = self._info_gen
        project, line, client = self.project, self.line, self.client
        if not project:
            return
        if line.kind == "tag":
            self._set_line_info(messages.TAG_VIEW_ONLY)
            return
        if line.kind in ("trunk", "folder") or not client:
            self._set_line_info("")
            return
        url = project.line_url(line)
        self._set_line_info("Загружаю сведения…")

        def work():
            return client.branch_history(url, limit=messages.HISTORY_LIMIT)   # занятые файлы здесь не нужны

        self.run_auth(work, lambda res: self._on_line_info(gen, line, res),
                      lambda kind: self._on_line_info_failed(gen, kind))

    def _on_line_info(self, gen: int, line: server.Line, history) -> None:
        if gen != self._info_gen:
            return
        self._set_line_info(messages.branch_summary(history))

    def _on_line_info_failed(self, gen: int, kind: ErrorKind) -> None:
        if gen == self._info_gen:
            self._set_line_info("Сведения получить не удалось: " + messages.short_message(kind))

    # -- папка на компьютере ----------------------------------------------------

    def _on_browse(self) -> None:
        initial = self.dir_var.get().strip()
        initial = checks.nearest_existing(initial) if initial else ""
        chosen = filedialog.askdirectory(parent=self.root, title="Папка для проектов",
                                         initialdir=initial or None, mustexist=False)
        if chosen:     # «Выбрать…» на любой вкладке добавляет папку в общий список и делает её текущей
            self._remember_project_dir(chosen)
            self.dir_var.set(os.path.normpath(chosen))

    def _update_dest_label(self) -> None:
        self._dest_gen += 1
        gen = self._dest_gen
        base = self.dir_var.get().strip()
        if not (self.project and base):
            self.dest_var.set("")
            return
        project, line = self.project, self.line
        dest = os.path.join(os.path.normpath(base), self._dest_name(project, line))
        if not os.path.isdir(os.path.join(dest, ".svn")):
            self.dest_var.set("Проект будет сохранён в: " + shorten_path(dest, 55))
            return
        self.dest_var.set(f"Уже скачано: {shorten_path(dest, 55)}. Свежее — на вкладке «Мои проекты».")
        client = self.client
        if client is None:
            return
        wanted = project.line_url(line)

        def done(info, err, _stamp) -> None:
            """Сверяем по адресу линии: в папке мог остаться другой проект или другая ветка того же проекта."""
            if gen != self._dest_gen or err is not None or same_url(info.url, wanted):
                return
            found = server.line_from_url(info.url, info.root)
            other = f" ({messages.line_label(found[1])})" if found else ""
            self.dest_var.set(f"В папке {shorten_path(dest, 55)} уже лежит другой проект или другая ветка{other}. "
                              "Выберите другую папку.")

        self.run_bg(lambda: client.info(dest), done)

    # -- состояние кнопок ------------------------------------------------------

    def _update_buttons(self) -> None:
        busy = self.job is not None
        any_busy = busy or self.upd is not None
        self.download_btn.configure(
            state="normal" if (self.project and not any_busy and self.client) else "disabled")
        other = "disabled" if any_busy else "normal"
        self.browse_btn.configure(state=other)
        self.refresh_btn.configure(state=other)
        self.dir_entry.configure(state=other)
        self.url_entry.configure(state=other)
        self.browse_disk_btn.configure(state=other)
        self.recent_btn.configure(state=other)
        self.create_btn.configure(state="normal" if (self._create_target and not any_busy and self.client)
                                  else "disabled")
        self.fav_btn.configure(state="normal" if (self.project and not any_busy) else "disabled")
        self.line_combo.configure(state="readonly" if (self.project and not any_busy) else "disabled")
        self._sync_download_status()
        self._update_mine_buttons()

    def _notify_done(self, started: float, cancelled: bool = False) -> None:
        """Долгая задача закончилась, а окно не в фокусе: мигаем кнопкой в панели задач и подаём звук."""
        if self._closing or cancelled or time.monotonic() - started < LONG_TASK_SEC:
            return
        if self.root.focus_displayof() is not None and self.root.state() != "iconic":
            return  # человек смотрит в окно: он и так увидит результат
        winutil.flash_window(winutil.toplevel_handle(self.root.winfo_id()))
        winutil.beep()

    # -- скачивание ---------------------------------------------------------------

    @staticmethod
    def _was_fresh(dest: str) -> bool:
        """Папки проекта ещё нет или она пуста: значит, всё, что в ней появится, принесла эта программа."""
        try:
            return not os.path.exists(dest) or not os.listdir(dest)
        except OSError:
            return False

    def _on_download(self) -> None:
        if self.job or not self.project or not self.client:
            return
        project, line = self.project, self.line
        name = self._dest_name(project, line)
        check = checks.check_target(self.dir_var.get(), name)
        if check.error:
            messagebox.showerror("Сюда скачать нельзя", check.error, parent=self.root)
            return
        if check.warning and not messagebox.askyesno(
                "Длинный путь", check.warning + "\n\nВсё равно продолжить?", parent=self.root):
            return
        self._remember_project_dir(check.base)    # новая папка попадает в общий список папок
        self.info_var.set("")
        self._hide_cleanup()
        self._show(self.progress, True)
        self.job = DownloadJob(url=project.line_url(line), name=name, dest=check.dest, client=self.client,
                               base=check.base, dest_fresh=self._was_fresh(check.dest),
                               display_url=self._display_line_url(project, line), line=line)
        self._update_buttons()
        self._start_estimate()

    def _start_estimate(self) -> None:
        job = self.job
        job.phase = "estimate"
        self.status_var.set("Подсчёт размера проекта…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.run_bg(lambda: job.client.estimate(job.url, job.cancel),
                    lambda res, err, gen: self._on_estimated(job, res, err, gen))

    def _on_estimated(self, job: DownloadJob, estimate: Optional[Estimate], error, gen: JobStamp) -> None:
        if job is not self.job:
            return
        if error is not None:
            kind = self._error_kind(error)
            if kind is ErrorKind.CANCELLED:
                self._finish_cancelled(job)
                return
            if kind is ErrorKind.AUTH:
                if self._closing:   # окно закрывается: входить незачем и диалоги не нужны
                    self._end_job()
                    return
                self.login.request(retry=self._start_estimate,
                                   give_up=lambda: self._finish_auth_declined(job), job_gen=gen)
                return
            if kind is not ErrorKind.OTHER:  # связи/прав/папки нет: checkout упадёт так же
                self._finish_error(job, kind, self._error_raw(error))
                return
            log.warning("Подсчёт размера не удался, скачиваем без процентов")
            job.audit_skipped = self.config.filter.enabled
            self.info_var.set("Размер проекта определить не удалось, процент выполнения не показывается."
                              + (" Проверка на мусор пропущена." if job.audit_skipped else ""))
        else:
            self.login.reset_attempts()
            self.info_var.set(messages.estimate_text(estimate))
            problem = checks.check_free_space(job.dest, estimate.size)
            if problem:
                self._finish_error_text(job, "Мало места на диске", problem)
                return
            job.total = estimate.entries or None
            rules = self.config.filter
            if rules.enabled:
                job.audit = audit.analyze(estimate.items, rules, job.dest)
                if job.audit.needs_attention and rules.show_report and not self._confirm_audit(job):
                    self.status_var.set("Скачивание отменено")
                    self._end_job()
                    return
        self._start_checkout()

    def _confirm_audit(self, job: DownloadJob) -> bool:
        self.progress.stop()
        dialog = AuditDialog(self.root, job.name, job.audit.report_lines())
        self.root.wait_window(dialog)
        return dialog.result

    def _start_checkout(self) -> None:
        job = self.job
        job.phase = "checkout"
        job.progress = CheckoutProgress()
        job.eta = EtaTracker()  # скорость считаем только по самому скачиванию, без подсчёта размера
        self.progress.stop()
        if job.total:
            self.progress.configure(mode="determinate", maximum=job.total, value=0)
        else:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        self.status_var.set("Скачивание…")

        def work():
            os.makedirs(os.path.dirname(job.dest), exist_ok=True)
            return job.client.checkout(job.url, job.dest, job.progress, job.cancel)

        self.run_bg(work, lambda res, err, gen: self._on_checked_out(job, res, err, gen))
        self.root.after(PROGRESS_MS, self._tick_progress, job)

    def _tick_progress(self, job: DownloadJob) -> None:
        if job is not self.job or job.phase != "checkout":
            return
        count = job.progress.count
        if job.cancel.cancelled:                # «Отмену» в панели уже нажали: ход не затирает «Отмена…»
            self.status_var.set("Отмена…")
        elif job.total:
            self.progress.configure(value=min(count, job.total))
            percent = min(100, count * 100 // job.total)
            self._set_download_tab_label(percent)
            text = f"Скачано {messages.format_number(count)} из {messages.format_number(job.total)} ({percent}%)"
            eta = format_eta(job.eta.update(count, job.total))
            self.status_var.set(f"{text}, {eta}" if eta else text)
        else:
            self.status_var.set(f"Скачано файлов и папок: {messages.format_number(count)}")
        self.root.after(PROGRESS_MS, self._tick_progress, job)

    def _on_checked_out(self, job: DownloadJob, result, error, gen: JobStamp) -> None:
        if job is not self.job:
            return
        if error is None:
            self.login.reset_attempts()
            self._after_checkout(job, result)
            return
        if isinstance(error, OSError):
            self._finish_error_text(job, "Не удалось создать папку",
                                    f"Не удалось создать папку для проекта: {error.strerror or error}")
            return
        kind = self._error_kind(error)
        if kind is ErrorKind.CANCELLED:
            self._finish_cancelled(job)
        elif kind is ErrorKind.AUTH:
            if self._closing:
                self._end_job()
                return
            self.login.request(retry=self._start_checkout_again,
                               give_up=lambda: self._finish_auth_declined(job), job_gen=gen)
        else:
            self._finish_error(job, kind, self._error_raw(error))

    def _after_checkout(self, job: DownloadJob, result) -> None:
        found = job.audit
        if not (found and found.skip_junk and found.junk_paths):
            self._finish_success(job, result)
            return
        job.phase = "exclude"
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status_var.set("Убираю лишние файлы…")
        client = job.client

        def work():
            try:
                client.exclude_paths(job.dest, found.junk_paths, job.cancel)
            except SvnError as exc:
                if exc.kind is ErrorKind.CANCELLED:
                    try:
                        client.cleanup(job.dest)  # прерванная операция оставляет копию заблокированной
                    except SvnError:
                        log.exception("Cleanup после отмены не удался")
                raise

        self.run_bg(work, lambda _res, err, _gen: self._on_excluded(job, result, err))

    def _on_excluded(self, job: DownloadJob, result, error) -> None:
        found = job.audit
        if self._closing:  # окно закрывается: итоговых диалогов не показываем
            self._end_job()
            return
        if isinstance(error, SvnError) and error.kind is ErrorKind.CANCELLED:
            note = "Удаление лишних файлов остановлено: часть лишнего осталась в папке."
        elif error is None:
            files = f"{found.junk_files} {messages.plural(found.junk_files, ('файл', 'файла', 'файлов'))}"
            note = f"Не оставлено в папке как лишнее: {files}, {messages.format_size(found.junk_size)}."
        else:
            log.error("Не удалось убрать лишние файлы", exc_info=error)
            note = ("Не удалось убрать лишние файлы (мусор остался в папке). "
                    f"Подробности записаны в файл:\n{self.log_path}")
        self._finish_success(job, result, note)

    def _start_checkout_again(self) -> None:
        if self.job:
            self._start_checkout()

    def _on_cancel(self) -> None:
        """«Отмена» в панели подсказок, пока идёт скачивание."""
        if self.job:
            self.job.cancel.cancel()
            self.status_var.set("Отмена…")      # панель покажет «Отмена…», её кнопка станет серой

    # -- завершение задачи -----------------------------------------------------------

    def _set_download_tab_label(self, percent: Optional[int] = None) -> None:
        """Ярлык подвкладки «Скачать проект»: пока идёт скачивание, с процентами («Скачать проект · 45%»)."""
        title = dict(self.LIST_MODES)["download"]
        self.list_tabs.tab(self.list_frames["download"], text=f"{title} · {percent}%" if percent is not None else title)

    def _end_job(self) -> None:
        self._set_download_tab_label()
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self._show(self.progress, False)
        self.job = None
        self._update_buttons()
        self._apply_pending_rebuild()

    # недокачанная папка: после отмены или ошибки её можно удалить одной кнопкой

    def _hide_cleanup(self) -> None:
        self._partial_job = None
        self._show(self.cleanup_btn, False)

    def _offer_cleanup(self, job: DownloadJob) -> None:
        """Показывает кнопку удаления, если на диске осталась папка, которую создала эта программа."""
        self._hide_cleanup()
        if not job.dest_fresh:
            return
        try:
            partial = os.path.isdir(job.dest) and bool(os.listdir(job.dest))
        except OSError:
            partial = False
        if partial:
            self._partial_job = job
            self.cleanup_btn.configure(state="normal")
            self._show(self.cleanup_btn, True)

    def _partial_note(self, job: DownloadJob) -> str:
        if self._partial_job is job:
            return (f"\n\nЧасть файлов уже скачана в папку:\n{job.dest}\n"
                    "Нажмите «Удалить недокачанную папку» справа, если она не нужна.")
        return ""

    def _on_remove_partial(self) -> None:
        job = self._partial_job
        if job is None or self.job or self.upd:
            return
        if not messagebox.askyesno(
                "Удалить недокачанную папку?",
                f"Будет удалена папка со всем, что в неё успело скачаться:\n{job.dest}\n\nУдалить?",
                parent=self.root):
            return
        self.cleanup_btn.configure(state="disabled")
        self.status_var.set("Удаляю недокачанную папку…")
        self._removing_partial = True
        self.run_bg(lambda: fsutil.remove_partial_dir(job.dest, job.base),
                    lambda _res, err, _stamp: self._on_partial_removed(job, err))

    def _on_partial_removed(self, job: DownloadJob, error: Optional[Exception]) -> None:
        self._removing_partial = False
        if self._closing:   # окно закрывается: итог в журнал, диалоги не нужны
            if error is not None:
                log.error("Не удалось удалить недокачанную папку %s", job.dest, exc_info=error)
            return
        if error is None:
            self._hide_cleanup()
            self._mine_dirty = True
            self.status_var.set("Недокачанная папка удалена")
            return
        log.error("Не удалось удалить недокачанную папку %s", job.dest, exc_info=error)
        self.cleanup_btn.configure(state="normal")
        self.status_var.set("Папку удалить не удалось")
        reason = (error.strerror or str(error)) if isinstance(error, OSError) else str(error)
        messagebox.showerror(
            "Не удалось удалить папку",
            f"Не удалось удалить папку:\n{job.dest}\n\nПричина: {reason}\n\n"
            "Закройте программы, в которых открыты файлы из этой папки, и попробуйте ещё раз."
            + (f"\n\nЕсли не получается: {self.config.support}" if self.config.support else ""),
            parent=self.root)

    def _finish_success(self, job: DownloadJob, result, note: str = "") -> None:
        if self._closing:  # окно закрывается: итоговых диалогов не показываем
            self._end_job()
            return
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=100)
        self.status_var.set("Готово")
        self._mine_dirty = True
        add_recent(self.settings, job.display_url, job.name, job.dest)
        self.settings_store.save()
        if job.audit_skipped:
            note = (note + "\n" if note else "") + "Проверка на мусор пропущена: не удалось подсчитать размер проекта."
        self._notify_done(job.started)
        version = f" Сохранение № {result.revision} на сервере." if result.revision else ""
        extra = f" {note}" if note else ""
        reminder = messages.TAG_EXPLAIN if job.line and job.line.read_only else messages.LOCK_REMINDER
        text = f"Проект «{job.name}» скачан.{version}{extra} {reminder}"
        self._end_job()
        self.progress.configure(value=100)    # после успешного скачивания полоса остаётся полной
        self._show(self.progress, True)
        # результат в карточку нового проекта вместо окна «Готово»; «Открыть папку» и так в колонке слева
        self._action_notes[os.path.normcase(os.path.normpath(job.dest))] = text
        self._show_new_project(job.dest)
        self.select_list_mode("mine")
        if not self._card_visible():           # «Состояние проекта» свёрнуто: итог в панели подсказок
            self.hint_bar.show_result(text)

    def _finish_cancelled(self, job: DownloadJob) -> None:
        self.status_var.set("Скачивание отменено")
        self._end_job()
        self._offer_cleanup(job)
        if self._closing:
            return
        text = "Скачивание остановлено."
        if self._partial_job is job:
            text += (f" Папка\n{job.dest}\nосталась заполненной частично. Нажмите «Удалить недокачанную папку» "
                     "справа, если она не нужна.")
        messagebox.showinfo("Скачивание отменено", text, parent=self.root)

    def _finish_error(self, job: DownloadJob, kind: ErrorKind, raw: str = "") -> None:
        text = messages.short_message(kind)
        if kind is ErrorKind.NOT_FOUND:
            text = "Проект по этой ссылке не найден. Проверьте ссылку: возможно, в ней опечатка."
        self._finish_error_text(job, "Не удалось скачать проект", text, svn_error=(kind, raw))

    def _finish_auth_declined(self, job: DownloadJob) -> None:
        """Вход отменён или не удался: окно входа уже объяснило причину, второе окно ошибки не нужно, хватит строки статуса."""
        if job is not self.job:
            return
        self.status_var.set("Скачивание не выполнено: нужен вход на сервер")
        self._end_job()
        if job.phase in ("checkout", "exclude"):
            self._offer_cleanup(job)

    def _finish_error_text(self, job: DownloadJob, title: str, text: str,
                           svn_error: Optional[tuple[ErrorKind, str]] = None) -> None:
        """Завершает задачу ошибкой. svn_error=(вид, вывод svn) — ошибка сервера: окно с отчётом для администратора."""
        if self._closing:   # окно закрывается: диалогов под «Останавливаю…» не показываем
            self._end_job()
            return
        self._notify_done(job.started, job.cancel.cancelled)
        self.status_var.set("Скачивание не выполнено")
        self._end_job()
        if job.phase in ("checkout", "exclude") or svn_error:
            self._offer_cleanup(job)
        text += self._partial_note(job)
        if svn_error:
            kind, raw = svn_error
            self.show_error(title, text, action="Скачивание проекта", kind=kind, raw=raw,
                            extra={"Проект": job.name, "Адрес": job.url, "Папка": job.dest})
        else:
            messagebox.showerror(title, text, parent=self.root)
