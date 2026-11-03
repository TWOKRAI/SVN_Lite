"""Диалоги окна (вход, параметры, состав, выбор) и ряд кнопок с переносом."""
from __future__ import annotations

import os
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import field
from tkinter import messagebox, ttk
from typing import Callable, Optional, Sequence

from .. import theme
from ..config import (APP_UPDATE_DAYS_CHOICES, CHECK_MIN_CHOICES, clean_notify, Config, NOTIFY_DEFAULTS,
                      POPUP_SEC_CHOICES, REPEAT_HOURS_CHOICES, Settings, UNSAVED_HOURS_CHOICES)
from ..dialogs import center_window, FONT_FAMILY
from .models import REMEMBER_HINT


# --------------------------------------------------------------------------
# Окно входа и прочие диалоги
# --------------------------------------------------------------------------

class LoginDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, username: str, message: str, remember: bool = True):
        super().__init__(parent, background=theme.PALETTE.surface)
        self.title("Вход на сервер")
        self.resizable(False, False)
        self.transient(parent)
        self.result: Optional[tuple[str, str]] = None
        self.remember = remember          # итог флажка «Запомнить пароль»
        self._user = tk.StringVar(value=username)
        self._password = tk.StringVar()
        self._remember = tk.BooleanVar(value=remember)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, wraplength=380, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(body, text="Логин:").grid(row=1, column=0, sticky="w", pady=4)
        user_entry = ttk.Entry(body, textvariable=self._user, width=32)
        user_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(body, text="Пароль:").grid(row=2, column=0, sticky="w", pady=4)
        pass_entry = ttk.Entry(body, textvariable=self._password, show="•", width=32)
        pass_entry.grid(row=2, column=1, sticky="ew", padx=(8, 0))
        ttk.Checkbutton(body, text="Запомнить пароль", variable=self._remember).grid(
            row=3, column=1, sticky="w", pady=(6, 0))
        # у ttk.Checkbutton нет переноса строк: длинное пояснение вынесено в отдельную подпись
        ttk.Label(body, text=REMEMBER_HINT, style="Hint.TLabel", wraplength=380, justify="left").grid(
            row=4, column=1, sticky="w")
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Войти", command=self._ok).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Отмена", command=self._cancel).pack(side="left")

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        center_window(self, parent=parent)
        self.grab_set()
        (pass_entry if username else user_entry).focus_set()

    def _ok(self) -> None:
        user = self._user.get().strip()
        if not user:
            return
        self.result = (user, self._password.get())
        self.remember = bool(self._remember.get())
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class SettingsDialog(tk.Toplevel):
    """Окно «Параметры». result — словарь значений или None.

    Две вкладки: «Основное» (вход, общие и свои настройки) и «Уведомления и фон».
    """

    # выпадающие списки вкладки «Уведомления и фон»: ключ настройки -> (значение, подпись)
    _CHOICES = {
        "notify_check_min": CHECK_MIN_CHOICES, "notify_app_update_days": APP_UPDATE_DAYS_CHOICES,
        "notify_unsaved_hours": UNSAVED_HOURS_CHOICES, "notify_repeat_hours": REPEAT_HOURS_CHOICES,
        "notify_popup_sec": POPUP_SEC_CHOICES,
    }

    def __init__(self, parent: tk.Misc, config: Config, settings: Settings, has_password: bool,
                 check_cb: Optional[Callable[[dict, Callable[[str, bool], None]], None]] = None,
                 autostart: bool = False, notify_projects: Sequence[tuple[str, str]] = (),
                 test_cb: Optional[Callable[[], None]] = None,
                 popup_open: Optional[Callable[[], bool]] = None):
        """check_cb(значения окна, показать_итог(текст, успешно)) запускает «Проверить подключение» в фоне.

        autostart - включён ли сейчас запуск вместе с Windows (хранится в реестре, не в настройках).
        notify_projects - (путь, имя) скачанных проектов для «Не уведомлять об этом проекте».
        test_cb() показывает пробное окошко уведомления; popup_open() говорит, есть ли ещё окошки на экране
        (пока они есть, захват окна параметров снят, чтобы кнопки окошка нажимались).
        """
        super().__init__(parent, background=theme.PALETTE.surface)
        self.title("Параметры")
        self.resizable(False, False)
        self.transient(parent)
        self.result: Optional[dict] = None
        self._check_cb = check_cb
        self._test_cb = test_cb
        self._popup_open = popup_open
        self._settings = settings
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        body = ttk.Frame(tabs, padding=16)
        tabs.add(body, text="Основное")
        body.columnconfigure(1, weight=1)
        self._vars = {
            "config_path": tk.StringVar(value=settings.shared_source),
            "username": tk.StringVar(value=settings.username or config.username),
            "password": tk.StringVar(),
            "remember": tk.BooleanVar(
                value=True if settings.remember_password is None else settings.remember_password),
            "server": tk.StringVar(value=config.server),
            "repos": tk.StringVar(value=", ".join(config.repos)),
            "last_dir": tk.StringVar(value=settings.last_dir or config.work_dir),
            "svn_path": tk.StringVar(value=config.svn_path),
            "skip_junk": tk.BooleanVar(value=config.filter.skip_junk),
            "show_report": tk.BooleanVar(value=config.filter.show_report),
            "autostart": tk.BooleanVar(value=autostart),
            "tray_on_close": tk.BooleanVar(value=settings.tray_on_close),
        }
        for key in NOTIFY_DEFAULTS:
            value = getattr(settings, key)
            if key in self._CHOICES:
                self._vars[key] = tk.StringVar(value=self._choice_label(key, value))
            elif isinstance(value, bool):
                self._vars[key] = tk.BooleanVar(value=value)
            elif isinstance(value, str):
                self._vars[key] = tk.StringVar(value=value)
        self._muted_known = {_norm(p) for p, _name in notify_projects}
        muted = {_norm(p) for p in settings.notify_muted}
        self._project_vars = [(path, tk.BooleanVar(value=_norm(path) in muted), name)
                              for path, name in notify_projects]
        row = 0

        def section(title: str) -> None:
            nonlocal row
            ttk.Label(body, text=title, font=(FONT_FAMILY, 10, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(0 if row == 0 else 12, 2))
            row += 1

        def hint_label(text: str) -> None:
            nonlocal row
            ttk.Label(body, text=text, style="Hint.TLabel", wraplength=420, justify="left").grid(
                row=row, column=1, sticky="w")
            row += 1

        def field(label: str, key: str, show: str = "", hint: str = "") -> ttk.Entry:
            nonlocal row
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            entry = ttk.Entry(body, textvariable=self._vars[key], width=52, show=show)
            entry.grid(row=row, column=1, sticky="ew")
            row += 1
            if hint:
                hint_label(hint)
            return entry

        def check(label: str, key: str, hint: str = "") -> None:
            nonlocal row
            ttk.Checkbutton(body, text=label, variable=self._vars[key]).grid(
                row=row, column=1, sticky="w", pady=1)
            row += 1
            if hint:
                hint_label(hint)

        def choice(label: str, key: str, hint: str = "") -> None:
            nonlocal row
            ttk.Label(body, text=label, wraplength=250, justify="left").grid(
                row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            ttk.Combobox(body, textvariable=self._vars[key], state="readonly", width=22,
                         values=[text for _value, text in self._choices_of(key)]).grid(row=row, column=1, sticky="w")
            row += 1
            if hint:
                hint_label(hint)

        section("Вход на сервер")
        first = field("Логин:", "username",
                      hint="Можно ничего не вводить: окно входа появится само, когда сервер его потребует.")
        field("Пароль:", "password", show="•",
              hint="Пароль уже введён, пустое поле его не меняет." if has_password else "")
        check("Запомнить пароль", "remember")
        hint_label(REMEMBER_HINT)
        if check_cb:
            self._check_btn = ttk.Button(body, text="Проверить подключение", command=self._check)
            self._check_btn.grid(row=row, column=1, sticky="w", pady=(6, 0))
            row += 1
            self._check_label = ttk.Label(body, text="", wraplength=420, justify="left")
            self._check_label.grid(row=row, column=1, sticky="w", pady=(4, 0))
            row += 1
        section("Общие настройки")
        field("Общие настройки (svn_helper.json):", "config_path",
              hint="Путь на сервере (\\\\сервер\\папка\\svn_helper.json) или ссылка "
                   "svn://…/svn_helper.json. Читается при каждом запуске.")
        section("Свои значения (вместо общих)")
        field("Адрес сервера:", "server")
        field("Папки на сервере (через запятую):", "repos")
        field("Папка для проектов:", "last_dir")
        field("Путь к svn.exe:", "svn_path")
        check("Не оставлять мусор в папке проекта", "skip_junk")
        check("Показывать отчёт перед скачиванием", "show_report")

        # -- вкладка «Уведомления и фон»: те же помощники, но уже на другой странице --
        body = ttk.Frame(tabs, padding=16)
        tabs.add(body, text="Уведомления и фон")
        body.columnconfigure(1, weight=1)
        row = 0
        section("Работа в фоне")
        check("Запускать вместе с Windows", "autostart",
              "Программа запустится сама после входа в Windows и сразу спрячется в область "
              "уведомлений (рядом с часами).")
        check("Сворачивать в трей при закрытии окна", "tray_on_close",
              "Крестик прячет окно, а программа продолжает работать. Закрыть совсем можно "
              "через значок в области уведомлений (пункт «Выйти»).")
        section("Уведомления")
        check("Показывать уведомления", "notify_enabled",
              "Небольшое окошко в правом нижнем углу экрана. Пока вы работаете в самой программе, оно не появляется.")
        choice("Проверять новое на сервере раз в", "notify_check_min",
               "Как часто программа сама заглядывает на сервер. «Только вручную»: проверка по пункту "
               "«Проверить сейчас» в меню значка.")
        check("Сообщать о новом на сервере", "notify_incoming",
              "Одно уведомление на проект, пока число новых файлов не изменилось.")
        check("Проверять обновления программы", "notify_app_update")
        choice("Напоминать об обновлении", "notify_app_update_days",
               "Как часто напоминать, пока новая версия не установлена.")
        choice("Напоминать сохранить правки, если они не сохранены дольше", "notify_unsaved_hours",
               "О проектах с конфликтом и о версиях (только для чтения) не напоминаем.")
        choice("Напоминать повторно не чаще, чем раз в", "notify_repeat_hours")
        check("Не беспокоить по часам", "notify_quiet", "В это время уведомления не показываются.")
        ttk.Label(body, text="С (чч:мм):").grid(row=row, column=0, sticky="w", padx=(0, 10))
        times = ttk.Frame(body)
        times.grid(row=row, column=1, sticky="w")
        ttk.Entry(times, textvariable=self._vars["notify_quiet_from"], width=7).pack(side="left")
        ttk.Label(times, text="  до  ").pack(side="left")
        ttk.Entry(times, textvariable=self._vars["notify_quiet_to"], width=7).pack(side="left")
        row += 1
        check("И в субботу с воскресеньем", "notify_quiet_weekends")
        choice("Сколько секунд показывать окошко", "notify_popup_sec")
        check("Показывать только важное", "notify_important_only",
              "Только конфликты и несохранённые правки, без сообщений о новом и об обновлении программы.")
        if self._project_vars:
            section("Не уведомлять об этом проекте")
            holder = ttk.Frame(body)
            holder.grid(row=row, column=0, columnspan=2, sticky="w")
            row += 1
            for index, (_path, var, name) in enumerate(self._project_vars):
                ttk.Checkbutton(holder, text=name, variable=var).grid(
                    row=index // 2, column=index % 2, sticky="w", padx=(0, 16))
        extra = ttk.Frame(body)
        extra.grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))
        if test_cb:
            ttk.Button(extra, text="Проверить уведомления", command=self._show_test_notice).pack(
                side="left", padx=(0, 8))
        ttk.Button(extra, text="Вернуть настройки по умолчанию", command=self._reset_notify).pack(side="left")

        buttons = ttk.Frame(outer)
        buttons.pack(anchor="e", pady=(14, 0))
        ttk.Button(buttons, text="Сохранить", command=self._ok).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        center_window(self, parent=parent)
        self.grab_set()
        first.focus_set()

    # -- пробное окошко ------------------------------------------------------------

    POPUP_POLL_MS = 400

    def _show_test_notice(self) -> None:
        """Пробное окошко живёт отдельно от этого окна. Пока у параметров есть захват (grab_set), кнопки окошка
        не нажимаются, поэтому захват снимается до закрытия окошка и возвращается, когда оно исчезло."""
        if self._test_cb is None:
            return
        self.grab_release()
        self._test_cb()
        self.after(self.POPUP_POLL_MS, self._regrab_when_popup_closed)

    def _regrab_when_popup_closed(self) -> None:
        if not self.winfo_exists():
            return
        if self._popup_open is not None and self._popup_open():
            self.after(self.POPUP_POLL_MS, self._regrab_when_popup_closed)
            return
        self.grab_set()

    # -- варианты выпадающих списков ---------------------------------------------

    def _choices_of(self, key: str, extra: Optional[int] = None) -> list[tuple[int, str]]:
        """Варианты списка. Если в настройках иное число (файл правили вручную), оно тоже в списке."""
        choices = list(self._CHOICES[key])
        current = getattr(self._settings, key) if extra is None else extra
        if current not in [value for value, _text in choices]:
            choices.append((current, str(current)))
        return choices

    def _choice_label(self, key: str, value: int) -> str:
        return next((text for v, text in self._choices_of(key, value) if v == value), str(value))

    def _reset_notify(self) -> None:
        """«Вернуть настройки по умолчанию» для уведомлений (запишется после «Сохранить»)."""
        for key, default in NOTIFY_DEFAULTS.items():
            if key in self._CHOICES:
                self._vars[key].set(self._choice_label(key, default))
            elif key in self._vars:
                self._vars[key].set(default)
        for _path, var, _name in self._project_vars:
            var.set(False)

    def _values(self) -> dict:
        values = {k: (v.get().strip() if isinstance(v.get(), str) else v.get()) for k, v in self._vars.items()}
        for key in self._CHOICES:
            values[key] = next((v for v, text in self._choices_of(key) if text == values[key]),
                               NOTIFY_DEFAULTS[key])
        # выключенные проекты, которых сейчас нет в списке (папку убрали), остаются выключенными
        values["notify_muted"] = ([p for p in self._settings.notify_muted if _norm(p) not in self._muted_known]
                                  + [path for path, var, _name in self._project_vars if var.get()])
        return values

    def _check(self) -> None:
        """Проверка подключения со значениями, которые сейчас введены в окне (сохранять их не нужно)."""
        self._check_btn.configure(state="disabled")
        self._check_label.configure(text="Проверяю…", style="Hint.TLabel")
        self._check_cb(self._values(), self._show_check_result)

    def _show_check_result(self, text: str, ok: bool) -> None:
        if not self.winfo_exists():  # окно закрыли, пока шла проверка
            return
        self._check_btn.configure(state="normal")
        self._check_label.configure(text=text, style="Ok.TLabel" if ok else "Error.TLabel")

    def _ok(self) -> None:
        values = self._values()
        clean, problems = clean_notify({k: v for k, v in values.items() if k in NOTIFY_DEFAULTS})
        if problems:    # говорим, что заменили, и сохраняем уже допустимые значения
            messagebox.showinfo("Уведомления", "Некоторые значения исправлены:\n\n" + "\n".join(problems),
                                parent=self)
        values.update(clean)
        self.result = values
        self.destroy()


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


class AuditDialog(tk.Toplevel):
    """Отчёт о содержимом проекта. result=True, если пользователь решил скачивать."""

    def __init__(self, parent: tk.Misc, name: str, lines: list[str]):
        super().__init__(parent, background=theme.PALETTE.surface)
        self.title("Проверка проекта")
        self.transient(parent)
        self.result = False
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Перед скачиванием проекта «{name}» найдено:",
                  font=(FONT_FAMILY, 11, "bold")).pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, width=90, height=18, wrap="none", font=(FONT_FAMILY, 10))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        buttons = ttk.Frame(body)
        buttons.pack(anchor="e", pady=(12, 0))
        ttk.Button(buttons, text="Скачать", command=self._ok).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")
        self.bind("<Escape>", lambda _e: self.destroy())
        center_window(self, parent=parent)
        self.grab_set()

    def _ok(self) -> None:
        self.result = True
        self.destroy()


class PickDialog(tk.Toplevel):
    """Выбор нескольких пунктов из списка. result — список выбранных или None."""

    def __init__(self, parent: tk.Misc, title: str, prompt: str, items: list[str]):
        super().__init__(parent, background=theme.PALETTE.surface)
        self.title(title)
        self.transient(parent)
        self.result: Optional[list[str]] = None
        self._items = items
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=prompt, wraplength=520, justify="left").pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        self._box = tk.Listbox(frame, selectmode="extended", width=70, height=min(14, max(4, len(items))),
                               font=(FONT_FAMILY, 10), activestyle="none")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._box.yview)
        self._box.configure(yscrollcommand=scroll.set)
        self._box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for item in items:
            self._box.insert("end", item)
        self._box.select_set(0, "end")
        buttons = ttk.Frame(body)
        buttons.pack(anchor="e", pady=(12, 0))
        ttk.Button(buttons, text="Освободить", command=self._ok).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")
        self.bind("<Escape>", lambda _e: self.destroy())
        center_window(self, parent=parent)
        self.grab_set()

    def _ok(self) -> None:
        self.result = [self._items[i] for i in self._box.curselection()]
        self.destroy()


class ButtonRow(ttk.Frame):
    """Ряд кнопок: если они не помещаются по ширине окна, лишние переходят на вторую строку.

    Показывать и прятать кнопки нужно через set_visible (App._show делает это сам).
    """

    def __init__(self, parent: tk.Misc, gap: int, line_gap: int):
        super().__init__(parent)
        self._gap, self._line_gap = gap, line_gap
        self._items: list[tk.Misc] = []
        self._hidden: set = set()
        self._layout_key: Optional[tuple] = None
        self.bind("<Configure>", lambda _e: self.reflow())

    def add(self, widget: tk.Misc, visible: bool = True) -> None:
        self._items.append(widget)
        if not visible:
            self._hidden.add(widget)
        self.after_idle(self.reflow)

    def set_visible(self, widget: tk.Misc, visible: bool) -> None:
        if visible == (widget not in self._hidden):
            return
        self._hidden.discard(widget) if visible else self._hidden.add(widget)
        self.reflow()

    def reflow(self) -> None:
        shown = [w for w in self._items if w not in self._hidden]
        width = self.winfo_width()
        key = (width, tuple((str(w), w.winfo_reqwidth()) for w in shown))
        if key == self._layout_key:
            return
        self._layout_key = key
        for widget in self._items:
            if widget in self._hidden:
                widget.place_forget()
        lines: list[list[tk.Misc]] = [[]]
        x = 0
        for widget in shown:
            w = widget.winfo_reqwidth()
            if x and width > 1 and x + w > width:      # не помещается: переносим на следующую строку
                lines.append([])
                x = 0
            lines[-1].append(widget)
            x += w + self._gap
        y = 0
        for line in lines:
            line_height = max((w.winfo_reqheight() for w in line), default=0)
            x = 0
            for widget in line:
                widget.place(x=x, y=y + (line_height - widget.winfo_reqheight()) // 2)   # по центру строки
                x += widget.winfo_reqwidth() + self._gap
            y += line_height + self._line_gap
        y = max(0, y - self._line_gap)
        self.configure(height=y)                       # place не задаёт размер ряда сам


class SideButtons(ttk.Frame):
    """Колонка кнопок: кнопки друг под другом одной ширины. Те же add/set_visible, что у ButtonRow.

    Ширину колонки задаёт распорка (width в конструкторе или set_width): кнопки везде одного размера и не зависят
    от ширины окна. on_change() вызывается, когда набор показанных кнопок изменился (колонка с пустым набором
    не должна занимать место: это решает владелец колонки).
    """

    def __init__(self, parent: tk.Misc, gap: int, width: int = 0):
        super().__init__(parent)
        self._gap = gap
        self._items: list[tk.Misc] = []
        self._hidden: set = set()
        self.on_change: Optional[Callable[[], None]] = None
        # распорка задаёт одну ширину колонки на всех вкладках: кнопки везде одного размера
        self._spacer = tk.Frame(self, width=width, height=1)
        self._spacer.pack(side="top")

    def set_width(self, width: int) -> None:
        self._spacer.configure(width=width)

    def has_visible(self) -> bool:
        return any(widget not in self._hidden for widget in self._items)

    def add(self, widget: tk.Misc, visible: bool = True, separator: bool = False,
            before: Optional[tk.Misc] = None) -> None:
        """Добавляет кнопку в конец колонки, а с before - перед этой кнопкой (так обновляются шаблоны комментария)."""
        if before is not None and before in self._items:
            self._items.insert(self._items.index(before), widget)
        else:
            self._items.append(widget)
        widget._side_gap = self._gap * 3 if separator else self._gap      # type: ignore[attr-defined]
        if not visible:
            self._hidden.add(widget)
        self._repack()

    def remove(self, widget: tk.Misc) -> None:
        """Убирает кнопку из колонки и уничтожает её."""
        if widget in self._items:
            self._items.remove(widget)
        self._hidden.discard(widget)
        widget.destroy()
        self._repack()

    def set_visible(self, widget: tk.Misc, visible: bool) -> None:
        if visible == (widget not in self._hidden):
            return
        self._hidden.discard(widget) if visible else self._hidden.add(widget)
        self._repack()

    def _repack(self) -> None:
        for widget in self._items:
            widget.pack_forget()
        first = True
        for widget in self._items:
            if widget not in self._hidden:
                widget.pack(side="top", fill="x", pady=(0 if first else widget._side_gap, 0))   # type: ignore[attr-defined]
                first = False
        if self.on_change is not None:
            self.on_change()


BUTTON_FONT = (FONT_FAMILY, 10, "bold")


def button_padding(px: Callable[[int], int]) -> int:
    """Сколько ширины кнопка добавляет к тексту (отступы кнопок темы, масштаб экрана учтён)."""
    return 2 * px(14) + px(8)


def text_width(parent: tk.Misc, text: str) -> int:
    # «font measure» напрямую: объект tkfont.Font удаляется сборщиком мусора, а из чужого потока это роняет Tcl
    return int(parent.tk.call("font", "measure", BUTTON_FONT, text))


def button_column_width(parent: tk.Misc, texts: list[str], px: Callable[[int], int]) -> int:
    """Ширина колонки кнопок по самому длинному тексту (шрифт и отступы кнопок темы, масштаб экрана учтён)."""
    widest = max((text_width(parent, text) for text in texts), default=0)
    return widest + button_padding(px)


def fit_text(parent: tk.Misc, text: str, max_width: int) -> str:
    """Текст кнопки, укороченный с «…» так, чтобы он занимал не больше max_width пикселей (шрифт кнопок темы).

    Помещающийся текст возвращается как есть; полный текст вызывающий показывает во всплывающей подсказке."""
    if text_width(parent, text) <= max_width:
        return text
    low, high = 0, len(text)            # наибольшее число букв, при котором «начало…» ещё помещается
    while low < high:
        middle = (low + high + 1) // 2
        if text_width(parent, text[:middle].rstrip() + "…") <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def popup_menu_under(button: tk.Misc, menu: tk.Menu) -> None:
    """Открывает меню под кнопкой (для кнопок с «▾»)."""
    if str(button.cget("state")) == "disabled":
        return
    try:
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
    finally:
        menu.grab_release()


class CollapsibleColumn(ttk.Frame):
    """Колонка окна, которую можно свернуть в узкую полосу с вертикальной подписью.

    Развёрнутая: заголовок (название и стрелка) и self.body, куда вызывающий кладёт содержимое.
    Свёрнутая: полоса шириной strip_width (Canvas) со стрелкой и названием снизу вверх; щелчок по ней разворачивает.
    side — куда колонка сворачивается: "left" (стрелка «‹») или "right" (стрелка «›»).
    При каждой смене состояния генерируется событие <<ColumnToggled>> (состояние — свойство collapsed).
    Ширину развёрнутой колонки задаёт раскладка окна (grid с весами), сама колонка размер не навязывает:
    width — только запрошенная ширина, чтобы широкое содержимое (таблица) не растягивало окно.
    """

    EVENT = "<<ColumnToggled>>"

    def __init__(self, parent: tk.Misc, title: str, side: str = "left", *, width: int = 260,
                 strip_width: int = 28, collapsed: bool = False, collapsed_title: str = ""):
        super().__init__(parent)
        if side not in ("left", "right"):
            raise ValueError(f"side: left | right, получено {side!r}")
        self.title, self.side = title, side
        self.collapsed_title = collapsed_title or title
        self._width, self._strip_width = width, strip_width
        self._collapsed = collapsed
        # таблица в колонке не должна раздвигать окно: размер задаёт grid снаружи
        self.grid_propagate(False)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.header = ttk.Frame(self)
        self.header.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(self.header, text=title, style="Section.TLabel")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.toggle_btn = ttk.Button(self.header, text=self._arrow(collapse=True), width=2, style="Ghost.TButton",
                                     command=self.toggle)
        self.toggle_btn.grid(row=0, column=1, sticky="e")
        Tooltip(self.toggle_btn, lambda: "Свернуть")
        self.body = ttk.Frame(self)

        self.strip = tk.Canvas(self, width=strip_width, highlightthickness=0, borderwidth=0, cursor="hand2")
        self.strip.bind("<Button-1>", lambda _e: self.expand())
        self.strip.bind("<Configure>", lambda _e: self.redraw())
        Tooltip(self.strip, lambda: f"Показать: {self.title}")
        self._place()

    # -- состояние ---------------------------------------------------------------

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        """Сворачивает или разворачивает; событие <<ColumnToggled>> только если состояние изменилось."""
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._place()
        self.event_generate(self.EVENT)

    @property
    def width(self) -> int:
        return self._width

    def set_width(self, width: int) -> None:
        """Запрошенная ширина развёрнутой колонки (меняется перетаскиванием разделителя); у свёрнутой не трогается."""
        self._width = width
        if not self._collapsed:
            self.configure(width=width)

    def toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    def expand(self) -> None:
        self.set_collapsed(False)

    def collapse(self) -> None:
        self.set_collapsed(True)

    # -- вид ---------------------------------------------------------------------

    def _arrow(self, collapse: bool) -> str:
        """Стрелка показывает, куда уедет колонка: при сворачивании - к своему краю, при разворачивании - обратно."""
        towards_edge = "‹" if self.side == "left" else "›"
        back = "›" if self.side == "left" else "‹"
        return towards_edge if collapse else back

    def _place(self) -> None:
        if self._collapsed:
            self.header.grid_remove()
            self.body.grid_remove()
            self.strip.grid(row=0, column=0, rowspan=2, sticky="nsew")
            self.configure(width=self._strip_width)
            self.redraw()
        else:
            self.strip.grid_remove()
            self.header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
            self.body.grid(row=1, column=0, sticky="nsew")
            self.configure(width=self._width)

    def redraw(self) -> None:
        """Рисует свёрнутую полосу цветами текущей темы (после смены темы вызывается снова)."""
        if not self._collapsed:
            return
        p = theme.PALETTE
        canvas = self.strip
        canvas.configure(background=p.surface_alt)
        canvas.delete("all")
        width = int(canvas.winfo_width()) if canvas.winfo_width() > 1 else self._strip_width
        middle = width // 2
        arrow_font = tkfont.Font(root=self, family=FONT_FAMILY, size=12, weight="bold")
        line = arrow_font.metrics("linespace")          # отступы по высоте шрифта: верно при любом масштабе
        canvas.create_text(middle, line // 2 + 4, text=self._arrow(collapse=False), fill=p.accent_text,
                           font=arrow_font, anchor="center", tags=("arrow",))
        # название снизу вверх, начинается под стрелкой
        canvas.create_text(middle, line + 8, text=self.collapsed_title, angle=90, fill=p.muted,
                           font=(FONT_FAMILY, 10, "bold"), anchor="e", tags=("title",))


class Tooltip:
    """Подсказка при наведении; текст берётся из text_fn() в момент показа."""

    def __init__(self, widget: tk.Misc, text_fn: Callable[[], str]):
        self.widget, self.text_fn = widget, text_fn
        self.tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        text = self.text_fn()
        if self.tip is not None or not text:
            return
        p = theme.PALETTE
        self.tip = tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.configure(background=p.border_strong)       # рамка в цвет темы вместо системной чёрной
        tk.Label(tip, text=text, background=p.surface, foreground=p.text, padx=8, pady=4, justify="left",
                 wraplength=360, font=(FONT_FAMILY, 9)).pack(padx=1, pady=1)
        x = self.widget.winfo_rootx() + self.widget.winfo_width() - tip.winfo_reqwidth()
        tip.wm_geometry(f"+{max(0, x)}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 6}")

    def _hide(self, _event=None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class Lamp(tk.Canvas):
    """Круглый индикатор: цвет = состояние, в режиме busy по кругу бежит дуга."""

    def __init__(self, parent: tk.Misc, size: int, background: str, command: Optional[Callable] = None):
        super().__init__(parent, width=size, height=size, background=background, highlightthickness=0,
                         cursor="hand2" if command else "")
        self._size, self._color, self._busy, self._angle = size, "#888888", False, 0
        self._after: Optional[str] = None
        if command:
            self.bind("<Button-1>", lambda _e: command())
        self.bind("<Destroy>", lambda _e: self._stop())
        self._draw()

    def set_state(self, color: str, busy: bool) -> None:
        if color == self._color and busy == self._busy:
            return
        self._color, self._busy = color, busy
        if busy and self._after is None:
            self._spin()
        elif not busy:
            self._stop()
        self._draw()

    def _stop(self) -> None:
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _spin(self) -> None:
        self._angle = (self._angle - 30) % 360
        self._draw()
        self._after = self.after(80, self._spin)

    def _draw(self) -> None:
        self.delete("all")
        pad = 2
        box = (pad, pad, self._size - pad, self._size - pad)
        if self._busy:
            self.create_oval(*box, outline=self._color, width=2, stipple="gray25")
            self.create_arc(*box, start=self._angle, extent=100, style="arc", outline=self._color, width=3)
        else:
            self.create_oval(*box, fill=self._color, outline=self._color)
