"""jobs.py: запись журнала задачи с повторами, безопасное чтение, владелец задачи (pid и время старта процесса),
реестр живых задач; разбор при запуске (publish, localrepo, «Новый проект») пропускает задачи живого процесса."""
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svnhelper import jobs, localrepo, publish  # noqa: E402

MISSING_PID = 0x7FFFFFF0          # такого процесса нет (pid в Windows кратны 4 и намного меньше)


@dataclasses.dataclass
class SampleJob:
    id: str
    folder: str
    stage: str = "started"
    updated: str = ""
    kind: str = "sample"
    path: str = ""


def sleeper() -> subprocess.Popen:
    """Другой живой процесс: от его имени пишется журнал «чужой» задачи."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def stop(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait()


def owned_by(proc: subprocess.Popen) -> dict:
    alive, started = jobs.process_info(proc.pid)
    assert alive
    return {"pid": proc.pid, "started": started}


def write_as(job, owner: dict) -> None:
    """Журнал так, как его записал бы процесс owner ({} - прежняя версия без pid)."""
    data = {k: v for k, v in dataclasses.asdict(job).items() if k != "path"}
    data.update(owner)
    jobs.write(job.path, data)


class TempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.jobs_dir = self.root / "jobs"


class WriteReadTests(TempDir):
    def test_write_retries_busy_file_then_succeeds(self):
        real_replace, calls = os.replace, []

        def busy_twice(src, dst):
            calls.append(src)
            if len(calls) == 1:
                raise PermissionError(13, "занято")                       # WinError 5
            if len(calls) == 2:
                raise OSError(0, "файл занят другой программой", None, jobs.WINERROR_SHARING_VIOLATION)
            real_replace(src, dst)

        path = self.jobs_dir / "a.json"
        with mock.patch.object(jobs.os, "replace", side_effect=busy_twice), mock.patch.object(jobs, "RETRY_DELAY", 0):
            jobs.write(path, {"id": "a", "stage": "x"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(jobs.read(path), {"id": "a", "stage": "x"})

    def test_write_gives_up_after_all_attempts_and_other_errors_are_not_retried(self):
        path = self.jobs_dir / "a.json"
        with mock.patch.object(jobs.os, "replace", side_effect=PermissionError(13, "занято")) as replace, \
                mock.patch.object(jobs, "RETRY_DELAY", 0), self.assertRaises(PermissionError):
            jobs.write(path, {"id": "a"})
        self.assertEqual(replace.call_count, jobs.ATTEMPTS)
        with mock.patch.object(jobs.os, "replace", side_effect=OSError(28, "нет места")) as replace, \
                self.assertRaises(OSError):
            jobs.write(path, {"id": "a"})
        self.assertEqual(replace.call_count, 1)                             # не «занято»: без повторов

    def test_remove_retries_and_reports_failure(self):
        path = self.jobs_dir / "a.json"
        jobs.write(path, {"id": "a"})
        real_remove, calls = os.remove, []

        def busy_once(target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError(13, "занято")
            real_remove(target)

        with mock.patch.object(jobs.os, "remove", side_effect=busy_once), mock.patch.object(jobs, "RETRY_DELAY", 0):
            self.assertTrue(jobs.remove(path))
        self.assertFalse(path.exists())
        self.assertTrue(jobs.remove(path))                                  # файла уже нет - это не сбой
        jobs.write(path, {"id": "a"})
        with mock.patch.object(jobs.os, "remove", side_effect=PermissionError(13, "занято")), \
                mock.patch.object(jobs, "RETRY_DELAY", 0), self.assertLogs("svnhelper.jobs", "ERROR"):
            self.assertFalse(jobs.remove(path))

    def test_read_is_safe_during_races(self):
        path = self.jobs_dir / "a.json"
        self.assertIsNone(jobs.read(path))                                  # задача уже закончилась
        self.jobs_dir.mkdir()
        path.write_text("{", encoding="utf-8")
        self.assertIsNone(jobs.read(path))                                  # битый файл
        path.write_text("[1]", encoding="utf-8")
        self.assertIsNone(jobs.read(path))
        jobs.write(path, {"stage": "uploading"})
        real_read, calls = Path.read_text, []

        def busy_once(self_path, *args, **kwargs):
            calls.append(self_path)
            if len(calls) == 1:
                raise PermissionError(13, "занято: идёт os.replace")
            return real_read(self_path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", busy_once), mock.patch.object(jobs, "RETRY_DELAY", 0):
            self.assertEqual(jobs.read(path), {"stage": "uploading"})
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "занято")), \
                mock.patch.object(jobs, "RETRY_DELAY", 0):
            self.assertIsNone(jobs.read(path))                              # занят дольше всех попыток

    def test_save_writes_owner_and_skips_path(self):
        job = SampleJob("s1", "C:\\п", path=str(self.jobs_dir / "s1.json"))
        jobs.save(job)
        data = json.loads(Path(job.path).read_text("utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        self.assertNotIn("path", data)
        self.assertTrue(job.updated)
        if os.name == "nt":
            self.assertIsInstance(data["started"], int)
        loaded = jobs.from_data(SampleJob, data, job.path)
        self.assertEqual(loaded, job)

    def test_from_data_rejects_broken_fields(self):
        with self.assertRaises(ValueError):
            jobs.from_data(SampleJob, {"folder": "x"}, "p")                  # нет id
        with self.assertRaises(ValueError):
            jobs.from_data(SampleJob, {"id": "a", "folder": 5}, "p")        # не строка

    def test_is_child(self):
        base = os.path.join(self.root, "Проекты")
        self.assertTrue(jobs.is_child(os.path.join(base, ".п.creating"), base, r"\..+\.creating"))
        self.assertTrue(jobs.is_child(os.path.join(base.upper(), ".П.CREATING"), base, r"\..+\.creating"))
        self.assertFalse(jobs.is_child(os.path.join(base, "п"), base, r"\..+\.creating"))
        self.assertFalse(jobs.is_child(os.path.join(base, "a", ".п.creating"), base, r"\..+\.creating"))
        self.assertFalse(jobs.is_child(".п.creating", base))                 # не абсолютный
        self.assertFalse(jobs.is_child(None, base))


@unittest.skipUnless(os.name == "nt", "время старта процесса берётся из WinAPI")
class OwnerTests(unittest.TestCase):
    def test_old_journal_without_pid_is_abandoned(self):
        self.assertFalse(jobs.is_active({"id": "old", "stage": "uploading"}))
        self.assertFalse(jobs.is_active({"id": "old", "pid": "123"}))

    def test_own_process_job_is_alive_only_while_registered(self):
        data = {"id": "mine", **jobs.owner()}
        self.assertFalse(jobs.is_active(data))          # журнал оставлен нарочно: разбирать можно
        with jobs.active("mine"):
            self.assertTrue(jobs.is_active(data))
            with jobs.active("mine"):
                pass
            self.assertTrue(jobs.is_active(data))       # вложенный вход не снимает отметку внешнего
        self.assertFalse(jobs.is_active(data))

    def test_other_live_process_with_same_start_time(self):
        proc = sleeper()
        try:
            owner = owned_by(proc)
            self.assertIsInstance(owner["started"], int)
            self.assertTrue(jobs.is_active({"id": "x", **owner}))
            self.assertTrue(jobs.is_active({"id": "x", "pid": proc.pid}))               # время старта неизвестно
            self.assertFalse(jobs.is_active({"id": "x", "pid": proc.pid, "started": owner["started"] + 1}))  # pid занят другим
        finally:
            stop(proc)
        self.assertFalse(jobs.is_active({"id": "x", **owner}))                        # процесс завершился
        self.assertEqual(jobs.process_info(MISSING_PID), (False, None))


class RecoverSkipsLiveJobsTests(TempDir):
    """Разбор при запуске не трогает задачу, которую ведёт другой живой процесс; журнал без pid разбирается как раньше."""

    def setUp(self):
        super().setUp()
        self.base = self.root / "Проекты"
        self.folder = self.base / "проект"
        self.folder.mkdir(parents=True)

    @unittest.skipUnless(os.name == "nt", "нужен WinAPI")
    def test_publish_job_of_live_process_is_not_recovered(self):
        job = publish.PublishJob("p1", str(self.folder), "svn://srv/x/проект", svn_old=publish.svn_old_path(str(self.folder)),
                                 stage=publish.STAGE_UPLOADING, path=str(self.jobs_dir / "p1.json"))
        proc = sleeper()
        try:
            write_as(job, owned_by(proc))
            self.assertEqual(publish.pending_jobs(self.jobs_dir), [])
            self.assertEqual([j.id for j in publish.pending_jobs(self.jobs_dir, include_active=True)], ["p1"])
            self.assertEqual(publish.find_job(str(self.folder), jobs_dir=self.jobs_dir).id, "p1")   # для проверок - виден
        finally:
            stop(proc)
        self.assertEqual([j.id for j in publish.pending_jobs(self.jobs_dir)], ["p1"])

    @unittest.skipUnless(os.name == "nt", "нужен WinAPI")
    def test_local_history_job_of_live_process_is_not_undone(self):
        repo = Path(localrepo.history_root(str(self.base))) / "ab12cd34"
        (repo / "db").mkdir(parents=True)
        job = localrepo.LocalRepoJob("h1", str(self.folder), str(repo), stage=localrepo.STAGE_IMPORTED,
                                     path=str(self.jobs_dir / "h1.json"))
        proc = sleeper()
        try:
            write_as(job, owned_by(proc))
            self.assertEqual(localrepo.recover_jobs(self.jobs_dir), 0)
            self.assertTrue(repo.is_dir())
        finally:
            stop(proc)
        self.assertEqual(localrepo.recover_jobs(self.jobs_dir), 1)
        self.assertFalse(repo.exists())
        self.assertEqual(list(self.jobs_dir.glob("*.json")), [])

    @unittest.skipUnless(os.name == "nt", "нужен WinAPI")
    def test_create_job_of_live_process_keeps_creating_folder(self):
        from svnhelper.gui import new_project_dialog as npd
        creating = npd.creating_path(str(self.base), "новый")
        os.makedirs(os.path.join(creating, "01"))
        job = npd.CreateJob("c1", str(self.base), creating, str(self.base / "новый"), path=str(self.jobs_dir / "c1.json"))
        proc = sleeper()
        try:
            write_as(job, owned_by(proc))
            self.assertEqual(npd.recover_create_jobs(self.jobs_dir), 0)
            self.assertTrue(os.path.isdir(creating))                         # сборка живого процесса цела
        finally:
            stop(proc)
        self.assertEqual(npd.recover_create_jobs(self.jobs_dir), 1)
        self.assertFalse(os.path.exists(creating))

    def test_old_journals_without_pid_are_recovered_as_before(self):
        from svnhelper.gui import new_project_dialog as npd
        creating = npd.creating_path(str(self.base), "старый")
        os.makedirs(creating)
        self.jobs_dir.mkdir()
        # так писала прежняя версия: ни pid, ни started, ни номера правки
        (self.jobs_dir / "c0.json").write_text(json.dumps({
            "id": "c0", "base": str(self.base), "creating": creating, "final": str(self.base / "старый"),
            "stage": "building", "updated": "", "kind": "create_local", "project_url": "", "author": ""}),
            encoding="utf-8")
        (self.jobs_dir / "p0.json").write_text(json.dumps({
            "id": "p0", "folder": str(self.folder), "project_url": "svn://srv/x/проект",
            "svn_old": publish.svn_old_path(str(self.folder)), "stage": "uploaded", "kind": "publish"}),
            encoding="utf-8")
        self.assertEqual(npd.recover_create_jobs(self.jobs_dir), 1)
        self.assertFalse(os.path.exists(creating))
        [old] = publish.pending_jobs(self.jobs_dir)
        self.assertEqual((old.id, old.stage, old.layout_revision), ("p0", "uploaded", None))


if __name__ == "__main__":
    unittest.main()
