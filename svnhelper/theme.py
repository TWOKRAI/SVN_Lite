"""Оформление окна: светлая и тёмная палитры поверх ttk 'clam'. Только stdlib.

theme.PALETTE всегда указывает на палитру, применённую последним вызовом apply():
читайте её как `theme.PALETTE.surface` в момент построения виджета, а не копируйте при импорте.
"""
from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

FONT_FAMILY = "Segoe UI"
ARROW_FONT_SIZE = 19        # стрелка сворачивания «Состояния проекта» (обычный шрифт кнопок - 10)
ARROW_SMALL_FONT_SIZE = 16  # стрелка сворачивания «Проектов»: на 15% мельче первой
SEMIBOLD_FAMILY = "Segoe UI Semibold"
STATUS_DOT = "●"

MODES = ("system", "light", "dark")          # «Как в Windows», «Светлая», «Тёмная»
MODE_LABELS = {"system": "Как в Windows", "light": "Светлая", "dark": "Тёмная"}


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str                 # фон окна и полосы вкладок
    surface: str            # страницы и карточки
    surface_alt: str        # чередование строк, шапки таблиц
    border: str
    border_strong: str
    text: str
    muted: str
    accent: str             # спокойный голубой: заметен, но не кричит
    accent_hover: str
    accent_pressed: str
    accent_soft: str        # выделенная строка
    accent_disabled: str
    on_accent: str          # текст на акцентной кнопке
    field: str              # фон полей ввода
    button: str
    button_hover: str
    button_pressed: str
    disabled_text: str
    success: str
    warning: str
    danger: str
    frozen: str             # версии (теги)
    warn_bg: str            # жёлтая полоса-напоминание
    warn_border: str
    button_disabled: str    # фон неактивной кнопки: светлее панели в тёмной теме и темнее — в светлой, но мягче обычной
    accent_text: str        # синий для мелкого текста (вкладка, ссылка): контраст ≥ 4.5 к панели; accent — для заливок
    on_accent_disabled: str  # текст на неактивной акцентной кнопке


LIGHT = Palette(
    dark=False,
    bg="#DFE3E8", surface="#F1F3F5", surface_alt="#E8EBEF", border="#CBD1D9", border_strong="#A3ABB6",
    text="#151B26", muted="#475160",
    accent="#3F6EA8", accent_hover="#365F95", accent_pressed="#2E5081", accent_soft="#CCDCEE",
    accent_disabled="#7E96B6", on_accent="#FFFFFF",
    field="#FAFBFC", button="#D6DBE1", button_hover="#CBD1D9", button_pressed="#BDC5CE",
    disabled_text="#6F7782",
    success="#1F6B3F", warning="#8A4F0A", danger="#A12C2C", frozen="#4750A6",
    warn_bg="#F5E8BE", warn_border="#D9BB66",
    button_disabled="#E0E3E7", accent_text="#2F5F9A", on_accent_disabled="#FFFFFF",
)

DARK = Palette(
    dark=True,
    bg="#121519", surface="#1B1F24", surface_alt="#20252A", border="#2C323A", border_strong="#3D444D",
    text="#DDE2E8", muted="#A0A9B4",
    accent="#3F6EA8", accent_hover="#4573AC", accent_pressed="#36619A", accent_soft="#24364A",
    accent_disabled="#2B3C50", on_accent="#FFFFFF",
    field="#16191D", button="#2E353C", button_hover="#343B43", button_pressed="#3C444D",
    disabled_text="#7C8590",
    success="#5DB27F", warning="#D7A04E", danger="#EC8484", frozen="#9C9CEB",
    warn_bg="#342E20", warn_border="#5F4F2A",
    button_disabled="#262C32", accent_text="#7FA8DB", on_accent_disabled="#8E9BAB",
)

PALETTE = LIGHT


def system_prefers_dark() -> bool:
    """Тёмная тема приложений Windows (AppsUseLightTheme = 0). Не Windows или нет ключа → False."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except (OSError, ValueError):
        return False


def palette_for(mode: str) -> Palette:
    if mode == "dark":
        return DARK
    if mode == "system":
        return DARK if system_prefers_dark() else LIGHT
    return LIGHT


def set_title_bar(window: tk.Misc, dark: bool) -> None:
    """Тёмный заголовок окна на Windows 10 1809+ / 11 (DWMWA_USE_IMMERSIVE_DARK_MODE). Ошибки молча пропускаются."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):   # 20 — Windows 11 и новые 10, 19 — ранние сборки 10
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
        if window.winfo_ismapped():
            # Windows перерисовывает заголовок только при смене размера: сдвигаем на 1 px и обратно
            width, height = window.winfo_width(), window.winfo_height()
            window.geometry(f"{width + 1}x{height}")
            window.update_idletasks()
            window.geometry(f"{width}x{height}")
    except Exception:  # noqa: BLE001 — оформление заголовка не должно ронять программу
        pass


def _has_semibold(root: tk.Misc) -> bool:
    try:
        return SEMIBOLD_FAMILY in tkfont.families(root)
    except tk.TclError:
        return False


def _body(root: tk.Misc, size: int = 10) -> tuple:
    """Основной текст — полужирный (Segoe UI Semibold), без него — обычный."""
    return (SEMIBOLD_FAMILY, size) if _has_semibold(root) else (FONT_FAMILY, size)


def _strong(root: tk.Misc, size: int) -> tuple:
    """Заголовки и кнопки — жирные."""
    return (FONT_FAMILY, size, "bold")


def row_tags(p: Palette) -> dict[str, dict]:
    return {
        "ok": {"foreground": p.success},
        "new": {"foreground": p.warning},
        "bad": {"foreground": p.danger},
        "plain": {"foreground": p.muted},
        "frozen": {"foreground": p.frozen},
        "odd": {"background": p.surface_alt},
        "current": {"font": (FONT_FAMILY, 10, "bold")},
    }


ROW_TAGS: dict[str, dict] = row_tags(LIGHT)   # совместимость; актуальные теги — row_tags(PALETTE)


def configure_tree_tags(tree: ttk.Treeview) -> None:
    for tag, options in row_tags(PALETTE).items():
        tree.tag_configure(tag, **options)


def dot(tag: str = "") -> str:
    """Префикс для текста состояния: «● »."""
    return f"{STATUS_DOT} "


def separator(parent: tk.Misc) -> tk.Frame:
    """Линия высотой 1 px цвета border (создатель сам делает pack/grid)."""
    return tk.Frame(parent, height=1, background=PALETTE.border, borderwidth=0, highlightthickness=0)


_TK_COLOR_OPTIONS = ("background", "foreground", "activebackground", "activeforeground",
                     "highlightbackground", "highlightcolor", "selectbackground", "selectforeground",
                     "insertbackground", "disabledforeground", "troughcolor")


def recolor(widget: tk.Misc, old: Palette, new: Palette) -> None:
    """Перекрашивает tk-виджеты (не ttk) с явными цветами старой палитры в цвета новой — для смены темы
    без перезапуска. ttk-виджеты перекрашивает apply(); теги таблиц — configure_tree_tags()."""
    if old == new:
        return
    mapping: dict[str, str] = {}
    for name in old.__dataclass_fields__:
        before, after = getattr(old, name), getattr(new, name)
        if isinstance(before, str):
            mapping.setdefault(before.lower(), after)

    def walk(w: tk.Misc) -> None:
        if not isinstance(w, ttk.Widget):
            for option in _TK_COLOR_OPTIONS:
                try:
                    value = str(w.cget(option)).lower()
                except tk.TclError:
                    continue
                if value in mapping:
                    try:
                        w.configure(**{option: mapping[value]})
                    except tk.TclError:
                        pass
        if isinstance(w, ttk.Treeview):
            configure_tree_tags(w)
        for child in w.winfo_children():
            walk(child)

    walk(widget)


def apply(root: tk.Misc, px: Callable[[int], int] = lambda v: v, mode: str = "light") -> Palette:
    """Применяет палитру режима mode ('light' | 'dark' | 'system'). Повторный вызов перекрашивает ttk-стили;
    tk-виджеты с явными цветами (tk.Frame, tk.Label, Treeview-теги) перекрашивает вызывающий."""
    global PALETTE
    p = PALETTE = palette_for(mode)
    body = _body(root, 10)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        try:
            tkfont.nametofont(name, root=root).configure(family=body[0], size=10)
        except tk.TclError:
            pass

    if isinstance(root, (tk.Tk, tk.Toplevel)):
        root.configure(background=p.bg)
        set_title_bar(root, p.dark)

    font = body
    strong10 = _strong(root, 10)
    strong11 = _strong(root, 11)

    # tk.* виджеты (действует на создаваемые после вызова)
    for key, value in (
            ("*Menu.font", font), ("*Menu.background", p.surface), ("*Menu.foreground", p.text),
            ("*Menu.activeBackground", p.accent_soft), ("*Menu.activeForeground", p.text),
            ("*Menu.disabledForeground", p.disabled_text), ("*Menu.relief", "flat"),
            ("*Menu.borderWidth", 1), ("*Menu.activeBorderWidth", 0),
            ("*Listbox.font", font), ("*Listbox.background", p.field), ("*Listbox.foreground", p.text),
            ("*Listbox.selectBackground", p.accent_soft), ("*Listbox.selectForeground", p.text),
            ("*Listbox.relief", "flat"), ("*Listbox.highlightThickness", 1),
            ("*Listbox.highlightColor", p.accent), ("*Listbox.highlightBackground", p.border_strong),
            ("*Text.font", font), ("*Text.background", p.field), ("*Text.foreground", p.text),
            ("*Text.insertBackground", p.text),
            ("*Text.selectBackground", p.accent_soft), ("*Text.selectForeground", p.text),
            ("*Text.relief", "flat"), ("*Text.highlightThickness", 1),
            ("*Text.highlightColor", p.accent), ("*Text.highlightBackground", p.border_strong),
            ("*TCombobox*Listbox.font", font), ("*TCombobox*Listbox.background", p.field),
            ("*TCombobox*Listbox.foreground", p.text),
            ("*TCombobox*Listbox.selectBackground", p.accent_soft),
            ("*TCombobox*Listbox.selectForeground", p.text)):
        root.option_add(key, value)

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", font=font, background=p.surface, foreground=p.text, bordercolor=p.border,
                lightcolor=p.surface, darkcolor=p.surface, troughcolor=p.border,
                focuscolor=p.accent, selectbackground=p.accent_soft, selectforeground=p.text,
                insertcolor=p.text, fieldbackground=p.field)

    # --- контейнеры -------------------------------------------------------
    s.configure("TFrame", background=p.surface)
    s.configure("App.TFrame", background=p.bg)
    s.configure("Header.TFrame", background=p.surface)
    s.configure("Card.TFrame", background=p.surface, borderwidth=1, relief="solid",
                bordercolor=p.border, lightcolor=p.border, darkcolor=p.border)
    for name in ("Card.TLabelframe", "Step.TLabelframe"):
        s.configure(name, background=p.surface, borderwidth=1, relief="solid", bordercolor=p.border,
                    lightcolor=p.border, darkcolor=p.border, padding=px(14))
        s.configure(name + ".Label", font=strong11, foreground=p.text, background=p.surface)
    s.configure("TLabelframe", background=p.surface, bordercolor=p.border, lightcolor=p.border,
                darkcolor=p.border)
    s.configure("TLabelframe.Label", background=p.surface, foreground=p.text)

    # --- подписи ----------------------------------------------------------
    s.configure("TLabel", background=p.surface, foreground=p.text)
    for name in ("TCheckbutton", "TRadiobutton"):
        s.configure(name, background=p.surface, foreground=p.text, focuscolor=p.surface,
                    indicatorbackground=p.field, indicatorforeground=p.accent,
                    upperbordercolor=p.border_strong, lowerbordercolor=p.border_strong)
        s.map(name, background=[("active", p.surface)],
              indicatorbackground=[("disabled", p.surface_alt), ("selected", p.field)],
              foreground=[("disabled", p.disabled_text)])
    s.configure("Title.TLabel", font=_strong(root, 18))
    s.configure("Subtitle.TLabel", font=font, foreground=p.muted)
    s.configure("Section.TLabel", font=strong11)
    s.configure("Hint.TLabel", foreground=p.muted)
    s.configure("Error.TLabel", foreground=p.danger)
    s.configure("Ok.TLabel", foreground=p.success)
    s.configure("Warn.TLabel", foreground=p.warning)
    s.configure("Muted.TLabel", foreground=p.muted, font=_body(root, 9))
    s.configure("App.TLabel", background=p.bg, foreground=p.text)
    s.configure("AppTitle.TLabel", background=p.bg, foreground=p.text, font=_strong(root, 18))
    s.configure("AppHint.TLabel", background=p.bg, foreground=p.muted)
    s.configure("AppUser.TLabel", background=p.bg, foreground=p.text, font=_body(root, 13))   # имя пользователя
    s.configure("AppUserHint.TLabel", background=p.bg, foreground=p.muted, font=_body(root, 11))  # «Логин:»

    # --- кнопки -----------------------------------------------------------
    pad = (px(14), px(6))
    s.configure("TButton", background=p.button, foreground=p.text, bordercolor=p.border_strong,
                lightcolor=p.button, darkcolor=p.button, focuscolor=p.accent, padding=pad, relief="flat",
                borderwidth=1, anchor="center", font=strong10)
    states = [("disabled", p.button_disabled), ("pressed", p.button_pressed), ("active", p.button_hover)]
    s.map("TButton", background=states, lightcolor=states, darkcolor=states,
          foreground=[("disabled", p.disabled_text)], bordercolor=[("disabled", p.border)])

    def accent_button(name: str, padding: tuple, font_spec: tuple) -> None:
        s.configure(name, background=p.accent, foreground=p.on_accent, bordercolor=p.accent,
                    lightcolor=p.accent, darkcolor=p.accent, focuscolor=p.on_accent, padding=padding,
                    font=font_spec)
        accent_states = [("disabled", p.accent_disabled), ("pressed", p.accent_pressed),
                         ("active", p.accent_hover)]
        s.map(name, background=accent_states, lightcolor=accent_states, darkcolor=accent_states,
              bordercolor=accent_states, foreground=[("disabled", p.on_accent_disabled)])

    # невысокая кнопка («Отмена» в панели подсказок внизу окна): цвета и состояния - от TButton
    s.configure("Small.TButton", padding=(px(10), px(1)))
    accent_button("Accent.TButton", pad, strong10)
    accent_button("Big.TButton", (px(28), px(10)), _strong(root, 12))
    # кнопки колонок со значками (widgets.SideButtons): цвета обычной и подсвеченной кнопки, но значок и подпись
    # прижаты влево, чтобы значки всех кнопок колонки стояли друг под другом
    for name in ("Side.TButton", "Side.Accent.TButton"):
        s.configure(name, anchor="w")

    s.configure("Link.TButton", background=p.surface, foreground=p.accent_text, borderwidth=0,
                bordercolor=p.surface, lightcolor=p.surface, darkcolor=p.surface,
                focuscolor=p.surface, padding=(px(4), px(2)), font=font)
    # при наведении ссылка меняет цвет на текстовый: accent_hover на тёмной панели слишком тусклый
    s.map("Link.TButton", foreground=[("disabled", p.disabled_text), ("pressed", p.muted),
                                      ("active", p.text)],
          background=[("active", p.surface)], lightcolor=[("active", p.surface)],
          darkcolor=[("active", p.surface)], bordercolor=[("active", p.surface)])

    for name, base in (("Ghost.TButton", p.surface), ("AppGhost.TButton", p.bg)):
        s.configure(name, background=base, foreground=p.text, bordercolor=base,
                    lightcolor=base, darkcolor=base, focuscolor=p.accent, padding=pad, font=font)
        ghost_states = [("pressed", p.button_hover), ("active", p.button)]
        s.map(name, background=ghost_states, lightcolor=ghost_states, darkcolor=ghost_states,
              bordercolor=ghost_states, foreground=[("disabled", p.disabled_text)])

    # крупные стрелки заголовков колонок (сворачивание и вид кнопок): тот же цвет, шрифт крупнее обычного
    s.configure("Arrow.Ghost.TButton", font=(FONT_FAMILY, ARROW_FONT_SIZE, "bold"), padding=(px(2), 0))
    s.configure("ArrowSmall.Ghost.TButton", font=(FONT_FAMILY, ARROW_SMALL_FONT_SIZE, "bold"), padding=(px(2), 0))

    s.configure("TMenubutton", background=p.button, foreground=p.text, bordercolor=p.border_strong,
                lightcolor=p.button, darkcolor=p.button, arrowcolor=p.muted, padding=pad, relief="flat",
                font=strong10)
    s.map("TMenubutton", background=[("disabled", p.button_disabled), ("active", p.button_hover)],
          lightcolor=[("active", p.button_hover)], darkcolor=[("active", p.button_hover)],
          foreground=[("disabled", p.disabled_text)])

    # строка меню в самом окне (вместо родного меню Windows, которое тему не принимает): Menubutton без стрелки
    # на фоне окна; выпадающий список — tk.Menu, его цвета заданы выше через option_add (*Menu.*)
    s.layout("MenuBar.TMenubutton", [("Menubutton.border", {"sticky": "nswe", "children": [
        ("Menubutton.focus", {"sticky": "nswe", "children": [
            ("Menubutton.padding", {"sticky": "nswe", "children": [
                ("Menubutton.label", {"sticky": ""})]})]})]})])
    s.configure("MenuBar.TMenubutton", background=p.bg, foreground=p.text, bordercolor=p.bg,
                lightcolor=p.bg, darkcolor=p.bg, focuscolor=p.bg, padding=(px(10), px(4)), relief="flat",
                borderwidth=0, font=font, width=0)     # width=0: по тексту (в clam у кнопок минимум 11 символов)
    menubar_states = [("pressed", p.button_pressed), ("active", p.button_hover)]
    s.map("MenuBar.TMenubutton", background=menubar_states, lightcolor=menubar_states,
          darkcolor=menubar_states, bordercolor=menubar_states)

    # --- поля ввода -------------------------------------------------------
    field_pad = (px(6), px(4))
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        s.configure(name, fieldbackground=p.field, background=p.button, foreground=p.text,
                    bordercolor=p.border_strong, lightcolor=p.border_strong, darkcolor=p.border_strong,
                    insertcolor=p.text, arrowcolor=p.muted, padding=field_pad,
                    selectbackground=p.accent_soft, selectforeground=p.text)
        focus = [("focus", p.accent)]
        s.map(name, bordercolor=focus + [("disabled", p.border)], lightcolor=focus, darkcolor=focus,
              fieldbackground=[("disabled", p.surface_alt), ("readonly", p.field)],
              foreground=[("disabled", p.disabled_text)])
    s.map("TCombobox", selectbackground=[("readonly", p.field)], selectforeground=[("readonly", p.text)],
          background=[("active", p.button_hover)],
          fieldbackground=[("disabled", p.surface_alt), ("readonly", p.field)])

    # --- вкладки ----------------------------------------------------------
    s.configure("TNotebook", background=p.bg, borderwidth=0, bordercolor=p.bg, lightcolor=p.bg,
                darkcolor=p.bg, tabmargins=(px(12), px(8), px(12), 0))
    s.configure("TNotebook.Tab", padding=(px(18), px(8)), background=p.bg, foreground=p.muted,
                bordercolor=p.bg, lightcolor=p.bg, darkcolor=p.bg, font=strong10, focuscolor=p.bg)
    s.map("TNotebook.Tab",
          background=[("selected", p.surface), ("active", p.bg)],
          foreground=[("selected", p.accent_text), ("active", p.text)],
          bordercolor=[("selected", p.border)],
          lightcolor=[("selected", p.surface)], darkcolor=[("selected", p.surface)],
          expand=[("selected", (0, 0, 0, 0))])
    # вкладки внутри колонки «Состояние проекта»: компактнее, чтобы пять ярлыков помещались без обрезки
    s.configure("Inner.TNotebook", tabmargins=(0, px(4), 0, 0))
    s.configure("Inner.TNotebook.Tab", padding=(px(10), px(5)))

    # --- таблица ----------------------------------------------------------
    linespace = tkfont.Font(root=root, family=body[0], size=10).metrics("linespace")
    s.configure("Treeview", background=p.surface, fieldbackground=p.surface, foreground=p.text,
                borderwidth=0, rowheight=int(linespace * 2), font=font)
    s.map("Treeview", background=[("selected", p.accent_soft)], foreground=[("selected", p.text)])
    s.configure("Treeview.Heading", background=p.surface_alt, foreground=p.muted,
                font=_strong(root, 9), relief="flat", borderwidth=0, padding=(px(8), px(6)),
                bordercolor=p.border, lightcolor=p.surface_alt, darkcolor=p.surface_alt)
    s.map("Treeview.Heading", background=[("active", p.button)], lightcolor=[("active", p.button)],
          darkcolor=[("active", p.button)])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # --- прогресс, прокрутка, разделитель --------------------------------
    for name in ("TProgressbar", "Horizontal.TProgressbar"):
        s.configure(name, troughcolor=p.button_hover, background=p.accent, bordercolor=p.button_hover,
                    lightcolor=p.accent, darkcolor=p.accent, thickness=px(6), borderwidth=0)
    for name in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        s.configure(name, troughcolor=p.surface, background=p.button_pressed, bordercolor=p.surface,
                    lightcolor=p.button_pressed, darkcolor=p.button_pressed, arrowcolor=p.muted,
                    arrowsize=px(14), relief="flat", borderwidth=0)
        bar = [("active", p.muted), ("pressed", p.muted)]
        s.map(name, background=bar, lightcolor=bar, darkcolor=bar)
    s.configure("TSeparator", background=p.border)
    return p
