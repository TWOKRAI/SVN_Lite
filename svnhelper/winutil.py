"""Мелочи Windows через ctypes: один экземпляр программы, мигание в панели задач, ярлык. Без tkinter.

Вне Windows все функции ничего не делают и возвращают «безопасное» значение.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from typing import Optional

log = logging.getLogger("svnhelper.winutil")

IS_WINDOWS = sys.platform == "win32"
MUTEX_NAME = "Local\\SvnHelper_single"
WINDOW_TITLE_PREFIX = "SVN-lite"
TK_WINDOW_CLASS = "TkTopLevel"       # класс окна верхнего уровня у tkinter

_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9
_GA_ROOT = 2
_FLASHW_ALL = 0x3
_FLASHW_TIMERNOFG = 0xC              # мигать, пока окно не станет активным
_CREATE_NO_WINDOW = 0x08000000

_mutex_handle = None                 # держим ссылку: мьютекс живёт, пока жив процесс


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """True, если мы первый экземпляр (или проверка невозможна). False, если программа уже запущена."""
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, name)
        already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    except Exception:
        log.exception("Не удалось создать мьютекс единственного экземпляра")
        return True
    if already:
        return False
    _mutex_handle = handle
    return True


def release_single_instance() -> None:
    """Освобождает мьютекс: закрывающаяся программа не мешает запуску новой, даже если процесс ещё дожидается потоков."""
    global _mutex_handle
    if not IS_WINDOWS or _mutex_handle is None:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(_mutex_handle)
    except Exception:
        log.exception("Не удалось освободить мьютекс единственного экземпляра")
    _mutex_handle = None


class _FlashInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", wintypes.HWND), ("dwFlags", ctypes.c_uint32),
                ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint32)]


_user32_dll = None


def _user32():
    """Свой экземпляр user32 с описанными типами: глобальный ctypes.windll делится с остальными библиотеками."""
    global _user32_dll
    if _user32_dll is None:
        dll = ctypes.WinDLL("user32", use_last_error=True)
        dll.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                                    wintypes.LPARAM]
        dll.EnumWindows.restype = wintypes.BOOL
        for name in ("GetWindowTextW", "GetClassNameW"):
            func = getattr(dll, name)
            func.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            func.restype = ctypes.c_int
        dll.IsIconic.argtypes = [wintypes.HWND]
        dll.IsIconic.restype = wintypes.BOOL
        dll.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        dll.ShowWindow.restype = wintypes.BOOL
        dll.SetForegroundWindow.argtypes = [wintypes.HWND]
        dll.SetForegroundWindow.restype = wintypes.BOOL
        dll.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        dll.GetAncestor.restype = wintypes.HWND
        dll.FlashWindowEx.argtypes = [ctypes.POINTER(_FlashInfo)]
        dll.FlashWindowEx.restype = wintypes.BOOL
        _user32_dll = dll
    return _user32_dll


def find_window(title_prefix: str = WINDOW_TITLE_PREFIX, window_class: str = TK_WINDOW_CLASS) -> Optional[int]:
    """Дескриптор окна верхнего уровня, заголовок которого начинается с title_prefix (заголовок содержит версию)."""
    if not IS_WINDOWS:
        return None
    user32 = _user32()
    found: list[int] = []
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam) -> bool:
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value.startswith(title_prefix):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == window_class:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(enum_proc_type(visit), 0)
    return found[0] if found else None


def activate_window(hwnd: int) -> None:
    """Разворачивает свёрнутое окно и выводит его на передний план."""
    if not IS_WINDOWS:
        return
    user32 = _user32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def activate_existing_window(title_prefix: str = WINDOW_TITLE_PREFIX, wait_sec: float = 2.0) -> bool:
    """Находит окно уже запущенной программы и активирует его. Первое окно может ещё создаваться: ждём немного."""
    import time
    end = time.monotonic() + wait_sec
    while True:
        hwnd = find_window(title_prefix)
        if hwnd:
            activate_window(hwnd)
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)


def toplevel_handle(widget_id: int) -> int:
    """Дескриптор окна верхнего уровня по winfo_id() виджета tkinter."""
    if not IS_WINDOWS:
        return 0
    return _user32().GetAncestor(widget_id, _GA_ROOT) or widget_id


def flash_window(hwnd: int) -> bool:
    """Мигает кнопкой окна в панели задач, пока пользователь не вернётся в окно."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        info = _FlashInfo(ctypes.sizeof(_FlashInfo), hwnd, _FLASHW_ALL | _FLASHW_TIMERNOFG, 0, 0)
        _user32().FlashWindowEx(ctypes.byref(info))
        return True
    except Exception:
        log.exception("Не удалось мигнуть окном")
        return False


def drive_to_unc(drive: str) -> Optional[str]:
    """Сетевой адрес подключённого диска: «Z:» → «\\\\fs01\\share». None, если диск не сетевой или ошибка."""
    if not IS_WINDOWS or len(drive) < 2 or drive[1] != ":":
        return None
    try:
        mpr = ctypes.WinDLL("mpr")
        func = mpr.WNetGetConnectionW
        func.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        func.restype = wintypes.DWORD
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if func(drive[:2], buf, ctypes.byref(size)) == 0 and buf.value:
            return buf.value
    except Exception:
        log.exception("Не удалось узнать сетевой адрес диска %s", drive[:2])
    return None


def beep() -> None:
    """Короткий системный звук."""
    if not IS_WINDOWS:
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        log.exception("Не удалось проиграть звук")


# PowerShell читает пути из переменных окружения: в текст команды они не попадают,
# поэтому кавычки, апострофы и пробелы в путях ничего не ломают.
_SHORTCUT_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$shell = New-Object -ComObject WScript.Shell; "
    "$link = $shell.CreateShortcut($env:SVNH_LINK); "
    "$link.TargetPath = $env:SVNH_TARGET; "
    "$link.WorkingDirectory = $env:SVNH_WORKDIR; "
    "$link.Description = $env:SVNH_DESC; "
    "$link.Save()"
)


def desktop_dir() -> str:
    """Папка рабочего стола текущего пользователя (спрашиваем у PowerShell: стол может быть перенаправлен)."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [Text.Encoding]::UTF8; [Environment]::GetFolderPath('Desktop')"],
        capture_output=True, timeout=30, creationflags=_CREATE_NO_WINDOW)
    path = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or not path:
        raise OSError("не удалось определить папку рабочего стола")
    return path


def create_shortcut(target: str, link_path: str, description: str = "") -> None:
    """Создаёт ярлык link_path на target через PowerShell (WScript.Shell), без окна консоли. OSError при неудаче."""
    env = dict(os.environ, SVNH_LINK=link_path, SVNH_TARGET=target,
               SVNH_WORKDIR=os.path.dirname(target), SVNH_DESC=description)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
             _SHORTCUT_SCRIPT],
            capture_output=True, timeout=30, env=env, creationflags=_CREATE_NO_WINDOW)
    except subprocess.SubprocessError as exc:
        raise OSError(f"PowerShell не ответил: {exc}") from exc
    if proc.returncode != 0 or not os.path.isfile(link_path):
        details = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
        raise OSError(f"PowerShell не создал ярлык: {details[:300]}")


def create_desktop_shortcut(name: str = "SVN-lite") -> str:
    """Ярлык программы на рабочем столе. Только для собранной программы (exe). Возвращает путь к ярлыку."""
    if not getattr(sys, "frozen", False):
        raise OSError("ярлык создаётся только для собранной программы (SVN-lite.exe)")
    link = os.path.join(desktop_dir(), name + ".lnk")
    create_shortcut(sys.executable, link, name)
    return link
