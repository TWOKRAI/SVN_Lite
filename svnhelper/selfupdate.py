"""Самообновление SVN-lite: новая версия exe с сервера заменяет запущенную, и программа открывается заново.

Запущенный exe Windows перезаписать не даёт, поэтому порядок такой: новый файл копируется рядом со старым
(«SVN-lite.exe.new»), запускается маленький bat-скрипт, программа закрывается, скрипт ждёт, пока старый exe
освободится, подменяет его новым и запускает. Если подмена не удалась, скрипт открывает старую версию.
Работает только у собранного exe (PyInstaller). Модуль не зависит от tkinter.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

log = logging.getLogger("svnhelper.selfupdate")

EXE_NAME = "SVN-lite.exe"          # имя файла в папке с новой версией, если update_path указывает на папку
WAIT_TRIES = 60                    # сколько секунд скрипт ждёт, пока старый exe закроется
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


class UpdateError(Exception):
    """Понятная причина, по которой обновить не получилось (текст показывается пользователю)."""


@dataclass
class PreparedUpdate:
    new_path: str      # скачанный файл рядом с запущенным exe
    target: str        # запущенный exe, который нужно заменить


def is_supported() -> bool:
    """Заменить можно только собранный exe в Windows (при запуске из исходников заменять нечего)."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def resolve_source(update_path: str) -> str:
    """update_path из общего конфига: файл .exe или папка, где лежит SVN-lite.exe."""
    path = os.path.expandvars(update_path.strip()).strip('"')
    if not path:
        raise UpdateError("В общих настройках не указан путь к новой версии (update_path).")
    if "://" in path:
        raise UpdateError(f"Обновление по ссылке не поддерживается: {path}")
    if os.path.isdir(path):
        path = os.path.join(path, EXE_NAME)
    if not path.lower().endswith(".exe"):
        raise UpdateError(f"Новая версия должна быть файлом .exe: {path}")
    if not os.path.isfile(path):
        raise UpdateError(f"Новая версия не найдена или нет доступа к папке на сервере:\n{path}")
    return path


def _check_exe(path: str) -> None:
    """Защита от обрезанного или чужого файла: заголовок «MZ» и непустой размер."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        header = f.read(2)
    if header != b"MZ" or size < 1_000_000:
        raise UpdateError("Файл новой версии повреждён или не похож на программу. Сообщите администратору.")


def prepare(update_path: str) -> PreparedUpdate:
    """Копирует новую версию рядом с запущенным exe. Вызывать из фонового потока."""
    if not is_supported():
        raise UpdateError("Автообновление работает только у собранной программы SVN-lite.exe.")
    source = resolve_source(update_path)
    target = os.path.abspath(sys.executable)
    new_path = target + ".new"
    try:
        shutil.copyfile(source, new_path)
        if os.path.getsize(new_path) != os.path.getsize(source):
            raise UpdateError("Файл скопировался не полностью. Повторите попытку.")
        _check_exe(new_path)
    except OSError as exc:
        _remove(new_path)
        raise UpdateError(f"Не удалось скачать новую версию ({exc.strerror or exc}). "
                          "Проверьте доступ к серверу и права на папку с программой.") from exc
    except UpdateError:
        _remove(new_path)
        raise
    log.info("Новая версия скопирована: %s -> %s", source, new_path)
    return PreparedUpdate(new_path=new_path, target=target)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def swap_script(new_path: str, target: str) -> str:
    """Текст bat-скрипта: ждёт освобождения старого exe, подменяет его, запускает программу, удаляет себя."""
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        # собранный exe PyInstaller не должен унаследовать окружение закрывающегося процесса
        "set _MEIPASS2=\r\n"
        "set PYINSTALLER_RESET_ENVIRONMENT=1\r\n"
        "set tries=0\r\n"
        ":retry\r\n"
        f'move /y "{new_path}" "{target}" >nul 2>&1\r\n'
        "if not errorlevel 1 goto done\r\n"
        "set /a tries+=1\r\n"
        f"if %tries% geq {WAIT_TRIES} goto fail\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto retry\r\n"
        ":fail\r\n"
        f'del /q "{new_path}" >nul 2>&1\r\n'
        ":done\r\n"
        f'start "" "{target}"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )


def launch_swap(prepared: PreparedUpdate) -> None:
    """Запускает скрипт подмены отдельным процессом. После этого программа должна сразу закрыться."""
    folder = tempfile.mkdtemp(prefix="svnlite_upd_")
    script = os.path.join(folder, "update.bat")
    with open(script, "w", encoding="utf-8", newline="") as f:
        f.write(swap_script(prepared.new_path, prepared.target))
    subprocess.Popen(["cmd.exe", "/c", script], creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
                     close_fds=True, cwd=folder)
    log.info("Запущен скрипт подмены: %s", script)
