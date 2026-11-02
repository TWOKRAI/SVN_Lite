"""Окно «Новый проект из папки»: просмотр папки, подтверждение, загрузка на сервер, откат при сбое.

Исходная папка только читается. Тексты без терминов SVN. Окно модальное (grab): пока оно открыто,
другие задачи в главном окне не запускаются.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from .. import importer, messages, server, theme
from ..dialogs import center_window, FONT_FAMILY
from ..svn_core import CancelToken, CheckoutProgress, ErrorKind, SvnError
from .models import JobStamp, log, PROGRESS_MS, shorten_path

ACTION = "Создание проекта из папки"


class CreateProjectDialog(tk.Toplevel):
    """result — (ProjectRef, скачать ли к себе) после успешного создания, иначе None.

    app — главное окно (run_bg, show_error, login, client, config); app.py модуль не импортирует.
    """

    def __init__(self, app, src: str, prefix: str, segments: tuple):
        super().__init__(app.root, background=theme.PALETTE.surface)
        self.app = app
        self.src = os.path.normpath(src.strip().strip('"'))
        self.prefix = prefix
        self.parent_segments = tuple(segments[:-1])
        self.result: Optional[tuple[server.ProjectRef, bool]] = None
        self.state = "scan"              # scan | ready | running | done
        self.scan: Optional[importer.ScanResult] = None
        self.patterns = importer.name_patterns(app.config.filter)
        self.cancel = CancelToken()
        self.progress = CheckoutProgress()
        self.client = app.client         # свой клиент на всё время окна: смена настроек его не подменит
        self.title("Новый проект из папки")
        self.transient(app.root)
        px = app.px

        body = ttk.Frame(self, padding=px(16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Создать проект на сервере из папки", font=(FONT_FAMILY, 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, px(10)))
        ttk.Label(body, text="Папка:").grid(row=1, column=0, sticky="nw")
        ttk.Label(body, text=self.src, wraplength=px(520), justify="left").grid(row=1, column=1, sticky="w")
        ttk.Label(body, text="Из папки только читаем: в ней ничего не изменится и не удалится.",
                  style="Hint.TLabel").grid(row=2, column=1, sticky="w", pady=(0, px(8)))

        self.summary_var = tk.StringVar(value="Просматриваю папку…")
        ttk.Label(body, textvariable=self.summary_var, wraplength=px(560), justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w")
        self.warn_label = ttk.Label(body, text="", style="Error.TLabel", wraplength=px(560), justify="left")
        self.warn_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        self.warn_label.grid_remove()

        place = ttk.Frame(body)
        place.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(px(12), 0))
        place.columnconfigure(1, weight=1)
        ttk.Label(place, text="Где на сервере:").grid(row=0, column=0, sticky="w")
        ttk.Label(place, text=shorten_path(self._parent_display(), 70), style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=(px(10), 0))
        ttk.Label(place, text="Название проекта:").grid(row=1, column=0, sticky="w", pady=(px(6), 0))
        self.name_var = tk.StringVar(value=segments[-1])
        self.name_entry = ttk.Entry(place, textvariable=self.name_var, width=50)
        self.name_entry.grid(row=1, column=1, sticky="ew", padx=(px(10), 0), pady=(px(6), 0))
        ttk.Label(place, text="Оставьте имя как у папки, тогда путь на диске и на сервере совпадут.",
                  style="Hint.TLabel").grid(row=2, column=1, sticky="w", padx=(px(10), 0))
        ttk.Label(place, text="Комментарий:").grid(row=3, column=0, sticky="w", pady=(px(6), 0))
        self.comment_var = tk.StringVar(value=f"Проект перенесён из папки {self.src}")
        self.comment_entry = ttk.Entry(place, textvariable=self.comment_var, width=50)
        self.comment_entry.grid(row=3, column=1, sticky="ew", padx=(px(10), 0), pady=(px(6), 0))

        self.bar = ttk.Progressbar(body, mode="indeterminate", maximum=100)
        self.bar.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(px(14), px(4)))
        self.status_var = tk.StringVar()
        ttk.Label(body, textvariable=self.status_var, wraplength=px(560), justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(px(14), 0))
        self.create_btn = ttk.Button(buttons, text="Создать проект", style="Accent.TButton",
                                     command=self._on_create, state="disabled")
        self.create_btn.pack(side="left", padx=(0, px(8)))
        self.close_btn = ttk.Button(buttons, text="Отмена", command=self._on_close)
        self.close_btn.pack(side="left")
        self.bind("<Escape>", lambda _e: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        center_window(self, parent=app.root)
        self.grab_set()
        self._start_scan()

    # -- адреса ------------------------------------------------------------------

    def _parent_display(self) -> str:
        return self.prefix + "/" + "/".join(self.parent_segments) + "/"

    def project_ref(self) -> server.ProjectRef:
        return server.ProjectRef(self.prefix, (*self.parent_segments, self.name_var.get().strip()))

    def project_url(self) -> str:
        return server.build_url(self.prefix, [*self.parent_segments, self.name_var.get().strip()])

    # -- просмотр папки --------------------------------------------------------------

    def _start_scan(self) -> None:
        self.bar.start(12)
        progress, cancel, src, patterns = self.progress, self.cancel, self.src, self.patterns
        self.app.run_bg(lambda: importer.scan_source(src, patterns, cancel, progress),
                        lambda res, err, _stamp: self._on_scanned(res, err))
        self.after(PROGRESS_MS, self._tick_scan)

    def _tick_scan(self) -> None:
        if self.state == "scan" and self.winfo_exists():
            self.summary_var.set(f"Просматриваю папку… {messages.format_number(self.progress.count)}")
            self.after(PROGRESS_MS, self._tick_scan)

    def _on_scanned(self, result, error) -> None:
        if not self.winfo_exists():
            return
        self.bar.stop()
        if error is not None:
            if isinstance(error, SvnError) and error.kind is ErrorKind.CANCELLED:
                return
            log.error("Не удалось просмотреть папку %s", self.src, exc_info=error)
            self.state = "ready"
            self.summary_var.set(f"Не удалось просмотреть папку: {error}")
            return
        self.scan, extra = result
        self.patterns = list(dict.fromkeys([*self.patterns, *extra]))
        self.state = "ready"
        scan = self.scan
        text = (f"Будет загружено: {messages.format_number(scan.files)} "
                f"{messages.plural(scan.files, ('файл', 'файла', 'файлов'))}, {messages.format_size(scan.size)}.")
        if scan.junk_files:
            text += (f" Лишнее не загружается: {messages.format_number(scan.junk_files)} "
                     f"{messages.plural(scan.junk_files, ('файл', 'файла', 'файлов'))}, "
                     f"{messages.format_size(scan.junk_size)}.")
        self.summary_var.set(text)
        warnings = scan.warnings()
        if warnings:
            self.warn_label.configure(text="\n".join("⚠ " + w for w in warnings))
            self.warn_label.grid()
        self.create_btn.configure(state="normal")

    # -- создание --------------------------------------------------------------------

    def _check_name(self) -> Optional[str]:
        """Текст ошибки имени или None."""
        try:
            server.validate_line_name(self.name_var.get())
        except ValueError as exc:
            return str(exc)
        if any(s in (server.TRUNK, server.BRANCHES, server.TAGS) for s in self.parent_segments):
            return "Эта папка лежит внутри другого проекта. Создать в ней новый проект нельзя."
        return None

    def _on_create(self) -> None:
        if self.state != "ready" or self.scan is None:
            return
        problem = self._check_name()
        if problem:
            messagebox.showerror("Название проекта", problem, parent=self)
            return
        name = self.name_var.get().strip()
        scan = self.scan
        text = (f"Создать «{name}» на сервере?\n\nБудет загружено {messages.format_size(scan.size)} "
                f"({messages.format_number(scan.files)} {messages.plural(scan.files, ('файл', 'файла', 'файлов'))}).\n"
                f"Место: {self._parent_display()}{name}\n\nИсходная папка не изменится.")
        if not messagebox.askyesno("Создать проект?", text, parent=self):
            return
        self._start_create()

    def _start_create(self) -> None:
        if not self.winfo_exists():
            return
        self.grab_set()   # окно входа или ошибки могло забрать захват себе
        self.state = "running"
        self.cancel = CancelToken()
        self.progress = CheckoutProgress()
        for widget in (self.name_entry, self.comment_entry, self.create_btn):
            widget.configure(state="disabled")
        self.close_btn.configure(text="Остановить", state="normal")
        total = max(1, (self.scan.entries if self.scan else 0) + 2)
        self.bar.configure(mode="determinate", maximum=total, value=0)
        self.status_var.set("Создаю проект на сервере…")
        client, url, src = self.client, self.project_url(), self.src
        message = self.comment_var.get().strip() or f"Проект перенесён из папки {src}"
        ignores = importer.svn_ignores(self.patterns)
        progress, cancel = self.progress, self.cancel
        self.app.run_bg(lambda: importer.create_project(client, url, src, message, ignores, progress, cancel),
                        lambda res, err, stamp: self._on_created(res, err, stamp))
        self.after(PROGRESS_MS, self._tick_create, total)

    def _tick_create(self, total: int) -> None:
        if self.state != "running" or not self.winfo_exists():
            return
        count = self.progress.count
        if count:
            self.bar.configure(value=min(count, total))
            self.status_var.set(f"Загружено {messages.format_number(min(count, total))} из "
                                f"{messages.format_number(total)}")
        self.after(PROGRESS_MS, self._tick_create, total)

    def _back_to_ready(self, status: str) -> None:
        self.state = "ready"
        self.bar.configure(value=0)
        for widget in (self.name_entry, self.comment_entry, self.create_btn):
            widget.configure(state="normal")
        self.close_btn.configure(text="Закрыть", state="normal")
        self.status_var.set(status)

    def _on_created(self, result: Optional[importer.CreateResult], error, stamp: JobStamp) -> None:
        if not self.winfo_exists():
            return
        if error is None:
            self._finish_success(result)
            return
        if not isinstance(error, importer.CreateError):
            log.error("Непредвиденная ошибка при создании проекта", exc_info=error)
            self._back_to_ready("Проект не создан.")
            self.app.show_error("Проект не создан", "Произошла непредвиденная ошибка. Проект не создан.",
                                action=ACTION, raw=str(error), extra={"Папка": self.src})
            return
        kind = error.kind
        # вход нужен до того, как на сервере что-то появилось: после ввода пароля начинаем заново
        if kind is ErrorKind.AUTH and error.stage in ("check", "mkdir") and not self.app._closing:
            self.status_var.set("Нужен вход на сервер…")
            self.app.login.request(retry=self._start_create, give_up=lambda: self._back_to_ready(
                "Проект не создан: нужен вход на сервер."), job_gen=stamp)
            return
        undo = messages.create_undo_text(error.rolled_back, error.rollback_error)
        if kind is ErrorKind.CANCELLED:
            self._back_to_ready("Создание остановлено. " + undo)
            if not self.app._closing:
                messagebox.showinfo("Создание остановлено", ("Создание проекта остановлено. " + undo).strip(),
                                    parent=self)
            return
        text = messages.short_message(kind)
        if kind is ErrorKind.EXISTS:
            text = "Такой проект уже есть на сервере. Выберите другое название."
        elif kind is ErrorKind.AUTHZ:
            text = ("Нет прав создавать проекты в этом месте сервера. Попросите администратора "
                    "или утверждающего создать проект.")
        self._back_to_ready("Проект не создан. " + undo)
        self.app.show_error("Проект не создан", (text + "\n\n" + undo).strip(), action=ACTION, kind=kind,
                            raw=error.error.raw, extra={"Папка": self.src, "Адрес": self.project_url(),
                                                        "Этап": error.stage})
        if self.winfo_exists():
            self.grab_set()

    def _finish_success(self, result: importer.CreateResult) -> None:
        self.state = "done"
        self.bar.configure(value=self.bar.cget("maximum"))
        self.status_var.set("Проект создан.")
        self.close_btn.configure(text="Закрыть")
        ref = self.project_ref()
        log.info("Проект создан из папки %s: %s, ревизия %s", self.src, ref.display, result.revision)
        number = f" (сохранение № {result.revision})" if result.revision else ""
        download = messagebox.askyesno(
            "Проект создан",
            f"Проект «{ref.name}» создан на сервере{number}.\n\nСкачать его к себе, чтобы работать с ним? "
            "Он будет скачан в папку для проектов на этом компьютере (не на сетевой диск).",
            parent=self)
        self.result = (ref, download)
        self.destroy()

    def _on_close(self) -> None:
        if self.state == "running":
            if messagebox.askyesno("Остановить создание?", "Остановить создание проекта? "
                                   "Уже загруженное будет удалено с сервера.", parent=self):
                self.close_btn.configure(state="disabled")
                self.status_var.set("Останавливаю…")
                self.cancel.cancel()
            return
        self.cancel.cancel()   # просмотр папки ещё идёт: останавливаем
        self.destroy()

    def stop_for_exit(self) -> None:
        """Главное окно закрывается: останавливаем без вопросов."""
        self.cancel.cancel()
