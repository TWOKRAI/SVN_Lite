"""Task T.4: отправка проекта с компьютера на сервер и привязка папки на месте (настоящий svn, file:///).

Сервер: хранилище с папкой konstruktor/projects, на ней svn:auto-props «*.bin = svn:needs-lock=*»
(как настроит администратор). Падение программы имитируется исключением BaseException: оно проходит
мимо обработчиков `except Exception`, как если бы процесс убили посреди шага.
"""
import hashlib
import json
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
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svnhelper import checks, importer, projectmeta, publish, server  # noqa: E402
from svnhelper.svn_core import CancelToken, ErrorKind, SvnClient, SvnError, WcInfo  # noqa: E402

HAS_SVN = bool(shutil.which("svn") and shutil.which("svnadmin"))
FOLDER = "261028_отбраковщик @ 100%"


class Killed(BaseException):
    """Процесс «убит»: обработчики except Exception не срабатывают, уборки нет."""


def run(*args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(f"{args}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode("utf-8", errors="replace")


def tree_hash(root) -> dict:
    """Путь -> (хэш, время изменения, только чтение) для файлов, "dir" для папок; .svn не учитывается."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".svn"]
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if os.path.isfile(full):
                result[rel] = (hashlib.sha1(Path(full).read_bytes()).hexdigest(), os.stat(full).st_mtime_ns,
                               os.access(full, os.W_OK))
            else:
                result[rel] = "dir"
    return result


def install_reject_hook(repo: Path, marker: str) -> None:
    """pre-commit, отклоняющий правку с файлом marker (ломает загрузку после создания папок)."""
    svnlook = shutil.which("svnlook")
    findstr = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "findstr.exe")
    (repo / "hooks" / "pre-commit.bat").write_text(
        "@echo off\r\n"
        f'"{svnlook}" changed -t %2 %1 | "{findstr}" /C:"{marker}" >nul\r\n'
        "if %errorlevel%==0 (\r\n  echo rejected by test hook 1>&2\r\n  exit 1\r\n)\r\nexit 0\r\n",
        encoding="ascii")


def make_project(folder: Path) -> None:
    """Папка проекта как после создания по шаблону: project.json, документы, пустая папка, мусор, .git."""
    (folder / "docs" / "Спецификация").mkdir(parents=True)
    (folder / "пустая папка").mkdir()
    (folder / "docs" / "Спецификация" / "спецификация @1%.xlsx").write_bytes(b"PK\x03\x04" + bytes(range(256)))
    (folder / "Схема узла.bin").write_bytes(b"\x00\x01" * 1000)
    (folder / "README.txt").write_bytes("строка 1\r\nстрока 2\n".encode("utf-8"))
    (folder / "Thumbs.db").write_bytes(b"junk")
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("x")
    meta = projectmeta.new_meta("отбраковщик @ 100%", folder.name, login="ivanov",
                                template={"id": "electronics", "title": "Электроника", "revision": 7},
                                fields={"date": "261028", "name": "отбраковщик @ 100%"})
    projectmeta.write(folder, meta)


@unittest.skipUnless(HAS_SVN and os.name == "nt", "нужны Windows, svn и svnadmin")
class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "server repo"
        run("svnadmin", "create", str(self.repo))
        self.server = self.repo.as_uri()
        self.parent = server.build_url(self.server, ["konstruktor", "projects"])
        run("svn", "mkdir", "--parents", "-m", "место для проектов", self.parent)
        cfg = self.root / "cfg"
        run("svn", "checkout", "--depth", "empty", self.parent, str(cfg))
        run("svn", "propset", "svn:auto-props", "*.bin = svn:needs-lock=*", str(cfg))
        run("svn", "commit", "-m", "auto-props", str(cfg))
        self.base = self.root / "Проекты @ 1%"
        self.folder = self.base / FOLDER
        self.folder.mkdir(parents=True)
        make_project(self.folder)
        self.jobs = self.root / "jobs"
        self.client = SvnClient("svn")
        self.project_url = server.build_url(self.parent, [FOLDER])

    def tearDown(self):
        self.tmp.cleanup()

    # -- помощники -------------------------------------------------------------

    def publish(self, **kwargs):
        kwargs.setdefault("jobs_dir", self.jobs)
        kwargs.setdefault("retry_delay", 0)
        return publish.publish(self.client, str(self.folder), self.parent, "Отправка проекта", **kwargs)

    def head(self) -> int:
        return int(run("svnlook", "youngest", str(self.repo)))

    def make_local_history(self) -> Path:
        """Как T.6: локальное хранилище, файлы проекта сохранены в его trunk, папка привязана к нему."""
        hist = self.root / "history"
        run("svnadmin", "create", str(hist))
        run("svn", "mkdir", "-m", "init", hist.as_uri() + "/trunk")
        self.client.checkout(hist.as_uri() + "/trunk", str(self.folder), force=True)
        run("svn", "add", "--force", ".", cwd=self.folder)      # .git и Thumbs.db svn отсеет сам
        run("svn", "commit", "-m", "сохранено у себя", cwd=self.folder)
        return hist

    def assert_bound(self, result=None):
        info = self.client.info(str(self.folder))
        self.assertEqual(unquote(info.url), unquote(self.project_url + "/trunk"))   # svn печатает @ без %40
        st = self.client.status(str(self.folder))
        self.assertEqual([c.path for c in st.local_changes if c.kind != "unversioned"], [])
        self.assertFalse(os.access(self.folder / "Схема узла.bin", os.W_OK))    # svn:needs-lock
        self.assertTrue(os.access(self.folder / "README.txt", os.W_OK))
        server_meta = self.client.cat(self.project_url + "/trunk/project.json")
        self.assertEqual((self.folder / "project.json").read_bytes(), server_meta)
        self.assertEqual(json.loads(server_meta.decode("utf-8"))["svn"]["url"], self.project_url)
        self.assertFalse(os.path.exists(publish.svn_old_path(str(self.folder))))
        self.assertEqual(sorted(os.listdir(self.base)), [FOLDER])             # временных папок нет
        self.assertEqual(publish.pending_jobs(self.jobs), [])
        if result is not None:
            self.assertEqual(result.changed, [])
            self.assertEqual(result.project_url, self.project_url)

    def server_names(self):
        return sorted(e.name for e in self.client.list_entries(self.project_url + "/trunk@", recursive=True))

    # -- успешные сценарии -------------------------------------------------------

    def test_plain_folder_goes_to_server_and_is_bound_in_place(self):
        before = tree_hash(self.folder)
        seen = {}
        real_bind = publish._bind

        def bind_spy(client, job, *args):
            seen["hash"] = tree_hash(self.folder)          # загрузка прошла, привязка ещё не началась
            seen["busy"] = publish.is_busy(str(self.folder))
            return real_bind(client, job, *args)

        with mock.patch.object(publish, "_bind", bind_spy):
            result = self.publish()
        self.assertEqual(seen, {"hash": before, "busy": True})   # исходная папка не менялась до привязки
        self.assertFalse(publish.is_busy(str(self.folder)))
        self.assert_bound(result)
        self.assertEqual(self.server_names(), ["README.txt", "docs", "docs/Спецификация",
                                               "docs/Спецификация/спецификация @1%.xlsx", "project.json",
                                               "Схема узла.bin", "пустая папка"])
        self.assertEqual(sorted(self.client.list_dirs(self.project_url + "@")), ["branches", "tags", "trunk"])
        self.assertTrue((self.folder / "Thumbs.db").exists())        # мусор остался только на компьютере
        self.assertEqual([os.path.basename(p) for p in result.read_only], ["Схема узла.bin"])
        for rel, value in before.items():                            # содержимое файлов не тронуто
            if value != "dir" and rel != "project.json":
                self.assertEqual(tree_hash(self.folder)[rel][0], value[0], rel)

    def test_local_history_folder_is_exported_and_rebound(self):
        hist = self.make_local_history()
        (self.folder / "README.txt").write_text("правка без сохранения", encoding="utf-8")
        with self.assertRaises(publish.PublishError) as cm:
            self.publish()
        self.assertEqual((cm.exception.code, cm.exception.stage), (publish.LOCAL_CHANGES, "check"))
        run("svn", "commit", "-m", "сохранено", cwd=self.folder)
        (self.folder / "новый.txt").write_text("не сохранён")
        with self.assertRaises(publish.PublishError) as cm:
            self.publish()
        self.assertEqual(cm.exception.code, publish.LOCAL_CHANGES)
        os.remove(self.folder / "новый.txt")
        head = self.head()
        result = self.publish()
        self.assertEqual(self.head(), head + 2)
        self.assert_bound(result)
        self.assertEqual((self.folder / "README.txt").read_text(encoding="utf-8"), "правка без сохранения")
        self.assertTrue(hist.is_dir())                                 # локальное хранилище остаётся архивом
        with self.assertRaises(publish.PublishError) as cm:          # второй раз: уже на сервере
            self.publish()
        self.assertEqual(cm.exception.code, publish.ALREADY_ON_SERVER)

    # -- .svn держит другая программа -------------------------------------------

    def test_busy_svn_folder_three_attempts_then_clear_error(self):
        self.make_local_history()
        before = tree_hash(self.folder)
        renames = []
        real_rename = os.rename

        def counting_rename(src, dst):
            renames.append((os.path.basename(src), os.path.basename(dst)))
            return real_rename(src, dst)

        handle = open(self.folder / ".svn" / "wc.db", "rb")      # без разрешения удалять/переносить
        try:
            with mock.patch("svnhelper.publish.os.rename", counting_rename):
                with self.assertRaises(publish.PublishError) as cm:
                    self.publish()
        finally:
            handle.close()
        error = cm.exception
        self.assertEqual((error.code, error.stage), (publish.SVN_BUSY, "bind"))
        self.assertIn("Закройте окна Проводника и TortoiseSVN", error.message)
        self.assertEqual(renames, [(".svn", os.path.basename(publish.svn_old_path(str(self.folder))))] * 3)
        # папка не испорчена: прежняя привязка к локальной истории на месте, файлы те же
        self.assertTrue(self.client.info(str(self.folder)).url.startswith("file:///"))
        self.assertNotIn(self.server, self.client.info(str(self.folder)).url)
        self.assertEqual(tree_hash(self.folder), before)
        self.assertFalse(os.path.exists(publish.svn_old_path(str(self.folder))))
        # проект на сервере остался, журнал позволяет завершить отправку
        self.assertTrue(self.client.url_exists(self.project_url + "/trunk"))
        self.assertIsNotNone(error.job)
        with self.assertRaises(publish.PublishError) as cm:
            self.publish()
        self.assertEqual(cm.exception.code, publish.RESUMABLE)
        result = publish.resume_publish(self.client, cm.exception.job, retry_delay=0)
        self.assert_bound(result)

    # -- сбои и отказы ----------------------------------------------------------

    def test_failed_upload_rolls_back_server_and_keeps_folder(self):
        install_reject_hook(self.repo, "README")
        before, head = tree_hash(self.folder), self.head()
        with self.assertRaises(publish.PublishError) as cm:
            self.publish()
        self.assertEqual(cm.exception.stage, "upload")
        self.assertTrue(cm.exception.create_error.rolled_back)
        self.assertFalse(self.client.url_exists(self.project_url))
        self.assertEqual(tree_hash(self.folder), before)
        self.assertFalse((self.folder / ".svn").exists())
        self.assertEqual(sorted(os.listdir(self.base)), [FOLDER])
        self.assertEqual(publish.pending_jobs(self.jobs), [])
        self.assertEqual(self.head(), head + 2)                 # создание папок и их удаление при откате

    def test_cancel_before_upload_and_during_binding(self):
        before, head = tree_hash(self.folder), self.head()
        token = CancelToken()
        token.cancel()
        with self.assertRaises(publish.PublishError) as cm:
            self.publish(cancel=token)
        self.assertEqual((cm.exception.code, cm.exception.stage), (publish.CANCELLED, "source"))
        self.assertEqual((self.head(), tree_hash(self.folder)), (head, before))
        self.assertEqual(sorted(os.listdir(self.base)), [FOLDER])
        self.assertEqual(publish.pending_jobs(self.jobs), [])

        token = CancelToken()
        real_checkout = self.client.checkout

        def cancelled_checkout(*args, **kwargs):
            token.cancel()
            raise SvnError(ErrorKind.CANCELLED)

        with mock.patch.object(self.client, "checkout", cancelled_checkout):
            with self.assertRaises(publish.PublishError) as cm:
                self.publish(cancel=token)
        self.assertEqual((cm.exception.code, cm.exception.stage), (publish.CANCELLED, "bind"))
        self.assertIn("Завершить отправку", cm.exception.message)
        self.assertFalse((self.folder / ".svn").exists())
        self.assertEqual(tree_hash(self.folder), before)
        self.assertEqual([j.stage for j in publish.pending_jobs(self.jobs)], [publish.STAGE_UPLOADED])
        self.assertEqual(self.client.checkout, real_checkout)
        self.assert_bound(publish.resume_publish(self.client, cm.exception.job, retry_delay=0))

    def test_killed_during_upload_is_rolled_back_on_next_start(self):
        self.client.set_credentials("ivanov", "x")     # file:// пишет --username автором правки
        before, head = tree_hash(self.folder), self.head()

        def killed_import(*args, **kwargs):
            raise Killed()

        with mock.patch.object(self.client, "import_dir", killed_import):
            with self.assertRaises(Killed):
                self.publish()
        self.assertTrue(self.client.url_exists(self.project_url))       # папки проекта уже созданы
        jobs = publish.pending_jobs(self.jobs)
        self.assertEqual([j.stage for j in jobs], [publish.STAGE_UPLOADING])
        self.assertTrue(os.path.isdir(jobs[0].work_dir))
        with self.assertRaises(publish.PublishError) as cm:            # до разбора повторить нельзя
            self.publish()
        self.assertEqual(cm.exception.code, publish.UNFINISHED)
        self.assertIsNone(publish.recover_job(self.client, jobs[0], retry_delay=0))
        self.assertFalse(self.client.url_exists(self.project_url))
        self.assertEqual(self.head(), head + 2)
        self.assertEqual(sorted(os.listdir(self.base)), [FOLDER])
        self.assertEqual(publish.pending_jobs(self.jobs), [])
        self.assertEqual(tree_hash(self.folder), before)
        self.assert_bound(self.publish())                               # после разбора отправка проходит

    def test_killed_during_binding_restores_svn_and_can_finish(self):
        self.make_local_history()
        before = tree_hash(self.folder)

        def killed_checkout(url, dest, *args, **kwargs):
            os.makedirs(os.path.join(dest, ".svn"))                      # svn успел начать новую .svn
            Path(dest, ".svn", "wc.db").write_bytes(b"partial")
            raise Killed()

        with mock.patch.object(self.client, "checkout", killed_checkout):
            with self.assertRaises(Killed):
                self.publish()
        self.assertTrue(os.path.isdir(publish.svn_old_path(str(self.folder))))
        with self.assertRaises(publish.PublishError) as cm:
            self.publish()
        self.assertEqual(cm.exception.code, publish.UNFINISHED)
        [job] = publish.pending_jobs(self.jobs)
        self.assertEqual(job.stage, publish.STAGE_BINDING)
        attrs = os.stat(publish.svn_old_path(str(self.folder))).st_file_attributes
        self.assertTrue(attrs & 0x2)                                       # .svn-old скрыта (п.12)
        job = publish.recover_job(self.client, job, retry_delay=0)
        self.assertEqual(job.stage, publish.STAGE_UPLOADED)              # «Завершить отправку»
        self.assertTrue(self.client.info(str(self.folder)).url.startswith("file:///"))
        self.assertNotIn(self.server, self.client.info(str(self.folder)).url)
        self.assertEqual(tree_hash(self.folder), before)
        self.assertFalse(os.path.exists(publish.svn_old_path(str(self.folder))))
        self.assert_bound(publish.resume_publish(self.client, job, retry_delay=0))

    def test_killed_during_binding_plain_folder_discard(self):
        before = tree_hash(self.folder)

        def killed_checkout(url, dest, *args, **kwargs):
            os.makedirs(os.path.join(dest, ".svn"))
            raise Killed()

        with mock.patch.object(self.client, "checkout", killed_checkout):
            with self.assertRaises(Killed):
                self.publish()
        [job] = publish.pending_jobs(self.jobs)
        publish.discard_job(job, retry_delay=0)                          # «Убрать недоделанное»
        self.assertFalse((self.folder / ".svn").exists())
        self.assertEqual(tree_hash(self.folder), before)
        self.assertEqual(publish.pending_jobs(self.jobs), [])
        self.assertTrue(self.client.url_exists(self.project_url))       # загруженное на сервере остаётся

    def test_project_already_exists(self):
        run("svn", "mkdir", "--parents", "-m", "чужой проект", self.project_url + "/trunk")
        before, head = tree_hash(self.folder), self.head()
        with self.assertRaises(publish.PublishError) as cm:
            self.publish()
        self.assertEqual((cm.exception.code, cm.exception.stage), (publish.EXISTS, "check"))
        self.assertIn("уже есть проект", cm.exception.message)
        self.assertEqual((self.head(), tree_hash(self.folder)), (head, before))
        self.assertEqual(publish.pending_jobs(self.jobs), [])

    def test_missing_parent_is_refused_without_creating_folders(self):
        head = self.head()
        missing = server.build_url(self.server, ["konstruktor", "нет такой"])
        with self.assertRaises(publish.PublishError) as cm:
            publish.publish(self.client, str(self.folder), missing, "x", jobs_dir=self.jobs)
        self.assertEqual(cm.exception.code, publish.NO_PARENT)
        self.assertIn("Такого места на сервере нет", cm.exception.message)
        self.assertEqual(self.head(), head)

    def test_network_drive_is_refused(self):
        with mock.patch.object(checks, "drive_type", return_value=checks.DRIVE_REMOTE):
            with self.assertRaises(publish.PublishError) as cm:
                self.publish()
        self.assertEqual(cm.exception.code, publish.NETWORK_DRIVE)
        with self.assertRaises(publish.PublishError) as cm:
            publish.check_publish(self.client, "\\\\fs01\\share\\проект", self.parent)
        self.assertEqual(cm.exception.code, publish.NETWORK_DRIVE)

    def test_folder_bound_to_office_server_is_refused(self):
        (self.folder / ".svn").mkdir()
        office = WcInfo(url="svn://192.168.11.11/test/p/trunk", revision=5, root="svn://192.168.11.11/test")
        with mock.patch.object(self.client, "info", return_value=office):
            with self.assertRaises(publish.PublishError) as cm:
                self.publish()
        self.assertEqual(cm.exception.code, publish.ALREADY_ON_SERVER)

    def test_bad_name_is_refused(self):
        with self.assertRaises(publish.PublishError) as cm:
            self.publish(name="trunk")
        self.assertEqual(cm.exception.code, publish.BAD_NAME)

    def test_broken_journal_is_skipped(self):
        self.jobs.mkdir()
        (self.jobs / "bad.json").write_text("{", encoding="utf-8")
        (self.jobs / "other.json").write_text('{"kind": "create"}', encoding="utf-8")
        self.assertEqual(publish.pending_jobs(self.jobs), [])


    # -- правки по ревью ----------------------------------------------------------

    def test_killed_after_checkout_keeps_bound_svn_and_finishes(self):
        """п.1, п.2: .svn уже привязана к серверу -> разбор её не трогает, «Завершить» только доделывает."""
        self.make_local_history()
        real_checkout = self.client.checkout

        def checkout_then_killed(*args, **kwargs):
            real_checkout(*args, **kwargs)
            raise Killed()

        with mock.patch.object(self.client, "checkout", checkout_then_killed):
            with self.assertRaises(Killed):
                self.publish()
        [job] = publish.pending_jobs(self.jobs)
        self.assertEqual(job.stage, publish.STAGE_BINDING)
        job = publish.recover_job(self.client, job, retry_delay=0)
        self.assertEqual(job.stage, publish.STAGE_UPLOADED)
        self.assertIn("server%20repo", self.client.info(str(self.folder)).url)    # привязка не снесена
        self.assertTrue(os.path.isdir(publish.svn_old_path(str(self.folder))))   # прежняя .svn не наложена сверху
        self.assert_bound(publish.resume_publish(self.client, job, retry_delay=0))

    def killed_mid_download(self):
        """svn успел создать .svn проекта, но скачал не всё (--depth empty), затем процесс убит."""
        def partial_checkout(url, dest, *args, **kwargs):
            run("svn", "checkout", "--depth", "empty", url + "@", ".", cwd=dest)
            raise Killed()

        with mock.patch.object(self.client, "checkout", partial_checkout):
            with self.assertRaises(Killed):
                self.publish()
        [job] = publish.pending_jobs(self.jobs)
        return job

    def test_killed_mid_download_finish_completes_copy(self):
        self.make_local_history()
        job = self.killed_mid_download()
        (self.folder / "мой новый.txt").write_text("не трогать")     # файл пользователя после сбоя
        st = self.client.status(str(self.folder))
        self.assertTrue(any(c.kind == "unversioned" and c.path.endswith("README.txt") for c in st.local_changes))
        job = publish.recover_job(self.client, job, retry_delay=0)
        self.assertIn("server%20repo", self.client.info(str(self.folder)).url)     # привязку не сносим
        result = publish.resume_publish(self.client, job, retry_delay=0)
        self.assertEqual(run("svn", "info", "--show-item", "depth", str(self.folder) + "@").strip(), "infinity")
        self.assertEqual((self.folder / "мой новый.txt").read_text(), "не трогать")
        self.assertEqual(result.changed, [])
        self.assertEqual([os.path.basename(p) for p in result.not_uploaded if "мой" in p], ["мой новый.txt"])
        (self.folder / "мой новый.txt").unlink()
        self.assert_bound(result)

    def test_finish_failure_keeps_journal(self):
        job = self.killed_mid_download()
        job = publish.recover_job(self.client, job, retry_delay=0)
        with mock.patch.object(self.client, "checkout",
                               side_effect=SvnError(ErrorKind.OTHER, "svn: E155000: сбой")):
            with self.assertRaises(publish.PublishError) as cm:
                publish.resume_publish(self.client, job, retry_delay=0)
        self.assertEqual(cm.exception.stage, "recover")
        self.assertIn("Не удалось докачать файлы проекта", cm.exception.message)
        self.assertIs(cm.exception.job, job)
        self.assertEqual([j.id for j in publish.pending_jobs(self.jobs)], [job.id])
        self.assert_bound(publish.resume_publish(self.client, publish.pending_jobs(self.jobs)[0], retry_delay=0))

    def test_discard_keeps_bound_svn(self):
        real_checkout = self.client.checkout

        def checkout_then_killed(*args, **kwargs):
            real_checkout(*args, **kwargs)
            raise Killed()

        with mock.patch.object(self.client, "checkout", checkout_then_killed):
            with self.assertRaises(Killed):
                self.publish()
        [job] = publish.pending_jobs(self.jobs)
        publish.discard_job(job, retry_delay=0, client=self.client)
        self.assertIn("server%20repo", self.client.info(str(self.folder)).url)

    def test_restore_does_not_delete_svn_outside_binding_stage(self):
        """п.1: стадия не binding -> существующая .svn не наша, не удаляется."""
        (self.folder / ".svn").mkdir()
        (self.folder / ".svn" / "wc.db").write_bytes(b"x")
        job = publish.PublishJob(id="x", folder=str(self.folder), project_url=self.project_url,
                                 svn_old=publish.svn_old_path(str(self.folder)),
                                 stage=publish.STAGE_UPLOADED, had_svn=False)
        publish._restore_svn(job, 0)
        self.assertTrue((self.folder / ".svn" / "wc.db").exists())
        job.stage = publish.STAGE_BINDING
        publish._restore_svn(job, 0)
        self.assertFalse((self.folder / ".svn").exists())

    def write_job(self, **fields):
        job = publish.PublishJob(id="j1", folder=str(self.folder), project_url=self.project_url,
                                 work_dir=str(self.base / f".{FOLDER}.publish"),
                                 svn_old=publish.svn_old_path(str(self.folder)),
                                 path=str(self.jobs / "j1.json"), **fields)
        publish._save_job(job)
        return job

    def test_recover_before_upload_does_not_touch_server(self):
        """п.3: started/source_ready -> только местная уборка, на сервер не ходим."""
        for stage in (publish.STAGE_STARTED, publish.STAGE_SOURCE):
            job = self.write_job(stage=stage, author="ivanov")
            os.makedirs(job.work_dir)
            offline = mock.Mock(spec=SvnClient)
            offline.info.side_effect = AssertionError("сервер не нужен")
            self.assertIsNone(publish.recover_job(offline, job, retry_delay=0))
            self.assertEqual(offline.method_calls, [])
            self.assertFalse(os.path.exists(job.work_dir))
            self.assertEqual(publish.pending_jobs(self.jobs), [])

    def test_recover_uploading_requires_our_author(self):
        """п.3, п.11: журнал без номера правки (прежняя версия): чужой автор -> сервер не трогаем; неизвестный автор ->
        сервер не трогаем и журнал не удаляем молча (окно покажет отправку с кнопкой «Убрать недоделанное»)."""
        run("svn", "mkdir", "--parents", "-m", "чужое", "--username", "petrov",
            self.project_url + "/trunk", self.project_url + "/tags", self.project_url + "/branches")
        head = self.head()
        job = self.write_job(stage=publish.STAGE_UPLOADING, author="ivanov")
        self.assertIsNone(publish.recover_job(self.client, job, retry_delay=0))
        job = self.write_job(stage=publish.STAGE_UPLOADING, author="")
        with self.assertRaises(publish.PublishError) as cm:
            publish.recover_job(self.client, job, retry_delay=0)
        self.assertIn("Не удалось выяснить, чей недоделанный проект", cm.exception.message)
        self.assertIn("«Убрать недоделанное»", cm.exception.message)
        self.assertIsNone(cm.exception.job)              # «Завершить отправку» не должна привязать папку к каркасу
        self.assertEqual([j.id for j in publish.pending_jobs(self.jobs)], ["j1"])
        publish.discard_job(job, retry_delay=0)
        run("svn", "mkdir", "-m", "чужое 2", "--username", "petrov", self.project_url + "/trunk/x")
        job = self.write_job(stage=publish.STAGE_UPLOADING, author="ivanov")
        self.assertIsNone(publish.recover_job(self.client, job, retry_delay=0))   # 2 записи, но не наши
        self.assertTrue(self.client.url_exists(self.project_url))
        self.assertEqual(self.head(), head + 1)
        self.assertEqual(publish.pending_jobs(self.jobs), [])

    def test_killed_during_upload_without_login_is_rolled_back_by_revision(self):
        """Ревью п.4: вход через кэш svn (логин программе неизвестен) - разбор решает по номеру правки каркаса."""
        def killed_import(*args, **kwargs):
            raise Killed()

        with mock.patch.object(self.client, "import_dir", killed_import):
            with self.assertRaises(Killed):
                self.publish()
        [job] = publish.pending_jobs(self.jobs)
        self.assertEqual(job.layout_revision, self.head())          # номер правки трёх папок записан в журнал
        job.author = ""                                             # как в журнале, где логина нет
        publish._save_job(job)
        [job] = publish.pending_jobs(self.jobs)
        self.assertEqual(job.author, "")
        self.assertIsNone(publish.recover_job(self.client, job, retry_delay=0))
        self.assertFalse(self.client.url_exists(self.project_url))     # только каркас: откатан
        self.assertEqual(publish.pending_jobs(self.jobs), [])

    def test_killed_after_upload_without_login_is_resumable_by_revision(self):
        real_import = self.client.import_dir

        def import_then_killed(*args, **kwargs):
            real_import(*args, **kwargs)
            raise Killed()

        with mock.patch.object(self.client, "import_dir", import_then_killed):
            with self.assertRaises(Killed):
                self.publish()
        [job] = publish.pending_jobs(self.jobs)
        job.author = ""
        publish._save_job(job)
        job = publish.recover_job(self.client, job, retry_delay=0)
        self.assertEqual(job.stage, publish.STAGE_UPLOADED)         # после каркаса есть правка: загрузка дошла
        self.assert_bound(publish.resume_publish(self.client, job, retry_delay=0))

    def test_failed_rollback_keeps_journal_and_next_start_removes_scaffold(self):
        """Ревью п.3 и п.6: связь пропала посреди загрузки, откат не удался. Папка как была, журнал остаётся на стадии
        uploading; разбор при следующем запуске убирает каркас, после этого отправка проходит."""
        before = tree_hash(self.folder)
        lost = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013: Unable to connect")
        with mock.patch.object(self.client, "import_dir", side_effect=lost), \
                mock.patch.object(self.client, "remove_url", side_effect=lost):
            with self.assertRaises(publish.PublishError) as cm:
                self.publish()
        error = cm.exception
        self.assertEqual((error.code, error.stage), (publish.NO_CONNECTION, "upload"))
        self.assertIs(error.create_error.rolled_back, False)
        self.assertIn(publish.ROLLBACK_LATER_TEXT, error.message)
        self.assertEqual(tree_hash(self.folder), before)                   # проект остался на компьютере как был
        self.assertFalse((self.folder / ".svn").exists())
        self.assertEqual(sorted(os.listdir(self.base)), [FOLDER])          # временной копии нет
        self.assertTrue(self.client.url_exists(self.project_url))          # каркас на сервере
        [job] = publish.pending_jobs(self.jobs)
        self.assertEqual((job.stage, job.layout_revision), (publish.STAGE_UPLOADING, self.head()))
        with self.assertRaises(publish.PublishError) as cm:
            self.publish()
        self.assertEqual(cm.exception.code, publish.UNFINISHED)            # до разбора повтор не даст EXISTS
        # первый запуск: откат снова не вышел - журнал остаётся
        with mock.patch.object(self.client, "remove_url", side_effect=SvnError(ErrorKind.OTHER, "svn: E000000")):
            with self.assertRaises(publish.PublishError) as cm:
                publish.recover_job(self.client, job, retry_delay=0)
        self.assertEqual(cm.exception.message, publish.RECOVER_RETRY_TEXT)
        self.assertIsNone(cm.exception.job)
        [job] = publish.pending_jobs(self.jobs)
        # следующий запуск: каркас убран, журнал закрыт
        self.assertIsNone(publish.recover_job(self.client, job, retry_delay=0))
        self.assertFalse(self.client.url_exists(self.project_url))
        self.assertEqual(publish.pending_jobs(self.jobs), [])
        self.assert_bound(self.publish())

    def test_recover_during_publish_skips_live_job(self):
        """Ревью п.2: разбор журналов, запущенный посреди отправки (окно, вторая копия), её журнал не трогает."""
        seen = []
        real_prepare = publish._prepare_source

        def prepare_and_recover(*args, **kwargs):
            real_prepare(*args, **kwargs)
            seen.append(publish.pending_jobs(self.jobs))
            seen.append(publish.find_job(str(self.folder), jobs_dir=self.jobs) is not None)

        with mock.patch.object(publish, "_prepare_source", prepare_and_recover):
            self.assert_bound(self.publish())
        self.assertEqual(seen, [[], True])

    def test_folder_inside_bound_project_is_refused(self):
        """п.4: в самой папке .svn нет, но она внутри скачанного проекта."""
        other = server.build_url(self.server, ["другой"])
        run("svn", "mkdir", "-m", "x", other)
        wc = self.root / "скачанный"
        self.client.checkout(other, str(wc))
        inner = wc / "подпапка"
        inner.mkdir()
        with self.assertRaises(publish.PublishError) as cm:                # не сохранена в копии (W155010)
            publish.check_publish(self.client, str(inner), self.parent)
        self.assertEqual(cm.exception.code, publish.INSIDE_PROJECT)
        run("svn", "add", str(inner))
        run("svn", "commit", "-m", "папка", str(wc))
        with self.assertRaises(publish.PublishError) as cm:
            publish.check_publish(self.client, str(inner), self.parent)
        self.assertEqual(cm.exception.code, publish.INSIDE_PROJECT)
        plan = publish.check_publish(self.client, str(self.folder), self.parent)   # обычная папка: E155007
        self.assertIsNone(plan.local_url)

    def test_local_history_copy_at_old_revision_sends_what_is_in_folder(self):
        """п.5: выгрузка из самой копии; хранилище истории может даже пропасть."""
        hist = self.make_local_history()
        (self.folder / "README.txt").write_text("вторая правка", encoding="utf-8")
        run("svn", "commit", "-m", "2", cwd=self.folder)
        run("svn", "update", "-r", "2", cwd=self.folder)                   # копия на прежней правке
        old = (self.folder / "README.txt").read_bytes()
        self.assertNotEqual(old, "вторая правка".encode("utf-8"))
        hist.rename(self.root / "history_lost")                             # хранилище истории пропало
        result = self.publish()
        self.assertEqual(self.client.cat(self.project_url + "/trunk/README.txt"), old)
        self.assertEqual(result.changed, [])
        self.assert_bound(result)

    def test_lost_local_history_has_clear_text(self):
        code = publish._svn_code(SvnError(ErrorKind.LOCAL_HISTORY_MISSING))
        self.assertEqual(code, publish.LOCAL_HISTORY_MISSING)
        self.assertIn("История проекта на этом компьютере потеряна", publish.MESSAGES[code])

    def test_copy_errors_are_explained(self):
        """п.7: файл открыт другой программой; слишком длинный путь."""
        import errno as errno_mod
        for error, text in ((PermissionError(13, "denied"), "закройте программу, в которой он открыт"),
                            (OSError(errno_mod.ENAMETOOLONG, "too long"), "Слишком длинный путь")):
            with mock.patch.object(publish.shutil, "copy2", side_effect=error):
                with self.assertRaises(publish.PublishError) as cm:
                    self.publish()
            self.assertEqual(cm.exception.stage, "source")
            self.assertIn(text, cm.exception.message)
            self.assertRegex(cm.exception.message, "«.+»")
            self.assertEqual(sorted(os.listdir(self.base)), [FOLDER])
            self.assertEqual(publish.pending_jobs(self.jobs), [])

    def test_tampered_journal_is_skipped(self):
        """п.10: журнал с чужими путями не разбирается."""
        self.write_job(stage=publish.STAGE_BINDING)
        self.assertEqual(len(publish.pending_jobs(self.jobs)), 1)
        for key, value in (("svn_old", str(self.root / "чужая")), ("work_dir", str(self.root / "чужая")),
                           ("work_dir", str(self.base / "Проект"))):
            data = json.loads((self.jobs / "j1.json").read_text(encoding="utf-8"))
            good = data[key]
            data[key] = value
            (self.jobs / "j1.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(publish.pending_jobs(self.jobs), [], (key, value))
            data[key] = good
            (self.jobs / "j1.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(len(publish.pending_jobs(self.jobs)), 1)


class ReviewSmallTests(unittest.TestCase):
    def test_importer_public_names_and_aliases(self):
        """п.13."""
        self.assertIs(importer._is_junk, importer.is_junk)
        self.assertIs(importer._is_link, importer.is_link)
        self.assertTrue(importer.is_junk("Thumbs.db", importer.name_patterns(importer.FilterRules())))

    @unittest.skipUnless(os.name == "nt", "нужна Windows")
    def test_background_check_skips_folder_being_published(self):
        """п.6: снимок занятых папок делается до проверки, а отправка может начаться позже."""
        from types import SimpleNamespace
        from svnhelper.gui import notify_ui
        captured = {}
        fake = SimpleNamespace(_notice_force=False, _notice_checking=False, client=object(),
                               config=SimpleNamespace(filter=None), _notice_dirs=lambda: [],
                               _busy_paths=lambda: frozenset(),
                               run_bg=lambda work, done: captured.update(work=work))
        notify_ui.NotifyMixin._notice_start_check(fake)
        with mock.patch.object(notify_ui, "discover_projects", return_value=[]), \
                mock.patch.object(notify_ui, "check_projects",
                                  side_effect=lambda c, p, r, skip: captured.update(skip=skip)):
            captured["work"]()
        folder = "C:\\Проекты\\отправляется"
        self.assertFalse(captured["skip"](folder))
        with publish._mark_busy(folder):
            self.assertTrue(captured["skip"](folder))


@unittest.skipUnless(HAS_SVN and os.name == "nt" and shutil.which("svnserve"), "нет svn/svnadmin/svnserve")
class PublishRightsTests(unittest.TestCase):
    """Нет прав записи в выбранное место (authz на svnserve): отказ, проект остаётся на компьютере."""

    def test_no_write_rights(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "demo")
            run("svnadmin", "create", str(repo))
            run("svn", "mkdir", "--parents", "-m", "init", repo.as_uri() + "/konstruktor/projects")
            (repo / "conf" / "svnserve.conf").write_text(
                "[general]\nanon-access = none\nauth-access = write\npassword-db = passwd\nauthz-db = authz\n")
            (repo / "conf" / "passwd").write_text("[users]\nivanov = пароль 1\n", encoding="utf-8")
            (repo / "conf" / "authz").write_text("[/]\nivanov = r\n", encoding="utf-8")
            folder = Path(tmp, "Проекты", FOLDER)
            folder.mkdir(parents=True)
            make_project(folder)
            before = tree_hash(folder)
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            srv = subprocess.Popen(["svnserve", "-d", "--foreground", "--listen-port", str(port), "-r", tmp])
            try:
                time.sleep(1)
                client = SvnClient("svn")
                client.store_auth = False
                client.set_credentials("ivanov", "пароль 1")
                parent = server.build_url(f"svn://127.0.0.1:{port}", ["demo", "konstruktor", "projects"])
                with self.assertRaises(publish.PublishError) as cm:
                    publish.publish(client, str(folder), parent, "x", jobs_dir=Path(tmp, "jobs"), retry_delay=0)
                self.assertEqual((cm.exception.code, cm.exception.stage), (publish.NO_RIGHTS, "upload"))
                self.assertIn("Нет прав создавать проекты", cm.exception.message)
                self.assertFalse(client.url_exists(server.build_url(parent, [FOLDER])))
            finally:
                srv.kill()
                srv.wait()
            self.assertEqual(tree_hash(folder), before)
            self.assertFalse((folder / ".svn").exists())
            self.assertEqual(sorted(os.listdir(folder.parent)), [FOLDER])
            self.assertEqual(publish.pending_jobs(Path(tmp, "jobs")), [])


class BusyRegistryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "регистр путей не важен только в Windows")
    def test_busy_mark_is_released_and_normalized(self):
        with publish._mark_busy("C:\\Проекты\\P"):
            self.assertTrue(publish.is_busy("c:/проекты/p/"))
            self.assertIn(os.path.normcase(os.path.normpath("C:\\Проекты\\P")), publish.busy_paths())
            with self.assertRaises(publish.PublishError) as cm:
                with publish._mark_busy("C:\\Проекты\\P"):
                    pass
            self.assertEqual(cm.exception.code, publish.IN_PROGRESS)
        self.assertFalse(publish.is_busy("C:\\Проекты\\P"))


if __name__ == "__main__":
    unittest.main()
