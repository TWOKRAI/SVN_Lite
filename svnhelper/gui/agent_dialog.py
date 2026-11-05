"""Окно «Справка → ИИ-помощник…»: что разрешено помощнику сейчас, в каких папках, как его подключить.

Показывает режим, решение администратора (потолок) и состояние подписи настроек помощника (`svnhelper.agent_policy`),
разрешённые папки и журнал действий. Для подключения копирует в буфер команду для Claude Code и настройку для Claude
Desktop. Переключатели разрешений («Разрешить помощнику: ...») пока неактивны: запись появится в следующей версии
(Task AG.5), сейчас помощник только смотрит. Политику окно пишет только для списка дополнительных папок
(`agent_policy.write`, единственный способ её изменить); при неверной подписи и недоступном файле запись выключена.

Тексты по-русски, без терминов svn. Окно не модальное: открыто не больше одного (`App._open_agent_dialog`).
"""
from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .. import agent_policy, selfupdate, theme
from ..config import data_dir
from ..dialogs import center_window, FONT_FAMILY
from ..ops import texts
from .models import log

TITLE = "ИИ-помощник"
INTRO = ("ИИ-помощник (например, Claude) может смотреть ваши проекты: что изменилось, что нового у коллег, историю и "
         "занятые файлы. Пароль помощнику не передаётся.")
CEILING_TEXTS = {
    "off": "помощник выключен",
    "read_only": "помощнику разрешено только смотреть",
    "write": "помощнику можно разрешить запись",
}
# состояние подписи настроек помощника -> слово в окне (нормальные ok и no_file без тревоги)
SIGNATURE_TEXTS = {
    agent_policy.SIG_OK: "нормально",
    agent_policy.SIG_NO_FILE: "не задана, помощник только смотрит",
    agent_policy.SIG_INVALID: "изменена",
    agent_policy.SIG_FOREIGN: "нужно включить заново",
    agent_policy.SIG_UNREADABLE: "файл сейчас не прочитан",
}
# разрешения: (код из agent_policy.ACTIONS, продолжение фразы «Разрешить помощнику: ...»)
PERMISSIONS = (
    ("save_server", "сохранять на сервере"),
    ("save_local", "сохранять у себя"),
    ("lock", "занимать файлы"),
    ("create_local", "создавать проекты на компьютере"),
    ("create_server", "создавать проекты на сервере (с осторожностью)"),
    ("publish", "отправлять папки на сервер (с осторожностью)"),
)
PERMISSION_PREFIX = "Разрешить помощнику: "
NOT_YET_TEXT = "запись появится в следующей версии"
FOLDERS_FIXED_TEXT = "Ваши папки для проектов (доступны помощнику всегда):"
FOLDERS_EXTRA_TEXT = "Дополнительные папки, разрешённые помощнику:"
NO_FOLDERS_TEXT = "не заданы"
FOLDERS_LOCKED_TEXT = "Список папок изменить нельзя, пока настройки помощника не включены заново."
FOLDER_PICK_TITLE = "Папка для помощника"
FOLDERS_CHANGED_TEXT = ("Папки не изменены: настройки помощника изменились, пока это окно было открыто. "
                        "Окно показало их заново.")
FOLDER_NETWORK_TEXT = ("Папку по сетевому адресу (вида \\\\сервер\\папка) помощнику открыть нельзя. "
                       "Выберите папку на этом компьютере или на подключённом диске.")
COPIED_CODE_TEXT = "Команда скопирована. Вставьте её в окно командной строки, где работает Claude Code."
COPIED_DESKTOP_TEXT = ("Настройка скопирована. Добавьте её в файл настроек Claude Desktop "
                       "(%APPDATA%\\Claude\\claude_desktop_config.json) и перезапустите Claude Desktop.")
CLI_MISSING_TEXT = ("Файл svnlite.exe рядом с программой не найден. Возьмите его у администратора: "
                    "без него подключить помощника нельзя.")
SAVE_ERROR_TEXT = "Не удалось записать настройки помощника"
LOG_FOLDER_TEXT = "Журнал действий помощника находится здесь:"


# --------------------------------------------------------------------------
# Чистые функции (без окна): команды подключения и сведения о политике
# --------------------------------------------------------------------------

def cli_command() -> list:
    """Как запустить сервер помощника: [svnlite.exe] у собранной программы, [python, svnlite_cli.py] из исходников.
    Продолжение команды: аргумент `mcp`."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve().with_name(selfupdate.CLI_EXE_NAME))]
    return [console_python(), str(Path(__file__).resolve().parents[2] / "svnlite_cli.py")]


def console_python() -> str:
    """python.exe для помощника: у pythonw.exe нет консоли, а сервер помощника общается через стандартный ввод и вывод.
    Окно из исходников запускают через pythonw.exe, поэтому берём соседний python.exe, если он есть."""
    path = Path(sys.executable)
    if path.name.lower() == "pythonw.exe" and path.with_name("python.exe").is_file():
        return str(path.with_name("python.exe"))
    return str(path)


def claude_code_command(parts: Optional[list] = None) -> str:
    """Команда для Claude Code: добавить сервер помощника для текущего пользователя."""
    parts = parts or cli_command()
    return "claude mcp add --scope user svnlite -- " + " ".join(f'"{part}"' for part in parts) + " mcp"


def claude_desktop_json(parts: Optional[list] = None) -> str:
    """Фрагмент claude_desktop_config.json: command и args с `mcp` в конце."""
    parts = parts or cli_command()
    config = {"mcpServers": {"svnlite": {"command": parts[0], "args": [*parts[1:], "mcp"]}}}
    return json.dumps(config, ensure_ascii=False, indent=2)


def policy_values(policy: agent_policy.Policy) -> dict:
    """Значения подписанной политики для записи (всё, кроме списка папок, остаётся как есть)."""
    limits = policy.describe()["limits"]          # числовые пределы и метка в комментариях
    return {**agent_policy.DEFAULTS, **limits, "mode": policy.file_mode, "allow": list(policy.allow),
            "allowed_dirs": list(policy.allowed_dirs)}


def folders_editable(policy: agent_policy.Policy) -> bool:
    """Список папок можно менять, если настройки в порядке или их ещё нет (запись заново подпишет файл)."""
    return policy.signature in (agent_policy.SIG_OK, agent_policy.SIG_NO_FILE)


def cli_available() -> bool:
    """Есть ли что запускать: у собранной программы рядом с окном должен лежать svnlite.exe."""
    return os.path.isfile(cli_command()[0])


def is_network_path(path: str) -> bool:
    """Сетевой путь вида \\\\сервер\\папка (в том числе с прямыми слешами, как отдаёт окно выбора папки)."""
    return path.replace("/", "\\").startswith("\\\\")


def journal_folder() -> Path:
    return data_dir() / "agent"


# --------------------------------------------------------------------------
# Окно
# --------------------------------------------------------------------------

class AgentDialog(tk.Toplevel):
    """app - главное окно (root, config, settings, px); app.py модуль не импортирует."""

    def __init__(self, app):
        super().__init__(app.root, background=theme.PALETTE.surface)
        self.app = app
        self.policy = self._load_policy()
        self.title(TITLE)
        self.transient(app.root)
        self.resizable(False, False)
        px = app.px
        wrap = px(560)

        body = ttk.Frame(self, padding=px(12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text=INTRO, wraplength=wrap, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, px(8)))

        # состояние
        self.mode_var, self.ceiling_var, self.signature_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for row, (caption, var) in enumerate((("Сейчас:", self.mode_var), ("Решение администратора:", self.ceiling_var),
                                              ("Подпись настроек:", self.signature_var)), start=2):
            ttk.Label(body, text=caption).grid(row=row, column=0, sticky="nw", padx=(0, px(10)))
            ttk.Label(body, textvariable=var, wraplength=px(400), justify="left").grid(row=row, column=1, sticky="w")
        self.note_label = ttk.Label(body, text="", style="Error.TLabel", wraplength=wrap, justify="left")
        self.note_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(px(4), 0))

        # разрешения (запись появится в следующей версии: переключатели выключены и неактивны)
        perms = ttk.LabelFrame(body, text=f"Что разрешено помощнику ({NOT_YET_TEXT})", padding=px(8))
        perms.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        self.permission_vars: dict = {}
        self.permission_boxes: dict = {}
        for row, (action, label) in enumerate(PERMISSIONS):
            var = tk.BooleanVar(value=False)
            box = ttk.Checkbutton(perms, text=PERMISSION_PREFIX + label, variable=var, state="disabled")
            box.grid(row=row % 3, column=row // 3, sticky="w", padx=(0, px(12)))
            self.permission_vars[action] = var
            self.permission_boxes[action] = box

        # папки
        folders = ttk.LabelFrame(body, text="Папки", padding=px(8))
        folders.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        folders.columnconfigure(0, weight=1)
        ttk.Label(folders, text=FOLDERS_FIXED_TEXT).grid(row=0, column=0, columnspan=2, sticky="w")
        self.fixed_var = tk.StringVar()
        ttk.Label(folders, textvariable=self.fixed_var, style="Muted.TLabel", wraplength=wrap, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, px(4)))
        ttk.Label(folders, text=FOLDERS_EXTRA_TEXT).grid(row=2, column=0, columnspan=2, sticky="w")
        palette = theme.PALETTE
        self.folder_list = tk.Listbox(folders, height=3, width=70, exportselection=False, activestyle="none",
                                      font=(FONT_FAMILY, 10), background=palette.field, foreground=palette.text,
                                      selectbackground=palette.accent_soft, selectforeground=palette.text,
                                      highlightthickness=1, highlightbackground=palette.border, relief="flat")
        self.folder_list.grid(row=3, column=0, sticky="ew")
        folder_buttons = ttk.Frame(folders)
        folder_buttons.grid(row=3, column=1, sticky="n", padx=(px(8), 0))
        self.add_btn = ttk.Button(folder_buttons, text="Добавить папку…", command=self._on_add_folder)
        self.add_btn.pack(fill="x")
        self.remove_btn = ttk.Button(folder_buttons, text="Убрать", command=self._on_remove_folder)
        self.remove_btn.pack(fill="x", pady=(px(4), 0))
        self.folders_hint = ttk.Label(folders, text=FOLDERS_LOCKED_TEXT, style="Hint.TLabel", wraplength=wrap,
                                      justify="left")
        self.folders_hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(px(4), 0))

        # подключение и журнал
        connect = ttk.LabelFrame(body, text="Подключение", padding=px(8))
        connect.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        self.cli_label = ttk.Label(connect, text="", style="Muted.TLabel", wraplength=wrap, justify="left")
        self.cli_label.pack(anchor="w", pady=(0, px(4)))
        row_buttons = ttk.Frame(connect)
        row_buttons.pack(anchor="w")
        self.copy_code_btn = ttk.Button(row_buttons, text="Скопировать команду подключения (Claude Code)",
                                        command=self._copy_claude_code)
        self.copy_code_btn.pack(side="left")
        self.copy_desktop_btn = ttk.Button(row_buttons, text="Скопировать настройку для Claude Desktop",
                                           command=self._copy_claude_desktop)
        self.copy_desktop_btn.pack(side="left", padx=(px(8), 0))
        self.status_var = tk.StringVar()
        self.status_label = ttk.Label(connect, textvariable=self.status_var, wraplength=wrap, justify="left")

        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=2, sticky="e", pady=(px(10), 0))
        self.log_btn = ttk.Button(buttons, text="Открыть журнал действий помощника", command=self._open_journal_window)
        self.log_btn.pack(side="left", padx=(0, px(8)))
        self.folder_btn = ttk.Button(buttons, text="Папка с файлами журнала", command=self._open_journal)
        self.folder_btn.pack(side="left", padx=(0, px(8)))
        self.close_btn = ttk.Button(buttons, text="Закрыть", command=self.destroy)
        self.close_btn.pack(side="left")

        self.bind("<Escape>", lambda _e: self.destroy())
        self._refresh()
        center_window(self, parent=app.root)

    # -- состояние -----------------------------------------------------------------

    def _load_policy(self) -> agent_policy.Policy:
        return agent_policy.Policy.load(self.app.config.agent_max_mode)

    def _refresh(self) -> None:
        """Перерисовывает всё, что зависит от файла настроек помощника."""
        policy = self.policy
        self.mode_var.set(texts.policy_mode_text(policy.mode))
        self.ceiling_var.set(CEILING_TEXTS.get(policy.ceiling, CEILING_TEXTS["read_only"]))
        self.signature_var.set(SIGNATURE_TEXTS.get(policy.signature, policy.signature))
        note = texts.POLICY_SIGNATURE_NOTES.get(policy.signature, "")
        self.note_label.configure(text=note)
        if note:
            self.note_label.grid()
        else:
            self.note_label.grid_remove()          # пустая строка не занимает места: окно должно помещаться на экран
        for action, var in self.permission_vars.items():
            var.set(action in policy.allow)      # показываем действующее разрешение, менять его пока нельзя
        fixed = [d for d in self.app.settings.project_dirs if d]
        self.fixed_var.set("\n".join(fixed) if fixed else NO_FOLDERS_TEXT)
        self.folder_list.delete(0, "end")
        for folder in policy.allowed_dirs:
            self.folder_list.insert("end", folder)
        editable = folders_editable(policy)
        for button in (self.add_btn, self.remove_btn):
            button.configure(state="normal" if editable else "disabled")
        if editable:
            self.folders_hint.grid_remove()
        else:
            self.folders_hint.grid()
        available = cli_available()
        self.cli_label.configure(text="Программа для помощника: " + cli_command()[0]
                                 + ("" if available else "\n" + CLI_MISSING_TEXT))
        for button in (self.copy_code_btn, self.copy_desktop_btn):
            button.configure(state="normal" if available else "disabled")

    # -- папки ---------------------------------------------------------------------

    def _write_dirs(self, change: Callable[[list], list]) -> bool:
        """Меняет список дополнительных папок и записывает политику. True, если записано.

        Администратор мог изменить политику, пока окно было открыто: перед записью она читается заново, `change`
        получает СВЕЖИЙ список папок, а режим, разрешения и пределы берутся из свежей политики (правку
        администратора окно не откатывает). Подпись стала неверной, чужой или файл не прочитан: ничего не пишется."""
        self.policy = self._load_policy()
        if not folders_editable(self.policy):
            self._refresh()
            messagebox.showwarning(TITLE, f"{FOLDERS_CHANGED_TEXT} {FOLDERS_LOCKED_TEXT}", parent=self)
            return False
        current = list(self.policy.allowed_dirs)
        dirs = change(list(current))
        if dirs == current:                         # уже так: другой человек или повторное нажатие
            self._refresh()
            return True
        values = policy_values(self.policy)
        values["allowed_dirs"] = dirs
        try:
            agent_policy.write(values)
        except OSError as exc:
            log.error("Настройки помощника не записаны", exc_info=exc)
            messagebox.showerror(TITLE, f"{SAVE_ERROR_TEXT}: {exc.strerror or exc}", parent=self)
            return False
        self.policy = self._load_policy()
        self._refresh()
        return True

    def _on_add_folder(self) -> None:
        if not folders_editable(self.policy):
            return
        chosen = filedialog.askdirectory(parent=self, title=FOLDER_PICK_TITLE, mustexist=True)
        if not chosen:
            return
        if is_network_path(chosen):
            messagebox.showwarning(TITLE, FOLDER_NETWORK_TEXT, parent=self)
            return
        folder = os.path.normpath(chosen)

        def add(dirs: list) -> list:
            known = {os.path.normcase(d) for d in dirs}
            return dirs if os.path.normcase(folder) in known else [*dirs, folder]

        self._write_dirs(add)

    def _on_remove_folder(self) -> None:
        selected = self.folder_list.curselection()
        if not selected or not folders_editable(self.policy):
            return
        folder = self.folder_list.get(selected[0])      # по значению: список в окне мог устареть

        def remove(dirs: list) -> list:
            return [d for d in dirs if os.path.normcase(d) != os.path.normcase(folder)]

        self._write_dirs(remove)

    # -- подключение и журнал --------------------------------------------------------

    def _copy(self, text: str, done: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set(done)
        self.status_label.pack(anchor="w", pady=(self.app.px(4), 0))

    def _copy_claude_code(self) -> None:
        self._copy(claude_code_command(), COPIED_CODE_TEXT)

    def _copy_claude_desktop(self) -> None:
        self._copy(claude_desktop_json(), COPIED_DESKTOP_TEXT)

    def _open_journal_window(self) -> None:
        self.app._open_agent_journal()

    def _open_journal(self) -> None:
        folder = journal_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            messagebox.showinfo(TITLE, f"{LOG_FOLDER_TEXT}\n{folder}", parent=self)
