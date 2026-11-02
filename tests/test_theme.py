import unittest

from svnhelper import theme

try:
    import tkinter as tk
    from tkinter import ttk
    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    HAS_DISPLAY = True
except Exception:
    HAS_DISPLAY = False


def _luminance(color: str) -> float:
    """Относительная яркость WCAG 2.x для «#RRGGBB»."""
    channels = []
    for i in (1, 3, 5):
        c = int(color[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _contrast_pairs() -> list[tuple[str, str, float]]:
    """(цвет-1, цвет-2, минимальное отношение) по именам полей Palette."""
    pairs = [("text", bg, 7.0) for bg in ("bg", "surface", "surface_alt", "field", "button", "accent_soft",
                                         "warn_bg")]
    pairs += [("text", bg, 4.5) for bg in ("button_hover", "button_pressed")]
    pairs += [("muted", bg, 4.5) for bg in ("bg", "surface", "surface_alt", "field", "button", "button_hover",
                                           "accent_soft", "warn_bg")]
    # неактивное: читается (≥ 3), но слабее muted
    pairs += [("disabled_text", bg, 3.0) for bg in ("button_disabled", "button", "surface", "bg", "surface_alt")]
    pairs += [("accent_text", bg, 4.5) for bg in ("surface", "bg", "surface_alt")]
    pairs += [("accent", "surface", 3.0)]
    pairs += [("on_accent", bg, 4.5) for bg in ("accent", "accent_hover", "accent_pressed")]
    pairs += [("on_accent_disabled", "accent_disabled", 3.0)]
    pairs += [(status, bg, 4.5) for status in ("success", "warning", "danger", "frozen")
              for bg in ("surface", "surface_alt", "field", "accent_soft")]
    pairs += [(status, "warn_bg", 4.5) for status in ("success", "warning", "danger")]
    # рамки, кнопки, выделение — различимость соседних поверхностей
    pairs += [("border_strong", "surface", 1.5), ("border_strong", "field", 1.5), ("border_strong", "bg", 1.3),
              ("border", "surface", 1.2), ("border_strong", "button", 1.15),
              ("button", "surface", 1.15), ("button_disabled", "surface", 1.15),
              ("button_pressed", "surface", 1.3),          # бегунок прокрутки на фоне панели
              ("surface", "bg", 1.1), ("accent_soft", "surface", 1.15),
              ("button_hover", "button", 1.05), ("surface_alt", "surface", 1.03)]
    return pairs


class PaletteContrastTests(unittest.TestCase):
    """Палитру нельзя испортить: контраст пар текст/фон и различимость поверхностей (WCAG)."""

    def test_contrast_pairs(self):
        for name, palette in (("LIGHT", theme.LIGHT), ("DARK", theme.DARK)):
            for first, second, minimum in _contrast_pairs():
                a, b = getattr(palette, first), getattr(palette, second)
                self.assertGreaterEqual(_contrast(a, b), minimum,
                                        f"{name}: {first} {a} на {second} {b} — {_contrast(a, b):.2f} < {minimum}")

    def test_light_is_not_dazzling_white(self):
        light = theme.LIGHT
        # ступени серого: окно темнее панели, панель темнее поля; чистого белого нет нигде, кроме текста на акценте
        self.assertLess(_luminance(light.bg), _luminance(light.surface_alt))
        self.assertLess(_luminance(light.surface_alt), _luminance(light.surface))
        self.assertLess(_luminance(light.surface), _luminance(light.field))
        for name in ("bg", "surface", "surface_alt", "field", "button", "warn_bg"):
            self.assertNotEqual(getattr(light, name), "#FFFFFF", name)
        self.assertLess(_luminance(light.surface), 0.9)

    def test_dark_steps_are_ordered_and_text_not_pure_white(self):
        dark = theme.DARK
        steps = [_luminance(getattr(dark, name)) for name in
                 ("bg", "surface", "surface_alt", "button_disabled", "button", "button_hover", "button_pressed")]
        self.assertEqual(steps, sorted(steps))
        self.assertNotEqual(dark.text, "#FFFFFF")
        self.assertLess(_luminance(dark.button), 0.05)          # кнопки не «горят» на тёмной панели

    def test_disabled_text_weaker_than_muted(self):
        for palette in (theme.LIGHT, theme.DARK):
            self.assertLess(_contrast(palette.disabled_text, palette.surface), _contrast(palette.muted, palette.surface))


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_apply_returns_palette_and_is_idempotent(self):
        self.assertIs(theme.apply(self.root), theme.PALETTE)
        self.assertIs(theme.apply(self.root, px=lambda v: v * 2), theme.PALETTE)

    def test_styles_exist(self):
        theme.apply(self.root)
        p = theme.PALETTE
        style = ttk.Style(self.root)
        self.assertEqual(style.theme_use(), "clam")
        self.assertEqual(style.lookup("Accent.TButton", "background"), p.accent)
        self.assertEqual(style.lookup("Big.TButton", "background"), p.accent)
        self.assertEqual(style.lookup("Step.TLabelframe", "background"), p.surface)
        self.assertEqual(style.lookup("Error.TLabel", "foreground"), p.danger)
        self.assertEqual(style.lookup("App.TFrame", "background"), p.bg)
        self.assertEqual(style.lookup("Treeview", "fieldbackground"), p.surface)
        self.assertEqual(style.lookup("TNotebook.Tab", "foreground"), p.muted)

    def test_disabled_button_and_menubar_styles(self):
        for mode, palette in (("light", theme.LIGHT), ("dark", theme.DARK)):
            theme.apply(self.root, mode=mode)
            style = ttk.Style(self.root)
            disabled = style.map("TButton", "background")
            self.assertIn(("disabled", palette.button_disabled), [tuple(x) for x in disabled])
            self.assertEqual(style.lookup("MenuBar.TMenubutton", "background"), palette.bg)
            self.assertEqual(style.lookup("MenuBar.TMenubutton", "foreground"), palette.text)
            self.assertEqual(style.lookup("Link.TButton", "foreground"), palette.accent_text)
            # стиль строки меню должен собираться в реальный виджет
            ttk.Menubutton(self.root, text="Вид", style="MenuBar.TMenubutton", menu=tk.Menu(self.root)).update_idletasks()
        theme.apply(self.root, mode="light")

    def test_tree_tags(self):
        theme.apply(self.root)
        tree = ttk.Treeview(self.root)
        theme.configure_tree_tags(tree)
        self.assertEqual(str(tree.tag_configure("ok", "foreground")), theme.PALETTE.success)
        self.assertEqual(str(tree.tag_configure("odd", "background")), theme.PALETTE.surface_alt)

    def test_dark_mode(self):
        p = theme.apply(self.root, mode="dark")
        self.assertIs(p, theme.DARK)
        self.assertIs(theme.PALETTE, theme.DARK)
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup("TFrame", "background"), theme.DARK.surface)
        self.assertEqual(style.lookup("TEntry", "fieldbackground"), theme.DARK.field)
        tree = ttk.Treeview(self.root)
        theme.configure_tree_tags(tree)
        self.assertEqual(str(tree.tag_configure("ok", "foreground")), theme.DARK.success)
        self.assertIs(theme.apply(self.root, mode="light"), theme.LIGHT)

    def test_palettes_complete_and_system_mode(self):
        from dataclasses import fields
        for palette in (theme.LIGHT, theme.DARK):
            for f in fields(palette):
                value = getattr(palette, f.name)
                if f.name != "dark":
                    self.assertRegex(value, r"^#[0-9A-F]{6}$", f.name)
        self.assertIn(theme.palette_for("system"), (theme.LIGHT, theme.DARK))
        self.assertIs(theme.palette_for("нечто"), theme.LIGHT)

    def test_recolor_tk_widgets(self):
        theme.apply(self.root, mode="light")
        bar = tk.Frame(self.root, background=theme.LIGHT.warn_bg)
        label = tk.Label(bar, background=theme.LIGHT.warn_bg, foreground=theme.LIGHT.text)
        other = tk.Label(bar, background="#123456")
        theme.apply(self.root, mode="dark")
        theme.recolor(self.root, theme.LIGHT, theme.DARK)
        self.assertEqual(bar.cget("background"), theme.DARK.warn_bg)
        self.assertEqual(label.cget("foreground"), theme.DARK.text)
        self.assertEqual(other.cget("background"), "#123456")

    def test_dot_and_separator(self):
        self.assertTrue(theme.dot("ok").startswith(theme.STATUS_DOT))
        self.assertEqual(int(theme.separator(self.root).cget("height")), 1)


if __name__ == "__main__":
    unittest.main()
