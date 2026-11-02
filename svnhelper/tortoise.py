"""Запуск окон TortoiseSVN (TortoiseProc.exe): журнал, разбор конфликтов, возврат правок, починка папки.

Сохранение на сервер и выбор при конфликте программа делает сама; окна TortoiseSVN - дополнительный путь.
Модуль не зависит от tkinter.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EXE_NAME = "TortoiseProc.exe"

# команды, которые поддерживает программа (имена команд TortoiseProc)
COMMANDS = {"commit", "update", "log", "resolve", "conflicteditor", "cleanup",
            "repostatus", "lock", "unlock", "diff", "merge", "revert"}


def find_tortoiseproc(svn_path: str = "") -> Optional[str]:
    """Рядом с svn.exe → PATH → Program Files\\TortoiseSVN\\bin\\TortoiseProc.exe."""
    if svn_path:
        beside = os.path.join(os.path.dirname(svn_path), EXE_NAME)
        if os.path.isfile(beside):
            return beside
    found = shutil.which("TortoiseProc")
    if found:
        return found
    for var in ("ProgramFiles", "ProgramW6432"):
        root = os.environ.get(var)
        if root:
            candidate = os.path.join(root, "TortoiseSVN", "bin", EXE_NAME)
            if os.path.isfile(candidate):
                return candidate
    return None


def build_args(exe: str, command: str, paths: list[str], **opts) -> list[str]:
    """[exe, "/command:<cmd>", "/path:<p1*p2>", ...]. Несколько путей TortoiseProc принимает через '*'.

    opts: closeonend (int, например 0 — не закрывать окно сам), logmsg (str — готовый текст сообщения).
    resolve для одного файла открывает редактор конфликтов (conflicteditor), для папки — окно resolve.
    """
    if command not in COMMANDS:
        raise ValueError(f"Неизвестная команда TortoiseSVN: {command}")
    if not paths:
        raise ValueError("Не указан путь для TortoiseSVN")
    if command == "resolve" and len(paths) == 1 and os.path.isfile(paths[0]):
        command = "conflicteditor"
    args = [exe, f"/command:{command}", "/path:" + "*".join(paths)]
    if opts.get("closeonend") is not None:
        args.append(f"/closeonend:{opts['closeonend']}")
    if opts.get("logmsg"):
        args.append(f"/logmsg:{opts['logmsg']}")
    return args


def command_line(args: list[str]) -> str:
    """Командная строка в том виде, который понимает TortoiseProc: /path:"C:\\a b".

    Обычная склейка subprocess дала бы "/path:C:\\a b": TortoiseProc ищет '/' сам и
    оборвал бы путь на пробеле. Кавычки внутри значения TortoiseProc не поддерживает.
    """
    parts = [f'"{args[0]}"']
    for opt in args[1:]:
        key, sep, value = opt.partition(":")
        if sep and key in ("/path", "/logmsg"):
            opt = f'{key}:"{value.replace(chr(34), chr(39))}"'
        parts.append(opt)
    return " ".join(parts)


def launch(args: list[str]) -> subprocess.Popen:
    """Запускает TortoiseProc без окна консоли. OSError (нет файла, нет прав) пробрасывается."""
    return subprocess.Popen(command_line(args), creationflags=CREATE_NO_WINDOW)
