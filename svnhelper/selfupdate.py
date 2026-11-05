"""Самообновление SVN-lite: новая версия exe с сервера заменяет запущенную, и программа открывается заново.

Запущенный exe Windows перезаписать не даёт, поэтому порядок такой: новый файл копируется рядом со старым
(«SVN-lite.exe.new»), запускается маленький bat-скрипт, программа закрывается, скрипт ждёт, пока старый exe
освободится, подменяет его новым и запускает. Если подмена не удалась, скрипт открывает старую версию.
Работает только у собранного exe (PyInstaller). Модуль не зависит от tkinter.

Рядом с окном может лежать консольная программа svnlite.exe (для ИИ-помощника, Task AG.7); её раздаёт на ПК вручную
администратор, самообновление окна её не устанавливает. Если она уже лежит рядом, её обновляют вместе с окном, но
без ожидания: помощник может держать svnlite.exe запущенным часами, и из-за него обновление окна не должно зависнуть.
Поэтому `prepare` кладёт новую версию рядом как «svnlite.exe.new», скрипт подмены делает одну попытку `move`, а если
файл занят, `.new` остаётся и подменяется при следующем запуске окна (`apply_pending`). Окно при запуске записывает
свою версию в `window_version.json` (`record_window_version`), чтобы `doctor` в svnlite.exe заметил разные версии.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("svnhelper.selfupdate")

EXE_NAME = "SVN-lite.exe"          # имя файла в папке с новой версией, если update_path указывает на папку
CLI_EXE_NAME = "svnlite.exe"       # консольная программа для ИИ-помощника: лежит рядом с окном и в папке обновлений
WINDOW_VERSION_FILE = "window_version.json"
WAIT_TRIES = 60                    # сколько секунд скрипт ждёт, пока старый exe закроется
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


class UpdateError(Exception):
    """Понятная причина, по которой обновить не получилось (текст показывается пользователю)."""


@dataclass
class PreparedUpdate:
    new_path: str      # скачанный файл рядом с запущенным exe
    target: str        # запущенный exe, который нужно заменить
    # (новый файл, что заменить) для svnlite.exe: подменяются одной попыткой, без ожидания
    extras: list = field(default_factory=list)


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
    return PreparedUpdate(new_path=new_path, target=target, extras=_prepare_cli(source, target))


def _prepare_cli(source: str, target: str) -> list:
    """Копирует svnlite.exe из папки с новой версией окна рядом с запущенным exe как «svnlite.exe.new».
    Обновляется только уже установленный svnlite.exe: консольную программу сам администратор раздаёт на ПК вручную
    (её нельзя ставить всем без согласования с ИТ и проверки антивирусом), обновление окна её не устанавливает.
    Нет файла в папке обновлений или он не скопировался: окно всё равно обновляется, причина только в журнале."""
    cli_target = os.path.join(os.path.dirname(target), CLI_EXE_NAME)
    if not os.path.isfile(cli_target):
        log.info("Рядом с окном нет %s: консольная программа не устанавливается обновлением", CLI_EXE_NAME)
        return []
    cli_source = os.path.join(os.path.dirname(source), CLI_EXE_NAME)
    if not os.path.isfile(cli_source):
        log.info("В папке обновлений нет %s: консольная программа не обновляется", CLI_EXE_NAME)
        return []
    cli_new = cli_target + ".new"
    try:
        shutil.copyfile(cli_source, cli_new)
        if os.path.getsize(cli_new) != os.path.getsize(cli_source):
            raise UpdateError("файл скопировался не полностью")
        _check_exe(cli_new)
    except (OSError, UpdateError) as exc:
        log.warning("%s не скопирован, окно обновляется без него: %s", CLI_EXE_NAME, exc)
        _remove(cli_new)
        return []
    log.info("Консольная программа скопирована: %s -> %s", cli_source, cli_new)
    return [(cli_new, cli_target)]


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def swap_script(new_path: str, target: str, extras: tuple = ()) -> str:
    """Текст bat-скрипта: ждёт освобождения старого exe, подменяет его, запускает программу, удаляет себя.

    extras: пары (новый файл, что заменить) для svnlite.exe. Для каждой одна попытка `move` без ожидания: занятый файл
    (его держит запущенный помощник) остаётся как «.new» и подменяется при следующем запуске окна. Если не удалась
    подмена самого окна, новые файлы помощника удаляются: версии программ не должны разойтись."""
    extra_moves = "".join(f'move /y "{new}" "{old}" >nul 2>&1\r\n' for new, old in extras)
    extra_drops = "".join(f'del /q "{new}" >nul 2>&1\r\n' for new, _old in extras)
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
        f"{extra_drops}"
        "goto launch\r\n"
        ":done\r\n"
        f"{extra_moves}"
        ":launch\r\n"
        f'start "" "{target}"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )


def launch_swap(prepared: PreparedUpdate) -> None:
    """Запускает скрипт подмены отдельным процессом. После этого программа должна сразу закрыться."""
    folder = tempfile.mkdtemp(prefix="svnlite_upd_")
    script = os.path.join(folder, "update.bat")
    with open(script, "w", encoding="utf-8", newline="") as f:
        f.write(swap_script(prepared.new_path, prepared.target, tuple(prepared.extras)))
    subprocess.Popen(["cmd.exe", "/c", script], creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
                     close_fds=True, cwd=folder)
    log.info("Запущен скрипт подмены: %s", script)


def _pending_cli_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), CLI_EXE_NAME + ".new")


def pending_cli_update() -> bool:
    """Лежит ли рядом с программой «svnlite.exe.new»: обновление помощника скачано, но ещё не подменило старый файл
    (его держит запущенный `svnlite.exe mcp`). У программы, запущенной из исходников, всегда False."""
    return is_supported() and os.path.isfile(_pending_cli_path())


VERSION_TIMEOUT = 15.0
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


def _exe_version(path: str) -> Optional[tuple]:
    """Версия программы по `<exe> --version` («svnlite 1.2.9» -> (1, 2, 9)). None: не запустилась или версии нет."""
    try:
        done = subprocess.run([path, "--version"], capture_output=True, timeout=VERSION_TIMEOUT, stdin=subprocess.DEVNULL,
                              creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search((done.stdout or b"").decode("utf-8", "replace"))
    return tuple(int(part) for part in match.group(1).split(".")) if done.returncode == 0 and match else None


def apply_pending() -> list:
    """При запуске окна подменяет «svnlite.exe.new», который скрипт обновления не смог положить (файл был занят).
    Занят и сейчас: остаётся до следующего запуска. Повреждённый файл удаляется. Возвращает подменённые файлы.
    Исключений не бросает: запуск окна из-за этого ломаться не должен."""
    if not is_supported():
        return []
    pending = _pending_cli_path()
    cli = pending[:-len(".new")]
    if not os.path.isfile(pending):
        return []
    try:
        _check_exe(pending)
    except (OSError, UpdateError) as exc:
        log.warning("Отложенный %s повреждён и удалён: %s", pending, exc)
        _remove(pending)
        return []
    new_version = _exe_version(pending)
    if new_version is None:
        log.warning("Версия отложенного %s не определена, файл удалён", pending)
        _remove(pending)
        return []
    current = _exe_version(cli) if os.path.isfile(cli) else None
    if current is not None and new_version < current:
        # «.new» от прошлого выпуска (окно не обновилось или обновилось позже): свежий помощник не заменяется старым
        log.warning("Отложенный %s (%s) старее установленного (%s), файл удалён", pending,
                    ".".join(map(str, new_version)), ".".join(map(str, current)))
        _remove(pending)
        return []
    try:
        os.replace(pending, cli)
    except OSError as exc:
        log.info("%s сейчас занят, подмена отложена до следующего запуска: %s", CLI_EXE_NAME, exc)
        return []
    log.info("Консольная программа обновлена при запуске: %s", cli)
    return [cli]


def record_window_version() -> None:
    """Окно записывает свою версию в папку данных: по ней doctor в svnlite.exe видит, что версии разошлись.
    Только у собранного exe (запуск из исходников не должен подменять запись). Сбой записи только в журнал."""
    if not is_supported():
        return
    from . import __version__
    from .config import data_dir
    try:
        path = data_dir() / WINDOW_VERSION_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": __version__}), encoding="utf-8")
    except OSError:
        log.exception("Версия окна не записана")


def window_version() -> str:
    """Версия окна SVN-lite, записанная им при последнем запуске; пустая строка, если окно ещё не запускалось."""
    from .config import data_dir
    try:
        data = json.loads((data_dir() / WINDOW_VERSION_FILE).read_text(encoding="utf-8"))
        version = data.get("version", "")
        return version if isinstance(version, str) else ""
    except (OSError, ValueError, AttributeError):
        return ""
