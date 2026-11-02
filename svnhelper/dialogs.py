"""Общие диалоги (tkinter): окно ошибки, выбор при конфликте и «О программе»."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from . import messages, theme
from .server import validate_line_name

FONT_FAMILY = "Segoe UI"
NAME_TAKEN = "Такое имя уже есть — выберите другое"


def center_window(win: tk.Misc, width: Optional[int] = None, height: Optional[int] = None,
                  parent: Optional[tk.Misc] = None) -> None:
    win.update_idletasks()
    w = width or win.winfo_reqwidth()
    h = height or win.winfo_reqheight()
    if parent is not None:
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}" if width else f"+{max(x, 0)}+{max(y, 0)}")


class ErrorDialog(tk.Toplevel):
    """Ошибка: понятный текст, контакт администратора, кнопки «Скопировать отчёт» и «Открыть журнал».

    report — готовый текст для администратора (messages.admin_report, пароли уже вырезаны).
    """

    def __init__(self, parent: tk.Misc, title: str, text: str, report: str, support: str = "",
                 log_path: str = "", on_open_log: Optional[Callable[[], None]] = None):
        super().__init__(parent, background=theme.PALETTE.surface)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.report = report
        self._on_open_log = on_open_log
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body)
        head.pack(fill="x")
        tk.Label(head, text="⚠", font=(FONT_FAMILY, 26), foreground=theme.PALETTE.danger,
                 background=theme.PALETTE.surface).pack(side="left", padx=(0, 12), anchor="n")
        texts = ttk.Frame(head)
        texts.pack(side="left", fill="x", expand=True)
        ttk.Label(texts, text=text, wraplength=460, justify="left").pack(anchor="w")
        if support:
            ttk.Label(texts, text=f"Если не получается: {support}", wraplength=460, justify="left",
                      font=(FONT_FAMILY, 10, "bold")).pack(anchor="w", pady=(10, 0))
        if log_path:
            ttk.Label(texts, text=f"Подробности записаны в файл: {log_path}", style="Hint.TLabel",
                      wraplength=460, justify="left").pack(anchor="w", pady=(10, 0))
        self._hint = ttk.Label(body, text="Чтобы сообщить об ошибке, скопируйте отчёт и вставьте его в письмо "
                               "администратору.", style="Hint.TLabel", wraplength=520, justify="left")
        self._hint.pack(anchor="w", pady=(14, 6))
        # кнопки одной ширины, выровнены вправо; подпись кнопки не меняется, поэтому окно не «прыгает»
        buttons = ttk.Frame(body)
        buttons.pack(anchor="e")
        width = 36
        self.copy_btn = ttk.Button(buttons, text="Скопировать отчёт для администратора", width=width,
                                   command=self.copy_report)
        self.copy_btn.pack(anchor="e", pady=2)
        self.log_btn = ttk.Button(buttons, text="Открыть журнал", width=width, command=self._open_log)
        self.log_btn.pack(anchor="e", pady=2)
        ttk.Button(buttons, text="Закрыть", width=width, command=self.destroy).pack(anchor="e", pady=2)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self.destroy())
        center_window(self, parent=parent)
        self.grab_set()

    def copy_report(self) -> None:
        """Кладёт отчёт в буфер обмена, чтобы его можно было вставить в письмо или чат."""
        self.clipboard_clear()
        self.clipboard_append(self.report)
        self.update_idletasks()  # не крутим обработчики событий внутри обработчика кнопки
        self._hint.configure(text="Отчёт скопирован. Вставьте его в письмо администратору.")

    def _open_log(self) -> None:
        if self._on_open_log:
            self._on_open_log()


class ConflictDialog(tk.Toplevel):
    """Конфликт файла: какую версию оставить. result - "mine-full" (моя), "theirs-full" (с сервера) или None (отмена).

    Значения result - то, что принимает SvnClient.resolve(путь, accept).
    """

    def __init__(self, parent: tk.Misc, name: str, index: int = 1, total: int = 1):
        super().__init__(parent, background=theme.PALETTE.surface)
        self.title(messages.CONFLICT_TITLE)
        self.resizable(False, False)
        self.transient(parent)
        self.result: Optional[str] = None
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=messages.CONFLICT_FILE.format(name=name), wraplength=460, justify="left",
                  font=(FONT_FAMILY, 10, "bold")).pack(anchor="w")
        if total > 1:
            ttk.Label(body, text=messages.CONFLICT_COUNT.format(index=index, total=total), style="Hint.TLabel").pack(
                anchor="w", pady=(2, 0))
        for hint in (messages.CONFLICT_MINE_HINT, messages.CONFLICT_THEIRS_HINT):
            ttk.Label(body, text=hint, wraplength=460, justify="left").pack(anchor="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.pack(anchor="e", pady=(16, 0))
        self.mine_btn = ttk.Button(buttons, text="Оставить мою версию", command=lambda: self._choose("mine-full"))
        self.mine_btn.pack(side="left", padx=(0, 8))
        self.theirs_btn = ttk.Button(buttons, text="Взять версию с сервера",
                                     command=lambda: self._choose("theirs-full"))
        self.theirs_btn.pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")
        self.bind("<Escape>", lambda _e: self.destroy())
        center_window(self, parent=parent)
        self.grab_set()

    def _choose(self, accept: str) -> None:
        self.result = accept
        self.destroy()


class AboutDialog(tk.Toplevel):
    """«О программе»: версия, svn, контакт поддержки."""

    def __init__(self, parent: tk.Misc, version: str, details: list[str], support: str = "",
                 on_open_log: Optional[Callable[[], None]] = None):
        super().__init__(parent, background=theme.PALETTE.surface)
        self.title("О программе")
        self.resizable(False, False)
        self.transient(parent)
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="SVN-lite", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"Версия программы: {version}", style="Hint.TLabel").pack(anchor="w", pady=(0, 10))
        for line in details:
            ttk.Label(body, text=line, wraplength=420, justify="left").pack(anchor="w", pady=1)
        ttk.Label(body, text=f"Если что-то не получается: {support}" if support else
                  "Если что-то не получается, обратитесь к администратору.",
                  wraplength=420, justify="left").pack(anchor="w", pady=(10, 0))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))
        if on_open_log:
            ttk.Button(buttons, text="Открыть журнал", command=on_open_log).pack(side="left")
        ttk.Button(buttons, text="Закрыть", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())
        center_window(self, parent=parent)
        self.grab_set()


class NewLineDialog(tk.Toplevel):
    """Создание ветки или версии: название, комментарий и (для ветки) «Сразу переключиться».

    revision — номер сохранения, из которого делается копия; included — что в неё войдёт.
    check_name(имя, done) в фоне проверяет, что такого имени нет на сервере, и вызывает done(True/False);
    пока идёт проверка, окно показывает «Проверяю имя…», а при занятом имени остаётся открытым.
    can_switch=False (есть несохранённые правки): галочка «Сразу переключиться» выключена и неактивна.
    result — {"name", "comment", "switch"} или None, если окно закрыли без создания.
    """

    def __init__(self, parent: tk.Misc, kind: str, revision: Optional[int], included: str, default_name: str,
                 taken: list[str], check_name: Optional[Callable[[str, Callable[[bool], None]], None]] = None,
                 can_switch: bool = True):
        super().__init__(parent, background=theme.PALETTE.surface)
        self.kind = kind
        self.result: Optional[dict] = None
        self._taken = {t.casefold() for t in taken}
        self._check_name = check_name
        self._word = "Версия" if kind == "tag" else "Ветка"
        self.title("Зафиксировать версию" if kind == "tag" else "Создать ветку")
        self.resizable(False, False)
        self.transient(parent)
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        if kind == "tag":
            at = f" (сохранение № {revision})" if revision else ""
            intro = f"Версия — снимок проекта, как он сохранён на сервере{at}. Изменить его потом нельзя."
        else:
            intro = "Ветка — отдельная копия проекта для пробных изменений. Основная при этом не меняется."
        ttk.Label(body, text=intro, wraplength=460, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body, text=included, style="Hint.TLabel", wraplength=460, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))
        ttk.Label(body, text="Название:").grid(row=2, column=0, sticky="w", pady=3, padx=(0, 8))
        self._name_var = tk.StringVar(value=default_name)
        name_entry = ttk.Entry(body, textvariable=self._name_var, width=40)
        name_entry.grid(row=2, column=1, sticky="ew")
        ttk.Label(body, text="Комментарий (что в ней):").grid(row=3, column=0, sticky="nw", pady=3, padx=(0, 8))
        self._comment = tk.Text(body, width=40, height=4, wrap="word", font=(FONT_FAMILY, 10))
        self._comment.grid(row=3, column=1, sticky="ew")
        self._auto_comment = self._comment_for(default_name)
        self._comment.insert("1.0", self._auto_comment)
        self._switch = tk.BooleanVar(value=can_switch)
        row = 4
        self.switch_box: Optional[ttk.Checkbutton] = None
        if kind == "branch":
            self.switch_box = ttk.Checkbutton(body, text="Сразу переключиться", variable=self._switch,
                                              state="normal" if can_switch else "disabled")
            self.switch_box.grid(row=row, column=1, sticky="w", pady=(6, 0))
            row += 1
            if not can_switch:
                self._switch.set(False)
                ttk.Label(body, text="Сначала сохраните правки, потом можно переключиться",
                          style="Hint.TLabel", wraplength=460, justify="left").grid(row=row, column=1, sticky="w")
                row += 1
        self._error = ttk.Label(body, text="", style="Error.TLabel", wraplength=460, justify="left")
        self._error.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=row + 1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.ok_btn = ttk.Button(buttons, text="Создать", command=self._ok)
        self.ok_btn.pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")
        self._name_var.trace_add("write", lambda *_: self._sync_comment())
        self.bind("<Escape>", lambda _e: self.destroy())
        center_window(self, parent=parent)
        self.grab_set()
        name_entry.focus_set()

    def _comment_for(self, name: str) -> str:
        return f"{self._word} {name.strip()}".strip()

    def _sync_comment(self) -> None:
        """Пока комментарий не правили руками, он следует за названием."""
        if self._comment.get("1.0", "end").strip() != self._auto_comment.strip():
            return
        self._auto_comment = self._comment_for(self._name_var.get())
        self._comment.delete("1.0", "end")
        self._comment.insert("1.0", self._auto_comment)

    def _ok(self) -> None:
        try:
            name = validate_line_name(self._name_var.get())
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        if name.casefold() in self._taken:
            self._error.configure(text=NAME_TAKEN)
            return
        comment = self._comment.get("1.0", "end").strip()
        if not comment or comment == self._word:
            self._error.configure(text="Напишите комментарий: " + (
                "что за версия." if self.kind == "tag" else "для чего нужна ветка."))
            return
        if self._check_name is None:
            self._finish(name, comment)
            return
        self._error.configure(text="Проверяю имя…", style="Hint.TLabel")
        self.ok_btn.configure(state="disabled")
        self._check_name(name, lambda exists: self._checked(name, comment, exists))

    def _checked(self, name: str, comment: str, exists: bool) -> None:
        if not self.winfo_exists():     # окно закрыли, пока шла проверка
            return
        self.ok_btn.configure(state="normal")
        if exists:
            self._error.configure(text=NAME_TAKEN, style="Error.TLabel")
            return
        self._error.configure(style="Error.TLabel")
        self._finish(name, comment)

    def _finish(self, name: str, comment: str) -> None:
        self.result = {"name": name, "comment": comment, "switch": bool(self._switch.get())}
        self.destroy()
