"""Колонка 3 «Состояние проекта»: шапка выбранного проекта и вкладки Изменения / Новое / История / Занятые / Ветки.

Здесь только виджеты: вызовов svn нет. Данные приходят от окна (ProjectsTabMixin), действия уходят обратно через
функции, переданные при создании. Содержимое вкладки «Ветки» строит LinesTabMixin прямо в `frames["lines"]`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Hashable

import tkinter as tk
from tkinter import ttk

from .. import localrepo, messages, theme
from ..svn_core import CancelToken, LockInfo, LogEntry
from . import hints
from .commit_panel import CommitPanel
from .widgets import button_column_width, button_padding, fit_text, SideButtons, Tooltip

# ключ вкладки -> подпись на ярлыке
PROJECT_TABS = (("changes", "Изменения"), ("incoming", "Новое"), ("history", "История"),
                ("locks", "Занятые"), ("lines", "Ветки"))

FILES_SHOWN = 3        # сколько имён файлов называем в строке, остальные - «и ещё N»
# вкладки, которых нет у проекта с историей на этом компьютере: ни сервера, ни занятых файлов, ни веток (Task T.6)
LOCAL_HIDDEN_TABS = ("incoming", "locks", "lines")


@dataclass
class LoadTicket:
    """Одна загрузка вкладки: номер проекта на момент старта и свой токен отмены svn."""
    tab: str
    marker: Hashable
    gen: int
    cancel: CancelToken = field(default_factory=CancelToken)


class TabLoads:
    """Какие вкладки проекта уже загружены и что грузится сейчас.

    reset() при смене проекта отменяет все незаконченные загрузки (убивает их svn) и делает их ответы устаревшими.
    marker - то, от чего зависит содержимое вкладки (папка, версия копии, версия на сервере): пока он тот же,
    вкладку заново не читаем.
    """

    def __init__(self) -> None:
        self.gen = 0
        self._loaded: dict[str, Hashable] = {}       # вкладка -> marker, с которым её уже читали (в том числе неудачно)
        self._running: dict[str, LoadTicket] = {}
        self._failed: set[str] = set()

    def reset(self) -> None:
        self.gen += 1
        for ticket in self._running.values():
            ticket.cancel.cancel()
        self._running.clear()
        self._loaded.clear()
        self._failed.clear()

    def invalidate(self, tab: str) -> None:
        """Вкладку надо прочитать заново (кнопка «⟳»)."""
        ticket = self._running.pop(tab, None)
        if ticket is not None:
            ticket.cancel.cancel()
        self._loaded.pop(tab, None)
        self._failed.discard(tab)

    def retry_failed(self) -> None:
        """Связь появилась: вкладки, которые не удалось прочитать, читаются заново (без этого сбой не повторяется)."""
        for tab in list(self._failed):
            self.invalidate(tab)

    def needed(self, tab: str, marker: Hashable) -> bool:
        running = self._running.get(tab)
        return self._loaded.get(tab) != marker and (running is None or running.marker != marker)

    def start(self, tab: str, marker: Hashable) -> LoadTicket:
        self.invalidate(tab)
        ticket = self._running[tab] = LoadTicket(tab, marker, self.gen)
        return ticket

    def current(self, ticket: LoadTicket) -> bool:
        """Ответ ещё нужен: проект не менялся и вкладку не начали читать заново."""
        return ticket.gen == self.gen and self._running.get(ticket.tab) is ticket

    def finish(self, ticket: LoadTicket, ok: bool) -> bool:
        """Отмечает конец загрузки. False, если ответ устарел и показывать его не нужно.
        Неудачное чтение тоже запоминается, иначе оно повторялось бы при каждой проверке проекта."""
        if not self.current(ticket):
            return False
        del self._running[ticket.tab]
        self._loaded[ticket.tab] = ticket.marker
        if ok:
            self._failed.discard(ticket.tab)
        else:
            self._failed.add(ticket.tab)
        return True


def when_text(when) -> str:
    return f"{when.astimezone():%d.%m.%Y %H:%M}" if when else "—"


def files_text(entry: LogEntry, names: bool) -> str:
    """«3 файла» или имена файлов («a.txt, b.txt и ещё 2»)."""
    paths = [p.path for p in entry.paths]
    if not names:
        return messages.files_count(len(paths)) if paths else "—"
    base = [os.path.basename(p.rstrip("/")) or p for p in paths]
    text = ", ".join(base[:FILES_SHOWN])
    if len(base) > FILES_SHOWN:
        text += f" и ещё {len(base) - FILES_SHOWN}"
    return text or "—"


def log_row(entry: LogEntry, names: bool) -> tuple[str, str, str, str]:
    """Строка таблицы: кто, когда, комментарий (в одну строку), файлы."""
    comment = " ".join(entry.message.split()) or "—"
    return entry.author or "—", when_text(entry.date), comment, files_text(entry, names)


class LogView(ttk.Frame):
    """Список записей журнала (Новое, История) с одной строкой сообщения над таблицей.

    set_message(text) - «Загружаю…», «Нет связи», «Нового нет» (пустая строка прячет сообщение);
    set_entries(entries) - показать записи; clear() - убрать всё.
    """

    def __init__(self, parent: tk.Misc, px: Callable[[int], int], *, names: bool, note: str = "",
                 hint: str = ""):
        super().__init__(parent)
        self.names = names
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.note = ttk.Label(self, text=note, style="Hint.TLabel", justify="left")
        self.note.grid(row=0, column=0, sticky="w")
        if not note:
            self.note.grid_remove()
        self.message = ttk.Label(self, text="", justify="left")
        self.message.grid(row=1, column=0, sticky="w", pady=(px(4), px(4)))
        frame = ttk.Frame(self, style="Card.TFrame", padding=1)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=("who", "when", "what", "files"), show="headings",
                                 selectmode="browse", height=6)
        for column, title, width, stretch in (("who", "Кто", 80, False), ("when", "Когда", 110, False),
                                              ("what", "Что сделали", 200, True),
                                              ("files", "Файлы" if names else "Сколько файлов", 120, False)):
            self.tree.heading(column, text=title, anchor="w")
            self.tree.column(column, width=px(width), minwidth=px(50), anchor="w", stretch=stretch)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        theme.configure_tree_tags(self.tree)
        if hint:
            hints.hint(self.tree, hint)
        self._table = frame
        self._show_table(False)

    def _show_table(self, visible: bool) -> None:
        if visible:
            self._table.grid()
        else:
            self._table.grid_remove()

    def set_message(self, text: str) -> None:
        self.message.configure(text=text)
        if text:
            self.message.grid()
        else:
            self.message.grid_remove()

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._show_table(False)

    def set_entries(self, entries: list[LogEntry]) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, entry in enumerate(entries):
            self.tree.insert("", "end", values=log_row(entry, self.names), tags=("odd",) if index % 2 else ())
        self._show_table(bool(entries))

    def rows(self) -> list[tuple]:
        return [self.tree.item(iid, "values") for iid in self.tree.get_children()]


class ProjectPanel(ttk.Frame):
    """Шапка (название, ● статус, «Ветка: …», что сделать, результат последнего действия) и вкладки проекта.

    Публичные виджеты: title_label, status_label, branch_label, todo_label, note_label; tabs (Notebook),
    frames[ключ вкладки], commit_panel, save_local_btn («Сохранить у себя» на «Изменениях»), incoming_view,
    history_view, locks_tree, locks_message, release_btn, lock_btn. Кнопки «Занятых» вызывают on_release(пути выбранных своих файлов; пустой список, если
    ничего не выбрано: тогда окно спросит, какие свои файлы освободить) и on_lock().

    Кнопки вкладок стоят не под таблицей, а вертикальной колонкой справа: у каждой вкладки своя колонка
    SideButtons (actions_for(ключ)), она лежит внутри вкладки (ярлыки вкладок занимают всю ширину панели, колонка
    не отнимает её у них); self.side - колонка выбранной вкладки. У вкладки без кнопок (Новое, История) колонки
    нет, и места она не занимает. Колонка лежит в панели, поэтому прячется и показывается вместе с ней при
    сворачивании «Состояния проекта». Колонка не шире SIDE_MAX_WIDTH: подпись длиннее (шаблон комментария)
    укорачивается с «…», полный текст виден во всплывающей подсказке.
    """

    SIDE_GAP = 6
    SIDE_MAX_WIDTH = 240

    TAG_COLOR = {"ok": "success", "new": "warning", "bad": "danger", "plain": "muted", "frozen": "frozen"}

    def __init__(self, parent: tk.Misc, px: Callable[[int], int], *, templates, recent_comments,
                 on_save: Callable[[list[str], str], None], on_release: Callable[[list[str]], None],
                 on_lock: Callable[[], None], on_save_local: Callable[[], None] = lambda: None):
        super().__init__(parent)
        self.px = px
        self._on_release, self._on_lock = on_release, on_lock
        self._local_view = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._no_side = ttk.Frame(self)      # «колонка» вкладки без кнопок (side): не показана

        # колонки кнопок: у каждой вкладки своя, лежит в самой вкладке справа и видна, только когда в ней есть кнопки
        # (sync_actions). Не в панели рядом с блокнотом: колонка отнимала бы ширину у ярлыков вкладок
        self._side_bars: dict[str, SideButtons] = {}

        # шапка
        head = ttk.Frame(self, style="Card.TFrame", padding=(px(14), px(8)))
        head.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        head.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(head, text="", style="Section.TLabel", justify="left")
        self.status_label = ttk.Label(head, text="", justify="left")
        self.branch_label = ttk.Label(head, text="", style="Muted.TLabel", justify="left")
        self.todo_label = ttk.Label(head, text="", style="Todo.TLabel", justify="left")
        self.note_label = ttk.Label(head, text="", style="Todo.TLabel", justify="left")
        self._head_labels = [self.title_label, self.status_label, self.branch_label, self.todo_label, self.note_label]
        for index, label in enumerate(self._head_labels):
            label.grid(row=index, column=0, sticky="w")
        head.bind("<Configure>", self._wrap_head)
        self.clear_header()

        # вкладки
        self.tabs = ttk.Notebook(self, style="Inner.TNotebook")
        self.tabs.grid(row=1, column=0, sticky="nsew")
        self.frames: dict[str, ttk.Frame] = {}
        for key, title in PROJECT_TABS:
            frame = ttk.Frame(self.tabs, padding=(px(12), px(10)))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            self.tabs.add(frame, text=title)
            self.frames[key] = frame

        self.tabs.bind("<<NotebookTabChanged>>", lambda _e: self.sync_actions())
        hints.hint_tabs(self.tabs, [f"tab.{key}" for key, _title in PROJECT_TABS])
        self.commit_panel = CommitPanel(self.frames["changes"], templates=templates, recent_comments=recent_comments,
                                        on_save=on_save, actions=self.actions_for("changes"))
        self.commit_panel.grid(row=0, column=0, sticky="nsew")
        # проект с историей на компьютере: вместо «Сохранить отмеченное» одна кнопка, сохраняет все правки
        self.save_local_btn = ttk.Button(self.actions_for("changes"), text=localrepo.SAVE_BUTTON, command=on_save_local)
        self.actions_for("changes").add(self.save_local_btn, visible=False, before=self.commit_panel.save_button,
                                        hint="changes.save_local")
        self.incoming_view = LogView(self.frames["incoming"], px, names=True, note=messages.INCOMING_NOTE,
                                     hint="incoming.table")
        self.incoming_view.grid(row=0, column=0, sticky="nsew")
        self.history_view = LogView(self.frames["history"], px, names=False, hint="history.table")
        self.history_view.grid(row=0, column=0, sticky="nsew")
        self._build_locks(self.frames["locks"])
        self.sync_actions()

    # -- колонка кнопок справа ------------------------------------------------------

    def actions_for(self, key: str) -> SideButtons:
        """Колонка кнопок вкладки key (создаётся при первом обращении); кнопки создают с ней как с родителем."""
        bar = self._side_bars.get(key)
        if bar is None:
            if key not in self.frames:
                raise ValueError(f"нет вкладки «{key}»")
            bar = self._side_bars[key] = SideButtons(self.frames[key], self.px(self.SIDE_GAP))
            bar.on_change = self.sync_actions
        return bar

    @property
    def side(self) -> tk.Misc:
        """Колонка кнопок выбранной вкладки (пока в ней нет кнопок, она не показана: winfo_manager() пуст)."""
        return self._side_bars.get(self.current_tab()) or self._no_side

    def sync_actions(self) -> None:
        """Колонки перестраиваются: у каждой вкладки колонка показана, когда в ней есть кнопки; ширина у всех одна
        (по самому длинному тексту, но не больше SIDE_MAX_WIDTH). Нет кнопок - нет и колонки."""
        px = self.px
        buttons = [b for bar in self._side_bars.values() for b in bar._items]
        width = min(button_column_width(self, [self._full_text(b) for b in buttons], px), px(self.SIDE_MAX_WIDTH))
        for button in buttons:
            self._fit_button(button, width - button_padding(px))
        for bar in self._side_bars.values():
            bar.set_width(width)
            if bar.has_visible():
                bar.grid(row=0, column=1, rowspan=2, sticky="n", padx=(px(12), 0))
            else:
                bar.grid_remove()

    @staticmethod
    def _full_text(button: tk.Misc) -> str:
        """Полная подпись кнопки: у кнопок с укорачиваемой подписью (шаблоны) она в атрибуте full_text."""
        return getattr(button, "full_text", None) or str(button.cget("text"))

    def _fit_button(self, button: tk.Misc, room: int) -> None:
        """Подпись кнопки с full_text укорачивается, чтобы войти в колонку; полный текст - в подсказке."""
        full = getattr(button, "full_text", None)
        if full is None:
            return
        shown = fit_text(self, full, room)
        if str(button.cget("text")) != shown:
            button.configure(text=shown)
        if not hasattr(button, "_full_tip"):
            button._full_tip = Tooltip(                    # type: ignore[attr-defined]
                button, lambda b=button: b.full_text if str(b.cget("text")) != b.full_text else "")

    def side_buttons(self) -> list[tk.Misc]:
        """Кнопки колонки справа сверху вниз (только показанные); пусто, если колонки нет."""
        current = self._side_bars.get(self.current_tab())
        if current is None or not current.winfo_manager():
            return []
        return [b for b in current._items if b not in current._hidden]

    # -- шапка ----------------------------------------------------------------------

    def _wrap_head(self, event: tk.Event) -> None:
        width = max(self.px(200), event.width - self.px(36))
        for label in self._head_labels:
            if str(label.cget("wraplength")) != str(width):
                label.configure(wraplength=width)

    def _set_line(self, label: ttk.Label, text: str) -> None:
        label.configure(text=text)
        if text:
            label.grid()
        else:
            label.grid_remove()

    def clear_header(self, hint: str = messages.PANEL_NO_PROJECT) -> None:
        """Проект не выбран: в шапке одна подсказка."""
        self._set_line(self.title_label, "")
        self.status_label.configure(style="Hint.TLabel")
        self._set_line(self.status_label, hint)
        for label in (self.branch_label, self.todo_label, self.note_label):
            self._set_line(label, "")

    def set_header(self, title: str, status: str, tag: str, branch: str, todo: str = "", note: str = "",
                   local: bool = False) -> None:
        """Название, «● состояние» цветом тега (ok|new|bad|plain|frozen), «Ветка: …», что сделать, результат действия.

        local: проект с историей на компьютере - вместо «Ветка: …» строка branch показывается как есть."""
        self._set_line(self.title_label, title)
        self.status_label.configure(style="TLabel", foreground=self._tag_color(tag))
        self._set_line(self.status_label, f"{theme.dot(tag)}{status}" if status else "")
        self._set_line(self.branch_label, (branch if local else f"Ветка: {branch}") if branch else "")
        self._set_line(self.todo_label, f"Что сделать: {todo}" if todo else "")
        self._set_line(self.note_label, note)

    def _tag_color(self, tag: str) -> str:
        return getattr(theme.PALETTE, self.TAG_COLOR.get(tag, "text"), theme.PALETTE.text)

    def header_text(self) -> str:
        """Весь видимый текст шапки (для проверок)."""
        return "\n".join(str(label.cget("text")) for label in self._head_labels if str(label.cget("text")))

    # -- вкладки --------------------------------------------------------------------

    def set_changes_count(self, count: int) -> None:
        """Счётчик правок на ярлыке «Изменения»."""
        self.tabs.tab(self.frames["changes"], text=f"Изменения {count}" if count else "Изменения")

    def set_incoming_count(self, count: int) -> None:
        """Счётчик нового на сервере на ярлыке «Новое»."""
        self.tabs.tab(self.frames["incoming"], text=f"Новое {count}" if count else "Новое")

    def set_local_view(self, local: bool) -> None:
        """Проект с историей на компьютере: вкладки «Новое», «Занятые», «Ветки» прячутся (у других проектов возвращаются)."""
        if local == self._local_view:
            return
        self._local_view = local
        self.commit_panel.set_save_hidden(local)
        for key in LOCAL_HIDDEN_TABS:
            if local:
                self.tabs.hide(self.frames[key])
            else:
                self.tabs.add(self.frames[key])      # скрытая вкладка возвращается на прежнее место
        self.sync_actions()

    def set_local_save(self, visible: bool, enabled: bool) -> None:
        """«Сохранить у себя» на «Изменениях»: видна у проекта с историей на компьютере, доступна, когда папку можно менять."""
        self._side_bars["changes"].set_visible(self.save_local_btn, visible)
        self.save_local_btn.configure(state="normal" if enabled else "disabled")

    def set_save_accent(self, accent: bool) -> None:
        """Подсвечена кнопка сохранения справа (сохранение - главное действие проекта), остальные обычные."""
        self.commit_panel.set_save_accent(accent)
        self.save_local_btn.configure(style="Accent.TButton" if accent else "TButton")

    def current_tab(self) -> str:
        """Ключ выбранной вкладки."""
        selected = self.tabs.select()
        return next((key for key, frame in self.frames.items() if str(frame) == selected), "")

    # -- «Занятые» --------------------------------------------------------------------

    def _build_locks(self, frame: ttk.Frame) -> None:
        px = self.px
        frame.rowconfigure(0, weight=0)
        frame.rowconfigure(1, weight=1)
        self.locks_message = ttk.Label(frame, text="", justify="left")
        self.locks_message.grid(row=0, column=0, sticky="w", pady=(0, px(4)))
        table = ttk.Frame(frame, style="Card.TFrame", padding=1)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.locks_tree = ttk.Treeview(table, columns=("file", "who", "days"), show="headings", selectmode="extended",
                                       height=6)
        for column, title, width, stretch in (("file", "Файл", 220, True), ("who", "Кто занял", 100, False),
                                              ("days", "Сколько", 110, False)):
            self.locks_tree.heading(column, text=title, anchor="w")
            self.locks_tree.column(column, width=px(width), minwidth=px(60), anchor="w", stretch=stretch)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.locks_tree.yview)
        self.locks_tree.configure(yscrollcommand=scroll.set)
        self.locks_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        theme.configure_tree_tags(self.locks_tree)
        self.locks_tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_lock_buttons())
        hints.hint(self.locks_tree, "locks.table")
        # «Занять файл…» открывает окно выбора; «Освободить…» берёт выбранные свои строки таблицы, а без выбора
        # открывает окно выбора своих файлов
        bar = self.actions_for("locks")
        self.lock_btn = ttk.Button(bar, text="Занять файл…", command=self._on_lock, state="disabled")
        self.release_btn = ttk.Button(bar, text="Освободить…", command=self._release_clicked, state="disabled")
        bar.add(self.lock_btn, hint="locks.take")
        bar.add(self.release_btn, hint="locks.release")
        self._own_locks: dict[str, str] = {}      # строка таблицы -> путь файла, занятого вами
        self._release_allowed = False

    def set_locks(self, base: str, mine: list[LockInfo], others: list[LockInfo]) -> None:
        """Занятые файлы: свои (их можно освободить) и чужие (кто и сколько)."""
        self.locks_tree.delete(*self.locks_tree.get_children())
        self._own_locks = {}
        for owned, locks in ((True, mine), (False, others)):
            for lock in locks:
                path = lock.path if os.path.isabs(lock.path) else os.path.join(base, lock.path)
                shown = os.path.relpath(path, base) if base and os.path.isabs(path) else lock.path
                age = lock.age_days
                iid = self.locks_tree.insert("", "end", tags=("ok" if owned else "new",), values=(
                    shown, messages.LOCKS_OWNER_ME if owned else lock.owner,
                    messages.format_age(age) if age is not None else "—"))
                if owned:
                    self._own_locks[iid] = path
        self.set_locks_message("" if (mine or others) else messages.LOCKS_NONE)
        self._sync_lock_buttons()

    def clear_locks(self, message: str) -> None:
        self.locks_tree.delete(*self.locks_tree.get_children())
        self._own_locks = {}
        self.set_locks_message(message)
        self._sync_lock_buttons()

    def set_locks_message(self, text: str) -> None:
        self.locks_message.configure(text=text)
        if text:
            self.locks_message.grid()
        else:
            self.locks_message.grid_remove()

    def set_locks_enabled(self, lock: bool, release: bool) -> None:
        """Кнопки «Занятых» доступны, только когда с проектом можно работать (есть svn, папка свободна, не версия)."""
        self._release_allowed = release
        self.lock_btn.configure(state="normal" if lock else "disabled")
        self._sync_lock_buttons()

    def selected_own_locks(self) -> list[str]:
        return [self._own_locks[iid] for iid in self.locks_tree.selection() if iid in self._own_locks]

    def _sync_lock_buttons(self) -> None:
        """«Освободить…» доступна, если ничего не выбрано или среди выбранного есть свои файлы; выбраны только
        чужие - освобождать нечего (чужое освободить нельзя)."""
        ok = self._release_allowed and (not self.locks_tree.selection() or bool(self.selected_own_locks()))
        # «…» обещает окно: оно открывается, только когда ничего не выбрано; выбранные строки освобождаются сразу
        text = "Освободить" if self.selected_own_locks() else "Освободить…"
        if str(self.release_btn.cget("text")) != text:
            self.release_btn.configure(text=text)
        self.release_btn.configure(state="normal" if ok else "disabled")

    def _release_clicked(self) -> None:
        self._on_release(self.selected_own_locks())
