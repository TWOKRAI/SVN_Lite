"""Фоновая проверка и уведомления (Task E.2): настройки, решения «что показать», позиция окошка, проверка на fake_svn.

Настоящий TortoiseSVN, реестр и значок в трее не трогаются.
"""
import gc
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svnhelper import notifier, tray  # noqa: E402
from svnhelper.config import (clean_notify, Config, NOTIFY_DEFAULTS, apply_notify,  # noqa: E402
                              Settings, SettingsStore)
from svnhelper.notifier import (CheckResult, check_app_update, check_projects, discover_projects,  # noqa: E402
                                in_quiet_hours, Notifier, ProjectInfo)
from svnhelper.svn_core import SvnClient  # noqa: E402


def release_test_windows(test, before):
    """Окна теста собирает главный поток: иначе мусор закрытого окна попадает под сборщик в фоновом потоке другого
    теста и роняет процесс (Tcl_AsyncDelete). Атрибуты, созданные в setUp, снимаются, потом gc.collect()."""
    for name in set(vars(test)) - before:
        delattr(test, name)
    gc.collect()


try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    HAS_DISPLAY = True
except Exception:
    HAS_DISPLAY = False

FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_svn.py")]


class Clock:
    """Управляемые часы для Notifier: начинаем в среду утром."""

    def __init__(self):
        self.now = datetime(2026, 10, 21, 10, 0)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

class NotifySettingsTests(unittest.TestCase):
    def test_safe_defaults(self):
        s = Settings()
        self.assertTrue(s.notify_enabled)
        self.assertEqual(s.notify_check_min, 15)
        self.assertEqual(s.notify_unsaved_hours, 4)
        self.assertEqual(s.notify_popup_sec, 15)
        self.assertFalse(s.notify_quiet)
        self.assertEqual({k: getattr(s, k) for k in NOTIFY_DEFAULTS}, NOTIFY_DEFAULTS)

    def test_invalid_values_replaced_with_message(self):
        clean, problems = clean_notify({
            "notify_check_min": "abc", "notify_repeat_hours": 0, "notify_app_update_days": -3,
            "notify_unsaved_hours": 10_000, "notify_popup_sec": True, "notify_quiet_from": "25:99",
            "notify_enabled": "yes", "notify_muted": "oops"})
        self.assertEqual(clean["notify_check_min"], 15)
        self.assertEqual(clean["notify_repeat_hours"], 4)          # ноль здесь не имеет смысла
        self.assertEqual(clean["notify_app_update_days"], 1)
        self.assertEqual(clean["notify_unsaved_hours"], 4)
        self.assertEqual(clean["notify_popup_sec"], 15)
        self.assertEqual(clean["notify_quiet_from"], "18:00")
        self.assertIs(clean["notify_enabled"], True)
        self.assertEqual(clean["notify_muted"], [])
        self.assertEqual(len(problems), 7)
        self.assertTrue(all("svn" not in p.casefold() for p in problems))

    def test_zero_means_something_where_allowed(self):
        clean, problems = clean_notify({"notify_check_min": 0, "notify_unsaved_hours": "0", "notify_popup_sec": 0})
        self.assertEqual(clean, {"notify_check_min": 0, "notify_unsaved_hours": 0, "notify_popup_sec": 0})
        self.assertEqual(problems, [])

    def test_time_is_normalized(self):
        clean, problems = clean_notify({"notify_quiet_from": " 9:05 ", "notify_quiet_to": "18.30"})
        self.assertEqual((clean["notify_quiet_from"], clean["notify_quiet_to"]), ("09:05", "18:30"))
        self.assertEqual(problems, [])

    def test_apply(self):
        s = Settings()
        problems = apply_notify(s, {"notify_check_min": 60, "notify_unsaved_hours": "zz", "server": "ignored",
                                    "notify_muted": ["C:\\a", "C:\\a", ""]})
        self.assertEqual((s.notify_check_min, s.notify_unsaved_hours), (60, 4))
        self.assertEqual(s.notify_muted, ["C:\\a"])
        self.assertEqual(len(problems), 1)

    def test_persistence_and_garbage_in_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            store = SettingsStore(path)
            store.settings.notify_check_min = 30
            store.settings.notify_quiet = True
            store.settings.notify_muted = ["C:\\x"]
            store.save()
            loaded = SettingsStore(path).settings
            self.assertEqual((loaded.notify_check_min, loaded.notify_quiet, loaded.notify_muted), (30, True, ["C:\\x"]))
            path.write_text('{"notify_check_min": "много", "notify_popup_sec": -1, "notify_quiet_to": 5}',
                            encoding="utf-8")
            broken = SettingsStore(path).settings
            self.assertEqual((broken.notify_check_min, broken.notify_popup_sec, broken.notify_quiet_to),
                             (15, 15, "09:00"))

    def test_default_old_file_without_notify_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text('{"theme": "dark"}', encoding="utf-8")
            self.assertEqual(SettingsStore(path).settings.notify_check_min, 15)


# --------------------------------------------------------------------------
# Время: не беспокоить, интервалы
# --------------------------------------------------------------------------

class QuietHoursTests(unittest.TestCase):
    def test_disabled(self):
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 21, 23, 0), False, "18:00", "09:00", True))

    def test_overnight_window(self):
        for hour, expected in ((8, True), (9, False), (12, False), (17, False), (18, True), (23, True), (0, True)):
            self.assertEqual(in_quiet_hours(datetime(2026, 10, 21, hour, 0), True, "18:00", "09:00", False),
                             expected, hour)

    def test_daytime_window(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 21, 13, 0), True, "12:00", "14:00", False))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 21, 14, 0), True, "12:00", "14:00", False))

    def test_weekend(self):
        saturday = datetime(2026, 10, 24, 12, 0)
        self.assertTrue(in_quiet_hours(saturday, True, "18:00", "09:00", True))
        self.assertFalse(in_quiet_hours(saturday, True, "18:00", "09:00", False))

    def test_bad_or_empty_range_is_ignored(self):
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 21, 12, 0), True, "xx", "09:00", False))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 21, 12, 0), True, "10:00", "10:00", False))


class CheckTimerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.settings = Settings()
        self.notifier = Notifier(lambda: self.settings, self.clock)

    def test_first_check_is_due(self):
        self.assertTrue(self.notifier.due_for_check(None))

    def test_interval(self):
        last = self.notifier.timestamp()
        self.clock.advance(minutes=14)
        self.assertFalse(self.notifier.due_for_check(last))
        self.clock.advance(minutes=1)
        self.assertTrue(self.notifier.due_for_check(last))

    def test_interval_follows_settings_without_restart(self):
        last = self.notifier.timestamp()
        self.clock.advance(minutes=10)
        self.assertFalse(self.notifier.due_for_check(last))
        self.settings.notify_check_min = 5
        self.assertTrue(self.notifier.due_for_check(last))

    def test_manual_only_never_due(self):
        self.settings.notify_check_min = 0
        self.assertFalse(self.notifier.due_for_check(None))
        self.assertFalse(self.notifier.due_for_check(0.0))


# --------------------------------------------------------------------------
# Решения: что и когда показывать
# --------------------------------------------------------------------------

def info(name="Проект", **kwargs):
    return ProjectInfo(os.path.join("C:\\Проекты", name), name, **kwargs)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.settings = Settings()
        self.notifier = Notifier(lambda: self.settings, self.clock)

    def show(self, **kwargs):
        """Все уведомления, как их показало бы окно: сразу отмечаются показанными."""
        notices = self.notifier.candidates(**kwargs)
        for notice in notices:
            self.notifier.mark_shown(notice)
        return notices

    def test_incoming_text_and_tab(self):
        self.notifier.update([info(incoming=3, authors="Иван")])
        (notice,) = self.show()
        self.assertEqual(notice.kind, "incoming")
        self.assertEqual(notice.tab, "incoming")
        self.assertEqual(notice.text, "В проекте «Проект» есть новое от Иван (3 файла).")
        self.assertNotIn("svn", notice.text.casefold())

    def test_incoming_without_author(self):
        self.notifier.update([info(incoming=1)])
        self.assertEqual(self.show()[0].text, "В проекте «Проект» есть новое (1 файл).")

    def test_incoming_deduplicated_until_count_changes(self):
        self.notifier.update([info(incoming=2)])
        self.assertEqual(len(self.show()), 1)
        self.notifier.update([info(incoming=2)])
        self.assertEqual(self.show(), [])
        self.clock.advance(hours=3)
        self.assertEqual(self.show(), [])                      # время само по себе повода не даёт
        self.notifier.update([info(incoming=5)])
        self.assertEqual(len(self.show()), 1)

    def test_incoming_notified_again_after_it_was_received(self):
        self.notifier.update([info(incoming=2)])
        self.show()
        self.notifier.update([info(incoming=0)])               # получили новое
        self.notifier.update([info(incoming=2)])               # и снова два новых файла
        self.assertEqual(len(self.show()), 1)

    def test_not_shown_is_not_marked(self):
        """Окошко не показали (окно в фокусе): при следующей возможности уведомление всё ещё ждёт."""
        self.notifier.update([info(incoming=2)])
        self.assertEqual(len(self.notifier.candidates()), 1)
        self.assertEqual(len(self.notifier.candidates()), 1)

    def test_disabled(self):
        self.settings.notify_enabled = False
        self.notifier.update([info(incoming=2, unsaved=1, conflicts=1)])
        self.assertEqual(self.notifier.candidates(latest_version="9.9", current_version="1.0"), [])

    def test_incoming_switch_and_important_only(self):
        self.notifier.update([info(incoming=2, unsaved=1)])
        self.settings.notify_incoming = False
        self.assertEqual([n.kind for n in self.notifier.candidates()], [])      # правки ещё не «дольше N часов»
        self.settings.notify_incoming = True
        self.settings.notify_important_only = True
        self.assertEqual(self.notifier.candidates(latest_version="9.9", current_version="1.0"), [])

    def test_unsaved_reminder_after_n_hours_and_repeat_interval(self):
        self.notifier.update([info(unsaved=2)])
        self.assertEqual(self.show(), [])                      # только что появились
        self.clock.advance(hours=3, minutes=59)
        self.assertEqual(self.show(), [])
        self.clock.advance(minutes=2)
        (notice,) = self.show()
        self.assertEqual(notice.kind, "unsaved")
        self.assertEqual(notice.tab, "changes")
        self.assertTrue(notice.important)
        self.assertIn("сохраните на сервер", notice.text)
        self.clock.advance(hours=3)
        self.assertEqual(self.show(), [])                      # повтор не чаще, чем раз в 4 часа
        self.clock.advance(hours=1, minutes=1)
        self.assertEqual(len(self.show()), 1)

    def test_unsaved_timer_resets_when_saved(self):
        self.notifier.update([info(unsaved=2)])
        self.clock.advance(hours=5)
        self.notifier.update([info(unsaved=0)])                # сохранили
        self.notifier.update([info(unsaved=1)])                # снова правят
        self.assertEqual(self.show(), [])

    def test_unsaved_hours_setting(self):
        self.settings.notify_unsaved_hours = 1
        self.notifier.update([info(unsaved=2)])
        self.clock.advance(hours=1, minutes=1)
        self.assertEqual(len(self.show()), 1)
        self.settings.notify_unsaved_hours = 0                 # «не напоминать»
        self.notifier.update([info("Другой", unsaved=1)])
        self.clock.advance(hours=48)
        self.assertEqual(self.show(), [])

    def test_no_reminder_for_conflict_or_tag(self):
        self.notifier.update([info("С", unsaved=2, conflicts=1), info("Т", unsaved=2, read_only=True)])
        self.clock.advance(hours=10)
        kinds = [(n.kind, n.title) for n in self.show()]
        self.assertEqual(kinds, [("conflict", "С")])           # о конфликте сказано, о правках - нет

    def test_conflict_notified_once(self):
        self.notifier.update([info(conflicts=1)])
        self.assertEqual(len(self.show()), 1)
        self.assertEqual(self.show(), [])
        self.settings.notify_important_only = True
        self.notifier.update([info("Другой", conflicts=2)])
        self.assertEqual([n.kind for n in self.show()], ["conflict"])           # важное показывается

    def test_muted_project(self):
        self.notifier.update([info("А", incoming=1), info("Б", incoming=1)])
        self.settings.notify_muted = [os.path.join("C:\\Проекты", "А").lower()]
        self.assertEqual([n.title for n in self.show()], ["Б"])

    def test_project_with_check_error_is_silent(self):
        self.notifier.update([info(incoming=3, error="auth")])
        self.assertEqual(self.show(), [])

    def test_snooze(self):
        self.notifier.update([info(incoming=1)])
        self.notifier.snooze(2)
        self.assertTrue(self.notifier.snoozed)
        self.assertEqual(self.notifier.candidates(), [])
        self.clock.advance(hours=2, minutes=1)
        self.assertFalse(self.notifier.snoozed)
        self.assertEqual(len(self.notifier.candidates()), 1)

    def test_cancel_snooze(self):
        self.notifier.snooze()
        self.notifier.cancel_snooze()
        self.assertFalse(self.notifier.snoozed)

    def test_quiet_hours_block_and_release(self):
        self.settings.notify_quiet = True
        self.notifier.update([info(incoming=1)])
        self.clock.now = datetime(2026, 10, 21, 19, 0)
        self.assertEqual(self.notifier.candidates(), [])
        self.clock.now = datetime(2026, 10, 22, 9, 1)
        self.assertEqual(len(self.notifier.candidates()), 1)

    def test_force_ignores_dedup_snooze_and_quiet(self):
        self.settings.notify_quiet = True
        self.settings.notify_enabled = False
        self.notifier.snooze()
        self.notifier.update([info(incoming=1, unsaved=1)])
        self.clock.now = datetime(2026, 10, 21, 23, 0)
        self.show(force=True)
        self.assertEqual({n.kind for n in self.notifier.candidates(force=True)}, {"incoming", "unsaved"})

    def test_later_postpones_even_with_same_signature(self):
        self.notifier.update([info(incoming=1)])
        (notice,) = self.show()
        self.notifier.postpone(notice)
        self.assertEqual(self.notifier.candidates(), [])
        self.clock.advance(minutes=59)
        self.assertEqual(self.notifier.candidates(), [])
        self.clock.advance(minutes=2)
        self.assertEqual(len(self.notifier.candidates()), 1)

    def test_app_update(self):
        self.assertEqual(check_app_update("1.2.0", "1.1.9"), "1.2.0")
        self.assertEqual(check_app_update("1.1.0", "1.1.0"), "")
        self.assertEqual(check_app_update("", "1.1.0"), "")
        (notice,) = self.show(latest_version="1.2.0", current_version="1.1.0")
        self.assertEqual(notice.kind, "app_update")
        self.assertEqual(notice.text, "Доступна новая версия SVN-lite 1.2.0.")
        self.assertEqual(self.show(latest_version="1.2.0", current_version="1.1.0"), [])
        self.clock.advance(hours=23)
        self.assertEqual(self.show(latest_version="1.2.0", current_version="1.1.0"), [])
        self.clock.advance(hours=2)                            # прошли сутки, версию так и не поставили
        self.assertEqual(len(self.show(latest_version="1.2.0", current_version="1.1.0")), 1)
        self.assertEqual(len(self.show(latest_version="1.3.0", current_version="1.1.0")), 1)   # вышла ещё новее

    def test_app_update_weekly_and_switch(self):
        self.settings.notify_app_update_days = 7
        self.show(latest_version="1.2.0", current_version="1.1.0")
        self.clock.advance(days=3)
        self.assertEqual(self.show(latest_version="1.2.0", current_version="1.1.0"), [])
        self.settings.notify_app_update = False
        self.clock.advance(days=10)
        self.assertEqual(self.show(latest_version="1.2.0", current_version="1.1.0"), [])

    def test_many_notices_are_grouped(self):
        self.notifier.update([info(f"П{i}", incoming=1) for i in range(5)])
        (notice,) = self.show()
        self.assertEqual(notice.kind, "incoming")
        self.assertIn("5 проектах", notice.text)
        self.assertEqual(len(notice.parts), 5)
        self.notifier.update([info(f"П{i}", incoming=1) for i in range(5)])
        self.assertEqual(self.show(), [])                      # все пять отмечены показанными

    def test_few_notices_are_separate(self):
        self.notifier.update([info(f"П{i}", incoming=1) for i in range(3)])
        self.assertEqual(len(self.show()), 3)


# --------------------------------------------------------------------------
# Проверка проектов на fake_svn
# --------------------------------------------------------------------------

class FakeSvnCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "Проекты")
        self.wc = os.path.join(self.base, "копия")
        os.makedirs(os.path.join(self.wc, ".svn"))
        os.makedirs(os.path.join(self.wc, "backup"))
        os.makedirs(os.path.join(self.base, "просто папка"))
        os.makedirs(os.path.join(self.base, ".скрытая", ".svn"))
        os.environ["FAKE_SVN_MODE"] = "ok"
        self.addCleanup(os.environ.pop, "FAKE_SVN_MODE", None)
        self.client = SvnClient(FAKE)

    def test_discover_projects(self):
        found = discover_projects([self.base, self.base, os.path.join(self.tmp.name, "нет такой")])
        self.assertEqual([name for _path, name in found], ["копия"])

    def test_check_reports_incoming_unsaved_and_conflict(self):
        result = check_projects(self.client, discover_projects([self.base]))
        self.assertFalse(result.offline or result.auth_failed)
        (project,) = result.projects
        self.assertEqual(project.name, "копия")
        self.assertEqual(project.incoming, 1)
        self.assertEqual(project.authors, "иванов, петров")
        self.assertGreater(project.unsaved, 0)
        self.assertEqual(project.conflicts, 1)
        self.assertEqual(project.error, "")

    def test_offline_stops_check(self):
        os.environ["FAKE_SVN_MODE"] = "noconn"
        result = check_projects(self.client, [(self.wc, "копия"), (self.wc, "ещё")])
        self.assertTrue(result.offline)
        self.assertEqual(result.projects, [])

    def test_auth_is_not_requested_in_background(self):
        os.environ["FAKE_SVN_MODE"] = "auth"
        result = check_projects(self.client, [(self.wc, "копия")])
        self.assertTrue(result.auth_failed)
        self.assertEqual(result.projects, [])

    def test_other_error_marks_only_that_project(self):
        os.environ["FAKE_SVN_MODE"] = "authz"
        result = check_projects(self.client, [(self.wc, "копия")])
        self.assertEqual(len(result.projects), 1)
        self.assertNotEqual(result.projects[0].error, "")

    def test_skip_busy_projects(self):
        result = check_projects(self.client, [(self.wc, "копия")], skip=lambda path: True)
        self.assertEqual(result.projects, [])

    def test_end_to_end_with_policy(self):
        result = check_projects(self.client, discover_projects([self.base]))
        policy = Notifier(lambda: Settings(), Clock())
        policy.update(result.projects)
        self.assertEqual({n.kind for n in policy.candidates()}, {"conflict", "incoming"})


# --------------------------------------------------------------------------
# Окошко: позиция и стопка
# --------------------------------------------------------------------------

class PopupGeometryTests(unittest.TestCase):
    def test_single_window_sits_in_bottom_right_above_taskbar(self):
        from svnhelper.gui.popup import stack_positions
        area = (0, 0, 1920, 1040)          # экран 1080, панель задач 40 внизу
        self.assertEqual(stack_positions(area, [100], 360, 12, 8), [(1548, 928)])

    def test_stack_grows_upwards(self):
        from svnhelper.gui.popup import stack_positions
        self.assertEqual(stack_positions((0, 0, 1920, 1040), [100, 80, 60], 360, 12, 8),
                         [(1548, 928), (1548, 840), (1548, 772)])

    def test_taskbar_on_the_right_shifts_x(self):
        from svnhelper.gui.popup import stack_positions
        self.assertEqual(stack_positions((0, 0, 1860, 1080), [100], 360, 12, 8), [(1488, 968)])

    def test_scaled_sizes(self):
        from svnhelper.gui.popup import stack_positions
        px = lambda v: int(v * 1.5)        # масштаб 150%  # noqa: E731
        self.assertEqual(stack_positions((0, 0, 1920, 1040), [px(100)], px(360), px(12), px(8)), [(1362, 872)])

    def test_never_above_top_edge(self):
        from svnhelper.gui.popup import stack_positions
        places = stack_positions((0, 0, 800, 300), [200, 200], 100, 10, 10)
        self.assertEqual([y for _x, y in places], [90, 0])

    def test_work_area_fallback_without_windows_api(self):
        from svnhelper.gui import popup
        root = mock.Mock()
        root.winfo_screenwidth.return_value, root.winfo_screenheight.return_value = 1366, 768
        fake_windll = mock.Mock()
        fake_windll.user32.SystemParametersInfoW.return_value = 0
        with mock.patch.object(popup.ctypes, "windll", fake_windll, create=True), \
                mock.patch.object(popup.sys, "platform", "win32"):
            self.assertEqual(popup.work_area(root), (0, 0, 1366, 720))

    def test_work_area_from_windows(self):
        from svnhelper.gui import popup

        def spi(_action, _param, rect_ref, _flags):
            rect = rect_ref._obj
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1032
            return 1
        fake_windll = mock.Mock()
        fake_windll.user32.SystemParametersInfoW.side_effect = spi
        with mock.patch.object(popup.ctypes, "windll", fake_windll, create=True), \
                mock.patch.object(popup.sys, "platform", "win32"):
            self.assertEqual(popup.work_area(mock.Mock()), (0, 0, 1920, 1032))

    def test_snooze_until(self):
        from datetime import datetime, timedelta
        now = [datetime(2026, 10, 27, 13, 30)]
        n = Notifier(lambda: Settings(), now=lambda: now[0])
        self.assertIsNone(n.snooze_until)
        n.snooze()
        self.assertEqual(n.snooze_until, now[0] + timedelta(hours=notifier.SNOOZE_HOURS))
        now[0] += timedelta(hours=3)
        self.assertIsNone(n.snooze_until)                       # пауза кончилась

    def test_failed_author_lookup_is_logged(self):
        client = mock.Mock()
        client.incoming_log.side_effect = notifier.SvnError(notifier.ErrorKind.NO_CONNECTION, "x")
        with self.assertLogs("svnhelper.notifier", level="WARNING"):
            self.assertEqual(notifier._authors_text(client, "C:\\p"), "")

    def test_tray_menu_has_check_now(self):
        self.assertIn((tray.EVENT_CHECK, "Проверить сейчас"), tray.DEFAULT_MENU)
        self.assertIn(f"{notifier.SNOOZE_HOURS} часа", dict(tray.DEFAULT_MENU)[tray.EVENT_SNOOZE])


# --------------------------------------------------------------------------
# Окно: таймер, потоки, показ
# --------------------------------------------------------------------------

@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class AppNotifyTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(release_test_windows, self, set(vars(self)))
        from svnhelper.gui import App
        for target, kwargs in (("svnhelper.gui.download_tab.load_tree_cache", {"return_value": {}}),
                               ("svnhelper.gui.download_tab.save_tree_cache", {}),
                               ("svnhelper.tortoise.find_tortoiseproc", {"return_value": None}),
                               ("svnhelper.gui.app.autostart.is_enabled", {"return_value": False}),
                               ("svnhelper.gui.app.autostart.enable", {}),
                               ("svnhelper.gui.app.autostart.disable", {}),
                               ("svnhelper.gui.app.autostart.refresh", {"return_value": False}),
                               ("svnhelper.gui.popup.work_area", {"return_value": (0, 0, 1280, 700)}),
                               ("svnhelper.gui.popup._no_activate", {})):
            patch = mock.patch(target, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.clipboard_get = mock.Mock(side_effect=tk.TclError("clipboard disabled in tests"))
        self.addCleanup(self.root.destroy)
        self.app = App(self.root, Config(server="svn://x", repos=["a"], work_dir=os.path.join(self.tmp.name, "dl")),
                       SettingsStore(Path(self.tmp.name) / "s.json"), None, os.path.join(self.tmp.name, "log.txt"))
        self.addCleanup(self.app.popups.close_all)
        self.shown = []
        real_show = self.app.popups.show
        self.app.popups.show = lambda notice, seconds: (self.shown.append((notice, seconds)), real_show(notice, seconds))
        self.app.client = mock.Mock()      # настоящий svn не запускается: проверка проектов подменена

    def result(self, *infos):
        return CheckResult(list(infos))

    def run_check(self, result, manual=False):
        with mock.patch("svnhelper.gui.notify_ui.check_projects", return_value=result):
            self.app._notice_force = manual
            self.app._notice_start_check()
            self.assertTrue(self.app._notice_checking)
            self.pump()

    def pump(self):
        """Выполняет то, что фоновый поток передал в главный (пока не пусто)."""
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                fn, args = self.app._queue.get(timeout=0.05)
            except Exception:
                if not self.app._notice_checking:
                    return
                continue
            fn(*args)
            if not self.app._notice_checking:
                return
        self.fail("фоновая проверка не завершилась")

    def test_background_check_runs_off_the_main_thread_and_shows_in_main(self):
        import threading
        threads = {}

        def fake_check(client, projects, rules, skip):
            threads["check"] = threading.current_thread()
            return self.result(info(incoming=2, authors="Иван"))

        orig_show = self.app.popups.show
        self.app.popups.show = lambda n, s: (threads.setdefault("show", threading.current_thread()), orig_show(n, s))
        with mock.patch("svnhelper.gui.notify_ui.check_projects", side_effect=fake_check):
            self.app._notice_start_check()
            self.pump()
        self.assertIsNot(threads["check"], threading.main_thread())
        self.assertIs(threads["show"], threading.main_thread())

    def test_incoming_notice_shown_once_with_configured_duration(self):
        self.app.settings.notify_popup_sec = 30
        result = self.result(info(incoming=2, authors="Иван"))
        self.run_check(result)
        self.assertEqual(len(self.shown), 1)
        notice, seconds = self.shown[0]
        self.assertEqual((notice.kind, seconds), ("incoming", 30))
        self.run_check(result)
        self.assertEqual(len(self.shown), 1)                   # то же число новых: повторно не показываем

    def test_not_shown_while_window_is_active(self):
        with mock.patch.object(self.app, "_window_active", return_value=True):
            self.run_check(self.result(info(incoming=2)))
        self.assertEqual(self.shown, [])
        self.app._notice_tick(reschedule=False)                # окно ушло из фокуса: показываем отложенное
        self.assertEqual(len(self.shown), 1)

    def test_window_active_logic(self):
        self.assertFalse(self.app._window_active())            # окно скрыто в трее
        self.root.deiconify()
        with mock.patch.object(self.root, "focus_displayof", return_value=self.root):
            self.assertTrue(self.app._window_active())
        with mock.patch.object(self.root, "focus_displayof", return_value=None):
            self.assertFalse(self.app._window_active())

    def test_manual_check_with_nothing_new_says_so(self):
        self.run_check(self.result(info()), manual=True)
        (notice, _), = self.shown
        self.assertEqual(notice.kind, "info")

    def test_offline_and_auth_are_silent_unless_manual(self):
        self.run_check(CheckResult(offline=True))
        self.run_check(CheckResult(auth_failed=True))
        self.assertEqual(self.shown, [])
        self.run_check(CheckResult(offline=True), manual=True)
        self.assertEqual(len(self.shown), 1)
        self.assertIn("Нет связи", self.shown[0][0].text)

    def test_timer_starts_check_only_when_due_and_idle(self):
        with mock.patch.object(self.app, "_notice_start_check") as start:
            self.app._notice_tick(reschedule=False)            # ни разу не проверяли: пора
            start.assert_called_once()
            start.reset_mock()
            self.app._notice_last_check = self.app.notifier.timestamp()
            self.app._notice_tick(reschedule=False)
            start.assert_not_called()
            self.app._notice_last_check = self.app.notifier.timestamp() - 16 * 60
            self.app.upd = object()                            # идёт обновление проекта: не мешаем
            self.app._notice_tick(reschedule=False)
            start.assert_not_called()
            self.app.upd = None
            self.app.settings.notify_enabled = False
            self.app._notice_tick(reschedule=False)
            start.assert_not_called()
            self.app.request_notice_check()                    # «Проверить сейчас» работает и при выключенных
            start.assert_called_once()

    def test_manual_only_mode_never_starts_by_timer(self):
        self.app.settings.notify_check_min = 0
        with mock.patch.object(self.app, "_notice_start_check") as start:
            self.app._notice_tick(reschedule=False)
            start.assert_not_called()

    def test_tray_events(self):
        with mock.patch.object(self.app, "request_notice_check") as check:
            self.app._handle_tray_event(tray.EVENT_CHECK)
            check.assert_called_once()
        self.app._handle_tray_event(tray.EVENT_SNOOZE)
        self.assertTrue(self.app.notifier.snoozed)
        self.assertIn("2 часа", self.shown[-1][0].text)
        self.app._handle_tray_event(tray.EVENT_SNOOZE)
        self.assertFalse(self.app.notifier.snoozed)

    def test_live_status_of_projects_tab_cancels_stale_reminder(self):
        from svnhelper.gui.models import MineItem
        from svnhelper.svn_core import WcStatus
        project = info(unsaved=3)
        self.run_check(self.result(project))
        self.shown.clear()
        item = MineItem(project.path, project.name, "svn", status=WcStatus(), checked=True,
                        last_check=__import__("time").monotonic() + 5)       # вкладка проверила позже: правок нет
        self.app.mine = {"i": item}
        live = self.app._live_infos()
        self.assertEqual(live[0].unsaved, 0)

    def test_open_notice_shows_window_and_project_tab(self):
        from svnhelper.gui.models import MineItem
        item = MineItem(os.path.join(self.tmp.name, "dl", "Проект"), "Проект", "svn")
        iid = self.app._insert_mine_row(item)
        self.app.mine[iid] = item
        notice = notifier.Notice("incoming:x", "incoming", "Проект", "t", item.path, "incoming")
        with mock.patch.object(self.app, "show_window") as show, \
                mock.patch.object(self.app, "open_project_tab") as open_tab, \
                mock.patch.object(self.app, "_on_mine_select"):
            self.app._notice_open(notice)
        show.assert_called_once()
        open_tab.assert_called_once_with("incoming")
        self.assertEqual(self.app.mine_tree.selection(), (iid,))

    def test_open_notice_from_download_subtab_returns_to_my_projects(self):
        """U4.3: окно было на «Скачать проект» - «Открыть» возвращает на «Мои проекты» и открывает вкладку проекта."""
        from svnhelper.gui.models import MineItem
        item = MineItem(os.path.join(self.tmp.name, "dl", "Проект"), "Проект", "svn")
        iid = self.app._insert_mine_row(item)
        self.app.mine[iid] = item
        self.app._mine_dirty = False
        self.app.select_list_mode("download")
        notice = notifier.Notice("incoming:x", "incoming", "Проект", "t", item.path, "locks")
        with mock.patch.object(self.app, "show_window"), mock.patch.object(self.app, "_on_check_all"):
            self.app._notice_open(notice)
        self.assertEqual(self.app._list_mode(), "mine")
        self.assertEqual(self.app.project_panel.winfo_manager(), "grid")
        self.assertEqual(self.app.project_tabs.select(), str(self.app.project_tab_frames["locks"]))
        self.assertEqual(self.app.mine_tree.selection(), (iid,))
        # проекта ещё нет в списке (другая папка): тоже возврат на «Мои проекты»
        self.app.select_list_mode("download")
        other = notifier.Notice("incoming:y", "incoming", "Другой", "t", os.path.join(self.tmp.name, "b", "x"), "")
        with mock.patch.object(self.app, "show_window"), mock.patch.object(self.app, "_switch_project_dir"), \
                mock.patch.object(self.root, "after"):
            self.app._notice_open(other)
        self.assertEqual(self.app._list_mode(), "mine")

    def test_busy_snapshot_includes_running_update_and_commit(self):
        from svnhelper.gui.models import MineItem, UpdateJob
        folder = lambda name: os.path.join(self.tmp.name, name)  # noqa: E731
        updating, saving = MineItem(folder("a"), "a", "svn"), MineItem(folder("b"), "b", "svn")
        self.app._tortoise_open.add(folder("t"))
        self.app.upd = UpdateJob(updating)
        self.app._committing, self.app._commit_item = True, saving
        busy = self.app._busy_paths()
        self.assertIsInstance(busy, frozenset)                  # снимок, а не изменяемый set окна
        self.assertEqual(busy, frozenset(notifier.norm(folder(n)) for n in "abt"))
        self.app._tortoise_open.clear()
        self.assertEqual(len(busy), 3)                          # снимок не меняется вслед за окном

    def test_projects_busy_at_start_are_skipped_in_background(self):
        busy_path = os.path.join(self.tmp.name, "busy")
        self.app._tortoise_open.add(busy_path)
        skipped = {}

        def fake_check(client, projects, rules, skip):
            skipped["busy"], skipped["free"] = skip(busy_path), skip(os.path.join(self.tmp.name, "free"))
            return self.result()

        with mock.patch("svnhelper.gui.notify_ui.check_projects", side_effect=fake_check):
            self.app._notice_start_check()
            self.app._tortoise_open.clear()                     # окно изменилось, пока шла проверка
            self.pump()
        self.assertEqual(skipped, {"busy": True, "free": False})

    def test_projects_are_discovered_in_background_thread(self):
        import threading
        threads = {}

        def fake_discover(dirs):
            threads["discover"] = threading.current_thread()
            return []

        with mock.patch("svnhelper.gui.notify_ui.discover_projects", side_effect=fake_discover), \
                mock.patch("svnhelper.gui.notify_ui.check_projects", return_value=self.result()):
            self.app._notice_start_check()
            self.pump()
        self.assertIsNot(threads["discover"], threading.main_thread())

    def test_first_hide_to_tray_shows_hint_once(self):
        self.app.hide_to_tray()
        self.assertTrue(self.app.settings.tray_hint_shown)
        self.assertEqual(len(self.shown), 1)
        self.assertIn("у часов", self.shown[0][0].text)
        self.app.hide_to_tray()
        self.assertEqual(len(self.shown), 1)                    # второй раз и дальше - молча
        self.assertTrue(SettingsStore(self.app.settings_store.path).settings.tray_hint_shown)

    def test_tray_menu_label_shows_snooze_state(self):
        self.assertEqual(self.app.tray_labels(), {})
        icon = tray.Tray(lambda event: None, labels=self.app.tray_labels)
        self.assertEqual(dict(icon.menu_items())[tray.EVENT_SNOOZE], "Не беспокоить 2 часа")
        self.app.notifier.snooze()
        label = dict(icon.menu_items())[tray.EVENT_SNOOZE]
        self.assertRegex(label, r"^Не беспокоить: включено до \d\d:\d\d - выключить$")
        self.assertEqual(dict(icon.menu_items())[tray.EVENT_QUIT], "Выйти")
        self.app.notifier.cancel_snooze()
        self.assertEqual(dict(icon.menu_items())[tray.EVENT_SNOOZE], "Не беспокоить 2 часа")

    def test_background_error_and_login_dialogs_show_window_first(self):
        self.assertEqual(self.root.state(), "withdrawn")
        with mock.patch.object(self.app, "show_window") as show, mock.patch("svnhelper.gui.app.ErrorDialog"), \
                mock.patch.object(self.root, "wait_window"):
            self.app.show_error("Ошибка", "Текст", action="Проверка")
        show.assert_called_once()
        with mock.patch.object(self.app, "show_window") as show, mock.patch("svnhelper.gui.login.LoginDialog") as dlg, \
                mock.patch.object(self.root, "wait_window"):
            dlg.return_value.result = None
            self.app.login._ask(False)
        show.assert_called_once()
        with mock.patch.object(self.app, "show_window") as show:     # окно уже на экране: ничего не делаем
            self.root.deiconify()
            self.app.reveal_window()
        show.assert_not_called()

    def test_self_update_offer_waits_while_hidden_in_tray(self):
        self.app.config.latest_version = "99.0"
        self.assertEqual(self.root.state(), "withdrawn")
        with mock.patch("tkinter.messagebox.askyesno") as ask:
            self.app._offer_self_update()
            ask.assert_not_called()
            self.assertEqual(self.app._offered_version, "")      # вопрос не считается заданным
            self.root.deiconify()
            ask.return_value = False
            self.app._offer_self_update()
            ask.assert_called_once()
            self.assertEqual(self.app._offered_version, "99.0")

    def test_self_update_waits_for_saving_and_locking(self):
        for flag in ("_committing", "_locking"):
            setattr(self.app, flag, True)
            with mock.patch("tkinter.messagebox.showinfo") as info, mock.patch.object(self.app, "run_bg") as run:
                self.app._start_self_update()
            info.assert_called_once()
            run.assert_not_called()
            setattr(self.app, flag, False)

    def test_autostart_with_stale_path_is_rewritten_when_enabled(self):
        with mock.patch("svnhelper.gui.app.autostart.is_enabled", return_value=True), \
                mock.patch("svnhelper.gui.app.autostart.refresh") as refresh:
            self.app._apply_autostart(True)
            refresh.assert_called_once()
            refresh.reset_mock()
            self.app._apply_autostart(False)                     # выключили: путь переписывать незачем
            refresh.assert_not_called()

    def test_later_button_postpones(self):
        project = info(incoming=2)
        self.run_check(self.result(project))
        notice = self.shown[0][0]
        self.app._notice_later(notice)
        self.run_check(self.result(project))
        self.assertEqual(len(self.shown), 1)

    def test_apply_settings_saves_notify_values_without_restart(self):
        values = {"config_path": "", "username": "", "password": "", "remember": True, "server": "svn://x",
                  "repos": "a", "last_dir": "", "svn_path": "", "skip_junk": True, "show_report": True,
                  "notify_check_min": 60, "notify_quiet": True, "notify_quiet_from": "20:00",
                  "notify_muted": ["C:\\a"], "notify_repeat_hours": 0}
        with mock.patch.object(self.app, "_rebuild_config"):
            self.app._apply_settings(values)
        s = self.app.settings
        self.assertEqual((s.notify_check_min, s.notify_quiet, s.notify_quiet_from, s.notify_muted),
                         (60, True, "20:00", ["C:\\a"]))
        self.assertEqual(s.notify_repeat_hours, 4)             # ноль заменён
        self.assertEqual(SettingsStore(self.app.settings_store.path).settings.notify_check_min, 60)
        self.assertEqual(self.app.notifier._settings().notify_check_min, 60)


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class PopupWidgetTests(unittest.TestCase):
    def setUp(self):
        from svnhelper import theme
        for target, kwargs in (("svnhelper.gui.popup.work_area", {"return_value": (0, 0, 1280, 700)}),
                               ("svnhelper.gui.popup._no_activate", {})):
            patch = mock.patch(target, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.opened, self.later = [], []
        from svnhelper.gui.popup import PopupStack
        self.stack = PopupStack(self.root, lambda: theme.PALETTE, lambda v: v, self.opened.append, self.later.append)
        self.addCleanup(self.stack.close_all)

    def notice(self, n, kind="incoming"):
        return notifier.Notice(f"k{n}", kind, f"Проект {n}", "Есть новое", "C:\\p", "incoming")

    def test_popup_is_borderless_topmost_and_bottom_right(self):
        self.stack.show(self.notice(1), 0)
        (popup,) = self.stack.visible
        self.root.update()
        self.assertTrue(popup.overrideredirect())
        self.assertTrue(popup.attributes("-topmost"))
        self.assertEqual(popup.winfo_x() + popup.winfo_width(), 1280 - 12)
        self.assertEqual(popup.winfo_y() + popup.winfo_height(), 700 - 12)

    def test_stack_limit_and_queue(self):
        from svnhelper.gui.popup import MAX_VISIBLE
        for n in range(MAX_VISIBLE + 2):
            self.stack.show(self.notice(n), 0)
        self.assertEqual(len(self.stack.visible), MAX_VISIBLE)
        self.assertEqual(len(self.stack.waiting), 2)
        ys = [p.winfo_y() for p in self.stack.visible]
        self.assertEqual(ys, sorted(ys, reverse=True))        # каждое следующее выше предыдущего
        self.stack.visible[0].close()
        self.root.update()
        self.assertEqual(len(self.stack.visible), MAX_VISIBLE)  # освободилось место: вышло ожидавшее
        self.assertEqual(len(self.stack.waiting), 1)

    def test_same_notice_is_not_duplicated(self):
        self.stack.show(self.notice(1), 0)
        self.stack.show(self.notice(1), 0)
        self.assertEqual(len(self.stack.visible), 1)

    def test_autohide(self):
        self.stack.show(self.notice(1), 1)
        popup = self.stack.visible[0]
        popup._cancel_timer()
        with mock.patch.object(popup, "after", wraps=popup.after) as after:
            popup._start_timer()
        after.assert_called_once()
        self.assertEqual(after.call_args.args[0], 1000)
        popup.close()
        self.assertEqual(self.stack.visible, [])

    def test_until_closed_has_no_timer(self):
        self.stack.show(self.notice(1), 0)
        self.assertIsNone(self.stack.visible[0]._timer)

    def test_buttons_call_back_and_close(self):
        self.stack.show(self.notice(1), 0)
        self.stack.show(self.notice(2), 0)
        first, second = self.stack.visible
        first._act(self.stack._on_open)
        second._act(self.stack._on_later)
        self.assertEqual([n.key for n in self.opened], ["k1"])
        self.assertEqual([n.key for n in self.later], ["k2"])
        self.assertEqual(self.stack.visible, [])

    def test_info_notice_has_only_close(self):
        self.stack.show(notifier.sample_notice(), 0)
        popup = self.stack.visible[0]

        def texts(widget):
            found = [str(widget.cget("text"))] if "text" in widget.keys() else []
            for child in widget.winfo_children():
                found += texts(child)
            return found
        all_texts = texts(popup)
        self.assertIn("Закрыть", all_texts)
        self.assertNotIn("Открыть", all_texts)


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class SettingsDialogNotifyTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(release_test_windows, self, set(vars(self)))
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        grab = mock.patch.object(tk.Toplevel, "grab_set")
        grab.start()
        self.addCleanup(grab.stop)
        self.settings = Settings()

    def dialog(self, **kwargs):
        from svnhelper.gui.widgets import SettingsDialog
        dlg = SettingsDialog(self.root, Config(), self.settings, False, **kwargs)
        self.addCleanup(dlg.destroy)
        return dlg

    def test_defaults_in_values(self):
        values = self.dialog()._values()
        for key, default in NOTIFY_DEFAULTS.items():
            self.assertEqual(values[key], default, key)

    def test_fits_screen_width(self):
        dlg = self.dialog()
        self.root.update()
        self.assertLessEqual(dlg.winfo_reqwidth(), 800)

    def test_choices_map_to_numbers_and_custom_value_survives(self):
        self.settings.notify_check_min = 10                    # такого нет в списке, но его правили вручную
        dlg = self.dialog()
        self.assertEqual(dlg._values()["notify_check_min"], 10)
        dlg._vars["notify_check_min"].set("только вручную")
        dlg._vars["notify_popup_sec"].set("пока не закрою")
        values = dlg._values()
        self.assertEqual((values["notify_check_min"], values["notify_popup_sec"]), (0, 0))

    def test_invalid_time_is_replaced_with_message(self):
        dlg = self.dialog()
        dlg._vars["notify_quiet_from"].set("вечером")
        with mock.patch("svnhelper.gui.widgets.messagebox.showinfo") as info_box:
            dlg._ok()
        info_box.assert_called_once()
        self.assertIn("18:00", info_box.call_args.args[1])
        self.assertEqual(dlg.result["notify_quiet_from"], "18:00")

    def test_valid_values_saved_without_message(self):
        dlg = self.dialog()
        dlg._vars["notify_quiet_from"].set("9:30")
        with mock.patch("svnhelper.gui.widgets.messagebox.showinfo") as info_box:
            dlg._ok()
        info_box.assert_not_called()
        self.assertEqual(dlg.result["notify_quiet_from"], "09:30")

    def test_reset_to_defaults(self):
        self.settings.notify_check_min = 60
        self.settings.notify_important_only = True
        dlg = self.dialog()
        dlg._reset_notify()
        values = dlg._values()
        self.assertEqual((values["notify_check_min"], values["notify_important_only"]), (15, False))

    def test_muted_projects(self):
        base = os.path.join(tempfile.gettempdir(), "проекты")
        a, b, gone = os.path.join(base, "А"), os.path.join(base, "Б"), os.path.join(base, "убранный")
        self.settings.notify_muted = [a, gone]
        dlg = self.dialog(notify_projects=[(a, "А"), (b, "Б")])
        self.assertEqual(dlg._values()["notify_muted"], [gone, a])
        dlg._project_vars[0][1].set(False)
        dlg._project_vars[1][1].set(True)
        self.assertEqual(dlg._values()["notify_muted"], [gone, b])

    def test_test_notice_releases_grab_until_popup_closes(self):
        popups = {"open": True}
        dlg = self.dialog(test_cb=lambda: None, popup_open=lambda: popups["open"])
        regrabs = []
        with mock.patch.object(dlg, "grab_release") as release, mock.patch.object(dlg, "grab_set",
                                                                                  side_effect=lambda: regrabs.append(1)):
            dlg._show_test_notice()
            release.assert_called_once()                         # окошко живо: кнопки в нём должны нажиматься
            dlg._regrab_when_popup_closed()
            self.assertEqual(regrabs, [])
            popups["open"] = False
            dlg._regrab_when_popup_closed()
            self.assertEqual(regrabs, [1])                       # окошко закрыли: захват возвращён

    def test_test_button_present_only_with_callback(self):
        calls = []
        dlg = self.dialog(test_cb=lambda: calls.append(1))

        def buttons(widget):
            found = [widget] if widget.winfo_class() == "TButton" else []
            for child in widget.winfo_children():
                found += buttons(child)
            return found
        test_button = next(b for b in buttons(dlg) if b.cget("text") == "Проверить уведомления")
        test_button.invoke()
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
