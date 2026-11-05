"""Вкладка «Ветки» колонки «Состояние проекта»: список линий выбранного проекта, создание, переключение, слияние."""
from __future__ import annotations

import os
from datetime import date
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .. import messages, server, theme
from ..dialogs import NewLineDialog
from ..svn_core import CancelToken, ErrorKind, WcStatus
from . import hints
from .models import JobStamp, LINES_PREFETCH, log, MineItem, UpdateJob


class LinesTabMixin:
    """Вкладка «Ветки» колонки «Состояние проекта»: список линий выбранного проекта, создание, переключение, слияние."""

    # ветки и версии проекта: создать, переключиться, слить

    def _line_item(self) -> Optional[MineItem]:
        """Проект вкладки «Ветки», если с ним сейчас можно работать (подключён, линия известна, папка свободна)."""
        item = self._lines_folder_item()
        if (item is None or item.state != "svn" or item.line is None or not self.client or self.upd or self.job
                or self._locking or self._path_busy(item.path)):
            return None
        return item

    def _prepare_line_op(self, item: MineItem, cont: Callable[[dict, WcStatus], None]) -> None:
        """В фоне: свежий список веток и версий с сервера и статус копии с версиями файлов (status -v).

        Список всегда свежий: по нему проверяются имена и выбирается, куда переключиться.
        Потом cont(линии, статус) в главном потоке. Пока идёт подготовка, копия занята (_locking).
        """
        client = self.client
        key, project_url = item.project_key, item.project_url
        self._locking = True
        self._refresh_mine_progress()
        self._update_mine_buttons()
        self.mine_status.set("Проверяю проект…")

        def work():
            return client.list_lines(project_url), client.status(item.path, verbose=True)

        def finish() -> None:
            self._locking = False
            self._refresh_mine_progress()
            self._update_mine_buttons()

        def ok(result) -> None:
            finish()
            self.mine_status.set("")
            self._lines_cache[key] = result[0]
            cont(*result)

        def fail(kind: ErrorKind) -> None:
            finish()
            if kind is ErrorKind.AUTH:
                self.mine_status.set("Нужен вход на сервер.")
                return
            self.mine_status.set("")
            self.show_error("Не удалось получить сведения о проекте", messages.short_message(kind),
                            action=f"Ветки и версии проекта {item.name}", kind=kind,
                            extra={"Папка": item.path, "Адрес проекта": project_url})

        self.run_auth(work, ok, fail)

    @staticmethod
    def _files_text(count: int) -> str:
        return messages.files_count(count)

    def _on_make_tag(self) -> None:
        item = self._line_item()
        if item is not None and not item.line.read_only:
            self._prepare_line_op(item, lambda lines, st: self._ask_new_line(item, "tag", lines, st))

    def _on_make_branch(self) -> None:
        item = self._line_item()
        if item is not None:
            self._prepare_line_op(item, lambda lines, st: self._ask_new_line(item, "branch", lines, st))

    def _ask_new_line(self, item: MineItem, kind: str, lines: dict, status: WcStatus) -> None:
        """Диалог «Зафиксировать версию» / «Создать ветку» и запуск копирования на сервере."""
        client = self.client
        taken = [e.name for e in lines.get("tags" if kind == "tag" else "branches", [])]
        default = server.suggest_tag_name(date.today(), taken) if kind == "tag" else ""
        revision = status.max_revision or item.revision      # после Commit корень копии остаётся на старом номере
        unsaved = status.modified + status.conflicts

        def check_name(name: str, done: Callable[[bool], None]) -> None:
            """Есть ли такое имя на сервере (в фоне, окно не закрывается). Сбой проверки: решит само копирование."""
            url = item.line_url_of(server.Line(kind, name))
            self.run_bg(lambda: client.url_exists(url),
                        lambda res, err, _stamp: done(bool(res) if err is None else False))

        dialog = NewLineDialog(self.root, kind, revision, messages.included_text(revision, unsaved), default, taken,
                               check_name, can_switch=not unsaved)
        self.root.wait_window(dialog)
        result = dialog.result
        if not result:
            return
        new_line = server.Line(kind, result["name"])
        switch = bool(result.get("switch")) and kind == "branch" and not unsaved
        self._run_copy(item, new_line, result["comment"], switch, status)

    def _run_copy(self, item: MineItem, new_line: server.Line, comment: str, switch: bool,
                  status: WcStatus) -> None:
        """Серверное копирование: текущая ветка на последнем сохранении копии → tags/<имя> или branches/<имя>."""
        client = self.client
        if not client or self._locking:
            return
        src, dst = item.line_url, item.line_url_of(new_line)
        revision = status.max_revision or item.revision
        what = "версию" if new_line.kind == "tag" else "ветку"
        self._locking = True
        self._refresh_mine_progress()
        self._update_mine_buttons()
        self.mine_status.set(f"Создаю {what} на сервере…")

        def done(_res, error, stamp: JobStamp) -> None:
            self._locking = False
            self._refresh_mine_progress()
            self._update_mine_buttons()
            if error is not None:
                kind = self._error_kind(error)
                if kind is ErrorKind.AUTH:
                    self.login.request(retry=lambda: self._run_copy(item, new_line, comment, switch, status),
                                       give_up=lambda: self.mine_status.set("Нужен вход на сервер."),
                                       job_gen=stamp)
                    return
                self.mine_status.set("")
                title = "Не удалось создать версию" if new_line.kind == "tag" else "Не удалось создать ветку"
                if kind is ErrorKind.EXISTS:
                    messagebox.showerror(title, f"{messages.short_message(kind)} Выберите другое имя.",
                                         parent=self.root)
                else:
                    self.show_error(title, messages.short_message(kind),
                                    action=f"Создание: {messages.line_label(new_line)} (проект {item.name})",
                                    kind=kind, raw=self._error_raw(error), extra={"Откуда": src, "Куда": dst})
                return
            self.login.reset_attempts()
            self._lines_cache.pop(item.project_key, None)
            if self.project and self.project.display == item.project_key:
                self._load_lines(self.project)   # список «Что скачать» на первой вкладке обновится
            self.mine_status.set("")
            self._lines_pending_select = new_line   # во вкладке «Ветки» новая линия сразу выбрана
            self._reload_lines_view(item)
            if switch and new_line.kind == "branch":
                self._begin_switch(item, new_line, status, created_label=messages.line_label(new_line))
            else:
                title = "Версия создана" if new_line.kind == "tag" else "Ветка создана"
                messagebox.showinfo(title, f"{messages.line_label(new_line)} создана.", parent=self.root)

        self.run_bg(lambda: client.copy_url(src, dst, comment, revision), done)

    def _begin_switch(self, item: MineItem, target: server.Line, status: WcStatus, created_label: str = "") -> None:
        """Проверки перед svn switch и запуск (тем же механизмом, что Update: прогресс, отмена, конфликты).

        created_label - «Ветка «x»», если переключаемся сразу после создания (итоговое окно будет одно).
        """
        item = self._current(item) or item
        if item.line is None or (target == item.line and not status.switched):
            return
        unsaved = status.modified + status.conflicts
        if unsaved and (status.conflicts or not item.line.read_only):
            text = messages.SWITCH_UNSAVED.format(count=unsaved)
            if messagebox.askyesno("Переключиться нельзя", text + "\n\n" + messages.SWITCH_UNSAVED_ASK,
                                   parent=self.root):
                self.open_project_tab("changes")
                self._on_commit()
            return
        if unsaved and not messagebox.askyesno(
                "Правки в версии",
                f"В папке есть правки ({self._files_text(unsaved)}), а в версию их сохранить нельзя. "
                f"Они останутся в папке и перейдут в: {messages.line_label(target)}. Продолжить?",
                parent=self.root):
            return
        if not self.client or self.upd or self.job or self._locking or self._path_busy(item.path):
            return
        # свои занятые файлы освобождаются сами: в другой ветке они уже не нужны, а другим мешали бы
        unlock = [lock.path if os.path.isabs(lock.path) else os.path.join(item.path, lock.path)
                  for lock in status.my_locks]
        self.upd = UpdateJob(item, client=self.client, url=item.line_url_of(target), target=target,
                             unlock_paths=unlock, created_label=created_label)
        self._start_update()

    def _on_merge(self) -> None:
        item = self._lines_folder_item()
        if (item is None or item.state != "svn" or not self.tortoise_exe
                or (item.line is not None and item.line.read_only)):
            return
        self._on_tortoise("merge", note=messages.MERGE_NOTE, item=item)

    # -- вкладка «Ветки» колонки «Состояние проекта» -----------------------------------
    # Папка вкладки - проект, выделенный в колонке 2 (_selected_item); своего выбора папки нет.

    def _build_lines_view(self, tab: ttk.Frame) -> None:
        """Строит содержимое вкладки «Ветки» в рамке tab: сообщение, таблица линий, карточка, подсказка, кнопки."""
        px = self.px
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=0)
        tab.rowconfigure(1, weight=1)
        self.lines_status = ttk.Label(tab, text="", justify="left")      # «Нет связи», «Загружаю…», что пошло не так
        self.lines_status.grid(row=0, column=0, sticky="w")
        self._autowrap(self.lines_status, tab, px(30))

        self.lines_body = body = ttk.Frame(tab)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        frame = ttk.Frame(body, style="Card.TFrame", padding=1)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.lines_tree = ttk.Treeview(frame, columns=("name", "by", "created", "last", "desc", "here", "path"),
                                       displaycolumns=("here", "name", "by", "created", "last", "desc"),
                                       show="headings", selectmode="browse", height=5)
        for col, title, width, minwidth, stretch in (
                ("name", "Название", 150, 110, False), ("by", "Создал", 70, 60, False),
                ("created", "Создана", 80, 70, False), ("last", "Последнее изменение", 130, 100, False),
                ("desc", "Описание", 150, 80, True), ("here", "", 26, 26, False), ("path", "Путь на сервере", 100, 60, False)):
            self.lines_tree.heading(col, text=title, anchor="w")
            self.lines_tree.column(col, width=px(width), minwidth=px(minwidth), anchor="w", stretch=stretch)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.lines_tree.yview)
        self.lines_tree.configure(yscrollcommand=lambda first, last: (scroll.set(first, last), self._lines_scrolled()))
        self.lines_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self._table_extras(self.lines_tree, frame)
        theme.configure_tree_tags(self.lines_tree)
        self.lines_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_lines_select())
        hints.hint(self.lines_tree, "lines.table")

        # карточка выбранной линии
        self.line_card = card = ttk.Frame(body, style="Card.TFrame", padding=(px(14), px(10)))
        card.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
        card.columnconfigure(0, weight=1)
        self.line_title = ttk.Label(card, text="", style="Section.TLabel")
        self.line_title.grid(row=0, column=0, sticky="w", pady=(0, px(2)))
        self.line_card_rows: list[ttk.Label] = []
        for index in range(4):
            label = ttk.Label(card, text="", justify="left")
            label.grid(row=index + 1, column=0, sticky="w")
            self._autowrap(label, card, px(30))
            self.line_card_rows.append(label)

        self.lines_hint = ttk.Label(body, text="", style="Hint.TLabel", justify="left")
        self.lines_hint.grid(row=2, column=0, sticky="w", pady=(px(8), 0))
        self._autowrap(self.lines_hint, body, px(30))

        # кнопки стоят в колонке справа от панели «Состояние проекта» (project_panel.actions_for), по частоте
        # использования сверху вниз: «Перейти», «Отличия», «Перенести правки…», «Новая ветка…»,
        # «Скачать отдельно», «Зафиксировать версию…»
        self.lines_buttons = row = self.project_panel.actions_for("lines")
        self.switch_here_btn = ttk.Button(row, text="Перейти", command=self._on_switch_here, state="disabled")
        self.diff_btn = ttk.Button(row, text="Отличия", command=self._on_show_diff, state="disabled")
        self.merge_btn = ttk.Button(row, text="Перенести правки…", command=self._on_merge, state="disabled")
        self.new_branch_btn = ttk.Button(row, text="Новая ветка…", command=self._on_make_branch, state="disabled")
        self.download_other_btn = ttk.Button(row, text="Скачать отдельно", command=self._on_download_separately,
                                             state="disabled")
        self.new_tag_btn = ttk.Button(row, text="Зафиксировать версию…", command=self._on_make_tag, state="disabled")
        for button, key in ((self.switch_here_btn, "lines.switch"), (self.diff_btn, "lines.compare"),
                            (self.merge_btn, "lines.merge"), (self.new_branch_btn, "lines.new_branch"),
                            (self.download_other_btn, "lines.download_other"), (self.new_tag_btn, "lines.new_tag")):
            row.add(button, hint=key)

        # «Отличия» выбранной ветки от Основной: подпись и список файлов под карточкой и подсказкой
        self.diff_box = box = ttk.Frame(body)
        box.grid(row=3, column=0, sticky="ew", pady=(px(8), 0))
        box.columnconfigure(0, weight=1)
        self.diff_label = ttk.Label(box, text="", justify="left")
        self.diff_label.grid(row=0, column=0, sticky="w")
        self._autowrap(self.diff_label, box, px(30))
        self.diff_frame = dframe = ttk.Frame(box, style="Card.TFrame", padding=1)
        dframe.grid(row=1, column=0, sticky="ew")
        dframe.columnconfigure(0, weight=1)
        self.diff_tree = ttk.Treeview(dframe, columns=("kind", "path"), show="headings", selectmode="browse", height=6)
        self.diff_tree.heading("kind", text="Что с файлом", anchor="w")
        self.diff_tree.heading("path", text="Файл", anchor="w")
        self.diff_tree.column("kind", width=px(100), minwidth=px(80), anchor="w", stretch=False)
        self.diff_tree.column("path", width=px(300), minwidth=px(100), anchor="w", stretch=True)
        dscroll = ttk.Scrollbar(dframe, orient="vertical", command=self.diff_tree.yview)
        self.diff_tree.configure(yscrollcommand=dscroll.set)
        self.diff_tree.grid(row=0, column=0, sticky="ew")
        dscroll.grid(row=0, column=1, sticky="ns")
        theme.configure_tree_tags(self.diff_tree)
        self._show(self.diff_box, False)
        self._show(self.lines_body, False)

    def _lines_tab_active(self) -> bool:
        """Вкладка «Ветки» сейчас на виду (окно на «Мои проекты», колонка 3 развёрнута, выбрана эта вкладка)."""
        return self._project_tab_shown("lines")

    def _lines_folder_item(self) -> Optional[MineItem]:
        """Проект вкладки «Ветки»: выделенный в колонке 2, если он из SVN и его линия известна."""
        item = self._selected_item()
        if item is None or item.state != "svn" or item.line is None or not item.project_prefix:
            return None
        return item

    def _lines_message(self) -> str:
        """Что сказать вместо таблицы: нет проекта, нет связи, идёт загрузка, сбой. Пусто - таблицу можно показывать."""
        selected = self._selected_item()
        if selected is None:
            return messages.LINES_PICK_PROJECT
        if selected.state == "broken":
            return messages.PANEL_BROKEN
        if selected.state != "svn":
            return messages.PANEL_NOT_SVN
        if self._lines_folder_item() is None:
            return messages.LINES_NO_INFO
        if self._lines_view_loading:
            return messages.LINES_LOADING
        if self._lines_view_error:
            return self._lines_view_error
        if self._lines_stale and (self._offline or not self.client):
            return messages.PANEL_OFFLINE
        return ""

    def _update_lines_message(self) -> None:
        text = self._lines_message()
        self.lines_status.configure(text=text)
        self._show(self.lines_status, bool(text))
        self._show(self.lines_body, bool(self._line_rows) and not text)

    def _sync_lines_view(self) -> None:
        """Вкладка «Ветки» по выбранному проекту: другой проект - таблица сбрасывается и читается заново (когда
        вкладка на виду и есть связь), тот же - обновляются только отметки."""
        if not hasattr(self, "lines_tree"):
            return
        item = self._lines_folder_item()
        path = os.path.normcase(item.path) if item is not None else None
        if path != self._lines_path:
            self._lines_path = path
            self._clear_lines_view()
            self._lines_stale = item is not None
        if item is not None and self._lines_stale and self._lines_tab_active() and not self._offline:
            self._load_lines_view(item)
        elif item is not None:
            self._refresh_lines_marks()
        else:
            self._update_lines_buttons()

    def _reload_lines_quietly(self) -> None:
        """Автообновление: перечитывает список линий без сброса выбора; таблица перерисовывается, только если он изменился."""
        item = self._lines_folder_item()
        client = self.client
        if item is None or client is None or self._lines_view_loading or self._lines_stale:
            return
        gen, key, url = self._lines_gen, item.project_key, item.project_url

        def done(res, err, _stamp) -> None:
            if err is not None or gen != self._lines_gen or self._lines_view_loading or self._closing:
                return                      # фоновое обновление молчит: ошибку покажет ручное «⟳»
            if self._lines_cache.get(key) == res:
                self._refresh_lines_marks()
                return
            self._lines_cache[key] = res
            selected = self._selected_line()
            self._lines_pending_select = selected[0] if selected else None
            self._fill_lines_rows(res)

        self.run_bg(lambda: client.list_lines(url), done)

    def _on_lines_refresh(self) -> None:
        item = self._lines_folder_item()
        if item is None or self.upd or self.job:
            return
        self._line_history.clear()
        self._line_locks.clear()
        self._load_lines_view(item)
        self._check_status([item])

    def _reload_lines_view(self, item: Optional[MineItem] = None) -> None:
        """Список линий изменился на сервере (создали ветку, переключили папку): читаем его заново."""
        current = self._lines_folder_item()
        if current is None or (item is not None and os.path.normcase(item.path) != os.path.normcase(current.path)):
            return
        if self._lines_tab_active():
            self._load_lines_view(current)
        else:
            self._lines_stale = True

    # список линий выбранной папки

    def _clear_lines_view(self) -> None:
        self._lines_gen += 1       # ответ про прежнюю папку уже не нужен
        if self._lines_cancel is not None:
            self._lines_cancel.cancel()      # и чтение списка для неё обрывается
            self._lines_cancel = None
        self._lines_view_loading = False
        self._lines_view_error = ""
        self._reset_diff()
        self._line_rows.clear()
        self.lines_tree.delete(*self.lines_tree.get_children())
        self._render_line_card()

    def _load_lines_view(self, item: MineItem) -> None:
        """Свежий список веток и версий проекта папки (в фоне): при выборе папки и по «⟳ Обновить»."""
        self._lines_stale = False
        self._clear_lines_view()
        gen = self._lines_gen
        client = self.client
        if client is None:
            self._lines_view_error = "Не найдена программа svn.exe (TortoiseSVN)."
            self._update_lines_buttons()
            return
        self._lines_view_loading = True
        self._update_lines_buttons()
        key, url = item.project_key, item.project_url
        cancel = self._lines_cancel = CancelToken()
        self.run_auth(lambda: client.list_lines(url, cancel), lambda res: self._on_lines_view_loaded(gen, key, res),
                      lambda kind: self._on_lines_view_failed(gen, kind))

    def _on_lines_view_loaded(self, gen: int, key: str, lines: dict) -> None:
        self._lines_cache[key] = lines
        if self.project and self.project.display == key:    # первая вкладка видит тот же свежий список
            self._fill_line_combo()
            self._set_line_info(self._info_text)
        if gen != self._lines_gen:
            return
        self._lines_view_loading = False
        self._fill_lines_rows(lines)

    def _on_lines_view_failed(self, gen: int, kind: ErrorKind) -> None:
        if gen != self._lines_gen:
            return
        self._lines_view_loading = False
        if kind in (ErrorKind.NO_CONNECTION, ErrorKind.TIMEOUT):
            self._lines_view_error = messages.PANEL_OFFLINE
        elif kind is ErrorKind.AUTH:
            self._lines_view_error = messages.PANEL_AUTH
        else:
            self._lines_view_error = "Список веток и версий получить не удалось: " + messages.short_message(kind)
        self._update_lines_buttons()

    def _fill_lines_rows(self, lines: dict) -> None:
        """Строки таблицы: Основная, потом ветки и версии, новые сверху."""
        entries: list[tuple[server.Line, Optional[object]]] = [(server.TRUNK_LINE, None)] if lines.get(
            "has_trunk", True) else []
        for kind, key in (("branch", "branches"), ("tag", "tags")):
            entries += [(server.Line(kind, e.name), e) for e in lines.get(key, [])]
        self.lines_tree.delete(*self.lines_tree.get_children())
        self._line_rows.clear()
        for line, entry in entries:
            self._line_rows[self.lines_tree.insert("", "end", values=("", "", "", "", "", "", ""))] = (line, entry)
        self._refresh_lines_marks()
        item = self._lines_folder_item()
        wanted, self._lines_pending_select = self._lines_pending_select or (item.line if item else None), None
        rows = list(self._line_rows)
        target = next((iid for iid, (line, _e) in self._line_rows.items() if line == wanted), rows[0] if rows else "")
        if target:
            self.lines_tree.selection_set(target)
            self.lines_tree.focus(target)
            self.lines_tree.see(target)
        self._on_lines_select()
        self.root.after_idle(self._request_line_histories)

    @staticmethod
    def _line_name(line: server.Line) -> str:
        """«Основная», «Ветка «x»», «🔒 Версия «y»»."""
        label = messages.line_label(line)
        return "🔒 " + label if line.kind == "tag" else label

    def _line_cells(self, item: MineItem, line: server.Line, entry) -> tuple[str, str, str, str]:
        """«Создал», «Создана», «Последнее изменение», «Описание»; «…» — сведения ещё загружаются, «—» — их нет."""
        history = self._line_history.get(item.line_url_of(line))      # BranchHistory, False (не вышло) или None
        wait = "…" if history is None else "—"
        if line.kind == "trunk":
            by, created = "—", "—"
        elif history:
            by, created = messages.created_cells(history)
        else:
            by, created = wait, wait
        if entry is not None and entry.last_date:
            last = ", ".join(p for p in (entry.last_author, f"{entry.last_date.astimezone():%d.%m}") if p)
        else:
            last = messages.last_change_cell(history) if history else wait
        return by, created, last, ("—" if line.kind == "trunk" else messages.description_cell(history))

    def _refresh_lines_marks(self) -> None:
        """Названия, значки и сведения в строках: «● вы здесь» там, где сейчас моя папка."""
        item = self._lines_folder_item()
        if item is None:
            return
        for index, (iid, (line, entry)) in enumerate(self._line_rows.items()):
            here = line == item.line
            tags = [t for t in ("frozen" if line.kind == "tag" and not here else "", "ok" if here else "",
                                "current" if here else "", "odd" if index % 2 else "") if t]
            self.lines_tree.item(iid, values=(self._line_name(line), *self._line_cells(item, line, entry),
                                              "✔" if here else "", self._server_dir(item) or "—"),
                                  tags=tuple(tags))
        self._autofit_columns(self.lines_tree)
        self._render_line_card()
        self._update_lines_buttons()

    # сведения о линиях: по требованию и с ограничением числа одновременных запросов

    def _lines_scrolled(self) -> None:
        if not self._lines_scroll_pending:
            self._lines_scroll_pending = True
            self.root.after_idle(self._request_line_histories)

    def _request_line_histories(self) -> None:
        """Сведения о создании линий нужны только для видимых строк; запросы идут через общий пул потоков."""
        self._lines_scroll_pending = False
        item = self._lines_folder_item()
        if item is None or self._closing or not self.client:
            return
        rows = list(self._line_rows.items())
        visible = [iid for iid, _row in rows if self.lines_tree.bbox(iid)] or [iid for iid, _row in rows[:LINES_PREFETCH]]
        for iid in visible:
            self._ensure_line_history(item, self._line_rows[iid][0])

    def _ensure_line_history(self, item: MineItem, line: server.Line) -> None:
        client, url = self.client, item.line_url_of(line)
        if client is None or self._closing or url in self._line_history or url in self._history_loading:
            return
        self._history_loading.add(url)

        def work() -> None:
            try:
                result = client.branch_history(url, limit=messages.HISTORY_LIMIT)
            except Exception as exc:
                log.warning("Сведения о линии %s не получены: %s", url, exc)
                result = None
            self.post(self._on_line_history, url, result)

        try:
            self._probe_pool.submit(work)
        except RuntimeError:            # окно закрывается: пул уже остановлен
            self._history_loading.discard(url)

    def _on_line_history(self, url: str, result) -> None:
        self._history_loading.discard(url)
        self._line_history[url] = result if result is not None else False
        self._refresh_lines_marks()
        for mine_item in self.mine.values():       # описание ветки видно и в «Мои проекты»
            if mine_item.state == "svn" and mine_item.line is not None and mine_item.line_url_of(mine_item.line) == url:
                self._refresh_row(mine_item)

    def _ensure_line_locks(self, item: MineItem, line: server.Line) -> None:
        """Занятые файлы показываем только для выбранной ветки и только когда её выбрали."""
        client, url = self.client, item.line_url_of(line)
        if client is None or line.kind != "branch" or url in self._line_locks or url in self._locks_loading:
            return
        self._locks_loading.add(url)

        def done(locks) -> None:
            self._locks_loading.discard(url)
            self._line_locks[url] = locks
            self._render_line_card()

        self.run_auth(lambda: client.locks_under(url), done, lambda _kind: done([]))

    def _selected_line(self) -> Optional[tuple[server.Line, Optional[object]]]:
        sel = self.lines_tree.selection()
        return self._line_rows.get(sel[0]) if sel else None

    def _on_lines_select(self) -> None:
        item, selected = self._lines_folder_item(), self._selected_line()
        if item is not None and selected is not None:
            self._ensure_line_history(item, selected[0])
            self._ensure_line_locks(item, selected[0])
            if self._diff_url != item.line_url_of(selected[0]):
                self._reset_diff()          # отличия показаны для другой ветки
        else:
            self._reset_diff()
        self._render_line_card()
        self._update_lines_buttons()

    def _render_line_card(self) -> None:
        """Карточка выбранной линии: название и строки «Создал… / Работали… / Заняты файлы…»."""
        item, selected = self._lines_folder_item(), self._selected_line()
        rows: list[str] = []
        if item is None or selected is None:
            title = ""
        else:
            line = selected[0]
            title = self._line_name(line) + ("  ● вы здесь" if line == item.line else "")
            url = item.line_url_of(line)
            history = self._line_history.get(url)
            if history:
                rows += messages.history_lines(history, self._line_locks.get(url), show_created=line.kind != "trunk")
            elif history is False:
                rows.append("Сведения получить не удалось. Нажмите «⟳», чтобы повторить.")
            else:
                rows.append("Загружаю сведения…")
            if line.read_only:
                rows.append("Только для просмотра.")
        self.line_title.configure(text=title)
        self._show(self.line_title, bool(title))
        for index, label in enumerate(self.line_card_rows):
            if index < len(rows):
                label.configure(text=rows[index])
            self._show(label, index < len(rows))

    def _lines_hint_text(self, item: MineItem, line: server.Line) -> str:
        unfinished = bool(item.status and item.status.switched)
        if line == item.line:
            if unfinished:
                return "Переключение не закончено. Нажмите «Перейти», чтобы довести его до конца."
            return f"Папка «{item.name}» уже здесь. Чтобы перейти на другую ветку, выберите её в списке."
        what = {"trunk": "Основную", "branch": "выбранную ветку", "tag": "выбранную версию (только для просмотра)"}
        return (f"Переключение заменит файлы в папке «{item.name}» на {what[line.kind]}. "
                "Несохранённые правки сначала сохраните.")

    def _update_lines_buttons(self) -> None:
        if not hasattr(self, "switch_here_btn"):
            return
        item, selected = self._lines_folder_item(), self._selected_line()
        busy = self.upd is not None or self.job is not None
        ready = bool(self.client) and not busy
        free = item is not None and not self._path_busy(item.path)
        can_change = ready and free and not self._locking
        read_only = bool(item and item.line is not None and item.line.read_only)
        unfinished = bool(item and item.status and item.status.switched)
        here = bool(item and selected and selected[0] == item.line)
        on = lambda flag: "normal" if flag else "disabled"  # noqa: E731
        self.switch_here_btn.configure(state=on(can_change and selected is not None and (not here or unfinished)))
        self.download_other_btn.configure(state=on(selected is not None and not busy))
        self.new_branch_btn.configure(state=on(can_change))
        self.new_tag_btn.configure(state=on(can_change and not read_only))
        self.merge_btn.configure(state=on(bool(self.tortoise_exe) and ready and free and not read_only))
        self.diff_btn.configure(state=on(self._diff_available(selected) and not self._diff_loading))
        text = self._lines_hint_text(item, selected[0]) if item is not None and selected is not None else ""
        self.lines_hint.configure(text=text)
        self._show(self.lines_hint, bool(text))
        self._update_lines_message()

    # действия вкладки

    def _on_switch_here(self) -> None:
        """«Перейти»: моя папка начинает показывать выбранную линию."""
        item, selected = self._line_item(), self._selected_line()
        if item is None or selected is None:
            return
        target = selected[0]
        if target == item.line and not (item.status and item.status.switched):
            return
        self._prepare_line_op(item, lambda _lines, status: self._begin_switch(item, target, status))

    def _on_download_separately(self) -> None:
        """«Скачать отдельно»: подвкладка «Скачать проект» со ссылкой на проект и выбранной линией в «Что скачать»."""
        item, selected = self._lines_folder_item(), self._selected_line()
        if item is None or selected is None or self.job or self.upd:
            return
        line = selected[0]
        project = server.ProjectRef(item.project_prefix, tuple(item.project_segments), line)
        text = self._display_line_url(project, line)
        self._tree_pick = (text, project)       # проект известен точно: ссылку заново разбирать не нужно
        self.select_list_mode("download")
        self.url_var.set(text)

    # «Отличия» выбранной ветки от Основной

    def _diff_available(self, selected) -> bool:
        """Сравнивать есть с чем: выбрана не Основная, а Основная в проекте есть."""
        return (selected is not None and selected[0].kind != "trunk"
                and any(line == server.TRUNK_LINE for line, _e in self._line_rows.values()))

    def _reset_diff(self) -> None:
        """Прячет отличия и обрывает чтение: выбрана другая ветка, другой проект или список перечитан."""
        self._diff_gen += 1
        if self._diff_cancel is not None:
            self._diff_cancel.cancel()
            self._diff_cancel = None
        self._diff_url = None
        self._diff_loading = False
        if hasattr(self, "diff_tree"):
            self.diff_tree.delete(*self.diff_tree.get_children())
            self._show_diff_message("")

    def _show_diff_message(self, text: str, rows: bool = False) -> None:
        self.diff_label.configure(text=text)
        self._show(self.diff_label, bool(text))
        self._show(self.diff_frame, rows)
        self._show(self.diff_box, bool(text) or rows)

    def _on_show_diff(self) -> None:
        """«Отличия»: в фоне читает, чем выбранная ветка отличается от Основной, и показывает список файлов."""
        item, selected = self._lines_folder_item(), self._selected_line()
        if item is None or not self._diff_available(selected) or self._diff_loading:
            return
        self._reset_diff()
        client = self.client
        if client is None or self._offline:
            self._show_diff_message(messages.DIFF_OFFLINE)       # без связи не пробуем и окон не показываем
            return
        gen = self._diff_gen
        url_a, url_b = item.line_url_of(server.TRUNK_LINE), item.line_url_of(selected[0])
        self._diff_url = url_b
        self._diff_loading = True
        cancel = self._diff_cancel = CancelToken()
        self._show_diff_message(messages.DIFF_LOADING)
        self._update_lines_buttons()
        self.run_auth(lambda: client.diff_summary(url_a, url_b, cancel),
                      lambda res: self._on_diff_loaded(gen, res),
                      lambda kind: self._on_diff_failed(gen, kind))

    def _on_diff_loaded(self, gen: int, changes: list) -> None:
        if gen != self._diff_gen:
            return
        self._diff_loading = False
        self.diff_tree.delete(*self.diff_tree.get_children())
        for index, (path, kind) in enumerate(sorted(changes, key=lambda c: c[0].lower())):
            self.diff_tree.insert("", "end", values=(messages.DIFF_KIND_TEXT.get(kind, "изменён"), path),
                                  tags=("odd",) if index % 2 else ())
        count = len(changes)
        self._show_diff_message(f"Отличия от Основной: {self._files_text(count)}." if count else messages.DIFF_EMPTY,
                                rows=bool(count))
        self._update_lines_buttons()

    def _on_diff_failed(self, gen: int, kind: ErrorKind) -> None:
        if gen != self._diff_gen:
            return
        self._diff_loading = False
        if kind in (ErrorKind.NO_CONNECTION, ErrorKind.TIMEOUT):
            text = messages.DIFF_OFFLINE
        elif kind is ErrorKind.AUTH:
            text = messages.DIFF_AUTH
        else:
            text = "Отличия получить не удалось: " + messages.short_message(kind)
        self._show_diff_message(text)
        self._update_lines_buttons()
