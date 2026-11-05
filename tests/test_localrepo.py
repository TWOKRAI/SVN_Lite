"""Task T.6: локальная история проекта (настоящий svn и svnadmin, file:///).

Падение программы имитируется исключением BaseException: оно проходит мимо обработчиков `except Exception`, как
если бы процесс убили посреди шага; уборку тогда делает recover_jobs.
"""
import gc
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_publish as tp  # noqa: E402
from svnhelper import config, localrepo, publish, server  # noqa: E402
from svnhelper.config import FilterRules, SettingsStore  # noqa: E402
from svnhelper.svn_core import CancelToken, ErrorKind, LocalChange, SvnClient, SvnError, WcStatus  # noqa: E402

FOLDER = "261028_отбраковщик @ 100%"
SVNADMIN = shutil.which("svnadmin")
RULES = localrepo.local_rules(FilterRules())


def snapshot(root) -> dict:
    """Всё под root, кроме .svn: путь -> хэш содержимого ("dir" для папок). Время изменения не учитывается."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".svn"]
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            result[rel] = hashlib.sha1(Path(full).read_bytes()).hexdigest() if os.path.isfile(full) else "dir"
    return result


def remove_tree(path) -> None:
    """Удаляет папку хранилища: файлы svn помечены «только для чтения»."""
    shutil.rmtree(path, onerror=lambda func, target, _exc: (os.chmod(target, 0o700), func(target)))


def setUpModule():
    """Тесты ниже запускают много процессов svn (у каждого читающие потоки). Мусор окон от предыдущих модулей
    собирается здесь, в главном потоке: иначе сборщик мусора мог бы запуститься в одном из этих потоков и
    уничтожить Tk из чужого потока (Tcl_AsyncDelete: async handler deleted by the wrong thread, падение процесса)."""
    gc.collect()


class Killed(BaseException):
    """Процесс «убит»: обработчики except Exception не срабатывают, уборки нет."""


@unittest.skipUnless(tp.HAS_SVN, "нужны svn и svnadmin")
class LocalRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "Проекты @ 1%"
        self.folder = self.base / FOLDER
        self.folder.mkdir(parents=True)
        tp.make_project(self.folder)            # документы, пустая папка, мусор (Thumbs.db) и .git
        self.jobs = self.root / "jobs"
        self.store = SettingsStore(self.root / "settings.json")
        self.client = SvnClient("svn")

    def create(self, **kwargs):
        kwargs.setdefault("store", self.store)
        kwargs.setdefault("jobs_dir", self.jobs)
        return localrepo.create_local_repo(self.client, str(self.folder), kwargs.pop("svnadmin", SVNADMIN), **kwargs)

    def history_dir(self) -> Path:
        return self.base / ".svnlite" / "history"

    def assert_untouched(self, before):
        """Папка проекта и её соседи как были: ни .svn, ни .svnlite, ни временных папок, ни журнала."""
        self.assertFalse((self.folder / ".svn").exists())
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(sorted(os.listdir(self.base)), [FOLDER])
        self.assertEqual(list(self.jobs.glob("*.json")) if self.jobs.exists() else [], [])
        self.assertEqual(self.store.settings.local_histories, {})

    # -- успешный путь -------------------------------------------------------------------

    def test_create_edit_save_and_status_clean(self):
        before = snapshot(self.base)
        result = self.create()
        repo = Path(result.repo)
        # хранилище: рядом с проектами, скрытое, с пояснением, короткий id
        self.assertEqual(repo.parent, self.history_dir())
        self.assertRegex(repo.name, r"^[0-9a-f]{8}$")
        self.assertTrue((self.base / ".svnlite" / "README.txt").is_file())
        self.assertTrue(repo.joinpath("format").is_file())
        # папка проекта привязана к нему, файлы на месте и не изменились
        info = self.client.info(str(self.folder))
        self.assertTrue(info.url.startswith("file:///"))
        self.assertTrue(info.url.endswith("/trunk"))
        self.assertEqual((info.revision, result.revision), (2, 2))         # папки трёх линий и загрузка
        projects = {k: v for k, v in snapshot(self.base).items() if not k.startswith(".svnlite")}
        self.assertEqual(projects, before)                                  # файлы проекта не тронуты
        status = self.client.status(str(self.folder), rules=RULES)
        self.assertEqual(status.local_changes, [])
        # мусор и служебные папки в историю не попали, пустая папка попала
        names = {e.name for e in self.client.list_entries(result.repo_url + "/trunk@", recursive=True)}
        self.assertIn("docs/Спецификация/спецификация @1%.xlsx", names)
        self.assertIn("пустая папка", names)
        self.assertNotIn("Thumbs.db", names)
        self.assertFalse(any(n.startswith(".git") for n in names))
        # путь записан в settings.json, журнал закрыт
        self.assertEqual(SettingsStore(self.root / "settings.json").settings.local_histories,
                         {os.path.normcase(str(self.folder)): str(repo)})
        self.assertEqual(list(self.jobs.glob("*.json")), [])

        # правка, новый файл и сохранение у себя: номер вырос, status чистый
        (self.folder / "README.txt").write_text("правка", encoding="utf-8")
        (self.folder / "новый файл №1.txt").write_text("новый", encoding="utf-8")
        status = self.client.status(str(self.folder), rules=RULES)
        kinds = sorted((os.path.basename(c.path), c.kind) for c in status.local_changes)
        self.assertEqual(kinds, [("README.txt", "modified"), ("новый файл №1.txt", "unversioned")])
        done = localrepo.save(self.client, str(self.folder), FilterRules(), "Сохранено у себя")
        self.assertEqual(done.revision, 3)
        self.assertEqual(self.client.info(str(self.folder)).revision, 3)
        self.assertEqual(int(tp.run("svnlook", "youngest", str(repo)).strip()), 3)
        self.assertEqual(self.client.status(str(self.folder), rules=RULES).local_changes, [])
        log = self.client.recent_log(str(self.folder), 10)
        self.assertEqual([e.revision for e in log], [3, 2, 1])
        self.assertEqual(log[0].message, "Сохранено у себя")
        self.assertIsNone(localrepo.save(self.client, str(self.folder), FilterRules()))      # больше нечего

    def test_second_project_in_same_folder_gets_own_history(self):
        first = self.create()
        other = self.base / "второй проект"
        other.mkdir()
        (other / "a.txt").write_text("a", encoding="utf-8")
        second = localrepo.create_local_repo(self.client, str(other), SVNADMIN, store=self.store, jobs_dir=self.jobs)
        self.assertNotEqual(first.repo, second.repo)
        self.assertEqual(len(self.store.settings.local_histories), 2)

    # -- отказы -----------------------------------------------------------------------------

    def test_refusals_leave_folder_alone(self):
        before = snapshot(self.base)
        with mock.patch("svnhelper.checks.drive_type", return_value=4):          # сетевой диск
            with self.assertRaises(localrepo.LocalRepoError) as cm:
                self.create()
        self.assertEqual(cm.exception.code, localrepo.NETWORK_DRIVE)
        self.assert_untouched(before)
        with self.assertRaises(localrepo.LocalRepoError) as cm:
            localrepo.create_local_repo(self.client, str(self.base / "нет такой"), SVNADMIN, jobs_dir=self.jobs)
        self.assertEqual(cm.exception.code, localrepo.NO_FOLDER)
        with publish.mark_busy(str(self.folder)):
            with self.assertRaises(localrepo.LocalRepoError) as cm:
                self.create()
        self.assertEqual(cm.exception.code, localrepo.IN_PROGRESS)
        self.assert_untouched(before)
        self.create()
        with self.assertRaises(localrepo.LocalRepoError) as cm:                  # уже с историей
            self.create()
        self.assertEqual(cm.exception.code, localrepo.ALREADY_VERSIONED)

    # -- откат при сбое ---------------------------------------------------------------------------

    def fail_after(self, method: str, error=None):
        """Подмена метода клиента: реальный метод выполняется, затем «падает» (папка и история уже изменены)."""
        real = getattr(self.client, method)

        def wrapper(*args, **kwargs):
            real(*args, **kwargs)
            raise error or SvnError(ErrorKind.OTHER, "svn: E000000: тестовый сбой после " + method)
        return mock.patch.object(self.client, method, side_effect=wrapper)

    def test_failure_at_any_step_restores_original_folder(self):
        before = snapshot(self.base)
        cancel = CancelToken()

        def cancel_then_import(*args, **kwargs):
            cancel.cancel()
            return real_import(*args, **kwargs)
        real_import = self.client.import_dir
        cases = [
            ("нет svnadmin", {"svnadmin": str(self.root / "нет.exe")}, None, localrepo.NO_SVNADMIN),
            ("сбой при создании папок", {}, mock.patch.object(
                self.client, "mkdir_urls", side_effect=SvnError(ErrorKind.OTHER, "svn: E000000: x")),
             localrepo.FAILED),
            ("сбой при загрузке", {}, self.fail_after("import_dir"), localrepo.FAILED),
            ("сбой при привязке", {}, self.fail_after("checkout"), localrepo.FAILED),
            ("отмена посреди загрузки", {"cancel": cancel}, mock.patch.object(
                self.client, "import_dir", side_effect=cancel_then_import), localrepo.CANCELLED),
        ]
        for title, kwargs, patch, code in cases:
            with self.subTest(title):
                cancel = kwargs.get("cancel", cancel)
                if patch is None:
                    with self.assertRaises(localrepo.LocalRepoError) as cm:
                        self.create(**kwargs)
                else:
                    with patch, self.assertRaises(localrepo.LocalRepoError) as cm:
                        self.create(**kwargs)
                self.assertEqual(cm.exception.code, code)
                self.assertTrue(cm.exception.rolled_back)
                self.assert_untouched(before)
        # после всех сбоев история создаётся нормально
        self.create()
        self.assertTrue((self.folder / ".svn").is_dir())

    def test_cancelled_before_start_changes_nothing(self):
        before = snapshot(self.base)
        cancel = CancelToken()
        cancel.cancel()
        with self.assertRaises(localrepo.LocalRepoError) as cm:
            self.create(cancel=cancel)
        self.assertEqual(cm.exception.code, localrepo.CANCELLED)
        self.assert_untouched(before)

    def test_rollback_keeps_other_projects_history(self):
        other = self.base / "второй"
        other.mkdir()
        (other / "a.txt").write_text("a", encoding="utf-8")
        kept = localrepo.create_local_repo(self.client, str(other), SVNADMIN, store=self.store, jobs_dir=self.jobs)
        before = snapshot(self.folder)
        with self.fail_after("import_dir"), self.assertRaises(localrepo.LocalRepoError):
            self.create()
        self.assertEqual(snapshot(self.folder), before)
        self.assertTrue(Path(kept.repo).is_dir())
        self.assertEqual(sorted(os.listdir(self.history_dir())), [Path(kept.repo).name])
        self.assertEqual(self.client.info(str(other)).revision, 2)

    def test_killed_process_is_cleaned_on_next_start(self):
        before = snapshot(self.base)
        for method in ("mkdir_urls", "import_dir", "checkout"):
            with self.subTest(method):
                with self.fail_after(method, Killed()), self.assertRaises(Killed):
                    self.create()
                self.assertEqual(len(list(self.jobs.glob("*.json"))), 1)            # журнал остался
                removed = localrepo.recover_jobs(self.jobs, self.store)
                self.assertEqual(removed, 1)
                self.assert_untouched(before)

    def test_startup_recovery_of_new_projects_also_cleans_local_history(self):
        from svnhelper.gui.new_project_dialog import recover_create_jobs      # его и зовёт программа при запуске
        before = snapshot(self.base)
        with self.fail_after("checkout", Killed()), self.assertRaises(Killed):
            self.create()
        self.assertTrue((self.folder / ".svn").exists())
        self.assertEqual(recover_create_jobs(self.jobs), 1)
        self.assert_untouched(before)

    def test_killed_after_checkout_is_finished_not_undone(self):
        with mock.patch.object(localrepo, "register", side_effect=Killed()), self.assertRaises(Killed):
            self.create()
        self.assertEqual(localrepo.recover_jobs(self.jobs, self.store), 0)
        self.assertTrue((self.folder / ".svn").is_dir())
        self.assertEqual(self.client.status(str(self.folder), rules=RULES).local_changes, [])
        self.assertEqual(len(self.store.settings.local_histories), 1)
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_recover_during_creation_skips_live_job(self):
        """Ревью п.2: разбор журналов посреди включения истории (окно запустило его во время создания) не удаляет
        ни историю, ни журнал живой задачи; история включается до конца."""
        real_import, seen = self.client.import_dir, []

        def import_and_recover(*args, **kwargs):
            count = real_import(*args, **kwargs)
            seen.append(localrepo.recover_jobs(self.jobs, self.store))
            return count

        with mock.patch.object(self.client, "import_dir", side_effect=import_and_recover):
            result = self.create()
        self.assertEqual(seen, [0])
        self.assertTrue(Path(result.repo).is_dir())
        self.assertTrue((self.folder / ".svn").is_dir())
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_recovery_ignores_foreign_journals(self):
        self.jobs.mkdir()
        victim = self.root / "чужая папка"
        victim.mkdir()
        (victim / "f.txt").write_text("1", encoding="utf-8")
        (self.jobs / "x.json").write_text(
            json.dumps({"kind": "local_history", "id": "x", "folder": str(self.folder), "repo": str(victim),
                        "stage": "imported"}), encoding="utf-8")
        self.assertEqual(localrepo.recover_jobs(self.jobs, self.store), 0)
        self.assertTrue((victim / "f.txt").is_file())

    # -- пропавшая история ----------------------------------------------------------------------------

    def test_detach_after_history_lost_keeps_files(self):
        result = self.create()
        before = snapshot(self.base)
        with self.assertRaises(localrepo.LocalRepoError) as cm:                  # история на месте - нельзя
            localrepo.detach_history(self.client, str(self.folder), self.store)
        self.assertEqual(cm.exception.code, localrepo.HISTORY_EXISTS)
        self.assertTrue((self.folder / ".svn").is_dir())
        os.rename(result.repo, result.repo + "_moved")                           # хранилище пропало
        with self.assertRaises(SvnError) as cm:
            self.client.status(str(self.folder), check_server=True)
        self.assertEqual(cm.exception.kind, ErrorKind.LOCAL_HISTORY_MISSING)
        info = self.client.info(str(self.folder))
        self.assertFalse(localrepo.repo_exists(info.root))
        localrepo.detach_history(self.client, str(self.folder), self.store)
        self.assertFalse((self.folder / ".svn").exists())
        self.assertEqual({k: v for k, v in snapshot(self.base).items() if not k.startswith(".svnlite")},
                         {k: v for k, v in before.items() if not k.startswith(".svnlite")})
        self.assertEqual(self.store.settings.local_histories, {})
        with self.assertRaises(SvnError):                                        # теперь это обычная папка
            self.client.info(str(self.folder))

    # -- «Восстановить историю» (ревью 2026-11-02, п.5) --------------------------------------------------------

    def rename_base(self, new_name="Переименованные @ 2%") -> Path:
        """Папку проектов переименовали: проект и его история переехали вместе. Возвращает новую папку проекта."""
        new_base = self.root / new_name
        os.rename(self.base, new_base)
        self.base, self.folder = new_base, new_base / FOLDER
        return self.folder

    def test_restore_after_projects_folder_renamed_keeps_history_and_saving_works(self):
        result = self.create()
        (self.folder / "README.txt").write_text("до переименования", encoding="utf-8")
        localrepo.save(self.client, str(self.folder), FilterRules(), "до")           # сохранение № 3
        old_repo = result.repo
        self.rename_base()
        info = self.client.info(str(self.folder))
        self.assertFalse(localrepo.repo_exists(info.root))                         # svn смотрит на прежний адрес
        with self.assertRaises(SvnError) as cm:
            self.client.status(str(self.folder), check_server=True)
        self.assertEqual(cm.exception.kind, ErrorKind.LOCAL_HISTORY_MISSING)
        new_repo = localrepo.restore_history(self.client, str(self.folder), self.store)
        self.assertEqual(new_repo, str(self.base / ".svnlite" / "history" / Path(old_repo).name))
        # папка снова на своей истории (путь с «@» и «%»), прежние сохранения на месте, файлы не тронуты
        info = self.client.info(str(self.folder))
        self.assertEqual(localrepo.path_from_url(info.root), new_repo)
        self.assertEqual((info.revision, info.uuid), (3, localrepo.repo_uuid(new_repo)))
        self.assertEqual([e.revision for e in self.client.recent_log(str(self.folder), 10)], [3, 2, 1])
        self.assertEqual((self.folder / "README.txt").read_text("utf-8"), "до переименования")
        self.assertEqual(self.client.status(str(self.folder), check_server=True, rules=RULES).local_changes, [])
        # запись в settings.json переписана: ключ - новая папка проекта, значение - новая история, старых записей нет
        self.assertEqual(self.store.settings.local_histories, {os.path.normcase(str(self.folder)): new_repo})
        self.assertEqual(SettingsStore(self.root / "settings.json").settings.local_histories,
                         {os.path.normcase(str(self.folder)): new_repo})
        self.assertEqual(localrepo.archive_path(self.store, str(self.folder)), new_repo)
        # сохранение работает
        (self.folder / "README.txt").write_text("после", encoding="utf-8")
        done = localrepo.save(self.client, str(self.folder), FilterRules(), "после")
        self.assertEqual(done.revision, 4)
        self.assertEqual(int(tp.run("svnlook", "youngest", new_repo).strip()), 4)
        with self.assertRaises(localrepo.LocalRepoError) as cm:                    # история на месте - восстанавливать нечего
            localrepo.restore_history(self.client, str(self.folder), self.store)
        self.assertEqual(cm.exception.code, localrepo.HISTORY_EXISTS)

    def test_restore_when_history_folder_was_renamed_in_place(self):
        result = self.create()
        moved = result.repo + "_moved"
        os.rename(result.repo, moved)
        self.assertEqual(localrepo.find_history(self.store, str(self.folder), self.client.info(str(self.folder)).uuid),
                         moved)
        self.assertEqual(localrepo.restore_history(self.client, str(self.folder), self.store), moved)
        self.assertEqual(self.client.status(str(self.folder), check_server=True, rules=RULES).local_changes, [])
        self.assertEqual(self.store.settings.local_histories, {os.path.normcase(str(self.folder)): moved})

    def test_restore_finds_history_by_settings_and_other_project_folders(self):
        result = self.create()
        uuid = self.client.info(str(self.folder)).uuid
        elsewhere = self.root / "где-то ещё" / "x"
        elsewhere.parent.mkdir()
        shutil.move(result.repo, str(elsewhere))
        self.assertEqual(localrepo.find_history(self.store, str(self.folder), uuid), "")      # нигде не записано
        self.store.settings.local_histories = {os.path.normcase(str(self.folder)): str(elsewhere)}
        self.assertEqual(localrepo.find_history(self.store, str(self.folder), uuid), str(elsewhere))   # по settings.json
        self.store.settings.local_histories = {}
        other_base = self.root / "другая папка проектов"
        (other_base / ".svnlite" / "history").mkdir(parents=True)
        shutil.move(str(elsewhere), str(other_base / ".svnlite" / "history" / "abc12345"))
        self.store.settings.project_dirs = [str(other_base)]
        found = localrepo.find_history(self.store, str(self.folder), uuid)                    # по списку папок проектов
        self.assertEqual(found, str(other_base / ".svnlite" / "history" / "abc12345"))
        self.assertEqual(localrepo.find_history(self.store, str(self.folder), "чужой-uuid"), "")
        self.assertEqual(localrepo.find_history(self.store, str(self.folder), ""), "")
        self.assertEqual(localrepo.restore_history(self.client, str(self.folder), self.store), found)
        self.assertEqual(self.client.status(str(self.folder), check_server=True, rules=RULES).local_changes, [])

    def test_restore_without_history_on_disk_changes_nothing(self):
        result = self.create()
        remove_tree(result.repo)
        before = snapshot(self.base)
        with self.assertRaises(localrepo.LocalRepoError) as cm:
            localrepo.restore_history(self.client, str(self.folder), self.store)
        self.assertEqual(cm.exception.code, localrepo.HISTORY_NOT_FOUND)
        self.assertIn("не найдена", cm.exception.message)
        self.assertEqual(snapshot(self.base), before)
        self.assertTrue((self.folder / ".svn").is_dir())
        self.assertEqual(self.store.settings.local_histories, {os.path.normcase(str(self.folder)): result.repo})
        plain = self.base / "обычная"
        plain.mkdir()
        with self.assertRaises(localrepo.LocalRepoError) as cm:
            localrepo.restore_history(self.client, str(plain), self.store)
        self.assertEqual(cm.exception.code, localrepo.BROKEN_COPY)

    def test_lost_history_can_still_be_published_even_with_unsaved_files(self):
        """Хранилища нет, в папке есть и правки, и новый файл: отправка проходит, на сервер уходит то, что лежит в папке."""
        result = self.create()
        (self.folder / "README.txt").write_text("правка без истории", encoding="utf-8")
        (self.folder / "новый файл.txt").write_text("новый", encoding="utf-8")
        remove_tree(result.repo)
        srv = self.root / "server repo"
        tp.run("svnadmin", "create", str(srv))
        parent = server.build_url(srv.as_uri(), ["konstruktor", "projects"])
        tp.run("svn", "mkdir", "--parents", "-m", "место", parent)
        plan = publish.check_publish(self.client, str(self.folder), parent, rules=RULES, jobs_dir=self.jobs)
        self.assertIsNone(plan.local_url)                                           # как у обычной папки
        published = publish.publish(self.client, str(self.folder), parent, "Отправка", rules=RULES,
                                    jobs_dir=self.jobs, retry_delay=0)
        trunk = published.project_url + "/trunk"
        self.assertEqual(self.client.cat(trunk + "/README.txt"), "правка без истории".encode("utf-8"))
        self.assertEqual(self.client.cat(trunk + "/новый файл.txt"), "новый".encode("utf-8"))
        info = self.client.info(str(self.folder))
        self.assertEqual(info.root, srv.as_uri())                                   # папка привязана к серверу
        self.assertFalse(os.path.lexists(publish.svn_old_path(str(self.folder))))
        self.assertEqual(published.changed, [])
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    # -- после отправки на сервер: архив ---------------------------------------------------------------------

    def test_history_stays_as_archive_after_publish_and_is_deleted_on_request(self):
        result = self.create()
        (self.folder / "README.txt").write_text("правка до отправки", encoding="utf-8")
        localrepo.save(self.client, str(self.folder), FilterRules(), "перед отправкой")
        srv = self.root / "server repo"
        tp.run("svnadmin", "create", str(srv))
        parent = server.build_url(srv.as_uri(), ["konstruktor", "projects"])
        tp.run("svn", "mkdir", "--parents", "-m", "место", parent)
        published = publish.publish(self.client, str(self.folder), parent, "Отправка", rules=RULES,
                                    jobs_dir=self.jobs, retry_delay=0)
        self.assertEqual(self.client.info(str(self.folder)).root, srv.as_uri())
        # архив на месте и записан; пока не попросили - не удаляется
        self.assertTrue(Path(result.repo).is_dir())
        self.assertEqual(localrepo.archive_path(self.store, str(self.folder)), result.repo)
        self.assertEqual(published.changed, [])
        # удаление - только явное; проект на сервере и файлы остаются
        localrepo.delete_history(self.store, str(self.folder), self.client)
        self.assertFalse(Path(result.repo).exists())
        self.assertFalse((self.base / ".svnlite").exists())
        self.assertEqual(self.store.settings.local_histories, {})
        self.assertEqual((self.folder / "README.txt").read_text("utf-8"), "правка до отправки")
        self.assertTrue(self.client.url_exists(published.project_url))

    def test_delete_refused_while_folder_uses_the_history(self):
        result = self.create()
        with self.assertRaises(localrepo.LocalRepoError) as cm:
            localrepo.delete_history(self.store, str(self.folder), self.client)
        self.assertEqual(cm.exception.code, localrepo.IN_USE)
        self.assertTrue(Path(result.repo).is_dir())
        other = self.base / "без истории"
        other.mkdir()
        with self.assertRaises(localrepo.LocalRepoError) as cm:
            localrepo.delete_history(self.store, str(other), self.client)
        self.assertEqual(cm.exception.code, localrepo.NOT_LOCAL)


class PureHelpersTests(unittest.TestCase):
    def test_find_svnadmin_next_to_svn(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp, "TortoiseSVN", "bin")
            bin_dir.mkdir(parents=True)
            (bin_dir / "svn.exe").write_bytes(b"")
            self.assertIsNone(config.find_svnadmin(str(bin_dir / "svn.exe")))       # svnadmin.exe рядом нет
            (bin_dir / "svnadmin.exe").write_bytes(b"")
            self.assertEqual(config.find_svnadmin(str(bin_dir / "svn.exe")), str(bin_dir / "svnadmin.exe"))
            with mock.patch.object(config, "find_svn", return_value=str(bin_dir / "svn.exe")):
                self.assertEqual(config.find_svnadmin(), str(bin_dir / "svnadmin.exe"))
            with mock.patch.object(config, "find_svn", return_value=None):
                self.assertIsNone(config.find_svnadmin())

    def test_local_url_and_paths(self):
        url = Path("C:/Проекты @ 1%/.svnlite/history/ab12cd34").as_uri()
        self.assertTrue(localrepo.is_local_url(url + "/trunk"))
        self.assertFalse(localrepo.is_local_url("svn://srv/konstruktor/p/trunk"))
        # сервер на file:// (тесты, демо): проекты внутри него - не локальная история
        server_url = Path("C:/server repo").as_uri()
        self.assertFalse(localrepo.is_local_url(server_url + "/test/p/trunk", server_url))
        self.assertTrue(localrepo.is_local_url(url + "/trunk", server_url))
        self.assertEqual(localrepo.path_from_url(url), os.path.normpath("C:/Проекты @ 1%/.svnlite/history/ab12cd34"))
        self.assertEqual(localrepo.path_from_url("svn://srv/x"), "")
        self.assertTrue(localrepo.repo_exists("svn://srv/x"))
        self.assertFalse(localrepo.repo_exists(Path(tempfile.gettempdir(), "нет такого хранилища").as_uri()))

    def test_status_texts_and_auto_comment(self):
        from datetime import datetime
        self.assertEqual(localrepo.status_line(WcStatus(), False, False)[::2], ("Всё сохранено у вас", "Можно отправить проект на сервер"))
        self.assertEqual(localrepo.status_line(WcStatus(modified=1, local_changes=[LocalChange("a.txt", "modified")]), False,
                                               False)[1], "new")
        self.assertEqual(localrepo.status_line(None, True, False)[2], "Идёт проверка…")
        self.assertEqual(localrepo.status_line(None, False, True)[0], "История на этом компьютере потеряна")
        self.assertRegex(localrepo.auto_comment(datetime(2026, 10, 28, 9, 5)), r"^Сохранено у себя 28\.10\.2026 09:05$")

    def test_journal_write_survives_briefly_busy_file(self):
        """Журнал на миг занят (разбор журналов при запуске, антивирус): запись повторяется, а не обрывает создание."""
        real_replace = os.replace
        calls = []

        def busy_twice(src, dst):
            calls.append(src)
            if len(calls) <= 2:
                raise PermissionError(13, "занято")
            real_replace(src, dst)

        from svnhelper import jobs                       # запись журнала общая для всех задач (jobs.py)
        with tempfile.TemporaryDirectory() as tmp:
            job = localrepo.LocalRepoJob("j1", str(Path(tmp, "p")), str(Path(tmp, "r")), path=str(Path(tmp, "j1.json")))
            with mock.patch.object(jobs.os, "replace", side_effect=busy_twice), \
                    mock.patch.object(jobs, "RETRY_DELAY", 0):
                localrepo._save_job(job)
            self.assertEqual(len(calls), 3)
            self.assertEqual(json.loads(Path(tmp, "j1.json").read_text("utf-8"))["stage"], "started")
            with mock.patch.object(jobs.os, "replace", side_effect=PermissionError(13, "занято")), \
                    mock.patch.object(jobs, "RETRY_DELAY", 0), self.assertRaises(PermissionError):
                localrepo._save_job(job)                  # занято совсем - сбой не скрывается

    def test_settings_roundtrip_ignores_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "settings.json")
            path.write_text('{"local_histories": {"c:\\\\p": "C:\\\\h", "": "x", "k": 5}}', encoding="utf-8")
            store = SettingsStore(path)
            self.assertEqual(store.settings.local_histories, {"c:\\p": "C:\\h"})
            path.write_text('{"local_histories": []}', encoding="utf-8")
            self.assertEqual(SettingsStore(path).settings.local_histories, {})


if __name__ == "__main__":
    unittest.main()
