"""Прототип для показа: путь на диске вместо ссылки, создание проекта из папки, «Вернуть как было».

Только file:/// и локальный временный svnserve: боевой сервер не трогаем.
"""
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from svnhelper import importer, mirrors, server, tortoise  # noqa: E402
from svnhelper.config import (apply_config_data, apply_overrides, Config, FilterRules, mirrors_text,  # noqa: E402
                              parse_mirrors_text, Settings, SettingsStore)
from svnhelper.svn_core import CancelToken, ErrorKind, SvnClient, SvnError  # noqa: E402

HAS_SVN = bool(shutil.which("svn") and shutil.which("svnadmin"))
MIRRORS = [{"path": "Z:\\Проекты_SVN", "repo": "Проекты_SVN"}]


def run(*args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(f"{args}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode("utf-8", errors="replace")


def tree_hash(root: str) -> dict:
    """Имя → хэш содержимого и время изменения: исходная папка не должна меняться ни на байт."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if os.path.isfile(full):
                result[rel] = (hashlib.sha1(Path(full).read_bytes()).hexdigest(), os.stat(full).st_mtime_ns)
            else:
                result[rel] = "dir"
    return result


def make_source(root: Path) -> None:
    """Папка «как на Z:»: чертежи, мусор, открытый документ, служебная .git, ссылка на чужую папку."""
    (root / "Сборки").mkdir(parents=True)
    (root / "Сборки" / "узел 1.cdw").write_bytes(b"\x01" * 3000)
    (root / "спецификация.txt").write_text("состав", encoding="utf-8")
    (root / "Thumbs.db").write_text("junk")
    (root / "~$отчёт.docx").write_text("lock")
    (root / "старое.BAK").write_text("junk")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x")
    if os.name == "nt":
        import _winapi
        outside = root.parent / "чужая папка"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("не загружать")
        _winapi.CreateJunction(str(outside), str(root / "ссылка на папку"))


# --------------------------------------------------------------------------
# Путь на диске → хранилище (без svn)
# --------------------------------------------------------------------------

class MirrorMappingTests(unittest.TestCase):
    def test_local_path_detection(self):
        for text in ("Z:\\Проекты_SVN\\x", "z:/a", '"C:\\a b"', "\\\\fs01\\share", "//fs01/share", "Z:"):
            self.assertTrue(mirrors.looks_like_local_path(text), text)
        for text in ("svn://192.168.11.11/test", "test/projects/x", "file:///C:/r", ""):
            self.assertFalse(mirrors.looks_like_local_path(text), text)

    def test_case_slashes_and_trailing_slash_do_not_matter(self):
        for text in ("Z:\\Проекты_SVN\\Азов\\Проект 1", "z:/ПРОЕКТЫ_svn/Азов/Проект 1/", '"Z:\\Проекты_SVN\\Азов\\Проект 1\\"'):
            match = mirrors.map_path(text, MIRRORS, "svn://192.168.11.11/")
            self.assertIsNotNone(match, text)
            self.assertEqual((match.prefix, match.segments), ("svn://192.168.11.11", ("Проекты_SVN", "Азов", "Проект 1")))
        # имена внутри берутся как ввёл человек: на сервере регистр важен
        self.assertEqual(mirrors.map_path("z:\\проекты_svn\\азов", MIRRORS, "svn://h").rest, ("азов",))

    def test_unc_and_other_drive_letter(self):
        unc = {"Z:": "\\\\fs01\\Share", "Y:": "\\\\FS01\\share"}.get
        by_unc = mirrors.map_path("\\\\FS01\\share\\Проекты_SVN\\Азов", MIRRORS, "svn://h", unc)
        by_y = mirrors.map_path("Y:\\Проекты_SVN\\Азов", MIRRORS, "svn://h", unc)
        self.assertEqual(by_unc.segments, ("Проекты_SVN", "Азов"))
        self.assertEqual(by_y.segments, ("Проекты_SVN", "Азов"))
        # без сетевого адреса другая буква диска — это другая папка
        self.assertIsNone(mirrors.map_path("Y:\\Проекты_SVN\\Азов", MIRRORS, "svn://h", lambda _d: None))

    def test_outside_root_longest_and_full_url(self):
        self.assertIsNone(mirrors.map_path("C:\\Проекты_SVN\\x", MIRRORS, "svn://h"))
        self.assertIsNone(mirrors.map_path("Z:\\Проекты_SVN_старые\\x", MIRRORS, "svn://h"))
        self.assertEqual(mirrors.map_path("Z:\\Проекты_SVN", MIRRORS, "svn://h").rest, ())
        both = MIRRORS + [{"path": "Z:\\Проекты_SVN\\Азов", "repo": "svn://other:3690/azov/projects"}]
        match = mirrors.map_path("Z:\\Проекты_SVN\\Азов\\p", both, "svn://h")
        self.assertEqual((match.prefix, match.segments), ("svn://other:3690", ("azov", "projects", "p")))

    def test_config_and_settings(self):
        cfg = Config()
        apply_config_data(cfg, {"mirrors": [{"path": "Z:\\A", "repo": "/A/"}, {"path": 1}, "мусор"]})
        self.assertEqual(cfg.mirrors, [{"path": "Z:\\A", "repo": "A"}])
        apply_config_data(cfg, {"mirrors": "Z:"})
        self.assertTrue(any("mirrors" in w for w in cfg.warnings))
        self.assertEqual(cfg.mirrors, [{"path": "Z:\\A", "repo": "A"}])   # неверное значение не стирает прежнее
        settings = Settings(mirrors=parse_mirrors_text("D:\\Витрина = test/projects ; плохо; C:\\x="))
        self.assertEqual(settings.mirrors, [{"path": "D:\\Витрина", "repo": "test/projects"}])
        apply_overrides(cfg, settings)
        self.assertEqual([m["repo"] for m in cfg.mirrors], ["test/projects", "A"])   # свои первыми
        self.assertEqual(parse_mirrors_text(mirrors_text(cfg.mirrors)), cfg.mirrors)
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp) / "settings.json")
            store.settings.mirrors = settings.mirrors
            store.save()
            self.assertEqual(SettingsStore(Path(tmp) / "settings.json").settings.mirrors, settings.mirrors)


class ScanAndPatternsTests(unittest.TestCase):
    def test_patterns_ignore_case_and_spaces(self):
        rules = FilterRules(junk_dirs=["Old Stuff"])
        patterns = importer.name_patterns(rules)
        self.assertIn("~$*", patterns)
        self.assertIn("old?stuff", patterns)
        self.assertIn(".git", patterns)
        ignores = importer.svn_ignores(patterns)
        self.assertIn("[tT][hH][uU][mM][bB][sS].[dD][bB]", ignores)
        self.assertTrue(all(" " not in p for p in ignores))
        self.assertEqual(importer.name_patterns(FilterRules(enabled=False)), [".svn", "_svn", ".git", ".hg"])

    def test_scan_counts_and_warnings_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "Проект с Z")
            make_source(src)
            before = tree_hash(str(src))
            result, extra = importer.scan_source(str(src), importer.name_patterns(FilterRules()))
            self.assertEqual(tree_hash(str(src)), before)
            self.assertEqual((result.files, result.dirs), (2, 1))           # узел 1.cdw, спецификация.txt; Сборки
            self.assertEqual(result.size, 3000 + len("состав".encode("utf-8")))
            self.assertEqual(result.junk_files, 3)                            # Thumbs.db, ~$отчёт.docx, старое.BAK
            self.assertEqual(result.vcs_dirs, [".git"])
            self.assertEqual(result.open_files, ["отчёт.docx"])
            text = "\n".join(result.warnings())
            self.assertIn("открыты документы", text)
            self.assertIn(".git", text)
            if os.name == "nt":
                self.assertEqual(result.links, ["ссылка на папку"])
                self.assertEqual(extra, ["ссылка?на?папку"])
                self.assertIn("Ссылки на другие папки", text)

    def test_cancel_scan(self):
        token = CancelToken()
        token.cancel()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SvnError) as cm:
            importer.scan_source(tmp, [], token)
        self.assertIs(cm.exception.kind, ErrorKind.CANCELLED)


class TortoiseRevertTests(unittest.TestCase):
    def test_revert_command(self):
        args = tortoise.build_args("T.exe", "revert", ["C:\\Проекты\\a b"])
        self.assertEqual(args, ["T.exe", "/command:revert", "/path:C:\\Проекты\\a b"])
        self.assertIn('/path:"C:\\Проекты\\a b"', tortoise.command_line(args))


# --------------------------------------------------------------------------
# Настоящий svn через file:///
# --------------------------------------------------------------------------

def install_reject_hook(repo: Path, marker: str) -> None:
    """pre-commit, который отклоняет коммит с файлом marker (чтобы сломать загрузку после создания папок)."""
    svnlook = shutil.which("svnlook")
    findstr = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "findstr.exe")
    (repo / "hooks" / "pre-commit.bat").write_text(
        "@echo off\r\n"
        f'"{svnlook}" changed -t %2 %1 | "{findstr}" /C:"{marker}" >nul\r\n'
        "if %errorlevel%==0 (\r\n  echo rejected by test hook 1>&2\r\n  exit 1\r\n)\r\nexit 0\r\n",
        encoding="ascii")


@unittest.skipUnless(HAS_SVN, "нет svn/svnadmin")
class RealCreateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.repo = root / "repos" / "test"
        cls.repo.parent.mkdir()
        run("svnadmin", "create", str(cls.repo))
        cls.server = (root / "repos").as_uri()
        url = cls.repo.as_uri()
        run("svn", "mkdir", "--parents", "-m", "init", url + "/projects/Азов/есть проект/trunk",
            url + "/projects/Азов/просто папка")
        cls.client = SvnClient("svn")
        cls.mirrors = [{"path": str(root / "Z" / "Проекты"), "repo": "test/projects"}]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def target(self, *rest):
        match = mirrors.map_path(os.path.join(self.mirrors[0]["path"], *rest), self.mirrors, self.server)
        return mirrors.find_target(self.client, match)

    def test_find_target_states(self):
        found = self.target("Азов", "есть проект")
        self.assertEqual(found.kind, mirrors.PROJECT)
        self.assertEqual(found.project.segments, ("test", "projects", "Азов", "есть проект"))
        sub = self.target("Азов", "есть проект", "Сборки", "узел")
        self.assertEqual((sub.kind, sub.project.name, sub.subfolder), (mirrors.PROJECT, "есть проект",
                                                                       ("Сборки", "узел")))
        missing = self.target("Азов", "новый")
        self.assertEqual((missing.kind, missing.create_segments), (mirrors.MISSING, ("test", "projects", "Азов", "новый")))
        self.assertEqual(self.target("Азов", "просто папка").kind, mirrors.FOLDER)
        self.assertEqual(self.target().kind, mirrors.ROOT)
        wrong = mirrors.map_path("Q:\\x\\y", [{"path": "Q:\\x", "repo": "test/нет такой"}], self.server)
        self.assertEqual(mirrors.find_target(self.client, wrong).kind, mirrors.NO_REPO)

    def test_create_project_from_folder(self):
        src = Path(self.tmp.name, "Z", "Проекты", "Азов", "Проект @ 100% и пробел")
        make_source(src)
        before = tree_hash(str(src))
        url = server.build_url(self.server, ["test", "projects", "Азов", "Проект @ 100% и пробел"])
        head = int(run("svnlook", "youngest", str(self.repo)))
        patterns = importer.name_patterns(FilterRules())
        _scan, extra = importer.scan_source(str(src), patterns)
        result = importer.create_project(self.client, url, str(src), "Перенос из Z: тест",
                                         importer.svn_ignores(patterns + extra))
        self.assertEqual(int(run("svnlook", "youngest", str(self.repo))), head + 2)   # две ревизии
        self.assertEqual((result.revision, result.entries), (head + 2, 3))   # прогресс: строки «Adding …»
        names = sorted(e.name for e in self.client.list_entries(url + "/trunk@", recursive=True))
        self.assertEqual(names, ["Сборки", "Сборки/узел 1.cdw", "спецификация.txt"])   # мусора и ссылки нет
        self.assertEqual(sorted(self.client.list_dirs(url + "@")), ["branches", "tags", "trunk"])
        self.assertIn("Перенос из Z: тест", run("svn", "log", "--xml", "-l", "1", url + "@"))
        self.assertEqual(tree_hash(str(src)), before)                                  # источник не изменился
        self.assertFalse(os.path.exists(src / ".svn"))
        found = self.target("Азов", "Проект @ 100% и пробел")
        self.assertEqual(found.kind, mirrors.PROJECT)

        with self.assertRaises(importer.CreateError) as cm:   # второй раз: уже есть, ничего не создаётся
            importer.create_project(self.client, url, str(src), "ещё раз", [])
        self.assertEqual((cm.exception.kind, cm.exception.stage), (ErrorKind.EXISTS, "check"))
        self.assertEqual(int(run("svnlook", "youngest", str(self.repo))), head + 2)

    def test_failed_upload_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "r")
            run("svnadmin", "create", str(repo))
            install_reject_hook(repo, "FAIL_ME")
            src = Path(tmp, "src")
            src.mkdir()
            (src / "FAIL_ME.txt").write_text("x")
            url = server.build_url(repo.as_uri(), ["Азов", "проект"])
            with self.assertRaises(importer.CreateError) as cm:
                importer.create_project(self.client, url, str(src), "тест отката", [])
            self.assertEqual((cm.exception.stage, cm.exception.rolled_back), ("import", True))
            self.assertFalse(self.client.url_exists(url))
            self.assertIn("rejected by test hook", cm.exception.error.raw)

    def test_rollback_refuses_when_someone_changed_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "r")
            run("svnadmin", "create", str(repo))
            url = server.build_url(repo.as_uri(), ["p"])
            self.client.mkdir_urls([url + "/trunk"], "наше создание")
            first = self.client.last_log_entry(url)[0]
            run("svn", "mkdir", "-m", "чужая правка", url + "/trunk/чужое", "--username", "petrov")
            ok, reason = importer.rollback(self.client, url, first.revision, first.author)
            self.assertFalse(ok)
            self.assertIn("другие изменения", reason)
            self.assertTrue(self.client.url_exists(url))


@unittest.skipUnless(HAS_SVN and shutil.which("svnserve"), "нет svn/svnadmin/svnserve")
class SvnserveCreateTests(unittest.TestCase):
    """Создание проекта на локальном svnserve (svn://127.0.0.1:<порт>) с входом по паролю."""

    def test_create_over_svn_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "demo")
            run("svnadmin", "create", str(repo))
            (repo / "conf" / "svnserve.conf").write_text(
                "[general]\nanon-access = none\nauth-access = write\npassword-db = passwd\n")
            (repo / "conf" / "passwd").write_text("[users]\nivanov = пароль 1\n", encoding="utf-8")
            src = Path(tmp, "Конвейер линия 1")
            make_source(src)
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            srv = subprocess.Popen(["svnserve", "-d", "--foreground", "--listen-port", str(port), "-r", tmp])
            try:
                time.sleep(1)
                client = SvnClient("svn")
                client.store_auth = False
                url = server.build_url(f"svn://127.0.0.1:{port}", ["demo", "Азов", src.name])
                with self.assertRaises(importer.CreateError) as cm:   # без входа: отказ до создания чего-либо
                    importer.create_project(client, url, str(src), "x", [])
                self.assertEqual((cm.exception.kind, cm.exception.stage), (ErrorKind.AUTH, "check"))
                client.set_credentials("ivanov", "пароль 1")
                patterns = importer.name_patterns(FilterRules())
                _scan, extra = importer.scan_source(str(src), patterns)
                result = importer.create_project(client, url, str(src), "Перенос с Z:",
                                                 importer.svn_ignores(patterns + extra))
                self.assertEqual(result.revision, 2)
                self.assertEqual(client.last_log_entry(url)[0].author, "ivanov")
                names = sorted(e.name for e in client.list_entries(url + "/trunk", recursive=True))
                self.assertEqual(names, ["Сборки", "Сборки/узел 1.cdw", "спецификация.txt"])
            finally:
                srv.kill()
                srv.wait()


# --------------------------------------------------------------------------
# Окно: путь на диске на вкладке «Скачать проект» и окно «Новый проект из папки»
# --------------------------------------------------------------------------

try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    HAS_DISPLAY = True
except Exception:
    HAS_DISPLAY = False


@unittest.skipUnless(HAS_DISPLAY and HAS_SVN, "нет экрана или svn")
class GuiLocalPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RealCreateTests.setUpClass.__func__(cls)
        cls.patches = [mock.patch("svnhelper.gui.download_tab.load_tree_cache", return_value={}),
                       mock.patch("svnhelper.gui.download_tab.save_tree_cache"),
                       mock.patch("svnhelper.gui.App.show_error")]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in cls.patches:
            patch.stop()
        RealCreateTests.tearDownClass.__func__(cls)

    def pump(self, root, cond, timeout=30):
        end = time.monotonic() + timeout
        while not cond():
            root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    def test_local_path_flow_and_create(self):
        from svnhelper.gui import App
        from svnhelper.gui.create_dialog import CreateProjectDialog
        root = tk.Tk()
        root.withdraw()
        root.clipboard_get = mock.Mock(side_effect=tk.TclError("clipboard disabled in tests"))
        work_dir = os.path.join(self.tmp.name, "мои проекты")
        cfg = Config(server=self.server, repos=["test"], work_dir=work_dir, mirrors=self.mirrors)
        store = SettingsStore(Path(self.tmp.name) / "settings.json")
        app = App(root, cfg, store, SvnClient("svn"), os.path.join(self.tmp.name, "log.txt"))
        App.show_error.reset_mock()
        z = Path(self.mirrors[0]["path"])
        try:
            status = lambda: app.url_status.cget("text")  # noqa: E731
            # путь вне хранилища: понятный текст, кнопок нет
            app.url_var.set("C:\\Совсем другая папка\\проект")
            self.pump(root, lambda: "не входит в проекты на сервере" in status())
            self.assertIsNone(app.project)
            self.assertEqual(str(app.download_btn.cget("state")), "disabled")

            # проект есть: подставляется ссылка, дальше как обычно
            app.url_var.set(str(z / "Азов" / "есть проект" / "Сборки"))   # подпапка проекта
            self.pump(root, lambda: app.project is not None)
            self.assertEqual(app.project.name, "есть проект")
            self.assertTrue(app.url_var.get().startswith(self.server))
            self.assertIn("найден по пути", status())
            self.assertEqual(str(app.download_btn.cget("state")), "normal")
            self.assertFalse(app.create_btn.winfo_manager())

            # проекта нет: статус и кнопка «Создать проект из этой папки…»
            src = z / "Азов" / "Новый проект"
            make_source(src)
            before = tree_hash(str(src))
            app.url_var.set(str(src))
            self.pump(root, lambda: bool(app.create_btn.winfo_manager()))
            self.assertIn("ещё нет на сервере", status())
            self.assertEqual(str(app.create_btn.cget("state")), "normal")

            class AutoCreate(CreateProjectDialog):
                """Нажимает «Создать проект», как только папка просмотрена."""
                def _on_scanned(self, result, error):
                    super()._on_scanned(result, error)
                    self.after(10, self._on_create)

            asked = []

            def answer(title, text, **_kw):
                asked.append(title)
                return title != "Готово"     # создать — да, скачать — да, открыть папку — нет

            with mock.patch("svnhelper.gui.download_tab.CreateProjectDialog", AutoCreate), \
                    mock.patch("tkinter.messagebox.askyesno", side_effect=answer), \
                    mock.patch("tkinter.messagebox.showerror") as err:
                app._on_create_project()
                self.pump(root, lambda: app.job is None and "Готово" in asked)
                err.assert_not_called()
            self.assertEqual(asked[:2], ["Создать проект?", "Проект создан"])
            App.show_error.assert_not_called()
            dest = os.path.join(work_dir, "Новый проект")
            self.assertTrue(os.path.isfile(os.path.join(dest, "Сборки", "узел 1.cdw")))
            self.assertFalse(os.path.exists(os.path.join(dest, "Thumbs.db")))
            self.assertTrue(os.path.isdir(os.path.join(dest, ".svn")))
            self.assertEqual(tree_hash(str(src)), before)
            self.assertFalse(os.path.exists(src / ".svn"))
            self.assertEqual(app.project.name, "Новый проект")
            self.assertFalse(app.create_btn.winfo_manager())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()


class DataDirOverrideTests(unittest.TestCase):
    """SVNLITE_DATA_DIR: демо-режим держит настройки и журнал отдельно от личных."""

    def test_env_var_switches_data_dir(self):
        from svnhelper import config
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": tmp}):
            self.assertEqual(config.data_dir(), Path(tmp))
            self.assertEqual(config.shared_cache_path().parent, Path(tmp))
        with mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": ""}):
            self.assertEqual(config.data_dir().name, config.APP_DIR_NAME)
