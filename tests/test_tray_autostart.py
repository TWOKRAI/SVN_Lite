"""Трей и автозапуск (Task E.1). Реестр Windows и значок в трее подменены: тесты ничего не пишут в систему."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svnhelper import autostart, tray  # noqa: E402
from svnhelper.config import Config, SettingsStore  # noqa: E402

try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    HAS_DISPLAY = True
except Exception:
    HAS_DISPLAY = False


class FakeRegistry:
    """Подмена модуля winreg: один раздел Run в памяти."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict = {}
        self.fail = False

    class _Key:
        def __init__(self, reg):
            self.reg = reg

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _check(self, root, path):
        assert root == "HKCU" and path == autostart.RUN_KEY, "автозапуск должен писать только в HKCU\\...\\Run"
        if self.fail:
            raise PermissionError("нет доступа")

    def OpenKey(self, root, path, reserved=0, access=0):
        self._check(root, path)
        return self._Key(self)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.reg = FakeRegistry()
        patch = mock.patch.object(autostart, "winreg", self.reg)
        patch.start()
        self.addCleanup(patch.stop)

    def test_enable_disable_roundtrip(self):
        self.assertFalse(autostart.is_enabled())
        autostart.enable()
        self.assertTrue(autostart.is_enabled())
        self.assertEqual(self.reg.values[autostart.VALUE_NAME], autostart.launch_command())
        autostart.disable()
        self.assertFalse(autostart.is_enabled())
        self.assertEqual(self.reg.values, {})

    def test_disable_when_not_enabled_is_silent(self):
        autostart.disable()
        self.assertFalse(autostart.is_enabled())

    def test_enable_twice_keeps_single_value(self):
        autostart.enable()
        autostart.enable()
        self.assertEqual(list(self.reg.values), [autostart.VALUE_NAME])

    def test_write_error_is_raised_for_the_caller(self):
        self.reg.fail = True
        with self.assertRaises(OSError):
            autostart.enable()

    def test_command_for_python_has_script_and_tray_flag(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            command = autostart.launch_command()
        self.assertTrue(command.endswith(" " + autostart.TRAY_ARG))
        self.assertIn("svn_helper.py", command)
        self.assertTrue(command.startswith('"'))

    def test_command_for_exe_is_quoted_path_and_tray_flag(self):
        exe = r"C:\Program Files\SVN-lite\SVN-lite.exe"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe):
            command = autostart.launch_command()
        self.assertEqual(command, f'"{os.path.abspath(exe)}" --tray')

    def test_current_command_reads_registry_value(self):
        self.assertIsNone(autostart.current_command())           # автозапуск выключен
        autostart.enable()
        self.assertEqual(autostart.current_command(), autostart.launch_command())

    def test_refresh_rewrites_stale_path_only_when_enabled(self):
        self.assertFalse(autostart.refresh())                    # выключенный автозапуск сам не включается
        self.assertEqual(self.reg.values, {})
        self.reg.values[autostart.VALUE_NAME] = '"C:\\старый путь\\SVN-lite.exe" --tray'
        self.assertTrue(autostart.refresh())
        self.assertEqual(self.reg.values[autostart.VALUE_NAME], autostart.launch_command())
        self.assertFalse(autostart.refresh())                    # путь уже верный: ничего не пишем

    def test_refresh_write_error_is_raised(self):
        self.reg.values[autostart.VALUE_NAME] = "старое"
        with mock.patch.object(autostart, "enable", side_effect=PermissionError("нет доступа")):
            with self.assertRaises(OSError):
                autostart.refresh()

    def test_unsupported_platform(self):
        with mock.patch.object(autostart, "winreg", None):
            self.assertFalse(autostart.is_enabled())
            autostart.disable()
            with self.assertRaises(OSError):
                autostart.enable()


class TrayModuleTests(unittest.TestCase):
    def test_menu_labels_come_from_provider(self):
        icon = tray.Tray(lambda event: None, labels=lambda: {tray.EVENT_SNOOZE: "Не беспокоить: включено до 15:30 - выключить"})
        labels = dict(icon.menu_items())
        self.assertEqual(labels[tray.EVENT_SNOOZE], "Не беспокоить: включено до 15:30 - выключить")
        self.assertEqual(labels[tray.EVENT_OPEN], "Открыть")
        broken = tray.Tray(lambda event: None, labels=mock.Mock(side_effect=RuntimeError("сбой")))
        self.assertEqual(broken.menu_items(), list(tray.DEFAULT_MENU))     # сбой подписей не ломает меню

    def test_start_off_windows_does_nothing(self):
        with mock.patch.object(tray, "IS_WINDOWS", False):
            icon = tray.Tray(lambda event: None)
            self.assertFalse(icon.start())
            icon.stop()
            self.assertFalse(tray.notify_running_instance())

    def test_menu_events(self):
        events = []
        icon = tray.Tray(events.append)
        self.assertEqual([e for e, _ in tray.DEFAULT_MENU],
                         [tray.EVENT_OPEN, tray.EVENT_CHECK, tray.EVENT_SNOOZE, tray.EVENT_QUIT])
        # щелчок левой кнопкой по значку открывает окно; правая показывает меню (здесь подменено)
        with mock.patch.object(icon, "_show_menu") as menu:
            self.assertTrue(icon._handle_message(tray._WM_TRAY, 0, tray._WM_LBUTTONUP))
            self.assertTrue(icon._handle_message(tray._WM_TRAY, 0, tray._WM_RBUTTONUP))
            menu.assert_called_once()
        self.assertTrue(icon._handle_message(tray._WM_OPEN_REQUEST, 0, 0))
        self.assertEqual(events, [tray.EVENT_OPEN, tray.EVENT_OPEN])
        self.assertFalse(icon._handle_message(0x0001, 0, 0))

    def test_handler_error_does_not_break_tray(self):
        def boom(event):
            raise RuntimeError("x")
        icon = tray.Tray(boom)
        self.assertTrue(icon._handle_message(tray._WM_OPEN_REQUEST, 0, 0))


class FakeTray:
    """Слой трея для тестов окна: настоящего значка нет."""

    def __init__(self, active=True):
        self.active = active
        self.stopped = False

    def stop(self):
        self.stopped = True


class SettingsTests(unittest.TestCase):
    def test_tray_on_close_default_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp) / "s.json")
            self.assertTrue(store.settings.tray_on_close)
            store.settings.tray_on_close = False
            store.save()
            self.assertFalse(SettingsStore(Path(tmp) / "s.json").settings.tray_on_close)


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class AppTrayTests(unittest.TestCase):
    def setUp(self):
        from svnhelper.gui import App
        for target, kwargs in (("svnhelper.gui.download_tab.load_tree_cache", {"return_value": {}}),
                               ("svnhelper.gui.download_tab.save_tree_cache", {}),
                               ("svnhelper.tortoise.find_tortoiseproc", {"return_value": None}),
                               ("svnhelper.gui.app.autostart.is_enabled", {"return_value": False}),
                               ("svnhelper.gui.app.autostart.enable", {}),
                               ("svnhelper.gui.app.autostart.disable", {})):
            patch = mock.patch(target, **kwargs)
            setattr(self, "p_" + target.rsplit(".", 1)[1], patch.start())
            self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.clipboard_get = mock.Mock(side_effect=tk.TclError("clipboard disabled in tests"))
        self.addCleanup(self.root.destroy)
        self.app = App(self.root, Config(server="svn://x", repos=["a"], work_dir=os.path.join(tmp.name, "dl")),
                       SettingsStore(Path(tmp.name) / "s.json"), None, os.path.join(tmp.name, "log.txt"))
        self.finish = mock.patch.object(App, "_finish_close")
        self.finish_mock = self.finish.start()
        self.addCleanup(self.finish.stop)

    def test_close_without_tray_exits(self):
        self.app._on_close()
        self.finish_mock.assert_called_once()

    def test_cross_hides_to_tray_and_does_not_exit(self):
        self.app.attach_tray(FakeTray())
        self.root.deiconify()
        self.app._on_close()
        self.assertEqual(self.root.state(), "withdrawn")
        self.finish_mock.assert_not_called()
        self.assertFalse(self.app._closing)

    def test_cross_exits_when_tray_on_close_is_off(self):
        self.app.attach_tray(FakeTray())
        self.app.settings.tray_on_close = False
        self.app._on_close()
        self.finish_mock.assert_called_once()

    def test_cross_exits_when_tray_icon_is_not_active(self):
        self.app.attach_tray(FakeTray(active=False))
        self.app._on_close()
        self.finish_mock.assert_called_once()

    def test_open_event_shows_window(self):
        self.app.attach_tray(FakeTray())
        self.app._on_close()
        with mock.patch("svnhelper.gui.app.winutil.activate_window"):
            self.app._handle_tray_event(tray.EVENT_OPEN)
        self.assertEqual(self.root.state(), "normal")

    def test_quit_event_exits(self):
        self.app.attach_tray(FakeTray())
        self.app._on_close()
        with mock.patch("svnhelper.gui.app.winutil.activate_window"):
            self.app._handle_tray_event(tray.EVENT_QUIT)
        self.finish_mock.assert_called_once()
        self.assertEqual(self.root.state(), "normal")       # вопросы при выходе должны быть видны

    def test_quit_event_keeps_unsaved_question(self):
        self.app.attach_tray(FakeTray())
        with mock.patch.object(self.app, "_unsaved_project_names", return_value=["Проект"]), \
                mock.patch("svnhelper.gui.app.winutil.activate_window"), \
                mock.patch("svnhelper.gui.app.messagebox.askyesno", return_value=False) as ask:
            self.app._handle_tray_event(tray.EVENT_QUIT)
        ask.assert_called_once()
        self.finish_mock.assert_not_called()                # ответили «нет»: программа остаётся

    def test_event_from_other_thread_goes_through_queue(self):
        self.app.tray_event(tray.EVENT_QUIT)
        self.finish_mock.assert_not_called()                # до опроса очереди в главном потоке ничего не делается
        with mock.patch("svnhelper.gui.app.winutil.activate_window"):
            fn, args = self.app._queue.get_nowait()
            fn(*args)
        self.finish_mock.assert_called_once()

    def test_settings_apply_switches_autostart_and_tray(self):
        values = {"config_path": "", "username": "", "password": "", "remember": True, "server": "svn://x",
                  "repos": "a", "last_dir": "", "svn_path": "", "skip_junk": True, "show_report": True,
                  "autostart": True, "tray_on_close": False}
        with mock.patch.object(self.app, "_rebuild_config"):
            self.app._apply_settings(values)
        self.p_enable.assert_called_once()
        self.assertFalse(self.app.settings.tray_on_close)
        self.p_enable.reset_mock()
        self.p_is_enabled.return_value = True
        values["autostart"] = False
        with mock.patch.object(self.app, "_rebuild_config"):
            self.app._apply_settings(values)
        self.p_disable.assert_called_once()
        self.p_enable.assert_not_called()

    def test_autostart_error_is_reported_in_russian(self):
        self.p_enable.side_effect = PermissionError("нет доступа")
        with mock.patch("svnhelper.gui.app.messagebox.showerror") as err:
            self.app._apply_autostart(True)
        err.assert_called_once()
        self.assertIn("Windows", err.call_args.args[1])

    def test_settings_dialog_shows_background_options(self):
        from svnhelper.gui.widgets import SettingsDialog
        with mock.patch.object(tk.Toplevel, "grab_set"):
            dlg = SettingsDialog(self.root, self.app.config, self.app.settings, False, autostart=True)
        self.addCleanup(dlg.destroy)
        values = dlg._values()
        self.assertTrue(values["autostart"])
        self.assertTrue(values["tray_on_close"])


if __name__ == "__main__":
    unittest.main()
