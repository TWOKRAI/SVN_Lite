"""Интеграционные тесты на настоящем svn через file:/// (нужны svn и svnadmin)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svnhelper import server
from svnhelper.svn_core import ErrorKind, CheckoutProgress, SvnClient, SvnError

PROJECT = "261019_отбраковщик и делитель_тест"
PROJECT_PATH = ["projects", "Specific_projects", "Russia_Azov_AKF", PROJECT]


def run(*args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(f"{args}: {proc.stderr.decode(errors='replace')}")


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "нет svn/svnadmin")
class RealSvnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        repo = root / "repos" / "test"
        repo.parent.mkdir()
        run("svnadmin", "create", str(repo))
        cls.server = (root / "repos").as_uri()
        wc = root / "wc"
        run("svn", "checkout", repo.as_uri(), str(wc))
        proj = wc.joinpath(*PROJECT_PATH)
        for sub in ("trunk/docs sub", "tags", "branches"):
            (proj / sub).mkdir(parents=True)
        (proj / "trunk" / "чертёж 1.txt").write_text("hello", encoding="utf-8")
        (proj / "trunk" / "docs sub" / "b.bin").write_bytes(b"\x00" * 5000)
        junk = wc / "projects" / "junk_project" / "trunk"
        (junk / "backup").mkdir(parents=True)
        (wc / "projects" / "junk_project" / "tags").mkdir()
        (junk / "keep файл.txt").write_text("keep", encoding="utf-8")
        (junk / "Thumbs.db").write_text("junk")
        (junk / "~$doc.docx").write_text("junk")
        (junk / "backup" / "old.txt").write_text("junk")
        upd = wc / "projects" / "upd_project"
        (upd / "trunk").mkdir(parents=True)
        (upd / "tags").mkdir()
        (upd / "trunk" / "keep файл.txt").write_text("keep", encoding="utf-8")
        (wc / "projects" / "plain_folder").mkdir()
        (wc / "projects" / "plain_folder" / "f.txt").write_text("x")
        run("svn", "add", "--force", "--no-ignore", ".", cwd=wc)
        run("svn", "propset", "svn:needs-lock", "ON", str(proj / "trunk" / "docs sub" / "b.bin"))
        run("svn", "commit", "-m", "init", cwd=wc)
        cls.client = SvnClient("svn")
        cls.pool = ThreadPoolExecutor(6)

    @classmethod
    def tearDownClass(cls):
        cls.pool.shutdown()
        cls.tmp.cleanup()

    def test_tree_detects_projects(self):
        listing = server.load_folder(self.client, self.server, ["test", "projects", "Specific_projects",
                                                                "Russia_Azov_AKF"], self.pool)
        self.assertEqual([(e.name, e.is_project) for e in listing.entries], [(PROJECT, True)])
        top = server.load_folder(self.client, self.server, ["test", "projects"], self.pool)
        self.assertEqual([(e.name, e.is_project) for e in top.entries],
                         [("junk_project", True), ("plain_folder", False),
                          ("Specific_projects", False), ("upd_project", True)])

    def test_missing_folder_is_not_found(self):
        with self.assertRaises(SvnError) as cm:
            self.client.list_dirs(server.build_url(self.server, ["test", "nope"]))
        self.assertEqual(cm.exception.kind, ErrorKind.NOT_FOUND)
        with self.assertRaises(SvnError) as cm:
            self.client.list_dirs(server.build_url(self.server, ["norepo"]))
        self.assertIn(cm.exception.kind, (ErrorKind.NOT_FOUND, ErrorKind.NO_CONNECTION,
                                          ErrorKind.LOCAL_HISTORY_MISSING))   # file://: хранилища нет

    def test_estimate_and_checkout(self):
        url = server.build_url(self.server, ["test", *PROJECT_PATH]) + "/trunk"
        est = self.client.estimate(url)
        self.assertEqual((est.files, est.dirs, est.size), (2, 1, 5 + 5000))
        dest = os.path.join(self.tmp.name, "Проекты с пробелом", PROJECT)
        progress = CheckoutProgress()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        res = self.client.checkout(url, dest, progress)
        self.assertEqual(progress.count, est.entries)
        self.assertEqual(res.revision, 1)
        self.assertTrue(os.path.isfile(os.path.join(dest, "чертёж 1.txt")))
        self.assertFalse(os.access(os.path.join(dest, "docs sub", "b.bin"), os.W_OK))  # needs-lock
        self.assertFalse(os.path.exists(os.path.join(dest, "trunk")))

    def test_junk_is_excluded_after_checkout(self):
        from svnhelper.audit import analyze
        from svnhelper.config import FilterRules
        url = server.build_url(self.server, ["test", "projects", "junk_project"]) + "/trunk"
        est = self.client.estimate(url)
        dest = os.path.join(self.tmp.name, "junk dl", "junk_project")
        res = analyze(est.items, FilterRules(junk_dirs=["backup"]), dest)
        self.assertEqual(res.junk_paths, ["backup", "Thumbs.db", "~$doc.docx"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        self.client.checkout(url, dest)
        self.client.exclude_paths(dest, res.junk_paths)
        self.assertEqual(sorted(n for n in os.listdir(dest) if n != ".svn"), ["keep файл.txt"])
        status = subprocess.run(["svn", "status", dest], stdout=subprocess.PIPE).stdout.decode(errors="replace")
        self.assertEqual(status.strip(), "")  # не «потеряно» и не «изменено»
        self.client.run(["update", dest])    # обычный Update мусор не возвращает
        self.assertFalse(os.path.exists(os.path.join(dest, "Thumbs.db")))

    def test_info_status_update_and_locks(self):
        from svnhelper.audit import junk_among
        from svnhelper.config import FilterRules
        url = server.build_url(self.server, ["test", "projects", "upd_project"]) + "/trunk"
        root = Path(self.tmp.name) / "upd"
        mine, other = str(root / "моя копия"), str(root / "other")
        root.mkdir(exist_ok=True)
        self.client.checkout(url, mine)
        run("svn", "checkout", url, other)
        info = self.client.info(mine)
        self.assertEqual((info.url, info.revision > 0), (url, True))
        with self.assertRaises(SvnError) as cm:
            self.client.info(str(root))
        self.assertIn("E155007", cm.exception.codes)

        clean = self.client.status(mine, check_server=True)
        self.assertEqual((clean.modified, clean.incoming, clean.conflicts), (0, 0, 0))
        self.assertEqual(clean.head, info.revision)

        # другой человек: правит файл, добавляет новый и мусор, захватывает файл
        Path(other, "keep файл.txt").write_text("changed by other", encoding="utf-8")
        Path(other, "new doc.txt").write_text("new", encoding="utf-8")
        Path(other, "копия.bak").write_text("junk2")
        run("svn", "add", "--no-ignore", str(Path(other, "new doc.txt")), str(Path(other, "копия.bak")))
        run("svn", "commit", "-m", "other", other, "--username", "petrov")
        Path(mine, "my_new.txt").write_text("mine")           # неверсионный
        Path(other, "new doc.txt").write_text("v2", encoding="utf-8")
        run("svn", "lock", str(Path(other, "new doc.txt")), "-m", "editing", "--username", "petrov")

        st = self.client.status(mine, check_server=True)
        self.assertEqual(st.unversioned, 1)
        self.assertGreaterEqual(st.incoming, 3)
        self.assertEqual([o for _p, o in st.locked_by_others], ["petrov"])
        self.assertEqual([(os.path.basename(l.path), l.owner, l.comment) for l in st.other_locks],
                         [("new doc.txt", "petrov", "editing")])
        self.assertEqual(st.other_locks[0].age_days, 0)
        self.assertEqual(st.my_locks, [])
        self.assertGreater(st.head, info.revision)

        progress = CheckoutProgress()
        res = self.client.update(mine, progress)
        self.assertEqual(res.revision, st.head)
        self.assertEqual((res.added, res.updated, res.conflicts), (2, 1, []))
        self.assertEqual(progress.count, 3)
        junk = junk_among(res.added_paths, FilterRules(), mine)
        self.assertEqual(junk, ["копия.bak"])
        self.client.exclude_paths(mine, junk)
        self.assertFalse(os.path.exists(os.path.join(mine, "копия.bak")))
        self.assertTrue(os.path.exists(os.path.join(mine, "new doc.txt")))

        after = self.client.status(mine, check_server=True)
        self.assertEqual((after.incoming, after.modified), (0, 0))
        self.assertEqual([o for _p, o in after.locked_by_others], ["petrov"])

        # конфликт: оба правят одно и то же
        Path(mine, "keep файл.txt").write_text("mine edit", encoding="utf-8")
        Path(other, "keep файл.txt").write_text("other edit 2", encoding="utf-8")
        run("svn", "update", other)
        run("svn", "commit", "-m", "o2", other)
        local = self.client.status(mine)
        self.assertEqual(local.modified, 1)
        res = self.client.update(mine)
        self.assertEqual(len(res.conflicts), 1)
        self.assertEqual(self.client.status(mine).conflicts, 1)

    def test_status_verbose_revisions_after_partial_commit(self):
        """Корень копии после коммита файла остаётся на старой версии: max_revision и mixed это показывают."""
        url = server.build_url(self.server, ["test", "projects", "junk_project"]) + "/trunk"
        mine = str(Path(self.tmp.name) / "mixed")
        self.client.checkout(url, mine)
        base = self.client.info(mine).revision
        plain = self.client.status(mine, verbose=True)
        self.assertEqual((plain.min_revision, plain.max_revision, plain.mixed, plain.switched), (base, base, False, 0))
        Path(mine, "keep файл.txt").write_text("edited", encoding="utf-8")
        run("svn", "commit", "-m", "один файл", str(Path(mine, "keep файл.txt")))
        self.assertEqual(self.client.info(mine).revision, base)          # корень не сдвинулся
        st = self.client.status(mine, verbose=True)
        self.assertEqual((st.max_revision > base, st.min_revision, st.mixed), (True, base, True))
        self.assertIsNone(self.client.status(mine).max_revision)         # без verbose версий нет
        self.client.update(mine)
        after = self.client.status(mine, verbose=True)
        self.assertEqual((after.min_revision == after.max_revision, after.mixed), (True, False))

    def test_tree_conflict_and_at_sign_in_folder(self):
        url = server.build_url(self.server, ["test", *PROJECT_PATH]) + "/trunk"
        root = Path(self.tmp.name) / "tree"
        root.mkdir(exist_ok=True)
        mine, other = str(root / "копия@2"), str(root / "other")
        self.client.checkout(url, mine)
        self.client.checkout(url, other)
        self.client.cleanup(mine)                              # '@' в имени папки не ломает cleanup
        Path(mine, "чертёж 1.txt").write_text("mine edit", encoding="utf-8")
        run("svn", "delete", str(Path(other, "чертёж 1.txt")))
        run("svn", "commit", "-m", "del", other)
        res = self.client.update(mine)                         # svn печатает «   C путь»
        self.assertEqual([os.path.basename(c) for c in res.conflicts], ["чертёж 1.txt"])

    def test_resolve_conflict_mine_and_theirs(self):
        url = server.build_url(self.server, ["test", *PROJECT_PATH]) + "/trunk"
        root = Path(self.tmp.name) / "resolve"
        root.mkdir(exist_ok=True)
        for choice in ("mine-full", "theirs-full"):
            mine, other = str(root / f"моя_{choice}"), str(root / f"чужая_{choice}")
            self.client.checkout(url, mine)
            self.client.checkout(url, other)
            name = "чертёж 1.txt"
            Path(mine, name).write_text(f"{choice}: моя правка", encoding="utf-8")
            Path(other, name).write_text(f"{choice}: правка коллеги", encoding="utf-8")
            run("svn", "commit", "-m", "colleague", other)
            self.assertEqual(len(self.client.update(mine).conflicts), 1)
            self.client.resolve(os.path.join(mine, name), choice)
            expected = f"{choice}: " + ("моя правка" if choice == "mine-full" else "правка коллеги")
            self.assertEqual(Path(mine, name).read_text(encoding="utf-8"), expected)
            self.assertEqual(self.client.status(mine).conflicts, 0)

    def test_get_and_release_lock(self):
        url = server.build_url(self.server, ["test", *PROJECT_PATH]) + "/trunk"
        root = Path(self.tmp.name) / "locks"
        root.mkdir(exist_ok=True)
        mine, other = str(root / "моя"), str(root / "чужая")
        self.client.checkout(url, mine)
        self.client.checkout(url, other)
        target = os.path.join(mine, "docs sub", "b.bin")
        self.assertFalse(os.access(target, os.W_OK))          # needs-lock: только чтение
        self.client.lock(target)
        self.assertTrue(os.access(target, os.W_OK))           # после Get lock файл можно править
        status = self.client.status(mine, check_server=True)
        self.assertEqual([os.path.basename(p) for p in status.locked_by_me], ["b.bin"])
        self.assertEqual([(os.path.basename(l.path), l.comment) for l in status.my_locks],
                         [("b.bin", "Правка через SVN-lite")])
        self.assertEqual(status.my_locks[0].age_days, 0)
        self.assertEqual(status.my_locks[0].created.utcoffset().total_seconds(), 0)
        # remote_lock видит блокировку и из «чужой» копии, и из своей; у свободного файла её нет
        seen = self.client.remote_lock(os.path.join(other, "docs sub", "b.bin"))
        self.assertEqual((seen.owner != "", seen.comment, seen.path.endswith("b.bin")),
                         (True, "Правка через SVN-lite", True))
        self.assertIsNone(self.client.remote_lock(os.path.join(other, "чертёж 1.txt")))
        with self.assertRaises(SvnError) as cm:                # из другой копии файл занят
            self.client.lock(os.path.join(other, "docs sub", "b.bin"))
        self.assertEqual(cm.exception.kind, ErrorKind.LOCKED)
        self.client.unlock(target)
        self.assertFalse(os.access(target, os.W_OK))
        self.assertEqual(self.client.status(mine, check_server=True).locked_by_me, [])

    def test_lock_on_outdated_file_is_out_of_date(self):
        """W160042: файл на сервере новее копии → отдельный вид ошибки, а не «не удалось»."""
        root = Path(self.tmp.name) / "ood"
        root.mkdir()
        repo = root / "repo"
        run("svnadmin", "create", str(repo))
        mine, other = str(root / "mine"), str(root / "other")
        run("svn", "checkout", repo.as_uri(), other)
        Path(other, "f.txt").write_text("v1")
        run("svn", "add", str(Path(other, "f.txt")))
        run("svn", "commit", "-m", "init", other)
        run("svn", "checkout", repo.as_uri(), mine)
        Path(other, "f.txt").write_text("v2")
        run("svn", "commit", "-m", "newer", other)
        with self.assertRaises(SvnError) as cm:
            self.client.lock(os.path.join(mine, "f.txt"))
        self.assertIn("W160042", cm.exception.codes)
        self.assertEqual(cm.exception.kind, ErrorKind.OUT_OF_DATE)
        self.client.update(mine)                                # после обновления занять можно
        self.client.lock(os.path.join(mine, "f.txt"))

    def test_no_auth_cache_option_is_accepted(self):
        client = SvnClient("svn")
        client.store_auth = False
        url = server.build_url(self.server, ["test", "projects"])
        self.assertIn("junk_project", client.list_dirs(url))


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "нет svn/svnadmin")
class RealLinesTests(unittest.TestCase):
    """Ветки и версии (теги) на настоящем svn через file:///."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.repo = cls.root / "repos" / "test"
        cls.repo.parent.mkdir()
        run("svnadmin", "create", str(cls.repo))
        cls.server = (cls.root / "repos").as_uri()
        wc = cls.root / "wc"
        run("svn", "checkout", cls.repo.as_uri(), str(wc))
        proj = wc / "projects" / "Проект один"
        (proj / "trunk").mkdir(parents=True)
        (proj / "trunk" / "a.txt").write_text("a", encoding="utf-8")
        (proj / "trunk" / "b.txt").write_text("b", encoding="utf-8")
        run("svn", "add", "--force", ".", cwd=wc)
        run("svn", "commit", "-m", "init", "--username", "ivanov", cwd=wc)
        cls.ref = server.ProjectRef(cls.server, ("test", "projects", "Проект один"))
        cls.client = SvnClient("svn")
        cls.client.store_auth = False
        cls.client.set_credentials("creator", "x")   # file:/// пароль не проверяет, но имя автора берёт отсюда

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def log_message(self, url):
        out = subprocess.run(["svn", "log", "--xml", "-l", "1", url + "@"], stdout=subprocess.PIPE, check=True).stdout
        from svnhelper.svn_core import parse_log_xml
        return parse_log_xml(out)[0].message

    def test_1_lines_lifecycle(self):
        client, ref = self.client, self.ref
        project_url = ref.line_url(server.TRUNK_LINE).rsplit("/", 1)[0]
        # веток и тегов ещё нет: папок нет, это не ошибка
        self.assertEqual(client.list_lines(project_url), {"branches": [], "tags": [], "has_trunk": True})

        comment = "Ветка для задачи №5\nвторая строка"
        branch = server.Line("branch", "задача 5")
        client.copy_url(ref.trunk_url, ref.line_url(branch), comment)
        self.assertEqual(self.log_message(ref.line_url(branch)), comment)   # кириллица и перевод строки целы
        tag = server.Line("tag", "2026-10-19")                              # tags/ ещё нет: создаётся (--parents)
        client.copy_url(ref.trunk_url, ref.line_url(tag), "Версия «первая»", revision=1)
        self.assertEqual(self.log_message(ref.line_url(tag)), "Версия «первая»")

        with self.assertRaises(SvnError) as cm:                             # повтор: не вложенная копия, а ошибка
            client.copy_url(ref.trunk_url, ref.line_url(branch), "ещё раз")
        self.assertEqual(cm.exception.kind, ErrorKind.EXISTS)
        self.assertEqual([e.name for e in client.list_entries(ref.line_url(branch)) if e.kind == "dir"], [])

        second = server.Line("branch", "вторая")
        client.copy_url(ref.trunk_url, ref.line_url(second), "Вторая")
        lines = client.list_lines(project_url)
        self.assertEqual([l.name for l in lines["branches"]][0], "вторая")   # новые первыми
        self.assertEqual(sorted(l.name for l in lines["branches"]), ["вторая", "задача 5"])
        self.assertEqual([(l.name, l.last_author != "", l.last_date is not None) for l in lines["tags"]],
                         [("2026-10-19", True, True)])
        self.assertTrue(lines["has_trunk"])

        # правки двумя авторами в ветке
        wc = str(self.root / "bwc")
        client.checkout(ref.line_url(branch), wc)
        Path(wc, "a.txt").write_text("v2", encoding="utf-8")
        run("svn", "commit", "-m", "первая правка", "--username", "ivanov", wc)
        Path(wc, "b.txt").write_text("v2", encoding="utf-8")
        run("svn", "commit", "-m", "вторая правка", "--username", "petrov", wc)
        Path(wc, "b.txt").write_text("v3", encoding="utf-8")
        run("svn", "commit", "-m", "третья правка", "--username", "petrov", wc)

        hist = client.branch_history(ref.line_url(branch))
        self.assertEqual(hist.copied_from, "/projects/Проект один/trunk")
        self.assertEqual(hist.copied_from_revision, 1)
        self.assertEqual(hist.comment, comment)
        self.assertEqual(hist.from_label, "из основной линии (версия 1)")
        self.assertEqual([(a.name, a.commits) for a in hist.authors][0], ("petrov", 2))
        self.assertEqual(hist.created_by, "creator")
        self.assertEqual({a.name: a.commits for a in hist.authors},
                         {"petrov": 2, "ivanov": 1, "creator": 1})
        self.assertGreater(hist.last_revision, hist.created_revision)
        self.assertTrue(hist.complete)
        short = client.branch_history(ref.line_url(branch), limit=1)
        self.assertFalse(short.complete)
        # limit обрезает только список авторов: создатель и источник определяются всегда
        self.assertEqual((short.created_by, short.copied_from, short.created_revision),
                         (hist.created_by, hist.copied_from, hist.created_revision))
        self.assertEqual(len(short.authors), 1)

        # история основной линии: копирования нет, создатель — автор самой ранней версии
        trunk_hist = client.branch_history(ref.trunk_url)
        self.assertEqual((trunk_hist.created_by, trunk_hist.copied_from, trunk_hist.from_label), ("ivanov", "", ""))

        # версия из другой версии
        tag_hist = client.branch_history(ref.line_url(tag))
        self.assertEqual(tag_hist.from_label, "из основной линии (версия 1)")

        # переключение копии: ветка → основная линия → ветка, адрес меняется
        res = client.switch(wc, ref.trunk_url)
        self.assertEqual(client.info(wc).url, ref.trunk_url)
        self.assertEqual((res.conflicts, res.revision is not None), ([], True))
        client.switch(wc, ref.line_url(branch))
        self.assertEqual(client.info(wc).url, ref.line_url(branch))
        self.assertEqual(client.status(wc, verbose=True).switched, 0)    # вся копия на одной линии
        run("svn", "switch", ref.trunk_url + "@", str(Path(wc, "b.txt")), "--ignore-ancestry")
        self.assertEqual(client.status(wc, verbose=True).switched, 1)    # один файл переключён отдельно
        client.switch(wc, ref.line_url(branch))

        # несвязанные линии: svn выдаёт E195012
        run("svn", "mkdir", "-m", "чужая", "--parents", self.repo.as_uri() + "/lonely/trunk")
        with self.assertRaises(SvnError) as cm:
            client.switch(wc, self.repo.as_uri() + "/lonely/trunk")
        self.assertIn("E195012", cm.exception.codes)
        self.assertEqual(cm.exception.kind, ErrorKind.UNRELATED)
        self.assertEqual(client.info(wc).url, ref.line_url(branch))

    def test_2_locks_under(self):
        client, ref = self.client, self.ref
        wc = str(self.root / "lockwc")
        client.checkout(ref.trunk_url, wc)
        self.assertEqual(client.locks_under(ref.trunk_url), [])
        run("svn", "lock", "-m", "правлю чертёж", os.path.join(wc, "a.txt"), "--username", "petrov")
        locks = client.locks_under(ref.trunk_url)
        self.assertEqual([(l.path, l.owner, l.comment) for l in locks], [("a.txt", "petrov", "правлю чертёж")])
        self.assertEqual(locks[0].age_days, 0)
        run("svn", "unlock", os.path.join(wc, "a.txt"), "--username", "petrov")


HOOK_DIR = Path(__file__).resolve().parent.parent / "server_hooks"
HOOK = HOOK_DIR / "pre-commit.bat"
BASH = shutil.which("bash")   # полный путь: голый "bash" на Windows может открыть WSL


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "нет svn/svnadmin")
class RealCommitTests(unittest.TestCase):
    """Сохранение на сервер, свои правки, новое на сервере (Task A.2) на настоящем svn через file:///."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        repo = root / "repo"
        run("svnadmin", "create", str(repo))
        self.url = repo.as_uri()
        seed = root / "seed"
        run("svn", "checkout", self.url, str(seed))
        for name, text in (("чертёж 1.txt", "один"), ("удалить меня.txt", "x"), ("замок файл.bin", "lock")):
            (seed / name).write_text(text, encoding="utf-8")
        (seed / "папка").mkdir()
        (seed / "папка" / "старый.txt").write_text("s", encoding="utf-8")
        run("svn", "add", "--force", ".", cwd=seed)
        run("svn", "propset", "svn:needs-lock", "ON", str(seed / "замок файл.bin"))
        run("svn", "commit", "-m", "init", cwd=seed)
        self.mine, self.other = str(root / "моя копия"), str(root / "other")
        run("svn", "checkout", self.url, self.mine)
        run("svn", "checkout", self.url, self.other)
        self.client = SvnClient("svn")
        self.client.store_auth = False

    def tearDown(self):
        self.tmp.cleanup()

    def mine_file(self, *parts):
        return os.path.join(self.mine, *parts)

    def other_commit(self, name="чертёж 1.txt", text="правка коллеги"):
        Path(self.other, name).write_text(text, encoding="utf-8")
        run("svn", "commit", "-m", "коллега", self.other)

    def test_commit_modify_add_delete_with_cyrillic(self):
        before = self.client.info(self.mine).revision
        Path(self.mine_file("чертёж 1.txt")).write_text("два", encoding="utf-8")
        os.makedirs(self.mine_file("новая папка"))
        Path(self.mine_file("новая папка", "КП ревизия 2.txt")).write_text("новый", encoding="utf-8")
        os.remove(self.mine_file("удалить меня.txt"))
        Path(self.mine_file("Thumbs.db")).write_text("мусор")
        st = self.client.status(self.mine)
        kinds = {os.path.basename(c.path): c.kind for c in st.local_changes}
        self.assertEqual(kinds, {"чертёж 1.txt": "modified", "новая папка": "unversioned",
                                 "удалить меня.txt": "missing"})            # Thumbs.db отфильтрован как мусор
        res = self.client.commit(self.mine, [c.path for c in st.local_changes],
                                 "Правка чертежа\nи новая ревизия КП")
        self.assertEqual(res.revision, before + 1)
        self.assertEqual(len(res.committed), 4)                             # файл, папка + файл в ней, удаление
        after = self.client.status(self.mine)
        self.assertEqual(after.local_changes, [])                           # остался только мусор Thumbs.db
        self.assertEqual((after.modified, after.conflicts), (0, 0))
        out = subprocess.run(["svn", "log", "--xml", "-v", "-l", "1", self.url],
                             stdout=subprocess.PIPE, check=True).stdout
        from svnhelper.svn_core import parse_log_xml
        entry = parse_log_xml(out)[0]
        self.assertEqual(entry.message, "Правка чертежа\nи новая ревизия КП")
        self.assertEqual(sorted((p.action, p.path) for p in entry.paths),
                         [("A", "/новая папка"), ("A", "/новая папка/КП ревизия 2.txt"),
                          ("D", "/удалить меня.txt"), ("M", "/чертёж 1.txt")])

    def test_commit_selected_file_only_and_nothing_to_commit(self):
        Path(self.mine_file("чертёж 1.txt")).write_text("a", encoding="utf-8")
        Path(self.mine_file("папка", "старый.txt")).write_text("b", encoding="utf-8")
        res = self.client.commit(self.mine, [self.mine_file("чертёж 1.txt")], "только один")
        self.assertEqual([os.path.basename(p) for p in res.committed], ["чертёж 1.txt"])
        left = self.client.status(self.mine).local_changes
        self.assertEqual([os.path.basename(c.path) for c in left], ["старый.txt"])
        empty = self.client.commit(self.mine, [self.mine_file("удалить меня.txt")], "ничего")
        self.assertEqual((empty.revision, empty.committed), (None, []))

    def test_commit_when_server_is_newer_is_out_of_date(self):
        Path(self.mine_file("чертёж 1.txt")).write_text("моя правка", encoding="utf-8")
        self.other_commit()
        with self.assertRaises(SvnError) as cm:
            self.client.commit(self.mine, [self.mine_file("чертёж 1.txt")], "m")
        self.assertEqual(cm.exception.kind, ErrorKind.OUT_OF_DATE)

    def test_needs_lock_file_without_lock_requires_lock(self):
        target = self.mine_file("замок файл.bin")
        os.chmod(target, 0o666)                                            # как будто файл правили принудительно
        Path(target).write_text("правка без замка", encoding="utf-8")
        change, = [c for c in self.client.status(self.mine).local_changes]
        self.assertEqual((change.kind, change.needs_lock, change.locked_by_me), ("modified", True, False))
        with self.assertRaises(SvnError) as cm:
            self.client.commit(self.mine, [target], "без замка")
        self.assertEqual(cm.exception.kind, ErrorKind.LOCK_REQUIRED)
        self.assertIn("замок файл.bin", cm.exception.raw)
        self.client.lock(target)
        change, = self.client.status(self.mine).local_changes
        self.assertEqual((change.needs_lock, change.locked_by_me), (True, True))
        self.assertGreater(self.client.commit(self.mine, [target], "с замком").revision, 0)
        self.assertEqual(self.client.status(self.mine, check_server=True).local_changes, [])

    def test_status_incoming_files_and_incoming_log(self):
        self.assertEqual(self.client.incoming_log(self.mine), [])           # нового нет (E160006 не наружу)
        self.other_commit()
        Path(self.other, "Новый от коллеги.txt").write_text("n", encoding="utf-8")
        run("svn", "add", str(Path(self.other, "Новый от коллеги.txt")))
        run("svn", "commit", "-m", "второй", self.other)
        st = self.client.status(self.mine, check_server=True)
        incoming = {os.path.basename(p) for p in st.incoming_files}
        self.assertTrue({"чертёж 1.txt", "Новый от коллеги.txt"} <= incoming, incoming)
        entries = self.client.incoming_log(self.mine)
        self.assertEqual([e.message for e in entries], ["коллега", "второй"])
        self.assertEqual([(p.action, p.path) for p in entries[1].paths], [("A", "/Новый от коллеги.txt")])
        # свои сохранённые правки новыми не считаются, хотя корень копии остался на старой версии
        Path(self.mine_file("папка", "старый.txt")).write_text("мой", encoding="utf-8")
        self.client.update(self.mine)
        Path(self.mine_file("папка", "старый.txt")).write_text("мой2", encoding="utf-8")
        self.client.commit(self.mine, [self.mine_file("папка", "старый.txt")], "мой")
        self.assertEqual(self.client.incoming_log(self.mine), [])

    def test_diff_summary(self):
        base = self.url
        run("svn", "copy", "-m", "ветка", base + "/папка", base + "/ветка")
        branch = Path(self.tmp.name, "br")
        run("svn", "checkout", base + "/ветка", str(branch))
        (branch / "новый файл.txt").write_text("n", encoding="utf-8")
        run("svn", "add", str(branch / "новый файл.txt"))
        (branch / "старый.txt").write_text("changed", encoding="utf-8")
        run("svn", "commit", "-m", "правки ветки", str(branch))
        got = self.client.diff_summary(base + "/папка", base + "/ветка")
        self.assertEqual(sorted(got), [("новый файл.txt", "added"), ("старый.txt", "modified")])


def run_err(*args, cwd=None):
    """Как run(), но возвращает текст ошибки (или None, если команда прошла)."""
    try:
        run(*args, cwd=cwd)
    except RuntimeError as exc:
        return str(exc)
    return None


@unittest.skipUnless(os.name == "nt" and shutil.which("svn") and shutil.which("svnadmin")
                     and shutil.which("svnlook"), "нужны Windows и svn/svnadmin/svnlook")
class TagHookTests(unittest.TestCase):
    """server_hooks/pre-commit.bat на настоящем репозитории: теги только для чтения."""

    def make_repo(self, tmp):
        repo = Path(tmp, "r")
        run("svnadmin", "create", str(repo))
        shutil.copy(HOOK, repo / "hooks" / "pre-commit.bat")
        return repo, repo.as_uri()

    def assertDenied(self, err):
        self.assertIsNotNone(err, "коммит должен быть отклонён")
        self.assertIn("E165001", err)
        self.assertTrue("только для чтения" in err or "read-only" in err, err)

    def test_tag_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, url = self.make_repo(tmp)
            wc = Path(tmp, "wc")
            run("svn", "mkdir", "-m", "init", "--parents", url + "/p/trunk", url + "/p/branches")
            run("svn", "checkout", url, str(wc))
            (wc / "p" / "trunk" / "a.txt").write_text("a")
            run("svn", "add", str(wc / "p" / "trunk" / "a.txt"))
            run("svn", "commit", "-m", "file", str(wc))
            client = SvnClient("svn")

            client.copy_url(url + "/p/trunk", url + "/p/tags/v1", "создаём версию")   # копирование разрешено
            client.copy_url(url + "/p/trunk", url + "/p/branches/b1", "ветка")
            run("svn", "mkdir", "-m", "папка tags внутри trunk - не версия", "--parents", url + "/p/trunk/tags/x/y")
            run("svn", "update", str(wc))
            (wc / "p" / "branches" / "b1" / "a.txt").write_text("edit")
            run("svn", "commit", "-m", "правка ветки", str(wc))                     # ветка редактируется

            (wc / "p" / "tags" / "v1" / "a.txt").write_text("edit")
            self.assertDenied(run_err("svn", "commit", "-m", "правка версии", str(wc)))
            run("svn", "revert", "-R", str(wc))
            self.assertDenied(run_err("svn", "delete", "-m", "удаляем", url + "/p/tags/v1"))
            self.assertDenied(run_err("svn", "mkdir", "-m", "внутри", url + "/p/tags/v1/new"))
            # свойство на самой версии тоже правка внутри версии
            run("svn", "update", str(wc))
            run("svn", "propset", "x", "y", str(wc / "p" / "tags" / "v1"))
            self.assertDenied(run_err("svn", "commit", "-m", "prop", str(wc)))

    def test_client_commit_into_tag_is_read_only_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, url = self.make_repo(tmp)
            wc = Path(tmp, "wc")
            run("svn", "mkdir", "-m", "init", "--parents", url + "/p/trunk")
            run("svn", "checkout", url, str(wc))
            (wc / "p" / "trunk" / "a.txt").write_text("a")
            run("svn", "add", str(wc / "p" / "trunk" / "a.txt"))
            run("svn", "commit", "-m", "file", str(wc))
            client = SvnClient("svn")
            client.copy_url(url + "/p/trunk", url + "/p/tags/v1", "версия")
            run("svn", "update", str(wc))
            tagged = wc / "p" / "tags" / "v1" / "a.txt"
            tagged.write_text("edit")
            with self.assertRaises(SvnError) as cm:
                client.commit(str(wc), [str(tagged)], "правка версии")
            self.assertEqual(cm.exception.kind, ErrorKind.READ_ONLY_LINE)

    def test_tags_folder_cannot_be_deleted_or_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, url = self.make_repo(tmp)
            run("svn", "mkdir", "-m", "init", "--parents", url + "/p/trunk", url + "/p/tags/v1")
            self.assertDenied(run_err("svn", "delete", "-m", "x", url + "/p/tags"))
            self.assertDenied(run_err("svn", "mv", "-m", "x", url + "/p/tags", url + "/p/old"))
            self.assertDenied(run_err("svn", "mv", "-m", "x", url + "/p/tags/v1", url + "/p/tags/v2"))
            self.assertIsNone(run_err("svn", "mkdir", "-m", "ок", url + "/p/branches"))   # прочее работает

    def test_project_inside_folder_named_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, url = self.make_repo(tmp)
            wc = Path(tmp, "wc")
            # проект g/tags/proj: у g нет trunk, значит tags здесь обычная папка
            run("svn", "mkdir", "-m", "проект", "--parents", url + "/g/tags/proj/trunk", url + "/g/tags/proj/tags")
            run("svn", "checkout", url + "/g/tags/proj", str(wc))
            (wc / "trunk" / "f.txt").write_text("1")
            run("svn", "add", str(wc / "trunk" / "f.txt"))
            run("svn", "commit", "-m", "файл", str(wc))                                 # можно сохранять
            (wc / "trunk" / "f.txt").write_text("2")
            run("svn", "commit", "-m", "правка", str(wc))
            run("svn", "copy", "-m", "версия проекта", url + "/g/tags/proj/trunk", url + "/g/tags/proj/tags/v1")
            (wc / "trunk" / "f.txt").write_text("3")
            run("svn", "commit", "-m", "ещё", str(wc))
            # а настоящая версия внутри проекта g/tags/proj по-прежнему защищена
            self.assertDenied(run_err("svn", "mkdir", "-m", "x", url + "/g/tags/proj/tags/v1/new"))
            self.assertDenied(run_err("svn", "delete", "-m", "x", url + "/g/tags/proj/tags/v1"))

    def test_folder_with_space_before_tags_is_not_a_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, url = self.make_repo(tmp)
            run("svn", "mkdir", "-m", "init", "--parents", url + "/p/trunk")
            run("svn", "mkdir", "-m", "old tags", "--parents", url + "/p/old tags/x/y")
            run("svn", "mkdir", "-m", "ещё", url + "/p/old tags/x/z")
            run("svn", "delete", "-m", "и удалить можно", url + "/p/old tags/x")

    def test_cyrillic_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, url = self.make_repo(tmp)
            proj = url + "/" + "%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82"       # проект
            run("svn", "mkdir", "-m", "init", "--parents", proj + "/trunk", proj + "/tags/v1")
            self.assertDenied(run_err("svn", "delete", "-m", "x", proj + "/tags"))
            self.assertDenied(run_err("svn", "mkdir", "-m", "x", proj + "/tags/v1/new"))

    def test_bad_svnlook_rejects_commit(self):
        """svnlook упал - коммит отклоняется (fail-closed), а не пропускается."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, url = self.make_repo(tmp)
            hook = repo / "hooks" / "pre-commit.bat"
            text = hook.read_bytes().decode("utf-8").replace("svnlook changed", "svnlook_missing changed")
            hook.write_bytes(text.encode("utf-8"))
            err = run_err("svn", "mkdir", "-m", "x", "--parents", url + "/p/trunk")
            self.assertIsNotNone(err)
            self.assertIn("E165001", err)


@unittest.skipUnless(shutil.which("bash") and os.path.isfile(HOOK_DIR / "pre-commit"), "нет bash")
class TagHookShellTests(unittest.TestCase):
    """server_hooks/pre-commit (sh) с подставным svnlook: описание репозитория лежит в файлах."""

    STUB = """#!/bin/sh
D="$STUB_DIR"
case "$1" in
  changed) [ -f "$D/fail" ] && { echo "svnlook: E000000: boom" >&2; exit 3; }; cat "$D/changed" ;;
  youngest) echo 5 ;;
  tree)
    if [ "$2" = "-t" ]; then f="$D/txn"; else f="$D/head"; fi
    grep -qxF -- "$5" "$f" && exit 0
    echo "svnlook: E160013: File not found: path '$5'" >&2
    exit 1 ;;
esac
"""

    @classmethod
    def setUpClass(cls):
        try:
            probe = subprocess.run([BASH, "-c", "echo ok"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.SubprocessError):
            probe = None
        if probe is None or probe.stdout.strip() != b"ok":
            raise unittest.SkipTest("bash не работает")

    def hook(self, changed, head=("p/trunk",), txn=(), fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "svnlook").write_text(self.STUB, encoding="utf-8", newline="\n")
            (d / "changed").write_text("\n".join(changed) + "\n", encoding="utf-8", newline="\n")
            (d / "head").write_text("\n".join(head) + "\n", encoding="utf-8", newline="\n")
            (d / "txn").write_text("\n".join([*head, *txn]) + "\n", encoding="utf-8", newline="\n")
            if fail:
                (d / "fail").write_text("1")
            env = dict(os.environ, STUB_DIR=d.as_posix(), SVNLOOK=(d / "svnlook").as_posix())
            proc = subprocess.run([BASH, (HOOK_DIR / "pre-commit").as_posix(), "repos", "txn"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            return proc.returncode, proc.stderr.decode("utf-8", errors="replace")

    def test_allowed(self):
        for changed, kw in (
            (["U   p/trunk/a.txt"], {}),
            (["A   p/tags/"], {}),
            (["A   p/tags/v1/"], {}),
            (["A   p/tags/v1/", "A   p/tags/v1/f.txt"], {}),             # версия создаётся целиком в этой транзакции
            (["A   old tags/x/"], {}),                                    # пробел перед tags - не граница сегмента
            (["U   g/tags/proj/trunk/f.txt"], {"head": ["g/tags/proj/trunk"]}),
            (["A   g/tags/proj/tags/v1/"], {"head": ["g/tags/proj/trunk"]}),
            (["U   p/trunk/tags/x/f"], {}),
            (["A   p/tags/"], {"head": [], "txn": ["p/trunk"]}),         # trunk появился в этой же транзакции
        ):
            with self.subTest(changed=changed):
                code, err = self.hook(changed, **kw)
                self.assertEqual((code, err), (0, ""))

    def test_denied(self):
        existing = ["p/trunk", "p/tags", "p/tags/v1"]
        for changed in (["U   p/tags/v1/a.txt"], ["_U  p/tags/v1/"], ["D   p/tags/v1/"], ["R   p/tags/v1/"],
                        ["A   p/tags/v1/new"], ["D   p/tags/"], ["D   p/tags/v1/a.txt"], ["R   p/tags/"],
                        ["UU  p/tags/v1/"]):
            with self.subTest(changed=changed):
                code, err = self.hook(changed, head=existing)
                self.assertEqual(code, 1)
                self.assertIn("read-only", err)
                self.assertIn("только для чтения", err)

    def test_project_deleted_in_same_commit_is_still_a_project(self):
        code, _err = self.hook(["D   p/tags/", "D   p/trunk/"], head=["p/trunk", "p/tags"], txn=[])
        self.assertEqual(code, 1)

    def test_svnlook_failure_is_fail_closed(self):
        code, err = self.hook(["U   p/trunk/a.txt"], fail=True)
        self.assertEqual(code, 1)
        self.assertIn("Хук не смог прочитать изменения (svnlook). Сообщите администратору.", err)


@unittest.skipUnless(os.name == "nt" and shutil.which("svn") and shutil.which("svnadmin"), "нужна Windows и svn")
class BusyFileTests(unittest.TestCase):
    """Файл, открытый другой программой, при update даёт ErrorKind.BUSY (E720005 + E155009 на svn 1.14)."""

    def test_update_over_open_file_is_busy(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "r")
            run("svnadmin", "create", str(repo))
            url = repo.as_uri()
            mine, other = Path(tmp, "mine"), Path(tmp, "other")
            run("svn", "checkout", url, str(other))
            (other / "f.txt").write_text("1")
            run("svn", "add", str(other / "f.txt"))
            run("svn", "commit", "-m", "1", str(other))
            run("svn", "checkout", url, str(mine))
            (other / "f.txt").write_text("2")
            run("svn", "commit", "-m", "2", str(other))
            # как Word/Компас: файл открыт без разрешения делить (dwShareMode=0)
            handle = kernel.CreateFileW(str(mine / "f.txt"), 0x80000000, 0, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            client = SvnClient("svn")
            try:
                with self.assertRaises(SvnError) as cm:
                    client.update(str(mine))
            finally:
                kernel.CloseHandle(handle)
            self.assertIn("E720005", cm.exception.codes)
            self.assertEqual(cm.exception.kind, ErrorKind.BUSY)
            client.cleanup(str(mine))                                  # после неудачи копия требует cleanup
            client.update(str(mine))                                   # файл закрыт - обновление проходит
            self.assertEqual((mine / "f.txt").read_text(), "2")


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "нет svn/svnadmin")
class RealTemplateCoreTests(unittest.TestCase):
    """Task T.0: export, cat, checkout --force, propget; '@' и '%' в именах; пропавшее локальное хранилище."""

    FOLDER = "шаблон @ 100%"
    FILES = {"ф@x%41.txt": b"one\r\n$Id$ \x00", "sub %41/n@.txt": "два".encode("utf-8")}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        run("svnadmin", "create", str(self.repo))
        wc = self.root / "seed"
        run("svn", "checkout", self.repo.as_uri(), str(wc))
        base = wc / self.FOLDER
        for rel, data in self.FILES.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        run("svn", "add", "--force", str(wc / self.FOLDER) + "@")
        run("svn", "propset", "svn:needs-lock", "ON", str(base / "sub %41" / "n@.txt") + "@")
        run("svn", "propset", "svn:keywords", "Id", str(base / "ф@x%41.txt") + "@")
        run("svn", "commit", "-m", "init", str(wc))
        self.url = (self.repo / self.FOLDER).as_uri()    # as_uri кодирует '%' -> %25, '@' -> %40
        self.client = SvnClient("svn")

    def tearDown(self):
        self.tmp.cleanup()

    def check_files(self, folder):
        for rel, data in self.FILES.items():
            self.assertEqual(Path(folder, rel).read_bytes(), data, rel)

    def test_export_names_with_at_and_percent(self):
        for name in ("out @1 %41", "out@", "out@2 %"):
            dest = self.root / name
            progress = CheckoutProgress()
            res = self.client.export(self.url, str(dest), progress)
            self.assertEqual(res.revision, 1)
            self.assertGreaterEqual(progress.count, 3)
            self.check_files(dest)                      # побайтово, $Id$ не раскрыт (--ignore-keywords)
            self.assertFalse((dest / ".svn").exists())

    def test_export_of_missing_folder_fails_cleanly(self):
        with self.assertRaises(SvnError) as cm:
            self.client.export((self.repo / "нет").as_uri(), str(self.root / "o"))
        self.assertIn("E170000", cm.exception.codes)
        self.assertFalse((self.root / "o").exists())

    def test_cat_name_with_at_and_percent(self):
        url = (self.repo / self.FOLDER / "ф@x%41.txt").as_uri()
        self.assertTrue(self.client.cat(url).startswith(b"one\r\n$Id: "))   # cat раскрывает svn:keywords, export нет
        url2 = (self.repo / self.FOLDER / "sub %41" / "n@.txt").as_uri()
        self.assertEqual(self.client.cat(url2), self.FILES["sub %41/n@.txt"])
        self.assertEqual(self.client.cat(url2, revision=1), self.FILES["sub %41/n@.txt"])

    def test_checkout_status_info_names_with_at_and_percent(self):
        for name in ("копия @1 %41", "копия@", "копия%2"):
            dest = self.root / name
            progress = CheckoutProgress()
            res = self.client.checkout(self.url, str(dest), progress)
            self.assertEqual((res.revision, progress.count >= 3), (1, True))
            info = self.client.info(str(dest))
            self.assertEqual((unquote(info.url), info.revision), (unquote(self.url), 1))   # svn печатает @ без %40
            Path(dest, "sub %41", "n@.txt").chmod(0o666)
            Path(dest, "sub %41", "n@.txt").write_bytes(b"changed")
            status = self.client.status(str(dest))
            self.assertEqual([os.path.basename(c.path) for c in status.local_changes], ["n@.txt"])
            self.assertTrue(status.local_changes[0].needs_lock)
            self.assertEqual([os.path.basename(p) for p in self.client.propget_needs_lock(str(dest))], ["n@.txt"])
            self.assertEqual(self.client.status(str(dest), check_server=True).head, 1)

    def test_checkout_force_binds_existing_folder_in_place(self):
        dest = self.root / "уже есть @ 1%"
        for rel, data in self.FILES.items():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_bytes(data)
        (dest / "мой новый.txt").write_text("mine")
        progress = CheckoutProgress()
        res = self.client.checkout(self.url, str(dest), progress, force=True)
        self.assertEqual(res.revision, 1)
        self.assertGreaterEqual(progress.count, 3)         # строки «E» для лежавших файлов тоже считаются
        status = self.client.status(str(dest))
        self.assertEqual([(os.path.basename(c.path), c.kind) for c in status.local_changes],
                         [("мой новый.txt", "unversioned")])
        # после --force svn не ставит «только для чтения» на файлы с needs-lock: это делает вызывающий
        self.assertTrue(os.access(dest / "sub %41" / "n@.txt", os.W_OK))

    def test_mkdir_and_import_names_with_at_and_percent(self):
        """T.7: import и mkdir (T.0 их не проверял): '@' и '%' в названии проекта и в именах файлов."""
        for name in ("проект @ 100%", "проект@", "50%41 проект"):
            with self.subTest(name):
                src = self.root / f"исходник {name}"
                for rel, data in self.FILES.items():
                    (src / rel).parent.mkdir(parents=True, exist_ok=True)
                    (src / rel).write_bytes(data)
                (src / "пустая @ папка%").mkdir()
                project = (self.repo / name).as_uri()
                self.client.mkdir_urls([f"{project}/trunk", f"{project}/branches", f"{project}/tags"], "каркас")
                self.assertTrue(self.client.url_exists(project + "/trunk"))
                count = self.client.import_dir(str(src), project + "/trunk", "загрузка", [])
                self.assertEqual(count, 4)                     # два файла, папка «sub %41» и пустая папка
                names = sorted(e.name for e in self.client.list_entries(project + "/trunk@", recursive=True))
                self.assertEqual(names, sorted(["ф@x%41.txt", "sub %41", "sub %41/n@.txt", "пустая @ папка%"]))
                url = server.build_url(project, ["trunk", "ф@x%41.txt"])
                self.assertEqual(self.client.cat(url), self.FILES["ф@x%41.txt"])

    def test_commit_names_with_at_and_percent(self):
        """T.7: commit (правка, новые файлы и папка, удаление) в копии и с файлами, в именах которых '@' и '%'."""
        dest = self.root / "копия @ 1% сохранить@"
        self.client.checkout(self.url, str(dest))
        base = self.client.info(str(dest)).revision
        (dest / "sub %41" / "n@.txt").chmod(0o666)                                  # svn:needs-lock: файл только для чтения
        self.client.lock(str(dest / "sub %41" / "n@.txt"))
        (dest / "sub %41" / "n@.txt").write_bytes(b"changed")
        (dest / "итог@").write_bytes(b"trailing at")                                # '@' в конце имени
        (dest / "доля 50%.txt").write_bytes(b"percent")
        (dest / "новая @ папка%").mkdir()
        (dest / "новая @ папка%" / "файл %41@.txt").write_bytes(b"nested")
        os.remove(dest / "ф@x%41.txt")
        changes = self.client.status(str(dest)).local_changes
        self.assertEqual(sorted(os.path.basename(c.path) for c in changes),
                         sorted(["n@.txt", "итог@", "доля 50%.txt", "новая @ папка%", "ф@x%41.txt"]))
        res = self.client.commit(str(dest), [c.path for c in changes], "правка @ 100%")
        self.assertEqual(res.revision, base + 1)
        self.assertEqual(self.client.status(str(dest), check_server=True).local_changes, [])
        names = sorted(e.name for e in self.client.list_entries(self.url + "@", recursive=True))
        self.assertEqual(names, sorted(["sub %41", "sub %41/n@.txt", "итог@", "доля 50%.txt", "новая @ папка%",
                                        "новая @ папка%/файл %41@.txt"]))
        url = server.build_url(self.url, ["sub %41", "n@.txt"])
        self.assertEqual(self.client.cat(url), b"changed")

    def test_lost_local_repository_does_not_stop_check_or_mark_offline(self):
        from svnhelper.notifier import check_projects
        lost_wc, fine_wc = self.root / "потерянная", self.root / "целая"
        self.client.checkout(self.url, str(fine_wc))
        other = self.root / "repo2"
        run("svnadmin", "create", str(other))
        self.client.checkout(other.as_uri(), str(lost_wc))
        other.rename(self.root / "repo2_renamed")
        with self.assertRaises(SvnError) as cm:
            self.client.status(str(lost_wc), check_server=True)
        self.assertEqual(cm.exception.kind, ErrorKind.LOCAL_HISTORY_MISSING)
        self.assertIn("E180001", cm.exception.codes)
        result = check_projects(self.client, [(str(lost_wc), "потерянная"), (str(fine_wc), "целая")])
        self.assertFalse(result.offline or result.auth_failed)
        self.assertEqual([(p.name, p.error) for p in result.projects],
                         [("потерянная", "local_history_missing"), ("целая", "")])


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin") and shutil.which("svnserve"),
                     "нет svn/svnadmin/svnserve")
class SvnserveAuthTests(unittest.TestCase):
    """Вход с паролем на настоящем svnserve (svn://), как на рабочем сервере."""

    def test_password_from_stdin_is_accepted_and_wrong_is_auth(self):
        import socket
        import time
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "r")
            run("svnadmin", "create", str(repo))
            (repo / "conf" / "svnserve.conf").write_text(
                "[general]\nanon-access = none\nauth-access = write\npassword-db = passwd\n")
            (repo / "conf" / "passwd").write_text("[users]\nalice = секрет a\n", encoding="utf-8")
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            srv = subprocess.Popen(["svnserve", "-d", "--foreground", "--listen-port", str(port), "-r", tmp])
            try:
                time.sleep(1)
                url = f"svn://127.0.0.1:{port}/r"
                client = SvnClient("svn")
                client.store_auth = False
                self.assertGreaterEqual(client.version(), (1, 10))   # пароль идёт через stdin
                client.set_credentials("alice", "секрет a")
                self.assertEqual(client.list_dirs(url), [])
                client.set_credentials("alice", "wrong")
                with self.assertRaises(SvnError) as cm:
                    client.list_dirs(url)
                self.assertEqual(cm.exception.kind, ErrorKind.AUTH)
            finally:
                srv.kill()
                srv.wait()


if __name__ == "__main__":
    unittest.main()
