"""Task AG.0a: межпроцессный замок папки (folderlock) и его связь с publish.mark_busy / is_busy.

Второй процесс - `python -c` с тем же SVNLITE_DATA_DIR: держит замок, пока его не убьют, или пробует взять его.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svnhelper import folderlock, publish  # noqa: E402
from svnhelper.svn_core import ErrorKind, SvnError  # noqa: E402

WINDOWS = os.name == "nt"

CHILD = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from svnhelper import folderlock, publish
mode, target = sys.argv[2], sys.argv[3]
if mode == "hold":
    with folderlock.hold(target, owner="второй процесс"):
        print("ready", flush=True)
        time.sleep(120)
elif mode == "mark_busy":
    with publish.mark_busy(target):
        print("ready", flush=True)
        time.sleep(120)
elif mode == "named":
    with folderlock.named_lock(target):
        print("ready", flush=True)
        time.sleep(120)
elif mode == "try":
    lock = folderlock.try_lock(target)
    if lock is None:
        print("busy", flush=True)
        sys.exit(1)
    lock.release()
    print("taken", flush=True)
"""


def make_junction(link: str, target: str) -> bool:
    if not WINDOWS:
        return False
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
    except (ImportError, AttributeError, OSError):
        return False
    return os.path.isdir(link)


class LockTestCase(unittest.TestCase):
    """Своя папка данных (SVNLITE_DATA_DIR) и папка проекта с кириллицей и пробелами."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "данные")
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": self.data})
        env.start()
        self.addCleanup(env.stop)
        self.folder = os.path.join(self.tmp.name, "Проекты", "261028_отбраковщик @ 100%")
        os.makedirs(self.folder)
        self.addCleanup(folderlock.set_strict, False)

    # -- второй процесс ---------------------------------------------------------------------------

    def child_env(self) -> dict:
        return dict(os.environ, SVNLITE_DATA_DIR=self.data, PYTHONIOENCODING="utf-8")

    def start_holder(self, mode: str = "hold", target: str = None) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT), mode, target or self.folder],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.child_env())
        self.addCleanup(self.stop, proc)
        line = proc.stdout.readline().decode("utf-8", "replace").strip()
        if line != "ready":
            self.stop(proc)
            self.fail(f"второй процесс не занял папку: {proc.stderr.read().decode('utf-8', 'replace')}")
        return proc

    @staticmethod
    def stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()

    def other_process_try(self, target: str = None) -> str:
        proc = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), "try", target or self.folder],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.child_env(), timeout=60)
        out = proc.stdout.decode("utf-8", "replace").strip()
        self.assertIn(out, ("busy", "taken"), proc.stderr.decode("utf-8", "replace"))
        return out


class KeyTests(LockTestCase):
    """Кириллица, пробелы, регистр диска, черты и junction дают один ключ."""

    def test_spellings_of_one_folder_give_one_key(self):
        key = folderlock.lock_key(self.folder)
        drive, rest = os.path.splitdrive(self.folder)
        variants = [self.folder + os.sep, self.folder.upper(), self.folder.lower(),
                    self.folder.replace("\\", "/"), drive.lower() + rest, drive.upper() + rest,
                    os.path.join(self.folder, "docs", "..")]
        for variant in variants:
            self.assertEqual(folderlock.lock_key(variant), key, variant)
        self.assertEqual(len(key), folderlock.KEY_LENGTH)
        self.assertNotEqual(folderlock.lock_key(self.folder + " 2"), key)
        self.assertEqual(folderlock.lock_file(self.folder).parent, Path(self.data) / "locks")

    def test_missing_folder_has_a_key(self):
        missing = os.path.join(self.tmp.name, "Нет такой", "папки")
        self.assertEqual(folderlock.lock_key(missing), folderlock.lock_key(missing.upper() + "\\"))

    @unittest.skipUnless(WINDOWS, "junction есть только в Windows")
    def test_junction_gives_target_key(self):
        link = os.path.join(self.tmp.name, "ссылка на проект")
        if not make_junction(link, self.folder):
            self.skipTest("не удалось создать junction")
        self.addCleanup(os.rmdir, link)
        self.assertEqual(folderlock.lock_key(link), folderlock.lock_key(self.folder))
        with folderlock.hold(link):
            with self.assertRaises(folderlock.FolderBusy) as cm:
                with folderlock.hold(self.folder):
                    pass
            self.assertTrue(cm.exception.same_process)


class SameProcessTests(LockTestCase):
    def test_hold_writes_owner_after_byte_64_and_removes_file(self):
        with folderlock.hold(self.folder, owner="проверка") as lock:
            self.assertTrue(lock.locked)
            file = folderlock.lock_file(self.folder)
            self.assertTrue(file.is_file())
            with open(file, "rb") as fh:            # байт 0 занят, сведения читаются со смещения 64
                fh.seek(folderlock.INFO_OFFSET)
                info = json.loads(fh.read().decode("utf-8"))
            self.assertEqual((info["pid"], info["owner"], info["path"]), (os.getpid(), "проверка", self.folder))
            self.assertIn("started", info)
            self.assertEqual(folderlock.holder_info(self.folder)["pid"], os.getpid())
            self.assertFalse(folderlock.held_by_other(self.folder))        # свой замок не чужой
            self.assertTrue(folderlock.held_here(self.folder))
        self.assertFalse(file.exists())
        self.assertIsNone(folderlock.holder_info(self.folder))
        self.assertFalse(folderlock.held_here(self.folder))

    def test_second_hold_in_same_process_is_refused(self):
        with folderlock.hold(self.folder):
            self.assertIsNone(folderlock.try_lock(self.folder.upper()))
            with self.assertRaises(folderlock.FolderBusy) as cm:
                with folderlock.hold(self.folder):
                    pass
            self.assertTrue(cm.exception.same_process)
            self.assertNotIn("lock", str(cm.exception).lower())
        with folderlock.hold(self.folder):          # отпущен: берётся снова
            pass

    def test_release_is_idempotent(self):
        lock = folderlock.try_lock(self.folder)
        lock.release()
        lock.release()
        self.assertFalse(folderlock.held_here(self.folder))

    def test_threads_of_one_process_do_not_share_a_folder(self):
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            lock = folderlock.try_lock(self.folder)
            results.append(lock)
            time.sleep(0.2)
            if lock is not None:
                lock.release()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(lock is not None for lock in results), 1)

    def test_is_busy_does_not_count_own_hold(self):
        with folderlock.hold(self.folder):
            self.assertFalse(publish.is_busy(self.folder))
        with publish.mark_busy(self.folder):
            self.assertTrue(publish.is_busy(self.folder))
            self.assertFalse(folderlock.held_by_other(self.folder))
            self.assertTrue(folderlock.lock_file(self.folder).is_file())
        self.assertFalse(publish.is_busy(self.folder))
        self.assertFalse(folderlock.lock_file(self.folder).exists())

    def test_hold_and_mark_busy_on_one_path_are_exclusive(self):
        with folderlock.hold(self.folder):
            with self.assertRaises(publish.PublishError) as cm:
                with publish.mark_busy(self.folder):
                    pass
            self.assertEqual(cm.exception.code, publish.IN_PROGRESS)
            self.assertEqual(cm.exception.message, folderlock.SAME_PROCESS_TEXT)
            self.assertEqual(publish.busy_paths(), frozenset())         # отказ не оставил папку в наборе
        with publish.mark_busy(self.folder):
            with self.assertRaises(folderlock.FolderBusy):
                with folderlock.hold(self.folder):
                    pass

    def test_busy_paths_stays_own_snapshot(self):
        with publish.mark_busy(self.folder):
            self.assertEqual(publish.busy_paths(), frozenset({os.path.normcase(os.path.normpath(self.folder))}))
        self.assertEqual(publish.busy_paths(), frozenset())


class UnavailableDirTests(LockTestCase):
    """Каталог замков создать нельзя: окно работает с предупреждением, строгий режим отказывает."""

    def setUp(self):
        super().setUp()
        blocker = Path(self.tmp.name, "файл вместо папки")
        blocker.write_text("x")
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": str(blocker / "данные")})
        env.start()
        self.addCleanup(env.stop)

    def test_normal_mode_works_inside_process_with_warning(self):
        with self.assertLogs("svnhelper.folderlock", "WARNING"):
            with folderlock.hold(self.folder) as lock:
                self.assertFalse(lock.locked)
                self.assertTrue(folderlock.held_here(self.folder))
                with self.assertRaises(folderlock.FolderBusy):
                    with folderlock.hold(self.folder):
                        pass
        self.assertFalse(folderlock.held_by_other(self.folder))
        with self.assertLogs("svnhelper.folderlock", "WARNING"):
            with publish.mark_busy(self.folder):
                self.assertTrue(publish.is_busy(self.folder))
        self.assertFalse(publish.is_busy(self.folder))

    def test_strict_mode_refuses(self):
        folderlock.set_strict(True)
        with self.assertRaises(folderlock.LockUnavailable):
            with folderlock.hold(self.folder):
                pass
        self.assertFalse(folderlock.held_here(self.folder))
        with self.assertRaises(publish.PublishError) as cm:
            with publish.mark_busy(self.folder):
                pass
        self.assertEqual(cm.exception.code, publish.LOCK_UNAVAILABLE)
        self.assertEqual(publish.busy_paths(), frozenset())
        with self.assertRaises(folderlock.LockUnavailable):
            with folderlock.named_lock("settings", timeout=0.1):
                pass


@unittest.skipUnless(WINDOWS, "msvcrt.locking есть только в Windows")
class OtherProcessTests(LockTestCase):
    def test_second_process_cannot_take_lock_until_first_is_killed(self):
        holder = self.start_holder("hold")
        self.assertEqual(self.other_process_try(), "busy")
        self.assertTrue(folderlock.held_by_other(self.folder))
        self.assertTrue(folderlock.held_by_other(self.folder.upper() + "\\"))
        info = folderlock.holder_info(self.folder)
        self.assertEqual((info["pid"], info["owner"]), (holder.pid, "второй процесс"))
        self.assertIsNone(folderlock.try_lock(self.folder))
        with self.assertRaises(folderlock.FolderBusy) as cm:
            with folderlock.hold(self.folder):
                pass
        self.assertFalse(cm.exception.same_process)
        self.assertEqual(cm.exception.holder["pid"], holder.pid)

        holder.kill()                       # как «Снять задачу» в диспетчере задач
        holder.wait()
        deadline = time.monotonic() + 10
        while folderlock.held_by_other(self.folder) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(folderlock.held_by_other(self.folder))
        self.assertIsNone(folderlock.holder_info(self.folder))      # сведения убитого процесса не в счёт
        self.assertEqual(self.other_process_try(), "taken")
        with folderlock.hold(self.folder) as lock:
            self.assertEqual(lock.info["pid"], os.getpid())

    def test_publish_sees_other_process(self):
        self.start_holder("hold")
        self.assertTrue(publish.is_busy(self.folder))
        self.assertEqual(publish.busy_paths(), frozenset())       # снимок только своего процесса
        with self.assertRaises(publish.PublishError) as cm:
            with publish.mark_busy(self.folder):
                pass
        self.assertEqual((cm.exception.code, cm.exception.stage), (publish.IN_PROGRESS, "check"))
        self.assertEqual(cm.exception.message, folderlock.BUSY_TEXT)
        self.assertEqual(publish.busy_paths(), frozenset())       # отказ не оставил папку в наборе

    def test_mark_busy_of_other_process_blocks_hold(self):
        holder = self.start_holder("mark_busy")
        self.assertEqual(self.other_process_try(), "busy")
        self.assertTrue(publish.is_busy(self.folder))
        self.stop(holder)
        deadline = time.monotonic() + 10
        while publish.is_busy(self.folder) and time.monotonic() < deadline:
            time.sleep(0.05)
        with publish.mark_busy(self.folder):
            self.assertEqual(self.other_process_try(), "busy")

    def test_junction_path_in_other_process(self):
        link = os.path.join(self.tmp.name, "ссылка на проект")
        if not make_junction(link, self.folder):
            self.skipTest("не удалось создать junction")
        self.addCleanup(os.rmdir, link)
        self.start_holder("hold", link)
        self.assertTrue(folderlock.held_by_other(self.folder))

    def test_named_lock_waits_for_other_process(self):
        holder = self.start_holder("named", "settings")
        started = time.monotonic()
        with self.assertRaises(folderlock.LockTimeout):
            with folderlock.named_lock("settings", timeout=0.3):
                pass
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.stop(holder)
        with folderlock.named_lock("settings", timeout=10):
            pass


class NamedLockTests(LockTestCase):
    def test_threads_take_turns(self):
        order = []

        def worker(n):
            with folderlock.named_lock("settings", timeout=10):
                order.append(("in", n))
                time.sleep(0.05)
                order.append(("out", n))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(order), 8)
        for i in range(0, 8, 2):                    # вход и выход одного потока идут подряд
            self.assertEqual((order[i][0], order[i + 1][0], order[i][1]), ("in", "out", order[i + 1][1]))

    def test_reentry_times_out_and_bad_name_is_refused(self):
        with folderlock.named_lock("audit"):
            with self.assertRaises(folderlock.LockTimeout):
                with folderlock.named_lock("audit", timeout=0.1):
                    pass
            with folderlock.named_lock("settings", timeout=0.1):     # другое имя не мешает
                pass
        for bad in ("", "../x", "Settings", "a" * 41):
            with self.assertRaises(ValueError):
                with folderlock.named_lock(bad):
                    pass


class StaleFilesTests(LockTestCase):
    def test_old_files_removed_but_held_and_fresh_stay(self):
        folder = Path(self.data, "locks")
        folder.mkdir(parents=True)
        old, fresh = folder / "0123456789abcdef.lock", folder / "fedcba9876543210.lock"
        old.write_bytes(b"")
        fresh.write_bytes(b"")
        week_ago = time.time() - 8 * 86400
        os.utime(old, (week_ago, week_ago))
        lock = folderlock.try_lock(self.folder)         # первое обращение процесса к каталогу: уборка
        self.addCleanup(lock.release)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        os.utime(lock.file, (week_ago, week_ago))
        self.assertEqual(folderlock.cleanup_stale(), 0)
        self.assertTrue(lock.file.exists())             # занятый файл не удаляется
        lock.release()                                  # свой файл удаляется при отпускании (Windows)
        if not WINDOWS:
            os.remove(lock.file)
        self.assertEqual(folderlock.cleanup_stale(max_age_days=0), 1)
        self.assertFalse(fresh.exists())


class PublishErrorDictTests(unittest.TestCase):
    def test_to_dict_has_no_raw_svn_text(self):
        error = SvnError(ErrorKind.AUTHZ, "svn: E170001: password=SECRET")
        exc = publish.PublishError(publish.NO_RIGHTS, "upload", error=error)
        data = exc.to_dict()
        self.assertEqual(data, {"code": publish.NO_RIGHTS, "stage": "upload",
                                "message": publish.MESSAGES[publish.NO_RIGHTS], "job_id": None})
        self.assertNotIn("SECRET", json.dumps(data, ensure_ascii=False))
        job = publish.PublishJob(id="abc123", folder="C:\\П", project_url="file:///x")
        self.assertEqual(publish.PublishError(publish.RESUMABLE, "check", job=job).to_dict()["job_id"], "abc123")


class ImportTests(unittest.TestCase):
    def test_folderlock_does_not_load_tkinter(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import svnhelper.folderlock; "
                "print(sorted(m for m in sys.modules if m == 'tkinter' or m.startswith('svnhelper.gui')))")
        proc = subprocess.run([sys.executable, "-c", code, str(ROOT)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.stdout.decode().strip(), "[]", proc.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
