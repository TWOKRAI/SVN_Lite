"""Окно «Журнал действий помощника»: лента «когда, кто, что, проект, файлы, сохранение №, итог» и кнопки отмены.

Показывает, что ИИ-помощник делал в ваших проектах (и смотрел, если выбрать «Все действия» или «Только чтение»).
Для своих сохранений помощника есть кнопки «Отменить это сохранение» и «Вернуть отмену»: окно показывает, какие файлы
изменятся, спрашивает «Да/Нет» и выполняет отмену новым сохранением, историю не стирая. Отмена идёт в фоне через
слой ops (`svnhelper.agent_journal`), все проверки (своё ли сохранение, не менял ли файлы кто-то после него) делает он,
окно только показывает причину отказа человеческими словами.

Вся логика ленты и отмены лежит в `svnhelper/agent_journal.py` и проверяется без экрана; здесь только рисунок.
Окно не модальное: открыто не больше одного (`App._open_agent_journal`). Пароля в ленте нет.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from .. import agent_journal as journal
from .. import theme
from ..dialogs import center_window, FONT_FAMILY
from .models import log

TITLE = "Журнал действий помощника"
INTRO = ("Здесь видно, что ИИ-помощник делал в ваших проектах. Свои сохранения, сделанные помощником, можно отменить: "
         "это новое сохранение, прежняя история остаётся. Отмена возможна, пока после сохранения никто не менял те же "
         "файлы.")
UNDO_BUTTON = "Отменить это сохранение"
REDO_BUTTON = "Вернуть отмену"
FOLDER_BUTTON = "Папка с файлами журнала"
REFRESH_BUTTON = "Обновить"
CLOSE_BUTTON = "Закрыть"
SHOW_LABEL, PERIOD_LABEL = "Показывать:", "За:"
SELECT_HINT = "Выберите строку, чтобы увидеть подробности."
BUSY_TEXT = "Выполняется отмена…"
NO_CLIENT_TEXT = "Нет связи с программой для работы с сервером: отмена сейчас невозможна."
UNEXPECTED_TEXT = "Отменить не удалось: неожиданная ошибка. Подробности в журнале программы."
FOLDER_TEXT = "Журнал действий помощника находится здесь:"
COLUMNS = (("when", "Когда", 120), ("who", "Кто", 150), ("what", "Что сделал", 190), ("project", "Проект", 120),
           ("files", "Файлы", 190), ("number", "Сохранение", 80), ("outcome", "Итог", 120), ("link", "Связь", 190))


def journal_folder():
    from ..config import data_dir
    return data_dir() / "agent"


class AgentJournalDialog(tk.Toplevel):
    """app - главное окно (root, px, settings_store, config, client, run_bg); app.py этот модуль не импортирует."""

    def __init__(self, app):
        super().__init__(app.root, background=theme.PALETTE.surface)
        self.app = app
        self.entries: list = []
        self.rows: dict = {}                    # iid строки таблицы -> journal.Row
        self.busy = False
        self.title(TITLE)
        self.transient(app.root)
        px = app.px
        self.minsize(px(640), px(380))

        body = ttk.Frame(self, padding=px(12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text=INTRO, wraplength=px(840), justify="left").grid(row=0, column=0, sticky="w",
                                                                              pady=(0, px(8)))

        bar = ttk.Frame(body)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, px(6)))
        ttk.Label(bar, text=SHOW_LABEL).pack(side="left")
        self.filter_var = tk.StringVar(value=dict(journal.FILTERS)[journal.DEFAULT_FILTER])
        self.filter_box = ttk.Combobox(bar, textvariable=self.filter_var, state="readonly", width=18,
                                       values=[label for _key, label in journal.FILTERS])
        self.filter_box.pack(side="left", padx=(px(6), px(14)))
        ttk.Label(bar, text=PERIOD_LABEL).pack(side="left")
        self.period_var = tk.StringVar(value=dict(journal.PERIODS)[journal.DEFAULT_MONTHS])
        self.period_box = ttk.Combobox(bar, textvariable=self.period_var, state="readonly", width=12,
                                       values=[label for _months, label in journal.PERIODS])
        self.period_box.pack(side="left", padx=(px(6), px(14)))
        self.refresh_btn = ttk.Button(bar, text=REFRESH_BUTTON, command=self.reload)
        self.refresh_btn.pack(side="left")
        for box in (self.filter_box, self.period_box):
            box.bind("<<ComboboxSelected>>", lambda _e: self.reload())

        table = ttk.Frame(body, style="Card.TFrame", padding=1)
        table.grid(row=2, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=[key for key, _t, _w in COLUMNS], show="headings",
                                 selectmode="browse", height=6)
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=px(width), minwidth=px(50), anchor="w", stretch=key in ("files", "link"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        theme.configure_tree_tags(self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        self.empty_var = tk.StringVar()
        ttk.Label(body, textvariable=self.empty_var, style="Hint.TLabel").grid(row=3, column=0, sticky="w",
                                                                                 pady=(px(4), 0))

        palette = theme.PALETTE
        self.detail = tk.Text(body, height=5, wrap="word", state="disabled", relief="flat", borderwidth=0,
                              font=(FONT_FAMILY, 10), background=palette.field, foreground=palette.text,
                              highlightthickness=1, highlightbackground=palette.border)
        self.detail.grid(row=4, column=0, sticky="ew", pady=(px(6), 0))

        self.status_var = tk.StringVar()
        ttk.Label(body, textvariable=self.status_var, wraplength=px(840), justify="left").grid(
            row=5, column=0, sticky="w", pady=(px(6), 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, sticky="ew", pady=(px(10), 0))
        self.undo_btn = ttk.Button(buttons, text=UNDO_BUTTON, state="disabled", command=lambda: self._start(False))
        self.undo_btn.pack(side="left")
        self.redo_btn = ttk.Button(buttons, text=REDO_BUTTON, state="disabled", command=lambda: self._start(True))
        self.redo_btn.pack(side="left", padx=(px(8), 0))
        self.close_btn = ttk.Button(buttons, text=CLOSE_BUTTON, command=self.destroy)
        self.close_btn.pack(side="right")
        self.folder_btn = ttk.Button(buttons, text=FOLDER_BUTTON, command=self._open_folder)
        self.folder_btn.pack(side="right", padx=(0, px(8)))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.reload()
        self.update_idletasks()
        height = min(max(px(560), self.winfo_reqheight()), self.winfo_screenheight() - px(80))
        center_window(self, min(px(980), self.winfo_screenwidth() - px(40)), height, parent=app.root)

    # -- лента ---------------------------------------------------------------------

    def _filter(self) -> str:
        labels = {label: key for key, label in journal.FILTERS}
        return labels.get(self.filter_var.get(), journal.DEFAULT_FILTER)

    def _months(self) -> int:
        labels = {label: months for months, label in journal.PERIODS}
        return labels.get(self.period_var.get(), journal.DEFAULT_MONTHS)

    def reload(self) -> None:
        """Читает файлы журнала заново и перерисовывает ленту. Выбранная строка (по месту в списке) не теряется."""
        previous = self._selected_row()
        self.entries = journal.load_entries(self._months())
        rows = journal.build_rows(self.entries, self._filter())
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for number, row in enumerate(rows):
            iid = f"r{row.index}"
            self.rows[iid] = row
            tags = ["bad"] if row.failed else ["plain"] if not row.is_change else []
            if row.link.startswith("отменено"):
                tags = ["frozen"]
            if number % 2 and not tags:
                tags.append("odd")
            self.tree.insert("", "end", iid=iid, tags=tags, values=(row.when, row.who, row.what, row.project, row.files,
                                                                    row.number, row.outcome, row.link))
        self.empty_var.set("" if rows else journal.empty_text(self._filter()))
        if previous is not None and f"r{previous.index}" in self.rows:
            self.tree.selection_set(f"r{previous.index}")
        self._on_select()

    def _selected_row(self) -> Optional[journal.Row]:
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def _on_select(self) -> None:
        row = self._selected_row()
        self._set_detail(journal.detail_text(row) if row else SELECT_HINT)
        self._update_buttons()

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _update_buttons(self) -> None:
        row = self._selected_row()
        free = not self.busy
        self.undo_btn.configure(state="normal" if free and row and row.can_undo else "disabled")
        self.redo_btn.configure(state="normal" if free and row and row.can_redo else "disabled")
        self.refresh_btn.configure(state="normal" if free else "disabled")

    # -- отмена --------------------------------------------------------------------

    def _context(self):
        app = self.app
        return journal.window_context(app.settings_store, app.config, app.client)

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        self.status_var.set(text)
        self._update_buttons()

    def _start(self, redo: bool) -> None:
        """Шаг 1: в фоне составить план отмены. Ничего не меняется, пока человек не ответит «Да»."""
        row = self._selected_row()
        if self.busy or row is None or not (row.can_redo if redo else row.can_undo):
            return
        if not self.app.client:
            messagebox.showwarning(TITLE, NO_CLIENT_TEXT, parent=self)
            return
        ctx = self._context()
        self._set_busy(True, BUSY_TEXT)
        self.app.run_bg(lambda: journal.plan_undo(ctx, row.entry), lambda res, err, _s: self._planned(row, ctx, res, err))

    def _planned(self, row: journal.Row, ctx, plan, error) -> None:
        if not self.winfo_exists():
            return
        if error is not None or plan is None:
            self._failed(error)
            return
        if not plan.ok:
            self._set_busy(False)
            messagebox.showwarning(TITLE, journal.refusal_text(plan), parent=self)
            self.reload()
            return
        self._set_busy(False)
        if not messagebox.askyesno(TITLE, journal.confirm_text(row, plan), parent=self, icon="question"):
            return
        self._set_busy(True, BUSY_TEXT)
        self.app.run_bg(lambda: journal.apply_undo(ctx, row.entry, plan),
                        lambda res, err, _s: self._applied(row, res, err))

    def _applied(self, row: journal.Row, result, error) -> None:
        if not self.winfo_exists():
            return
        if error is not None or result is None:
            self._failed(error)
            return
        self._set_busy(False)
        if result.ok:
            messagebox.showinfo(TITLE, journal.done_text(row, result), parent=self)
        else:
            messagebox.showwarning(TITLE, journal.refusal_text(result), parent=self)
        self.reload()

    def _failed(self, error: Optional[BaseException]) -> None:
        log.error("Отмена сохранения из журнала помощника не выполнена", exc_info=error)
        self._set_busy(False)
        messagebox.showerror(TITLE, UNEXPECTED_TEXT, parent=self)
        self.reload()

    # -- папка ---------------------------------------------------------------------

    def _open_folder(self) -> None:
        folder = journal_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            messagebox.showinfo(TITLE, f"{FOLDER_TEXT}\n{folder}", parent=self)
