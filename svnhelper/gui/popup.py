"""Всплывающие окошки уведомлений в правом нижнем углу экрана (Task E.2).

Не системные toast-уведомления Windows, а свои безрамочные окна: поверх остальных, над панелью
задач, без кражи фокуса. Расчёт мест (stack_positions) отделён от tkinter и проверяется тестами.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk
from typing import Callable, Optional

from .. import winutil
from ..dialogs import FONT_FAMILY
from ..notifier import KIND_INFO, Notice

log = logging.getLogger("svnhelper.popup")

MAX_VISIBLE = 4            # больше окошек на экране не складываем: остальные ждут очереди
WIDTH_PX = 360             # при масштабе 100%; дальше умножается на px() окна
MARGIN_PX = 12             # отступ от края рабочей области
GAP_PX = 8                 # между окошками в стопке

_SPI_GETWORKAREA = 0x0030
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080

Area = tuple[int, int, int, int]      # левый, верхний, правый, нижний край (пиксели экрана)


def work_area(root: tk.Misc) -> Area:
    """Рабочая область основного монитора: экран без панели задач, где бы она ни стояла.
    Не Windows или ошибка - весь экран без полосы под панель задач (запас внизу)."""
    if sys.platform == "win32":
        try:
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            log.exception("Не удалось узнать рабочую область экрана")
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 48


def stack_positions(area: Area, heights: list[int], width: int, margin: int, gap: int) -> list[tuple[int, int]]:
    """Левый верхний угол каждого окошка стопки. Окошка width шириной прижаты к правому краю рабочей области.
    Первое (самое старое) стоит внизу справа, следующие выше.
    Если стопка не помещается по высоте, лишние окошки не уходят за верхний край экрана: они прижимаются к нему."""
    _left, top, right, bottom = area
    x = right - margin - width
    y = bottom - margin
    result = []
    for height in heights:
        y -= height
        result.append((x, max(y, top)))
        y -= gap
    return result


def _no_activate(window: tk.Misc) -> None:
    """Окошко не получает фокус и не имеет кнопки в панели задач, даже когда на него нажимают."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        hwnd = winutil.toplevel_handle(window.winfo_id())
        style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
    except Exception:
        log.exception("Не удалось сделать окошко уведомления неактивным")


class NoticePopup(tk.Toplevel):
    """Одно окошко: заголовок, текст, кнопки «Открыть» и «Позже», крестик, автоскрытие."""

    def __init__(self, root: tk.Misc, notice: Notice, palette, px: Callable[[int], int], seconds: int,
                 on_open: Callable[[Notice], None], on_later: Callable[[Notice], None],
                 on_closed: Callable[["NoticePopup"], None]):
        super().__init__(root, background=palette.border_strong)
        self.notice = notice
        self._on_closed = on_closed
        self._timer: Optional[str] = None
        self._seconds = seconds
        self._closed = False
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        width = px(WIDTH_PX)
        body = tk.Frame(self, background=palette.surface, padx=px(12), pady=px(10))
        body.pack(fill="both", expand=True, padx=1, pady=1)       # рамка в 1 пиксель цветом фона окна
        head = tk.Frame(body, background=palette.surface)
        head.pack(fill="x")
        close = tk.Label(head, text="✕", background=palette.surface, foreground=palette.muted, cursor="hand2",
                         font=(FONT_FAMILY, 10))
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self.close())
        tk.Label(head, text=notice.title, background=palette.surface, foreground=palette.text, anchor="w",
                 font=(FONT_FAMILY, 10, "bold")).pack(side="left", fill="x", expand=True)
        tk.Label(body, text=notice.text, background=palette.surface, foreground=palette.text, justify="left",
                 anchor="w", wraplength=width - px(36), font=(FONT_FAMILY, 10)).pack(fill="x", pady=(4, 8))
        buttons = tk.Frame(body, background=palette.surface)
        buttons.pack(anchor="w")
        if notice.kind == KIND_INFO:
            ttk.Button(buttons, text="Закрыть", command=self.close).pack(side="left")
        else:
            ttk.Button(buttons, text="Открыть", style="Accent.TButton",
                       command=lambda: self._act(on_open)).pack(side="left", padx=(0, px(8)))
            ttk.Button(buttons, text="Позже", command=lambda: self._act(on_later)).pack(side="left")

        self.update_idletasks()
        self.size = (width, self.winfo_reqheight())
        self.geometry(f"{width}x{self.size[1]}")
        self.bind("<Enter>", lambda _e: self._cancel_timer())      # курсор над окошком: не прячем
        self.bind("<Leave>", lambda _e: self._start_timer())

    @property
    def height(self) -> int:
        return self.size[1]

    def place_at(self, x: int, y: int) -> None:
        self.geometry(f"{self.size[0]}x{self.size[1]}+{x}+{y}")

    def display(self) -> None:
        """Показывает окошко, не передавая ему фокус."""
        _no_activate(self)
        self.deiconify()
        self.attributes("-topmost", True)
        self._start_timer()

    def _start_timer(self) -> None:
        self._cancel_timer()
        if self._seconds > 0 and not self._closed:
            self._timer = self.after(self._seconds * 1000, self.close)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None

    def _act(self, callback: Callable[[Notice], None]) -> None:
        self.close()
        callback(self.notice)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_timer()
        self._on_closed(self)
        self.destroy()


class PopupStack:
    """Стопка окошек в правом нижнем углу: не больше MAX_VISIBLE сразу, остальные в очереди."""

    def __init__(self, root: tk.Misc, get_palette: Callable, px: Callable[[int], int],
                 on_open: Callable[[Notice], None], on_later: Callable[[Notice], None]):
        self._root = root
        self._get_palette = get_palette
        self._px = px
        self._on_open = on_open
        self._on_later = on_later
        self.visible: list[NoticePopup] = []
        self.waiting: list[tuple[Notice, int]] = []
        self._closing = False

    def show(self, notice: Notice, seconds: int) -> None:
        """Ставит уведомление в очередь и показывает, как только есть место. seconds=0 - пока не закроют."""
        if any(p.notice.key == notice.key for p in self.visible) or any(n.key == notice.key for n, _ in self.waiting):
            return      # такое окошко уже на экране или ждёт очереди
        self.waiting.append((notice, seconds))
        self._pump()

    def close_all(self) -> None:
        """Закрывает всё (выход из программы): очередь больше не разбирается."""
        self._closing = True
        self.waiting.clear()
        for popup in list(self.visible):
            popup.close()

    def _pump(self) -> None:
        while self.waiting and len(self.visible) < MAX_VISIBLE:
            notice, seconds = self.waiting.pop(0)
            popup = NoticePopup(self._root, notice, self._get_palette(), self._px, seconds,
                                self._on_open, self._on_later, self._closed)
            self.visible.append(popup)
            self._layout()
            popup.display()

    def _closed(self, popup: NoticePopup) -> None:
        if popup in self.visible:
            self.visible.remove(popup)
        self._layout()
        if not self._closing:
            self._root.after_idle(self._pump)

    def _layout(self) -> None:
        area = work_area(self._root)
        places = stack_positions(area, [p.height for p in self.visible], self._px(WIDTH_PX), self._px(MARGIN_PX),
                                 self._px(GAP_PX))
        for popup, (x, y) in zip(self.visible, places):
            popup.place_at(x, y)
