"""Панель «Изменения»: список своих правок с галочками и комментарием. Вызова svn здесь нет."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, Optional

from .. import theme
from ..config import DEFAULT_COMMIT_TEMPLATES
from ..svn_core import LocalChange
from .widgets import SideButtons

CHECKED, UNCHECKED = "☑", "☐"
LOCK = "🔒"

KIND_TEXT = {
    "modified": "изменён",
    "added": "новый",
    "unversioned": "новый",
    "deleted": "удалён",
    "missing": "удалён",
    "conflicted": "конфликт: сначала разберите конфликт",
}
KIND_TAG = {"modified": "plain", "added": "ok", "unversioned": "ok", "deleted": "new", "missing": "new",
            "conflicted": "bad"}

EMPTY_TEXT = "Правок нет. Измените файлы в папке проекта, и они появятся здесь."
NEED_COMMENT = "Напишите, что вы сделали"
NEED_FILE = "Отметьте хотя бы один файл"
NEED_LOCK_TEXT = "нужно занять"


class CommitPanel(ttk.Frame):
    """Список правок с галочками + комментарий.

    on_save(paths, comment) вызывается только после проверок (комментарий не пустой, отмечен хотя бы один файл).
    set_changes(list[LocalChange]) — показать правки (все отмечены, кроме конфликтных).
    set_enabled(False, reason) — заблокировать сохранение и показать причину.
    actions — колонка кнопок вкладки (SideButtons): «Сохранить отмеченное» стоит в ней и видна, пока есть правки;
    без неё кнопка стоит под полем комментария.
    """

    RECENT_LIMIT = 5

    def __init__(self, parent: tk.Misc, templates: Optional[Iterable[str]] = None,
                 recent_comments: Optional[Iterable[str]] = None,
                 on_save: Optional[Callable[[list[str], str], None]] = None, actions: Optional[SideButtons] = None):
        super().__init__(parent)
        self._actions = actions
        self.on_save: Optional[Callable[[list[str], str], None]] = on_save
        self._marks: dict[str, bool] = {}      # путь -> отмечен
        self._conflicts: set[str] = set()
        self._enabled = True
        self._comment = tk.StringVar()
        self._comment.trace_add("write", lambda *_: self._clear_error())

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._empty = ttk.Label(self, text=EMPTY_TEXT, style="Hint.TLabel", wraplength=360, justify="left")
        self._empty.grid(row=0, column=0, sticky="nw", padx=8, pady=8)

        self._body = ttk.Frame(self)
        self._body.grid(row=0, column=0, sticky="nsew")
        self._body.columnconfigure(0, weight=1)
        self._body.rowconfigure(0, weight=1)

        table = ttk.Frame(self._body)
        table.grid(row=0, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("mark", "file", "kind", "lock"), show="headings",
                                 selectmode="browse", height=8)
        for column, title, width, stretch in (("mark", "", 36, False), ("file", "Файл", 240, True),
                                              ("kind", "Что с ним", 150, False), ("lock", "Замок", 80, False)):
            self.tree.heading(column, text=title, anchor="w")
            self.tree.column(column, width=width, minwidth=width if not stretch else 80, stretch=stretch,
                             anchor="center" if column == "mark" else "w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        theme.configure_tree_tags(self.tree)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)

        ttk.Label(self._body, text="Что вы сделали?", style="Section.TLabel").grid(
            row=1, column=0, sticky="w", pady=(10, 2))
        self.entry = ttk.Entry(self._body, textvariable=self._comment)
        self.entry.grid(row=2, column=0, sticky="ew")
        self._error = ttk.Label(self._body, text="", style="Error.TLabel", wraplength=360, justify="left")
        self._error.grid(row=3, column=0, sticky="w", pady=(2, 0))

        self._templates_row = ttk.Frame(self._body)
        self._templates_row.grid(row=4, column=0, sticky="w", pady=(4, 0))
        self._recent = ttk.Combobox(self._body, state="readonly")
        self._recent.bind("<<ComboboxSelected>>", self._on_recent)
        self._recent_row = 5

        self._reason = ttk.Label(self._body, text="", style="Hint.TLabel", wraplength=360, justify="left")
        self._reason.grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.save_button = ttk.Button(actions if actions is not None else self._body, text="Сохранить отмеченное",
                                      style="Accent.TButton", command=self._save)
        if actions is not None:
            actions.add(self.save_button, visible=False)
        else:
            self.save_button.grid(row=7, column=0, sticky="w", pady=(8, 0))

        self.set_templates(list(DEFAULT_COMMIT_TEMPLATES) if templates is None else templates)
        self.set_recent_comments(recent_comments or [])
        self.set_changes([])

    # --- наполнение ---------------------------------------------------------

    def set_templates(self, templates: Iterable[str]) -> None:
        for child in self._templates_row.winfo_children():
            child.destroy()
        for text in list(templates)[:3]:
            ttk.Button(self._templates_row, text=text,
                       command=lambda t=text: self.set_comment(t)).pack(side="left", padx=(0, 6))

    def set_recent_comments(self, comments: Iterable[str]) -> None:
        values = [c for c in comments if c.strip()][:self.RECENT_LIMIT]
        self._recent.configure(values=values)
        self._recent.set("")
        if values:
            self._recent.grid(row=self._recent_row, column=0, sticky="ew", pady=(6, 0))
        else:
            self._recent.grid_remove()

    def set_changes(self, changes: list[LocalChange]) -> None:
        self.tree.delete(*self.tree.get_children())
        self._marks = {}
        self._conflicts = set()
        for index, change in enumerate(changes):
            conflict = change.kind == "conflicted"
            if conflict:
                self._conflicts.add(change.path)
            self._marks[change.path] = not conflict
            tags = [KIND_TAG.get(change.kind, "plain")]
            if index % 2:
                tags.append("odd")
            self.tree.insert("", "end", iid=change.path, tags=tags,
                             values=(CHECKED if not conflict else UNCHECKED, change.path,
                                     KIND_TEXT.get(change.kind, change.kind), self._lock_text(change)))
        has_changes = bool(changes)
        if has_changes:
            self._empty.grid_remove()
            self._body.grid()
        else:
            self._body.grid_remove()
            self._empty.grid()
        if self._actions is not None:
            self._actions.set_visible(self.save_button, has_changes)
        self._clear_error()

    @staticmethod
    def _lock_text(change: LocalChange) -> str:
        if change.locked_by_me:
            return LOCK
        return NEED_LOCK_TEXT if change.needs_lock and change.kind == "modified" else ""

    # --- состояние ------------------------------------------------------------

    def set_enabled(self, enabled: bool, reason: str = "") -> None:
        self._enabled = enabled
        self.save_button.configure(state="normal" if enabled else "disabled")
        self.entry.configure(state="normal" if enabled else "disabled")
        self._reason.configure(text="" if enabled else reason)

    def checked_paths(self) -> list[str]:
        return [path for path in self.tree.get_children() if self._marks.get(path)]

    def comment(self) -> str:
        return self._comment.get().strip()

    def set_comment(self, text: str) -> None:
        self._comment.set(text)
        self.entry.focus_set()
        self.entry.icursor("end")

    def error_text(self) -> str:
        return str(self._error.cget("text"))

    # --- действия ---------------------------------------------------------------

    def toggle(self, path: str) -> None:
        if not self._enabled or path in self._conflicts or path not in self._marks:
            return
        self._marks[path] = not self._marks[path]
        self.tree.set(path, "mark", CHECKED if self._marks[path] else UNCHECKED)
        self._clear_error()

    def _on_click(self, event: tk.Event) -> Optional[str]:
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) != "#1":
            return None
        row = self.tree.identify_row(event.y)
        if row:
            self.toggle(row)
        return "break"

    def _on_space(self, _event: tk.Event) -> str:
        for row in self.tree.selection():
            self.toggle(row)
        return "break"

    def _on_recent(self, _event: tk.Event) -> None:
        text = self._recent.get()
        if text:
            self.set_comment(text)

    def _clear_error(self) -> None:
        self._error.configure(text="")

    def _save(self) -> None:
        if not self._enabled:
            return
        if not self.comment():
            self._error.configure(text=NEED_COMMENT)
            return
        paths = self.checked_paths()
        if not paths:
            self._error.configure(text=NEED_FILE)
            return
        if self.on_save:
            self.on_save(paths, self.comment())
