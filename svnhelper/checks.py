"""Проверки перед скачиванием. Без tkinter: возвращают тексты ошибок для окна."""
from __future__ import annotations

import ctypes
import os
import re
import shutil
from dataclasses import dataclass
from typing import Optional

from .messages import format_size

DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOTE = 4

PATH_WARN_LENGTH = 110
SPACE_FACTOR = 2.2

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def drive_type(path: str) -> int:
    """Тип диска по GetDriveTypeW. Вне Windows возвращает DRIVE_FIXED (3)."""
    try:
        root = os.path.splitdrive(path)[0] + "\\"
        return int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except Exception:
        return 3


def safe_local_name(name: str) -> str:
    """Имя папки проекта, допустимое в Windows (на сервере имена могут быть любыми)."""
    cleaned = _BAD_NAME_CHARS.sub("_", name).rstrip(" .")
    return cleaned or "project"


@dataclass
class TargetCheck:
    error: Optional[str] = None
    warning: Optional[str] = None
    base: str = ""
    dest: str = ""


NETWORK_MESSAGE = (
    "Нельзя хранить проект на сетевом диске: работа с ним будет медленной и может "
    "ломаться. Выберите папку на локальном диске компьютера, например C:\\Проекты_svn."
)


def check_target(base_dir: str, project_name: str) -> TargetCheck:
    base_dir = base_dir.strip().strip('"')
    if not base_dir:
        return TargetCheck(error="Выберите папку на компьютере (шаг 2).")
    if base_dir.startswith(("\\\\", "//")):
        return TargetCheck(error=NETWORK_MESSAGE)
    if not _DRIVE_RE.match(base_dir):
        return TargetCheck(error=(
            "Укажите полный путь к папке, начиная с буквы диска, например C:\\Проекты_svn."))

    base = os.path.normpath(base_dir)
    dtype = drive_type(base)
    if dtype == DRIVE_REMOTE:
        return TargetCheck(error=NETWORK_MESSAGE)
    if dtype == DRIVE_NO_ROOT_DIR:
        return TargetCheck(error=f"Диск {base[:2]} не найден. Выберите другую папку.")
    if os.path.isfile(base):
        return TargetCheck(error="Выбранный путь — это файл, а не папка. Выберите папку.")

    dest = os.path.join(base, project_name)
    if os.path.isfile(dest):
        return TargetCheck(error=f"По пути {dest} уже лежит файл с таким именем.")
    if os.path.isdir(dest):
        if os.path.isdir(os.path.join(dest, ".svn")):
            return TargetCheck(error=(
                f"Папка {dest} уже содержит скачанный проект. Чтобы получить свежие "
                "изменения, откройте «Мои проекты» → «Получить новое»."))
        try:
            nonempty = bool(os.listdir(dest))
        except OSError as exc:
            return TargetCheck(error=f"Не удалось открыть папку {dest}: {exc.strerror}")
        if nonempty:
            return TargetCheck(error=(
                f"Папка {dest} уже существует и не пуста. Выберите другую папку "
                "или удалите эту."))

    warning = None
    if len(dest) > PATH_WARN_LENGTH:
        warning = (
            f"Путь к проекту очень длинный ({len(dest)} символов). В Windows есть ограничение "
            "в 260 символов на путь к файлу, поэтому часть файлов может не скачаться. "
            "Лучше выбрать более короткую папку, например C:\\P.")
    return TargetCheck(warning=warning, base=base, dest=dest)


def nearest_existing(path: str) -> str:
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def check_free_space(dest: str, project_size: int) -> Optional[str]:
    """Текст ошибки, если на диске не хватает места (нужно 2.2 × размер проекта)."""
    required = project_size * SPACE_FACTOR
    try:
        free = shutil.disk_usage(nearest_existing(dest)).free
    except OSError:
        return None  # не смогли узнать — не блокируем
    if free < required:
        return (f"На диске {os.path.splitdrive(dest)[0]} не хватает места: нужно около "
                f"{format_size(required)}, свободно {format_size(free)}. "
                "Освободите место или выберите другой диск.")
    return None
