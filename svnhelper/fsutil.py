"""Безопасное удаление недокачанной папки проекта. Без tkinter; вызывать только из фонового потока."""
from __future__ import annotations

import logging
import os
import shutil
import stat
import sys

log = logging.getLogger("svnhelper.fsutil")


def is_inside(path: str, base: str) -> bool:
    """True, если path лежит строго внутри base (сам base не считается)."""
    try:
        a = os.path.normcase(os.path.realpath(path))
        b = os.path.normcase(os.path.realpath(base))
        return a != b and os.path.commonpath([a, b]) == b
    except ValueError:  # разные диски
        return False


def _is_reparse_point(path: str) -> bool:
    """Ссылка любого вида: symlink или junction (reparse point). os.path.isjunction есть только с Python 3.12."""
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


_RETRY_FUNCS = (os.unlink, os.remove, os.rmdir)


def _make_writable_and_retry(func, path, exc: BaseException) -> None:
    """Файлы «только для чтения» (например, под Get lock) не удаляются: снимаем флаг и повторяем.

    Повторяем только удаление и только после отказа в доступе; любую другую ошибку пробрасываем.
    """
    if func in _RETRY_FUNCS and isinstance(exc, PermissionError):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(path)
        return
    raise exc


def remove_partial_dir(dest: str, base: str) -> None:
    """Удаляет dest вместе с содержимым. Бросает ValueError, если удалять нельзя, и OSError при сбое.

    Защита: dest обязана лежать внутри base и не быть ссылкой (symlink/junction).
    """
    if not is_inside(dest, base):
        raise ValueError(f"{dest} лежит не внутри {base}")
    if os.path.islink(dest) or _is_reparse_point(dest):
        raise ValueError(f"{dest} — ссылка на другую папку")
    if not os.path.isdir(dest):
        return
    log.info("Удаляю недокачанную папку %s", dest)
    if sys.version_info >= (3, 12):
        shutil.rmtree(dest, onexc=lambda func, path, exc: _make_writable_and_retry(func, path, exc))
    else:
        shutil.rmtree(dest, onerror=lambda func, path, info: _make_writable_and_retry(func, path, info[1]))
    if os.path.lexists(dest):  # rmtree мог молча пропустить часть файлов: «удалено» не говорим
        raise OSError(f"Папка {dest} удалена не полностью")
