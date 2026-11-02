"""Запуск программы вместе с Windows: значение в HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.

Без tkinter. Запись идёт в раздел текущего пользователя, права администратора не нужны.
Программа из автозапуска стартует с ключом --tray: окно скрыто, значок сразу в трее.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

try:
    import winreg
except ImportError:      # не Windows
    winreg = None        # type: ignore[assignment]

log = logging.getLogger("svnhelper.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SVN-lite"
TRAY_ARG = "--tray"


def launch_command() -> str:
    """Строка запуска для реестра: собранная программа (exe) или python-скрипт, плюс ключ --tray."""
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}" {TRAY_ARG}'
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")        # без чёрного окна консоли
    if windowed.is_file():
        python = windowed
    script = Path(__file__).resolve().parent.parent / "svn_helper.py"
    return f'"{python}" "{script}" {TRAY_ARG}'


def is_enabled() -> bool:
    """Есть ли значение автозапуска в реестре."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("Не удалось прочитать автозапуск из реестра")
        return False


def current_command() -> Optional[str]:
    """Что сейчас записано в реестре для автозапуска; None, если автозапуск выключен или значение не прочитать."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
        return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        log.exception("Не удалось прочитать автозапуск из реестра")
        return None


def refresh() -> bool:
    """Автозапуск включён, но указывает не туда (программу переместили или обновили): записывает верный путь.
    True, если значение переписано. Выключенный автозапуск не включает. OSError, если записать не удалось."""
    current = current_command()
    if current is None or current == launch_command():
        return False
    enable()
    return True


def enable() -> None:
    """Включает автозапуск. OSError, если записать не удалось."""
    if winreg is None:
        raise OSError("автозапуск доступен только в Windows")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())


def disable() -> None:
    """Выключает автозапуск. Если его не было, ничего не делает. OSError при другой ошибке."""
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
