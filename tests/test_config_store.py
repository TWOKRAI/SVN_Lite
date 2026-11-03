"""Task AG.0b: настройки под замком, to_dict ошибок, журнал агента."""
from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svnhelper import agentlog, localrepo                                     # noqa: E402
from svnhelper.config import Settings, SettingsStore, SettingsUnreadable      # noqa: E402
from svnhelper.importer import CreateError                                    # noqa: E402
from svnhelper.svn_core import ErrorKind, SvnError                            # noqa: E402

SECRET_RAW = ("svn: E170013: Unable to connect to a repository at URL 'svn://srv/x'\n"
              "svn: E215004: --password=SECRET --username ivanov password=SECRET")

_WRITER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from svnhelper.config import SettingsStore
store = SettingsStore(Path(sys.argv[2]))
tag = sys.argv[3]
for i in range(50):
    name = f"{tag}{i}"
    store.update(lambda s, n=name: s.favorites.append(n))
"""


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json"

    def test_update_does_not_lose_other_copy_changes(self):
        first, second = SettingsStore(self.path), SettingsStore(self.path)
        first.update(lambda s: setattr(s, "theme", "dark"))
        second.update(lambda s: setattr(s, "username", "ivanov"))
        saved = SettingsStore(self.path).settings
        self.assertEqual((saved.theme, saved.username), ("dark", "ivanov"))
        self.assertEqual((second.settings.theme, second.settings.username), ("dark", "ivanov"))

    def test_update_changes_settings_object_in_place(self):
        store = SettingsStore(self.path)
        window_ref = store.settings
        store.update(lambda s: setattr(s, "last_dir", "C:\\Проекты"))
        self.assertIs(store.settings, window_ref)
        self.assertEqual(window_ref.last_dir, "C:\\Проекты")

    def test_update_keeps_unsaved_window_edit_of_other_field(self):
        store = SettingsStore(self.path)
        store.settings.theme = "dark"                    # правка окна, ещё не сохранённая
        store.update(lambda s: setattr(s, "username", "ivanov"))
        self.assertEqual(store.settings.theme, "dark")
        self.assertEqual(store.settings.username, "ivanov")

    def test_save_keeps_local_histories_of_other_copy(self):
        window, agent = SettingsStore(self.path), SettingsStore(self.path)
        localrepo.register(agent, "C:\\p\\other", "C:\\h\\other")
        window.settings.theme = "dark"
        window.settings.local_histories = {"c:\\p\\mine": "C:\\h\\mine"}
        window.save()
        saved = SettingsStore(self.path).settings
        self.assertEqual(saved.local_histories, {"c:\\p\\other": "C:\\h\\other", "c:\\p\\mine": "C:\\h\\mine"})
        self.assertEqual(saved.theme, "dark")

    def test_unregister_removes_entry_only_through_update(self):
        window, agent = SettingsStore(self.path), SettingsStore(self.path)
        localrepo.register(window, "C:\\p\\a", "C:\\h\\a")
        localrepo.register(window, "C:\\p\\b", "C:\\h\\b")
        localrepo.unregister(agent, "C:\\p\\a")
        self.assertEqual(SettingsStore(self.path).settings.local_histories, {"c:\\p\\b": "C:\\h\\b"})
        window.refresh()
        self.assertEqual(window.settings.local_histories, {"c:\\p\\b": "C:\\h\\b"})

    def test_refresh_pulls_other_copy_records(self):
        window, agent = SettingsStore(self.path), SettingsStore(self.path)
        window.settings.theme = "dark"
        localrepo.register(agent, "C:\\p\\a", "C:\\h\\a")
        self.assertEqual(window.settings.local_histories, {})
        window.refresh()
        self.assertEqual(window.settings.local_histories, {"c:\\p\\a": "C:\\h\\a"})
        self.assertEqual(window.settings.theme, "dark")      # остальные поля refresh не трогает

    def test_refresh_without_file_changes_nothing(self):
        store = SettingsStore(self.path)
        store.settings.local_histories = {"k": "v"}
        store.refresh()
        self.assertEqual(store.settings.local_histories, {"k": "v"})

    def test_update_on_broken_file_starts_from_memory(self):
        store = SettingsStore(self.path)
        store.settings.theme = "dark"
        self.path.write_text("{ не json", encoding="utf-8")
        with self.assertLogs("svnhelper", "WARNING"):
            store.update(lambda s: setattr(s, "username", "ivanov"))
        saved = SettingsStore(self.path).settings
        self.assertEqual((saved.theme, saved.username), ("dark", "ivanov"))

    # --- чтение настроек при создании store ---

    def test_load_without_file_gives_default_settings(self):
        store = SettingsStore(self.path)
        self.assertEqual(store.settings.project_dirs, [])
        self.assertFalse(self.path.exists())                  # чтение ничего не создаёт

    def test_load_retries_busy_file_and_reads_real_settings(self):
        SettingsStore(self.path).update(lambda s: setattr(s, "username", "ivanov"))
        real_read = Path.read_bytes
        calls = []

        def flaky(path):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(32, "занято")
            return real_read(path)

        with mock.patch.object(Path, "read_bytes", flaky), mock.patch("svnhelper.config.time.sleep"):
            store = SettingsStore(self.path)
        self.assertEqual(store.settings.username, "ivanov")
        self.assertEqual(len(calls), 3)

    def test_load_of_file_that_stays_busy_is_logged_error_not_empty_settings(self):
        SettingsStore(self.path).update(lambda s: setattr(s, "username", "ivanov"))
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(32, "занято")) as read,                 mock.patch("svnhelper.config.time.sleep"):
            with self.assertLogs("svnhelper", "ERROR"):
                with self.assertRaises(SettingsUnreadable) as caught:
                    SettingsStore(self.path)
        self.assertIsInstance(caught.exception, OSError)
        self.assertEqual(read.call_count, 5)
        self.assertEqual(SettingsStore(self.path).settings.username, "ivanov")    # файл не тронут

    def test_load_of_broken_file_keeps_one_copy_and_does_not_rewrite_it(self):
        broken = "{ не json"
        self.path.write_text(broken, encoding="utf-8")
        with self.assertLogs("svnhelper", "WARNING"):
            store = SettingsStore(self.path)
        self.assertEqual(store.settings.project_dirs, [])
        with self.assertLogs("svnhelper", "WARNING"):
            SettingsStore(self.path)                           # второй запуск с тем же файлом: копия не множится
        copies = list(self.path.parent.glob("settings.json.bad-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8"), broken)
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)    # сам файл не затёрт

    def test_save_over_broken_file_keeps_its_copy(self):
        self.path.write_text("{ не json", encoding="utf-8")
        with self.assertLogs("svnhelper", "WARNING"):
            store = SettingsStore(self.path)
        store.settings.theme = "dark"
        store.save()
        self.assertEqual(SettingsStore(self.path).settings.theme, "dark")
        copies = list(self.path.parent.glob("settings.json.bad-*"))
        self.assertEqual([c.read_text(encoding="utf-8") for c in copies], ["{ не json"])

    def test_empty_file_is_default_settings_without_copy(self):
        self.path.write_text("", encoding="utf-8")
        with self.assertLogs("svnhelper", "WARNING"):
            SettingsStore(self.path)
        self.assertEqual(list(self.path.parent.glob("settings.json.bad-*")), [])

    def test_save_does_not_overwrite_file_it_cannot_read(self):
        store = SettingsStore(self.path)
        store.update(lambda s: setattr(s, "username", "ivanov"))
        store.settings.theme = "dark"
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(32, "занято")),                 mock.patch("svnhelper.config.time.sleep"):
            with self.assertLogs("svnhelper", "ERROR"):
                store.save()                                   # ошибка в журнал, файл остаётся прежним
        saved = SettingsStore(self.path).settings
        self.assertEqual((saved.username, saved.theme), ("ivanov", Settings().theme))

    def test_write_error_is_logged_and_raised(self):
        store = SettingsStore(self.path)
        with mock.patch("svnhelper.config.os.replace", side_effect=PermissionError("занято")) as replace, \
                mock.patch("svnhelper.config.time.sleep"):
            with self.assertLogs("svnhelper", "ERROR"):
                with self.assertRaises(PermissionError):
                    store.update(lambda s: setattr(s, "theme", "dark"))
        self.assertEqual(replace.call_count, 3)

    def test_update_retries_replace(self):
        store = SettingsStore(self.path)
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError("занято")
            real_replace(src, dst)

        with mock.patch("svnhelper.config.os.replace", side_effect=flaky), mock.patch("svnhelper.config.time.sleep"):
            store.update(lambda s: setattr(s, "theme", "dark"))
        self.assertEqual(SettingsStore(self.path).settings.theme, "dark")

    def test_two_processes_do_not_lose_updates(self):
        procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(ROOT), str(self.path), tag],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for tag in ("a", "b")]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(data["favorites"]), {f"{t}{i}" for t in "ab" for i in range(50)})
        self.assertEqual(len(data["favorites"]), 100)


class ToDictTests(unittest.TestCase):
    def assertNoSecret(self, value):
        text = json.dumps(value, ensure_ascii=False)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("password", text.lower())

    def test_svn_error(self):
        error = SvnError(ErrorKind.AUTH, SECRET_RAW, 1)
        data = error.to_dict()
        self.assertEqual(data["kind"], "auth")
        self.assertEqual(data["codes"], ["E170013", "E215004"])
        self.assertTrue(data["message"])
        self.assertNotIn("raw", data)
        self.assertNoSecret(data)

    def test_local_repo_error(self):
        error = localrepo.LocalRepoError(localrepo.FAILED, error=SvnError(ErrorKind.OTHER, SECRET_RAW), rolled_back=False)
        data = error.to_dict()
        self.assertEqual(set(data), {"code", "message", "rolled_back"})
        self.assertEqual(data["code"], localrepo.FAILED)
        self.assertIs(data["rolled_back"], False)
        self.assertNoSecret(data)

    def test_create_error(self):
        error = CreateError(SvnError(ErrorKind.NO_CONNECTION, SECRET_RAW), "import", rolled_back=True,
                            rollback_error=SECRET_RAW)
        data = error.to_dict()
        self.assertEqual(data["kind"], "no_connection")
        self.assertIs(data["rolled_back"], True)
        self.assertTrue(data["message"])
        self.assertNoSecret(data)

    def test_create_error_without_rollback(self):
        data = CreateError(SvnError(ErrorKind.EXISTS, "E160020"), "mkdir").to_dict()
        self.assertIsNone(data["rolled_back"])


class AgentLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.root = logging.getLogger("svnhelper")
        saved_handlers, saved_level = list(self.root.handlers), self.root.level
        self.addCleanup(self._restore, saved_handlers, saved_level)

    def _restore(self, handlers, level):
        for handler in list(self.root.handlers):
            if handler not in handlers:
                self.root.removeHandler(handler)
                handler.close()
        self.root.setLevel(level)

    def test_writes_to_monthly_file_and_does_not_touch_main_log(self):
        main_log = Path(self.tmp.name) / "svn_helper.log"
        main_log.write_text("окно\n", encoding="utf-8")
        path = agentlog.setup("cli", now=datetime(2026, 11, 2))
        self.assertEqual(path, Path(self.tmp.name) / "logs" / "svnlite_agent_2026-11.log")
        logging.getLogger("svnhelper.test").info("привет из агента")
        for handler in self.root.handlers:
            handler.flush()
        self.assertIn("[cli]", path.read_text(encoding="utf-8"))
        self.assertIn("привет из агента", path.read_text(encoding="utf-8"))
        self.assertEqual(main_log.read_text(encoding="utf-8"), "окно\n")

    def test_keeps_other_handlers_and_is_idempotent(self):
        window_handler = logging.NullHandler()
        self.root.addHandler(window_handler)
        agentlog.setup("cli")
        agentlog.setup("mcp")
        self.assertIn(window_handler, self.root.handlers)
        self.assertEqual(sum(1 for h in self.root.handlers if getattr(h, "_svnlite_agent_handler", False)), 1)

    def test_no_rotating_handler_and_no_stdout(self):
        out = io.StringIO()
        with redirect_stdout(out):
            agentlog.setup("mcp")
            logging.getLogger("svnhelper.test").warning("предупреждение")
        self.assertEqual(out.getvalue(), "")
        agent_handlers = [h for h in self.root.handlers if getattr(h, "_svnlite_agent_handler", False)]
        self.assertEqual(len(agent_handlers), 1)
        self.assertNotIsInstance(agent_handlers[0], logging.handlers.RotatingFileHandler)

    def test_password_does_not_reach_log(self):
        path = agentlog.setup("cli")
        logging.getLogger("svnhelper.test").error("команда svn --password SECRET и password=SECRET")
        raw = SECRET_RAW
        try:
            raise RuntimeError(raw)
        except RuntimeError:
            logging.getLogger("svnhelper.test").exception("сбой")
        for handler in self.root.handlers:
            handler.flush()
        self.assertNotIn("SECRET", path.read_text(encoding="utf-8"))

    def test_old_logs_removed_new_kept(self):
        folder = Path(self.tmp.name) / "logs"
        folder.mkdir()
        names = ["svnlite_agent_2026-06.log", "svnlite_agent_2026-07.log", "svnlite_agent_2026-08.log",
                 "svnlite_agent_2026-11.log", "svn_helper.log", "other.log"]
        for name in names:
            (folder / name).write_text("x", encoding="utf-8")
        agentlog.setup("cli", now=datetime(2026, 11, 2))
        left = {p.name for p in folder.iterdir()}
        self.assertEqual(left, {"svnlite_agent_2026-08.log", "svnlite_agent_2026-11.log", "svn_helper.log",
                                "other.log"})


if __name__ == "__main__":
    unittest.main()
