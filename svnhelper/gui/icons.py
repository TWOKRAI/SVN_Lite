"""Значки кнопок колонок (docs/PLAN_ui_v4.md, раздел 11): символы шрифта Windows, нарисованные в картинку.

Значок кнопки ищется по её ключу пояснения (hints.HINTS) в ICONS. Рисуется символом шрифта «Segoe MDL2 Assets»;
нет шрифта или символа в нём - запасным символом из FALLBACK шрифтом «Segoe UI Symbol»; нет и его - первой буквой
подписи в кружке. Картинок в программе нет: значок рисует Windows (GDI) в памяти, из него получается PNG с
прозрачностью, а из PNG - tk.PhotoImage. Так у кнопки остаются её подпись (cget("text")) и стиль, а значок стоит
над подписью (compound="top") или один (compound="image") и цветом совпадает с текстом кнопки в текущей теме.
Размер значка задаёт вызывающий в пикселях экрана (через px), поэтому он растёт вместе с масштабом Windows.

Не на Windows (или если GDI недоступен) render() возвращает None: кнопки остаются с подписями.
"""
from __future__ import annotations

import base64
import ctypes
import struct
import sys
import tkinter as tk
import zlib
from dataclasses import dataclass
from typing import Optional

ICON_FONT = "Segoe MDL2 Assets"
FALLBACK_FONT = "Segoe UI Symbol"
LETTER_FONT = "Segoe UI"

# ключ пояснения кнопки (hints.HINTS) -> символ шрифта Segoe MDL2 Assets (имя символа в справочнике Microsoft)
ICONS: dict[str, str] = {
    # колонка слева: проект целиком
    "project.check": "\uE72C",          # Refresh
    "project.resolve": "\uE7BA",        # Warning
    "project.repair": "\uE90F",         # Repair (гаечный ключ)
    "project.restore": "\uE81C",        # History
    "project.get_new": "\uE896",        # Download
    "project.publish": "\uE898",        # Upload
    "project.continue": "\uE72A",       # Forward
    "project.open": "\uED25",           # OpenFolderHorizontal (открытая папка)
    "project.server": "\uE8CE",         # MapDrive (сетевой диск)
    "project.more": "\uE712",           # More (…)
    "project.new": "\uE710",            # Add (+)
    # «Изменения»
    "changes.save": "\uE74E",           # Save
    "changes.save_local": "\uE78C",     # SaveLocal
    "changes.template": "\uE90A",       # Comment (облачко с текстом)
    "changes.recent": "\uE823",         # Recent (часы)
    # «Занятые»
    "locks.take": "\uE72E",             # Lock
    "locks.release": "\uE785",          # Unlock
    # «Ветки»
    "lines.switch": "\uE8AB",           # Switch
    "lines.compare": "\uE89A",          # TwoPage
    "lines.merge": "\uE8B5",            # Import
    "lines.new_branch": "\uF003",       # Relationship (развилка из узлов)
    "lines.download_other": "\uE8A7",   # OpenInNewWindow
    "lines.new_tag": "\uE8EC",          # Tag
    # форма «Скачивание»
    "download.start": "\uE896",         # Download
    "download.create": "\uE8F4",        # NewFolder (папка с плюсом)
    "download.cleanup": "\uE74D",       # Delete
}

# запасные символы Юникода для шрифта Segoe UI Symbol (если Segoe MDL2 Assets нет, как в Windows 7)
FALLBACK: dict[str, str] = {
    "project.check": "⟳", "project.resolve": "⚠", "project.repair": "⚒", "project.restore": "↺",
    "project.get_new": "⬇", "project.publish": "⬆", "project.continue": "➜", "project.open": "⌂",
    "project.server": "⛁", "project.more": "⋯", "project.new": "✚",
    "changes.save": "✔", "changes.save_local": "✔", "changes.template": "✎", "changes.recent": "⌚",
    "locks.take": "⚿", "locks.release": "⚿",
    "lines.switch": "⇄", "lines.compare": "⇔", "lines.merge": "⤵", "lines.new_branch": "⑂",
    "lines.download_other": "⇩", "lines.new_tag": "⚑",
    "download.start": "⬇", "download.create": "✚", "download.cleanup": "✖",
}


@dataclass(frozen=True)
class Glyph:
    """Что рисовать: символ text шрифтом font; circle - обвести кружком (запасной вид: буква подписи)."""
    text: str
    font: str
    circle: bool = False


def glyph_for(key: Optional[str], label: str = "") -> Optional[Glyph]:
    """Чем рисовать значок кнопки с ключом key: символом MDL2, запасным символом или буквой подписи в кружке.
    None - нечего рисовать (нет ни ключа, ни подписи)."""
    if key in ICONS and has_glyph(ICON_FONT, ICONS[key]):
        return Glyph(ICONS[key], ICON_FONT)
    if key in FALLBACK and has_glyph(FALLBACK_FONT, FALLBACK[key]):
        return Glyph(FALLBACK[key], FALLBACK_FONT)
    letter = next((ch for ch in label if ch.isalnum()), "")
    return Glyph(letter.upper(), LETTER_FONT, circle=True) if letter else None


# --------------------------------------------------------------------------
# Рисование символа средствами Windows (GDI)
# --------------------------------------------------------------------------

_WINDOWS = sys.platform == "win32"


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32),
                ("bottom", ctypes.c_int32)]


_gdi = None


def _gdi32():
    """Функции GDI с объявленными типами (на 64-битной Windows без этого ручки обрезаются до 32 бит)."""
    global _gdi
    if _gdi is not None or not _WINDOWS:
        return _gdi
    from ctypes import wintypes
    gdi32, user32 = ctypes.WinDLL("gdi32"), ctypes.WinDLL("user32")
    handle = wintypes.HANDLE
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [handle], handle
    gdi32.DeleteDC.argtypes = [handle]
    gdi32.CreateDIBSection.argtypes = [handle, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p),
                                       handle, ctypes.c_uint32]
    gdi32.CreateDIBSection.restype = handle
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [ctypes.c_uint32] * 8 + [wintypes.LPCWSTR]
    gdi32.CreateFontW.restype = handle
    gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [handle, handle], handle
    gdi32.DeleteObject.argtypes = [handle]
    gdi32.SetBkMode.argtypes = [handle, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [handle, ctypes.c_uint32]
    gdi32.GetTextFaceW.argtypes = [handle, ctypes.c_int, wintypes.LPWSTR]
    gdi32.GetGlyphIndicesW.argtypes = [handle, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(ctypes.c_uint16),
                                       ctypes.c_uint32]
    gdi32.GetGlyphIndicesW.restype = ctypes.c_uint32
    gdi32.GdiFlush.argtypes = []
    user32.DrawTextW.argtypes = [handle, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(_Rect), ctypes.c_uint]
    _gdi = (gdi32, user32)
    return _gdi


_FW_NORMAL, _FW_BOLD = 400, 700
_DEFAULT_CHARSET, _ANTIALIASED_QUALITY = 1, 4
_TRANSPARENT = 1
_DT_CENTER, _DT_VCENTER, _DT_SINGLELINE, _DT_NOPREFIX = 0x1, 0x4, 0x20, 0x800
_GGI_MARK_NONEXISTING_GLYPHS = 1

_glyph_known: dict[tuple[str, str], bool] = {}


class _Canvas:
    """Память под картинку w x h (32 бита на точку) с выбранным в неё шрифтом; закрывать через close()."""

    def __init__(self, width: int, height: int, font: str, size: int, bold: bool = False):
        gdi32, _user32 = _gdi32()
        self.width, self.height = width, height
        self.dc = gdi32.CreateCompatibleDC(None)
        header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        self.bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(self.dc, ctypes.byref(header), 0, ctypes.byref(self.bits), None, 0)
        self.old_bitmap = gdi32.SelectObject(self.dc, self.bitmap)
        self.font = gdi32.CreateFontW(-size, 0, 0, 0, _FW_BOLD if bold else _FW_NORMAL, 0, 0, 0, _DEFAULT_CHARSET,
                                      0, 0, _ANTIALIASED_QUALITY, 0, font)
        self.old_font = gdi32.SelectObject(self.dc, self.font)
        ctypes.memset(self.bits, 0, width * height * 4)

    def face(self) -> str:
        buffer = ctypes.create_unicode_buffer(64)
        _gdi32()[0].GetTextFaceW(self.dc, 64, buffer)
        return buffer.value

    def has(self, text: str) -> bool:
        indices = (ctypes.c_uint16 * len(text))()
        done = _gdi32()[0].GetGlyphIndicesW(self.dc, text, len(text), indices, _GGI_MARK_NONEXISTING_GLYPHS)
        return done != 0xFFFFFFFF and all(index != 0xFFFF for index in indices)

    def draw(self, text: str, left: int, top: int, right: int, bottom: int) -> None:
        gdi32, user32 = _gdi32()
        gdi32.SetBkMode(self.dc, _TRANSPARENT)
        gdi32.SetTextColor(self.dc, 0x00FFFFFF)
        rect = _Rect(left, top, right, bottom)
        user32.DrawTextW(self.dc, text, -1, ctypes.byref(rect), _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE | _DT_NOPREFIX)
        gdi32.GdiFlush()

    def coverage(self) -> list[float]:
        """Насколько закрашена каждая точка (0..1): белый текст на чёрном, берётся синий канал."""
        raw = ctypes.string_at(self.bits, self.width * self.height * 4)
        return [raw[i] / 255 for i in range(0, len(raw), 4)]

    def close(self) -> None:
        gdi32, _user32 = _gdi32()
        gdi32.SelectObject(self.dc, self.old_font)
        gdi32.DeleteObject(self.font)
        gdi32.SelectObject(self.dc, self.old_bitmap)
        gdi32.DeleteObject(self.bitmap)
        gdi32.DeleteDC(self.dc)


def available() -> bool:
    """Можно ли рисовать значки (Windows и GDI на месте)."""
    try:
        return _gdi32() is not None
    except (OSError, AttributeError):
        return False


def has_glyph(font: str, text: str) -> bool:
    """Есть ли в шрифте font (установленном в системе) символ text."""
    known = _glyph_known.get((font, text))
    if known is not None:
        return known
    result = False
    if available():
        canvas = _Canvas(1, 1, font, 16)
        try:
            result = canvas.face().lower() == font.lower() and canvas.has(text)
        finally:
            canvas.close()
    _glyph_known[(font, text)] = result
    return result


def _text_coverage(text: str, font: str, em: int, size: tuple[int, int], box: tuple[int, int, int, int],
                   bold: bool = False) -> list[float]:
    canvas = _Canvas(size[0], size[1], font, em, bold)
    try:
        canvas.draw(text, *box)
        return canvas.coverage()
    finally:
        canvas.close()


def _disc(width: int, height: int, cx: float, cy: float, radius: float, inner: float = -1.0) -> list[float]:
    """Сглаженный круг (или кольцо от inner до radius) с центром (cx, cy): доля закрашенной площади точек."""
    steps = 4
    result = []
    for y in range(height):
        for x in range(width):
            hit = 0
            for sy in range(steps):
                for sx in range(steps):
                    dx = x + (sx + 0.5) / steps - cx
                    dy = y + (sy + 0.5) / steps - cy
                    dist = (dx * dx + dy * dy) ** 0.5
                    if inner < dist <= radius:
                        hit += 1
            result.append(hit / (steps * steps))
    return result


def coverage(glyph: Glyph, size: int, badge: str = "") -> list[float]:
    """Значок size x size: насколько закрашена каждая точка (0..1). Символ всегда одного размера и в одном месте
    ячейки (по центру, по метрикам шрифта): значки разных кнопок стоят одинаково. badge - число в кружке в правом
    верхнем углу ячейки (счётчик «Получить новое (N)»): кружок ложится поверх символа с зазором, сам символ не
    сдвигается и не уменьшается."""
    em = round(size * (0.62 if glyph.circle else 0.8))
    cov = _text_coverage(glyph.text, glyph.font, em, (size, size), (0, 0, size, size), bold=glyph.circle)
    if glyph.circle:
        ring = _disc(size, size, size / 2, size / 2, size / 2 - 0.5, size / 2 - 0.5 - max(1.0, size / 14))
        cov = [max(a, b) for a, b in zip(cov, ring)]
    if not badge:
        return cov
    badge = badge if len(badge) <= 2 else "99"
    radius = size * (0.3 if len(badge) == 1 else 0.36)
    cx, cy = size - radius - 0.2, radius + 0.2
    gap = _disc(size, size, cx, cy, radius + max(1.0, size / 16))
    disc = _disc(size, size, cx, cy, radius)
    number = _text_coverage(badge, LETTER_FONT, max(6, round(radius * 1.45)), (size, size),
                            (round(cx - radius), round(cy - radius) - 1, round(cx + radius), round(cy + radius) - 1),
                            bold=True)
    return [max(g * (1 - h), d * (1 - n)) for g, h, d, n in zip(cov, gap, disc, number)]


# --------------------------------------------------------------------------
# PNG и tk.PhotoImage
# --------------------------------------------------------------------------

def _png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    stride = width * 4
    rows = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def _rgb(widget: tk.Misc, color: str) -> tuple[int, int, int]:
    red, green, blue = widget.winfo_rgb(color)
    return red >> 8, green >> 8, blue >> 8


_coverage_cache: dict[tuple, list[float]] = {}


def render(widget: tk.Misc, glyph: Optional[Glyph], size: int, color: str, badge: str = "",
           extra: int = 0) -> Optional[tk.PhotoImage]:
    """Картинка значка цвета color для окна widget (кэшируется в корневом окне); None - рисовать нечем.
    extra - прозрачное поле справа от ячейки значка (картинка шире на extra точек, значок на прежнем месте)."""
    if glyph is None or size < 4 or not available():
        return None
    root = widget._root()          # картинка принадлежит интерпретатору окна: у каждого Tk() свой кэш
    cache: dict = root.__dict__.setdefault("_icon_images", {})
    extra = max(0, extra)
    key = (glyph, size, color, badge, extra)
    image = cache.get(key)
    if image is not None:
        return image
    shape_key = (glyph, size, badge)
    cov = _coverage_cache.get(shape_key)
    if cov is None:
        cov = _coverage_cache[shape_key] = coverage(glyph, size, badge)
    red, green, blue = _rgb(widget, color)
    blank = bytes((red, green, blue, 0)) * extra
    rows = []
    for y in range(size):
        row = cov[y * size:(y + 1) * size]
        rows.append(b"".join(bytes((red, green, blue, round(255 * min(1.0, c)))) for c in row) + blank)
    image = tk.PhotoImage(master=root, data=base64.b64encode(_png(size + extra, size, b"".join(rows))), format="png")
    cache[key] = image
    return image
