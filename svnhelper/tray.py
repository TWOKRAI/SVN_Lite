"""Значок в области уведомлений Windows (Shell_NotifyIcon через ctypes, без сторонних библиотек). Без tkinter.

Значок живёт в своём потоке: у него невидимое окно и цикл сообщений Windows. В потоке tkinter ничего не
вызывается: о щелчках и выборе в меню значок сообщает функцией on_event(имя_события), а вызывающий код
сам передаёт событие в главный поток (App.post). Вне Windows Tray.start() просто возвращает False.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes
from typing import Callable, Optional, Sequence

log = logging.getLogger("svnhelper.tray")

IS_WINDOWS = sys.platform == "win32"

EVENT_OPEN = "open"
EVENT_QUIT = "quit"
EVENT_CHECK = "check"          # «Проверить сейчас»: фоновая проверка нового и несохранённых правок
EVENT_SNOOZE = "snooze"        # «Не беспокоить»: пауза уведомлений (повторный выбор снимает её)
DEFAULT_MENU: tuple[tuple[str, str], ...] = (
    (EVENT_OPEN, "Открыть"), (EVENT_CHECK, "Проверить сейчас"),
    (EVENT_SNOOZE, "Не беспокоить 2 часа"), (EVENT_QUIT, "Выйти"))
TOOLTIP = "SVN-lite"
WINDOW_CLASS = "SvnLiteTrayWindow"

_WM_NULL = 0x0000
_WM_APP = 0x8000
_WM_TRAY = _WM_APP + 1             # сообщение значка (щелчки мыши)
_WM_OPEN_REQUEST = _WM_APP + 2     # «покажи окно»: так просит второй запуск программы
_WM_STOP = _WM_APP + 3
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205

_NIM_ADD, _NIM_DELETE = 0, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 0x1, 0x2, 0x4
_MF_STRING = 0x0
_TPM_RIGHTBUTTON, _TPM_RETURNCMD = 0x0002, 0x0100
_IDI_APPLICATION = 32512
_WS_POPUP = 0x80000000


class _NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HANDLE),
    ]


_WndProcType = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WndClass(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", _WndProcType), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HANDLE), ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_user32_dll = None
_shell32_dll = None


def _user32():
    global _user32_dll
    if _user32_dll is None:
        dll = ctypes.WinDLL("user32", use_last_error=True)
        dll.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        dll.DefWindowProcW.restype = ctypes.c_ssize_t
        dll.RegisterClassW.argtypes = [ctypes.POINTER(_WndClass)]
        dll.RegisterClassW.restype = wintypes.ATOM
        dll.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                        wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        dll.CreateWindowExW.restype = wintypes.HWND
        dll.DestroyWindow.argtypes = [wintypes.HWND]
        dll.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        dll.GetMessageW.restype = ctypes.c_int
        dll.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        dll.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        dll.DispatchMessageW.restype = ctypes.c_ssize_t
        dll.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        dll.PostMessageW.restype = wintypes.BOOL
        dll.PostQuitMessage.argtypes = [ctypes.c_int]
        dll.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        dll.FindWindowW.restype = wintypes.HWND
        dll.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        dll.RegisterWindowMessageW.restype = wintypes.UINT
        dll.CreatePopupMenu.restype = wintypes.HMENU
        dll.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        dll.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.LPVOID]
        dll.TrackPopupMenu.restype = wintypes.UINT
        dll.DestroyMenu.argtypes = [wintypes.HMENU]
        dll.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
        dll.SetForegroundWindow.argtypes = [wintypes.HWND]
        dll.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        dll.LoadIconW.restype = wintypes.HANDLE
        _user32_dll = dll
    return _user32_dll


def _shell32():
    global _shell32_dll
    if _shell32_dll is None:
        dll = ctypes.WinDLL("shell32", use_last_error=True)
        dll.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NotifyIconData)]
        dll.Shell_NotifyIconW.restype = wintypes.BOOL
        dll.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
        dll.ExtractIconW.restype = wintypes.HANDLE
        _shell32_dll = dll
    return _shell32_dll


_instances: dict[int, "Tray"] = {}      # окно значка -> значок (оконная процедура одна на весь процесс)


def _wnd_proc(hwnd, msg, wparam, lparam):
    tray = _instances.get(hwnd)
    try:
        if tray is not None and tray._handle_message(msg, wparam, lparam):
            return 0
    except Exception:
        log.exception("Ошибка в обработчике сообщений значка в трее")
    return _user32().DefWindowProcW(hwnd, msg, wparam, lparam)


_WND_PROC = _WndProcType(_wnd_proc) if IS_WINDOWS else None     # ссылка живёт всё время работы процесса


class Tray:
    """Значок в трее с меню. Создаётся один на программу.

    items: пункты меню (событие, подпись). on_event вызывается из потока значка, а не из главного.
    """

    def __init__(self, on_event: Callable[[str], None], items: Sequence[tuple[str, str]] = DEFAULT_MENU,
                 tooltip: str = TOOLTIP, labels: Optional[Callable[[], dict[str, str]]] = None):
        self._on_event = on_event
        self._labels = labels          # подписи пунктов на момент показа меню: {событие: текст} (из потока значка)
        self._items = list(items)
        self._tooltip = tooltip
        self._hwnd = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._icon = None
        self._taskbar_created = 0

    @property
    def active(self) -> bool:
        return bool(self._hwnd)

    def start(self, timeout: float = 3.0) -> bool:
        """Показывает значок. True, если он появился (False: не Windows или не получилось, подробности в журнале)."""
        if not IS_WINDOWS or self._thread is not None:
            return self.active
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.active

    def stop(self) -> None:
        """Убирает значок и останавливает его поток."""
        thread, hwnd = self._thread, self._hwnd
        if thread is None:
            return
        if hwnd:
            _user32().PostMessageW(hwnd, _WM_STOP, 0, 0)
        thread.join(2.0)
        self._thread = None

    # -- поток значка --------------------------------------------------------

    def _run(self) -> None:
        try:
            self._create_window()
        except Exception:
            log.exception("Не удалось создать значок в трее")
        finally:
            self._ready.set()
        if not self._hwnd:
            return
        msg = wintypes.MSG()
        user32 = _user32()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        _instances.pop(self._hwnd, None)
        self._hwnd = 0

    def _create_window(self) -> None:
        user32 = _user32()
        cls = _WndClass()
        cls.lpfnWndProc = _WND_PROC
        cls.lpszClassName = WINDOW_CLASS
        if not user32.RegisterClassW(ctypes.byref(cls)) and ctypes.get_last_error() != 1410:   # 1410: уже есть
            raise OSError(ctypes.get_last_error(), "RegisterClassW")
        # обычное окно верхнего уровня, которое никогда не показывается: так его находит FindWindow
        # и оно получает общие сообщения системы (например, «панель задач перезапущена»)
        hwnd = user32.CreateWindowExW(0, WINDOW_CLASS, TOOLTIP, _WS_POPUP, 0, 0, 0, 0, None, None, None, None)
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW")
        _instances[hwnd] = self
        self._hwnd = hwnd
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._icon = _load_icon()
        if not self._add_icon():
            _instances.pop(hwnd, None)
            self._hwnd = 0
            user32.DestroyWindow(hwnd)
            raise OSError("Shell_NotifyIcon не добавил значок")

    def _data(self) -> _NotifyIconData:
        data = _NotifyIconData()
        data.cbSize = ctypes.sizeof(_NotifyIconData)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        data.uCallbackMessage = _WM_TRAY
        data.hIcon = self._icon
        data.szTip = self._tooltip[:127]
        return data

    def _add_icon(self) -> bool:
        return bool(_shell32().Shell_NotifyIconW(_NIM_ADD, ctypes.byref(self._data())))

    def _handle_message(self, msg: int, wparam: int, lparam: int) -> bool:
        if msg == _WM_TRAY:
            event = lparam & 0xFFFF
            if event in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                self._emit(EVENT_OPEN)
            elif event == _WM_RBUTTONUP:
                self._show_menu()
            return True
        if msg == _WM_OPEN_REQUEST:
            self._emit(EVENT_OPEN)
            return True
        if msg == _WM_STOP:
            _shell32().Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._data()))
            _user32().DestroyWindow(self._hwnd)
            _user32().PostQuitMessage(0)
            return True
        if self._taskbar_created and msg == self._taskbar_created:
            self._add_icon()          # проводник перезапустился: значки надо вернуть
            return True
        return False

    def menu_items(self) -> list[tuple[str, str]]:
        """Пункты меню с актуальными подписями (например, «Не беспокоить: включено до 15:30 - выключить»)."""
        try:
            overrides = self._labels() if self._labels else {}
        except Exception:
            log.exception("Не удалось получить подписи меню значка")
            overrides = {}
        return [(event, overrides.get(event, label)) for event, label in self._items]

    def _show_menu(self) -> None:
        user32 = _user32()
        menu = user32.CreatePopupMenu()
        items = self.menu_items()
        try:
            for index, (_event, label) in enumerate(items, start=1):
                user32.AppendMenuW(menu, _MF_STRING, index, label)
            point = _Point()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self._hwnd)      # без этого меню не закрывается щелчком мимо
            chosen = user32.TrackPopupMenu(menu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD, point.x, point.y, 0,
                                           self._hwnd, None)
            user32.PostMessageW(self._hwnd, _WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
        if 1 <= chosen <= len(items):
            self._emit(items[chosen - 1][0])

    def _emit(self, event: str) -> None:
        try:
            self._on_event(event)
        except Exception:
            log.exception("Ошибка при обработке события значка «%s»", event)


def _load_icon():
    """Значок программы из exe (собранная версия) или стандартный значок приложения."""
    if getattr(sys, "frozen", False):
        icon = _shell32().ExtractIconW(None, os.path.abspath(sys.executable), 0)
        if icon and icon != 1:
            return icon
    return _user32().LoadIconW(None, ctypes.c_void_p(_IDI_APPLICATION))


def notify_running_instance() -> bool:
    """Просит уже запущенную программу показать окно (она может быть скрыта в трее). False, если значка нет."""
    if not IS_WINDOWS:
        return False
    try:
        user32 = _user32()
        hwnd = user32.FindWindowW(WINDOW_CLASS, None)
        return bool(hwnd) and bool(user32.PostMessageW(hwnd, _WM_OPEN_REQUEST, 0, 0))
    except Exception:
        log.exception("Не удалось попросить запущенную программу показать окно")
        return False
